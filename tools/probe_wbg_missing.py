"""Probe the EXACT wbg import that fails when window.hsw(jwt) runs.

Strategy:
- Hook WebAssembly.instantiate to wrap each import function with a logging
  proxy that records (name, args, result/error) — including the EXACT
  "is not a function" / "is not a constructor" failures.
- Drive window.hsw(jwt) and report what blew up.
"""
from __future__ import annotations

import base64
import json
import time

import requests

from hcaptcha import version as _v
from hcaptcha.tools.js_runtime import JsRuntime


_HOOK_JS = r"""
(function () {
  globalThis.__wbg_log = [];
  globalThis.__wbg_err = [];
  globalThis.__qB_ref = null;
  function shortRepr(v) {
    try {
      if (v === null) return 'null';
      if (v === undefined) return 'undefined';
      const t = typeof v;
      if (t === 'string') return 'str(' + v.length + '):' + JSON.stringify(v.slice(0, 80));
      if (t === 'number' || t === 'boolean') return t + ':' + v;
      if (t === 'function') return 'fn:' + (v.name || '<anon>') + '/arity=' + v.length;
      if (v instanceof Uint8Array) return 'u8[' + v.length + ']';
      let proto = '?';
      try { proto = Object.prototype.toString.call(v); } catch (_) {}
      let keys = '';
      try { keys = Object.keys(v).slice(0, 12).join(','); } catch (_) {}
      let ctor = '';
      try { ctor = v && v.constructor ? v.constructor.name : ''; } catch (_) {}
      return proto + (ctor ? '/' + ctor : '') + ' keys=[' + keys + ']';
    } catch (e) { return '<unrepr:' + e.message + '>'; }
  }
  function listMethods(o) {
    const out = new Set();
    try {
      let cur = o;
      let depth = 0;
      while (cur && depth < 5) {
        for (const k of Object.getOwnPropertyNames(cur)) out.add(k);
        cur = Object.getPrototypeOf(cur);
        depth++;
      }
    } catch (_) {}
    return Array.from(out).slice(0, 50).join(',');
  }
  const origInst = WebAssembly.instantiate;
  WebAssembly.instantiate = function (buf, imports) {
    try {
      if (imports && typeof imports === 'object') {
        for (const ns of Object.keys(imports)) {
          const mod = imports[ns];
          if (!mod || typeof mod !== 'object') continue;
          for (const k of Object.keys(mod)) {
            const orig = mod[k];
            if (typeof orig !== 'function') continue;
            mod[k] = function (...a) {
              try {
                const r = orig.apply(this, a);
                return r;
              } catch (e) {
                const msg = (e && e.message) ? e.message : String(e);
                // Try to dig out what the receiver was. Most wbg shims do s(handle)[im(N)](...)
                // so the first arg is a handle. We can't reach into the closure's qB table
                // directly, but if globalThis.__qB_ref was set, peek there.
                let recvInfo = '';
                try {
                  if (globalThis.__qB_ref && typeof a[0] === 'number') {
                    const recv = globalThis.__qB_ref[a[0]];
                    recvInfo = ' recv0=' + shortRepr(recv);
                    recvInfo += ' methods=[' + listMethods(recv) + ']';
                  }
                } catch (_) {}
                const entry = ns + '.' + k + '(' + a.map(shortRepr).join(', ') + ')' + recvInfo + ' -> THROW: ' + msg;
                globalThis.__wbg_err.push(entry);
                throw e;
              }
            };
          }
        }
      }
    } catch (e) { globalThis.__wbg_err.push('hook setup error: ' + e.message); }
    return origInst.apply(this, arguments);
  };
})();
"""


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    ver = _v.latest_version()
    print(f"version: {ver[:16]}...")

    rt = JsRuntime()
    rt.eval(_HOOK_JS)

    r = requests.get(_v.asset_url(ver, "hsw.js"))
    src = r.content.decode("utf-8", errors="replace")
    print("loading hsw.js...")
    rt.eval(src, suppress=True)

    # Warmup: hsw(1, ...)
    rt.eval(
        """
        globalThis.__warm = 0;
        (async () => {
          try { await window.hsw(1, new Uint8Array(0)); } catch (e) { globalThis.__warm_err = String(e); }
          globalThis.__warm = 1;
        })();
        """,
        suppress=True,
    )
    for _ in range(60):
        if rt.eval("globalThis.__warm"):
            break
        time.sleep(0.2)
    print("warm err:", rt.eval("globalThis.__warm_err || ''"))

    now = int(time.time())
    jwt = (
        b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        + "."
        + b64u(json.dumps({"s": "test", "d": 1, "t": now, "exp": now + 600}).encode())
        + ".fake"
    )

    rt.eval(
        f"""
        globalThis.__jwt_done = 0;
        globalThis.__jwt_err = null;
        globalThis.__jwt_result = null;
        globalThis.__wbg_err = [];  // reset
        (async () => {{
          try {{
            globalThis.__jwt_result = await window.hsw('{jwt}');
          }} catch (e) {{
            globalThis.__jwt_err = String(e) + '\\nSTACK:\\n' + (e.stack || '<none>');
          }}
          globalThis.__jwt_done = 1;
        }})();
        """,
        suppress=True,
    )
    for _ in range(120):
        if rt.eval("globalThis.__jwt_done"):
            break
        time.sleep(0.5)

    done = rt.eval("globalThis.__jwt_done")
    err = rt.eval("globalThis.__jwt_err || ''")
    result = rt.eval(
        "globalThis.__jwt_result === null ? '<null>' : "
        "(typeof globalThis.__jwt_result === 'string' ? globalThis.__jwt_result : "
        "JSON.stringify(globalThis.__jwt_result))"
    )
    wbg_errs = rt.eval("JSON.stringify(globalThis.__wbg_err)")

    print(f"\ndone: {bool(done)}")
    print(f"jwt err: {err[:1000]}")
    print(f"result: {result}")
    print("\nwbg import errors:")
    for e in json.loads(wbg_errs or "[]")[:50]:
        print(f"  {e}")
    rt.close()


if __name__ == "__main__":
    main()
