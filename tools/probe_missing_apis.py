"""Find every JS property the WASM tries to read that doesn't exist.

Wraps window/document/navigator/screen/performance/etc. with logging
Proxies that return a no-op function on any missing property AND record
the access. Then loads hsw.js and drives window.hsw(jwt) to discover
exactly what surface the bundle expects.
"""
from __future__ import annotations

import base64
import json
import time

import requests

from hcaptcha import version as _v
from hcaptcha.tools.js_runtime import JsRuntime


_PROBE_JS = r"""
(function () {
  globalThis.__missing = [];
  function logAccess(host, prop) {
    globalThis.__missing.push(host + "." + String(prop));
  }
  function safeProxy(host, target) {
    return new Proxy(target || {}, {
      get(t, p) {
        if (p in t) return t[p];
        if (typeof p === "symbol") return undefined;
        logAccess(host, p);
        // return a no-op function that also acts as an object
        const fn = function () { return safeProxy(host + "." + String(p), {}); };
        Object.defineProperty(fn, "name", { value: String(p), configurable: true });
        return fn;
      },
      has(t, p) { return true; },
    });
  }
  // Don't fully proxy window — too invasive. Just stub a few risky globals.
  globalThis.navigator = safeProxy("navigator", globalThis.navigator || {
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    language: "en-US",
    languages: ["en-US"],
    hardwareConcurrency: 8,
    deviceMemory: 8,
    platform: "Win32",
    vendor: "Google Inc.",
    cookieEnabled: true,
    onLine: true,
    doNotTrack: null,
    maxTouchPoints: 0,
    webdriver: false,
    plugins: { length: 0 },
    mimeTypes: { length: 0 },
  });
  globalThis.screen = safeProxy("screen", globalThis.screen || {
    width: 1920, height: 1080, availWidth: 1920, availHeight: 1040,
    colorDepth: 24, pixelDepth: 24,
  });
  globalThis.performance = globalThis.performance || {
    now: () => Date.now(),
    timeOrigin: Date.now(),
    timing: {},
    getEntriesByType: () => [],
  };
  // Patch the existing Storage shim if jsdom provides one
})();
"""


def main():
    ver = _v.latest_version()
    print(f"version: {ver[:16]}...")

    rt = JsRuntime()
    rt.eval(_PROBE_JS)

    r = requests.get(f"https://newassets.hcaptcha.com/c/{ver}/hsw.js")
    r.encoding = "utf-8"
    print("loading hsw.js into probe sandbox...")
    rt.eval(r.text, suppress=True)

    # Wait for bundle ready
    rt.eval(
        """
        globalThis.__ready = 0;
        (async () => {
          try { await window.hsw(1, new Uint8Array(0)); } catch (e) { globalThis.__init_err = String(e); }
          globalThis.__ready = 1;
        })();
        """,
        suppress=True,
    )
    for _ in range(50):
        if rt.eval("globalThis.__ready"):
            break
        time.sleep(0.2)

    init_err = rt.eval("globalThis.__init_err || ''")
    if init_err:
        print(f"init error: {init_err}")

    missing_init = rt.eval("JSON.stringify(globalThis.__missing)")
    init_set = sorted(set(json.loads(missing_init or "[]")))
    print(f"\nMissing API access during init: {len(init_set)} distinct accesses")
    for m in init_set[:30]:
        print(f"  {m}")
    if len(init_set) > 30:
        print(f"  ... and {len(init_set)-30} more")

    # Now drive window.hsw(jwt) to discover its requirements
    print("\n--- driving window.hsw(jwt) ---")
    rt.eval("globalThis.__missing = []")
    now = int(time.time())

    def b64u(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    jwt = (
        b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        + "."
        + b64u(
            json.dumps({"s": "test", "d": 1, "t": now, "exp": now + 600}).encode()
        )
        + ".fake"
    )

    rt.eval(
        f"""
        globalThis.__jwt_done = 0;
        globalThis.__jwt_err = null;
        globalThis.__jwt_result = null;
        (async () => {{
          try {{
            globalThis.__jwt_result = await window.hsw('{jwt}');
          }} catch (e) {{
            globalThis.__jwt_err = String(e) + ' | stack: ' + (e.stack || '');
          }}
          globalThis.__jwt_done = 1;
        }})();
        """,
        suppress=True,
    )
    for _ in range(60):
        if rt.eval("globalThis.__jwt_done"):
            break
        time.sleep(0.5)

    jwt_done = rt.eval("globalThis.__jwt_done")
    jwt_err = rt.eval("globalThis.__jwt_err || ''")
    jwt_result = rt.eval(
        "globalThis.__jwt_result === null ? '<null>' : "
        "(typeof globalThis.__jwt_result === 'string' ? globalThis.__jwt_result : "
        "JSON.stringify(globalThis.__jwt_result))"
    )

    print(f"\njwt mode complete: {bool(jwt_done)}")
    if jwt_err:
        print(f"jwt ERROR: {jwt_err[:500]}")
    if jwt_result and jwt_result != "<null>":
        print(f"jwt RESULT (first 200): {str(jwt_result)[:200]}")

    missing_jwt = rt.eval("JSON.stringify(globalThis.__missing)")
    jwt_set = sorted(set(json.loads(missing_jwt or "[]")))
    print(f"\nMissing API access during window.hsw(jwt): {len(jwt_set)} distinct")
    for m in jwt_set:
        print(f"  {m}")


if __name__ == "__main__":
    main()
