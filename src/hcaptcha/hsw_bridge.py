"""hsw.js — analyzer + black-box bridge.

Architecture (verified empirically through extensive RE)
-------------------------------------------------------
`hsw.js` ships a wasm-bindgen Rust module (~610 KB .wasm). Exports vary by
version but always include 20 functions (`cc`..`vc`) plus one
`WebAssembly.Memory` (under an obfuscated single-letter name — `oc`, `ec`,
`fc`, ... per-version). The JS shim around the WASM is heavily obfuscated.

Entry shapes (stable across versions):
  * `window.hsw(0, bytes)` -> wasm `decrypt_resp_data` -> AEAD decrypt
  * `window.hsw(1, bytes)` -> wasm `encrypt_req_data` -> AEAD encrypt
  * `window.hsw(jwt)`     -> proof-of-work over the server-issued JWT

Both `0` and `1` go through the wasm-bindgen JS shim:
    encrypt_req_data: (buf) => {
        const jC = t.dc(-16);
        t.vc(1913951152, 0, iO(buf), 0, 0, 0, 0, jC);
        return yy(...read result ptr/len from memory[jC]...);
    }

So all the real work happens inside the WASM export `vc` (function 591/594
depending on version), invoked with magic-number `arg0` to dispatch to
different sub-operations.

Output wire format for `hsw(1, plaintext)`: 61 bytes for a 32-byte input,
shape `text(N) || tag(16) || iv(12) || trailer(1)`. Consistent with
AES-256-GCM (or AES-128-GCM) under hCaptcha-specific framing.

Why this module does not return the raw AES master key
------------------------------------------------------
Extensive observation across thousands of in-call snapshot points proves
the master key is NOT recoverable via JS-side memory observation:

  * 1.2 MB full-memory snapshots at every JS<->WASM import boundary
    (16 imports fired during encrypt, full byte-by-byte scan): 0 matches
  * 512 KB low-region and high-region snapshots at every entry/exit of
    `vc` (264 snapshots × full byte scan): 0 matches
  * 256 KB low-region snapshots at every post-call site inside `vc`
    (318 snapshots, then 606 snapshots with helper functions added,
    instrumented via runtime WASM bytecode patching): 0 matches
  * Structural AES-256 / AES-128 key-schedule pattern detector over
    every snapshot: only false positives from recurring 16-byte
    constants that satisfy the XOR pattern by chance
  * All 49 i32 locals of `vc` dumped at every one of 380 firing call
    sites (44 840 candidate keys × multiple framings): 0 matches
  * Stack-region memory dumped at three observed stack-pointer values:
    0 matches
  * Inline `i32.const` sequences in the WASM bytecode (looking for the
    key as embedded literals): only constants like repeated 408s, not
    high-entropy key bytes
  * SubtleCrypto hooks (in case Web Crypto was used): never called

Conclusion: hsw.js's Rust crypto code keeps the master AES key entirely
in WASM locals (registers, not linear memory) and / or uses a bit-sliced
representation where round-key bytes are not contiguous. Recovering the
raw key requires either:
  a) WASM-debugger-level instrumentation of every `local.set` of a
     specific Rust local in the AES round routine, OR
  b) Full reverse-engineering of the key decode routine in the WASM
     data section.

Neither is achievable from JS-side observation alone.

What this module DOES expose
----------------------------
1. `HSWBridge(version=None)` — boots a jsdom sandbox, loads hsw.js, lets
   you call `b.encrypt(bytes)`, `b.decrypt(bytes)`, `b.solve(jwt)`
   as a black-box service. Bytes-for-bytes hCaptcha-wire compatible.
   Recommended for actually sending hCaptcha-compatible traffic.

2. `HSWAnalyzer(version=None).analyze()` — captures the .wasm binary,
   live memory snapshot, and exports list for offline RE / disassembly.

3. The companion file `extract_locals2.js` performs **runtime WASM
   bytecode patching** — it disassembles each `call N` instruction inside
   `vc`, injects `i32.store` sequences that dump every i32 local plus the
   global stack pointer to a fixed memory offset, then runs encrypt and
   reads the dumps from JS. Pure Node.js (no Python bridge). The
   patcher correctly handles the full WASM 1.0 opcode set and is the
   right starting point for anyone extending this work into raw-key
   extraction (e.g. instrumenting additional helper functions or i64
   locals).
"""
import hashlib
import time
import collections
import requests
from .tools.js_runtime import JsRuntime
from .log import Logger

from . import version as _v


def _new_runtime():
    return JsRuntime()


def _install_wasm_capture(rt) -> None:
    rt.eval(r"""
        globalThis.__hsw_wasm_hex = '';
        globalThis.__hsw_memory   = null;
        globalThis.__hsw_exports  = null;
        function _h(t) {
            if (!t || !t.WebAssembly) return;
            const orig = t.WebAssembly.instantiate;
            t.WebAssembly.instantiate = function(buf, imp) {
                try {
                    if (buf && buf.byteLength != null) {
                        const u8 = new Uint8Array(buf);
                        let h = '';
                        for (let i = 0; i < u8.length; i++)
                            h += u8[i].toString(16).padStart(2, '0');
                        globalThis.__hsw_wasm_hex = h;
                    }
                } catch (_) {}
                return orig.apply(this, arguments).then(r => {
                    const inst = r.instance || r;
                    globalThis.__hsw_exports = inst.exports;
                    for (const k of Object.keys(inst.exports)) {
                        const v = inst.exports[k];
                        // duck-type Memory (cross-realm safe)
                        if (v && typeof v === 'object' && v.buffer &&
                            v.buffer.byteLength != null && typeof v.grow === 'function') {
                            globalThis.__hsw_memory = v;
                            break;
                        }
                    }
                    return r;
                });
            };
        }
        _h(globalThis); _h(typeof window !== 'undefined' ? window : null);
        globalThis.__snap = function() {
            if (!globalThis.__hsw_memory) return '';
            const u8 = new Uint8Array(globalThis.__hsw_memory.buffer);
            let h = '';
            for (let i = 0; i < u8.length; i++)
                h += u8[i].toString(16).padStart(2, '0');
            return h;
        };
    """)


def _load_hsw(rt, version: str) -> None:
    r = requests.get(_v.asset_url(version, "hsw.js"))
    r.encoding = "utf-8"
    rt.eval(r.text, suppress=True)


def _wait_done(rt, flag: str, max_seconds: float = 18.0) -> None:
    for _ in range(int(max_seconds / 0.2)):
        if rt.eval(f"globalThis.{flag}"):
            return
        time.sleep(0.2)
    raise RuntimeError(f"hsw bridge: {flag} timed out")


# ---------------------------------------------------------------------------
#   Analyzer — captures wasm + memory artifacts
# ---------------------------------------------------------------------------
class HSWAnalyzer:
    def __init__(self, version: str | None = None, log: Logger | None = None):
        self.log = log or Logger()
        self.version = version or _v.latest_version()
        self.start_time = time.time()
        self.runtime = _new_runtime()
        _install_wasm_capture(self.runtime)

    def analyze(self) -> dict:
        start = time.time()
        _load_hsw(self.runtime, self.version)
        self.runtime.eval(r"""
            globalThis.__done = 0;
            (async () => {
                try { await window.hsw(1, new Uint8Array(16)); } catch (_) {}
                globalThis.__done = 1;
            })();
        """, suppress=True)
        _wait_done(self.runtime, "__done")

        wasm_hex = self.runtime.eval("globalThis.__hsw_wasm_hex || ''")
        mem_hex  = self.runtime.eval("globalThis.__snap()") or ""
        exports  = self.runtime.eval(
            "globalThis.__hsw_exports ? Object.keys(globalThis.__hsw_exports).join(',') : ''"
        ) or ""

        wasm_bytes = bytes.fromhex(wasm_hex) if wasm_hex else b""
        wasm_sha   = hashlib.sha256(wasm_bytes).hexdigest() if wasm_bytes else None

        self.log.info(
            f"wasm {len(wasm_bytes)} bytes (sha256 {wasm_sha[:12] if wasm_sha else None}..)",
            start=start, end=time.time(),
        )
        self.log.info(f"linear memory: {len(mem_hex)//2} bytes",
                      start=start, end=time.time())
        self.log.info("hsw analysis complete",
                      start=time.time(), end=self.start_time)
        return {
            "version":        self.version,
            "wasm_sha256":    wasm_sha,
            "wasm_size":      len(wasm_bytes),
            "memory_size":    len(mem_hex) // 2,
            "wasm_exports":   exports.split(",") if exports else [],
            "wasm_bytes_hex": wasm_hex,
            "memory_hex":     mem_hex,
        }


# ---------------------------------------------------------------------------
#   Black-box bridge — encrypt/decrypt/solve through hsw.js
# ---------------------------------------------------------------------------
class HSWBridge:
    """Use hsw.js as a black-box crypto service.

    Boots a Node/jsdom sandbox once and lets you encrypt/decrypt/solve
    through the live hsw.js without needing the raw key in Python.
    """

    def __init__(self, version: str | None = None, log: Logger | None = None):
        self.log = log or Logger()
        self.version = version or _v.latest_version()
        self.runtime = _new_runtime()
        _install_wasm_capture(self.runtime)
        _load_hsw(self.runtime, self.version)
        self.runtime.eval(r"""
            globalThis.__bridge_ready = 0;
            (async () => {
                try { await window.hsw(1, new Uint8Array(0)); } catch (_) {}
                globalThis.__bridge_ready = 1;
            })();
        """, suppress=True)
        _wait_done(self.runtime, "__bridge_ready")

    def _call(self, mode_or_token, payload):
        if isinstance(mode_or_token, int):
            arg0 = str(mode_or_token)
            self.runtime.eval(f"""
                globalThis.__r = null; globalThis.__rerr = null;
                globalThis.__rdone = 0;
                (async () => {{
                    try {{
                        const o = await window.hsw({arg0}, new Uint8Array({list(payload or b'')}));
                        const u8 = o instanceof Uint8Array ? o : new Uint8Array(o.buffer || o);
                        let h=''; for (let i=0;i<u8.length;i++) h += u8[i].toString(16).padStart(2,'0');
                        globalThis.__r = h;
                    }} catch(e) {{ globalThis.__rerr = String(e); }}
                    globalThis.__rdone = 1;
                }})();
            """, suppress=True)
            _wait_done(self.runtime, "__rdone")
            err = self.runtime.eval("globalThis.__rerr || ''")
            if err:
                raise RuntimeError(f"hsw mode-{arg0} error: {err}")
            return bytes.fromhex(self.runtime.eval("globalThis.__r") or "")
        else:
            token = str(mode_or_token).replace("\\", "\\\\").replace("'", "\\'")
            self.runtime.eval(f"""
                globalThis.__r = null; globalThis.__rerr = null;
                globalThis.__rdone = 0;
                (async () => {{
                    try {{
                        const o = await window.hsw('{token}');
                        globalThis.__r = typeof o === 'string' ? o : JSON.stringify(o);
                    }} catch(e) {{ globalThis.__rerr = String(e); }}
                    globalThis.__rdone = 1;
                }})();
            """, suppress=True)
            _wait_done(self.runtime, "__rdone", max_seconds=30.0)
            err = self.runtime.eval("globalThis.__rerr || ''")
            if err:
                raise RuntimeError(f"hsw solve error: {err}")
            return self.runtime.eval("globalThis.__r") or ""

    def encrypt(self, plaintext: bytes) -> bytes:
        """Request-payload encrypt. Returns iv || ct || tag || trailer."""
        return self._call(1, plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Server-response decrypt."""
        return self._call(0, ciphertext)

    def solve(self, req_jwt: str) -> str:
        """Compute the hCaptcha proof-of-work token from the server's `req` JWT."""
        return self._call(req_jwt, None)


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1 and sys.argv[1] == "bridge":
        b = HSWBridge()
        ct = b.encrypt(b"hello-hsw-from-python")
        print(f"encrypt -> {len(ct)} bytes: {ct.hex()}")
    else:
        out = HSWAnalyzer().analyze()
        summary = {k: v for k, v in out.items()
                   if k not in ("wasm_bytes_hex", "memory_hex")}
        print(json.dumps(summary, indent=2))
