"""Multi-site runtime trace of all AES key-schedule sites in HSW.

Patches every fixslice (i32,i32)->() candidate to dump the 32 bytes at the
key buffer pointer, then runs window.hsw in all three modes (encrypt,
decrypt, n-token JWT) and reports all distinct keys observed plus
cross-checks against our 5 currently-extracted keys.
"""
from __future__ import annotations

import base64
import json
import sys
import time

import requests

from collections import defaultdict

from hcaptcha import KeyFetcher
from hcaptcha import version as _v
from hcaptcha.hsw import _build_injection, _find_deobf_helper
from hcaptcha.hsw_bridge import HSWAnalyzer
from hcaptcha.tools.js_runtime import JsRuntime
from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions
from hcaptcha.tools.wasm_writer import ModuleWriter


_HOOK_JS = r"""
(function () {
  function _b64ToU8(s) {
    if (typeof Buffer !== "undefined") {
      const b = Buffer.from(s, "base64");
      return new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
    }
    const bin = atob(s);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return u8;
  }
  function _install(t) {
    if (!t || !t.WebAssembly) return;
    const origInstantiate = t.WebAssembly.instantiate;
    t.WebAssembly.instantiate = function (buf, imp) {
      let useBuf = buf;
      if (buf && buf.byteLength != null) {
        useBuf = _b64ToU8(globalThis.__patched_wasm_b64);
      }
      return origInstantiate.call(this, useBuf, imp).then(r => {
        const inst = r.instance || r;
        if (inst && inst.exports) {
          globalThis.__hsw_exports = inst.exports;
          for (const k of Object.keys(inst.exports)) {
            const v = inst.exports[k];
            if (v && typeof v === "object" && v.buffer && typeof v.grow === "function") {
              globalThis.__hsw_memory = v;
              break;
            }
          }
        }
        return r;
      });
    };
    if (t.WebAssembly.instantiateStreaming) {
      t.WebAssembly.instantiateStreaming = async function (source, imp) {
        const resp = await source;
        const buf = await resp.arrayBuffer();
        return t.WebAssembly.instantiate(buf, imp);
      };
    }
  }
  _install(globalThis);
  _install(typeof window !== "undefined" ? window : null);
})();
"""


def main() -> int:
    ver = _v.latest_version()
    print(f"version: {ver[:16]}...")

    info = HSWAnalyzer(ver).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)

    canonical = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
    ks_candidates = []
    for _score, fi, masks in find_fixslice_functions(mod, top_n=100):
        canon = canonical & set(masks.keys())
        if len(canon) >= 3:
            fn = next((f for f in mod.functions if f["func_idx"] == fi), None)
            if fn and mod.types[fn["type_idx"]] == (["i32", "i32"], []):
                ks_candidates.append(fi)
    ks_candidates = sorted(set(ks_candidates))
    print(f"KS candidates: {ks_candidates}")

    SCRATCH_BASE = 40_000
    slots = {}
    for i, ks in enumerate(ks_candidates):
        base = SCRATCH_BASE + i * 80
        slots[ks] = {
            "scratch": base,
            "sentinel_addr": base + 40,
            "sentinel_val": 0xCAFE0000 | (i & 0xFF),
        }

    writer = ModuleWriter(mod)
    patched_count = 0
    for ks in ks_candidates:
        try:
            deobf = _find_deobf_helper(mod, ks)
        except Exception as e:
            print(f"  no deobf helper for KS {ks}: {e}")
            continue
        slot = slots[ks]
        sv = slot["sentinel_val"]
        if sv >= 0x80000000:
            sv -= 0x100000000
        inj = _build_injection(deobf, slot["scratch"], slot["sentinel_addr"], sv)
        writer.code.splice_code(ks, 0, n_replace=0, new_bytes=inj)
        patched_count += 1
        print(f"  patched KS {ks} deobf={deobf}")

    if not patched_count:
        print("no KS patches applied; aborting")
        return 1

    type_i32_to_i32 = next(
        (i for i, (p, r) in enumerate(mod.types) if p == ["i32"] and r == ["i32"]),
        None,
    )
    if type_i32_to_i32 is None:
        type_i32_to_i32 = writer.add_type(["i32"], ["i32"])
    type_i32i32_to_void = next(
        (i for i, (p, r) in enumerate(mod.types) if p == ["i32", "i32"] and r == []),
        None,
    )
    if type_i32i32_to_void is None:
        type_i32i32_to_void = writer.add_type(["i32", "i32"], [])

    writer.add_function(
        type_i32_to_i32,
        [],
        bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0B]),
        export_name="__peek32",
    )
    writer.add_function(
        type_i32i32_to_void,
        [],
        bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0B]),
        export_name="__poke32",
    )

    patched = writer.emit()
    print(f"patched WASM size: {len(patched)} (+{len(patched) - len(wasm)})")

    rt = JsRuntime()
    rt.eval(f"globalThis.__patched_wasm_b64 = '{base64.b64encode(patched).decode()}';")
    rt.eval(_HOOK_JS)

    r = requests.get(f"https://newassets.hcaptcha.com/c/{ver}/hsw.js")
    r.encoding = "utf-8"
    rt.eval(r.text, suppress=True)

    rt.eval(
        """
        globalThis.__ready = 0;
        (async () => {
          try { await window.hsw(1, new Uint8Array(0)); } catch (_) {}
          globalThis.__ready = 1;
        })();
        """,
        suppress=True,
    )
    for _ in range(60):
        if rt.eval("globalThis.__ready"):
            break
        time.sleep(0.2)

    def read_slot(ks_fn):
        slot = slots[ks_fn]
        words = []
        for off in range(0, 32, 4):
            v = rt.eval(
                f"globalThis.__hsw_exports.__peek32({slot['scratch']} + {off})"
            )
            words.append((v if v is not None else 0) & 0xFFFFFFFF)
        raw = b"".join(x.to_bytes(4, "little") for x in words)
        sent = (
            rt.eval(f"globalThis.__hsw_exports.__peek32({slot['sentinel_addr']})") or 0
        )
        return raw, sent & 0xFFFFFFFF

    def zero_all():
        for ks in ks_candidates:
            slot = slots[ks]
            for off in range(0, 48, 4):
                rt.eval(
                    f"globalThis.__hsw_exports.__poke32({slot['scratch']} + {off}, 0)"
                )

    captures = {}

    def drive(mode_label: str, js_invocation: str, timeout_iters: int = 60):
        print(f"\n=== mode: {mode_label} ===")
        zero_all()
        rt.eval(
            f"""
            globalThis.__done_{mode_label} = 0;
            (async () => {{
              try {{ {js_invocation} }} catch (_) {{}}
              globalThis.__done_{mode_label} = 1;
            }})();
            """,
            suppress=True,
        )
        for _ in range(timeout_iters):
            if rt.eval(f"globalThis.__done_{mode_label}"):
                break
            time.sleep(0.3)
        for ks in ks_candidates:
            raw, sent = read_slot(ks)
            fired = (sent & 0xFFFF0000) == 0xCAFE0000
            captures[(mode_label, ks)] = (raw.hex(), f"0x{sent:08x}", fired)
            mark = "[fired]" if fired else "[unset]"
            print(f"  KS {ks} {mark} key={raw.hex()[:40]}...")

    drive("encrypt", "await window.hsw(1, new Uint8Array(32));")
    drive("decrypt", "await window.hsw(0, new Uint8Array(32));")

    now = int(time.time())

    def b64u(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    jwt = (
        b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        + "."
        + b64u(
            json.dumps({"s": "00000000", "d": 1, "t": now, "exp": now + 600}).encode()
        )
        + ".fake"
    )
    drive("jwt", f"await window.hsw('{jwt}');", timeout_iters=120)

    print("\n=== distinct keys observed ===")
    distinct = defaultdict(list)
    for (mode, ks), (key, sent, fired) in captures.items():
        if not fired or key == "0" * 64:
            continue
        distinct[key].append((mode, ks))

    baseline = KeyFetcher(version=ver).fetch()
    known = {
        "hsj.n_key":                baseline["hsj"]["n_key"],
        "hsj.response_decrypt_key": baseline["hsj"]["response_decrypt_key"],
        "hsj.payload_encrypt_key":  baseline["hsj"]["payload_encrypt_key"],
        "hsw.encrypt_key":          baseline["hsw"]["encrypt_key"],
        "hsw.decrypt_key":          baseline["hsw"]["decrypt_key"],
    }
    known_inv = {v: k for k, v in known.items()}

    print(f"\n{len(distinct)} distinct AES-256 key(s) seen at runtime across 3 modes")
    for i, (key, modes) in enumerate(distinct.items(), 1):
        label = known_inv.get(key, "*** UNKNOWN — possibly N-key / 4th key ***")
        print(f"\n  ({i}) {label}")
        print(f"     bytes: {key}")
        print(f"     fired by: {modes}")

    new_keys = [k for k in distinct if k not in known_inv]
    if new_keys:
        print(f"\nFOUND {len(new_keys)} key(s) NOT in our current extractor:")
        for k in new_keys:
            print(f"  {k}")
    else:
        print("\nNo new keys beyond the 5 we already extract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
