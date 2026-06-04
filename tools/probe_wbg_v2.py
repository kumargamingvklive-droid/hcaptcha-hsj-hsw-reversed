"""Probe v2: patch the bundle to expose qB/im/s/FN, then capture the failing
wbg call WITH receiver introspection."""
from __future__ import annotations

import base64
import json
import re
import sys
import time

import requests

from hcaptcha import version as _v
from hcaptcha.tools.js_runtime import JsRuntime


_HOOK_JS = r"""
(function () {
  globalThis.__wbg_log = [];
  globalThis.__wbg_err = [];
  globalThis.__wbg_seen = {};
  function shortRepr(v) {
    try {
      if (v === null) return 'null';
      if (v === undefined) return 'undefined';
      const t = typeof v;
      if (t === 'string') return 'str(' + v.length + '):' + JSON.stringify(v.slice(0, 80));
      if (t === 'number' || t === 'boolean') return t + ':' + v;
      if (t === 'function') return 'fn:' + (v.name || '<anon>') + '/arity=' + v.length;
      if (v instanceof Uint8Array) return 'u8[' + v.length + ']';
      let proto = '?', ctor = '';
      try { proto = Object.prototype.toString.call(v); } catch (_) {}
      try { ctor = v && v.constructor ? v.constructor.name : ''; } catch (_) {}
      let keys = '';
      try { keys = Object.keys(v).slice(0, 12).join(','); } catch (_) {}
      return proto + (ctor ? '/' + ctor : '') + ' keys=[' + keys + ']';
    } catch (e) { return '<unrepr:' + e.message + '>'; }
  }
  function listMethods(o) {
    const out = new Set();
    try {
      let cur = o;
      for (let depth = 0; cur && depth < 6; depth++) {
        for (const k of Object.getOwnPropertyNames(cur)) out.add(k);
        cur = Object.getPrototypeOf(cur);
      }
    } catch (_) {}
    return Array.from(out).slice(0, 80).join(',');
  }
  globalThis.__shortRepr = shortRepr;
  globalThis.__listMethods = listMethods;

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
                let recvInfo = '';
                try {
                  if (globalThis.__qB && typeof a[0] === 'number') {
                    const recv = globalThis.__qB[a[0]];
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


def patch_bundle(src: str) -> tuple[str, dict]:
    """Inject `globalThis.__qB = qB` etc. right after their declarations.

    Variable names are obfuscated and change per-version, so detect them.
    Pattern of qB init (post-minify):
        var <QB>=new Array(1024)[<IM>(<num>)](void 0);
    """
    names = {}
    m = re.search(r"var (\w+)=new Array\(1024\)\[(\w+)\(\d+\)\]\(void 0\);", src)
    if not m:
        print("[patch] WARNING: qB injection point not found", file=sys.stderr)
        return src, names
    qb_name, im_name = m.group(1), m.group(2)
    names["qB"] = qb_name
    names["im"] = im_name

    # FN is defined as `<FN>=function(<X>){<COND> && <Y>;var <Z>=<C>;return <C>=<TBL>[<Z>],<TBL>[<Z>]=<X>,<Z>}`
    # Easier: the import shim object `Z: function(XH, xA) { return FN(s(XH)[xA >>> 0]) }` —
    # we can find a wbg shim and read which name it references. But for simplicity, ask the
    # bundle itself to expose:
    # im signature varies after self-deobfuscation; pass two args.
    inject = (
        f"globalThis.__qB={qb_name};"
        f"globalThis.__im=function(n){{return {im_name}(n,0);}};"
    )
    src = src[:m.end()] + inject + src[m.end():]
    print(f"[patch] injected: qB={qb_name} im={im_name}", file=sys.stderr)
    return src, names


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main():
    ver = _v.latest_version()
    print(f"version: {ver[:16]}...", flush=True)

    rt = JsRuntime()
    rt.eval(_HOOK_JS)

    r = requests.get(_v.asset_url(ver, "hsw.js"))
    src = r.content.decode("utf-8", errors="replace")
    src, names = patch_bundle(src)
    print("loading patched hsw.js...", flush=True)
    rt.eval(src, suppress=True)

    # Check the injection worked
    qb_ok = rt.eval("typeof globalThis.__qB === 'object' && globalThis.__qB && typeof globalThis.__im === 'function'")
    print(f"qB exposed: {qb_ok}", flush=True)

    # Warmup
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
    print("warm err:", rt.eval("globalThis.__warm_err || ''"), flush=True)

    # Optional: try im(201) -- it may fail on some versions
    try:
        im201 = rt.eval("(function(){try{return globalThis.__im(201);}catch(e){return '<err: ' + e.message + '>';}})()")
        print(f"im(201) sample = {im201!r}", flush=True)
    except Exception as e:
        print(f"im(201) probe failed: {e}", flush=True)

    # Drive window.hsw(jwt)
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
        globalThis.__wbg_err = [];
        // Catch async unhandled errors
        process.on('uncaughtException', (e) => {{
          globalThis.__wbg_err.push('UNCAUGHT: ' + (e && e.message ? e.message : String(e)) + (e && e.stack ? '\\n' + e.stack : ''));
        }});
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
    for i in range(120):
        if rt.eval("globalThis.__jwt_done"):
            break
        time.sleep(0.5)
        if i % 10 == 0:
            errs = rt.eval("JSON.stringify(globalThis.__wbg_err)")
            errs_n = len(json.loads(errs or "[]"))
            unh = rt.eval("JSON.stringify(globalThis.__unhandled || [])")
            unh_n = len(json.loads(unh or "[]"))
            print(f"  [t={i*0.5:.1f}s] wbg_errs={errs_n} unhandled={unh_n}", flush=True)
            if unh_n > 0:
                for u in json.loads(unh):
                    print(f"    UNH: {u[:300]}", flush=True)
                rt.eval("globalThis.__unhandled = []")

    done = rt.eval("globalThis.__jwt_done")
    err = rt.eval("globalThis.__jwt_err || ''")
    print(f"\ndone: {bool(done)}", flush=True)
    print(f"jwt err: {err[:1500]}", flush=True)

    wbg_errs = rt.eval("JSON.stringify(globalThis.__wbg_err)")
    print("\nwbg import errors:")
    for e in json.loads(wbg_errs or "[]")[:30]:
        print(f"  {e}")
    rt.close()


if __name__ == "__main__":
    main()
