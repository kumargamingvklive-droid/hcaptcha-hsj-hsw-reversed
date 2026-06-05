"""instrument_encrypt_entry.py — dump (arg0, arg1, arg2, *arg*) at the
n-token AES encrypt entry function.

We dynamically discover ENTRY_FN on the current build:
  * signature (i32,i32,i32) -> (i32)
  * calls a fixslice32 KS function 3+ times
  * reachable from one of {ec, pc, kc}

We splice a prologue that records a single ring of fixed-size records:

    struct rec {
        u32 counter;          // index in ring
        u32 arg0;
        u32 arg1;
        u32 arg2;
        u8  buf0[32];         // bytes at *arg0 (KEY candidate)
        u8  buf1[256];        // bytes at *arg1 (PT/CT/AAD candidate)
        u8  buf2[256];        // bytes at *arg2 (PT/CT/AAD candidate)
    };  // 4*4 + 32 + 256 + 256 = 560 bytes

We drive window.hsw(jwt) and read the ring. Then we also re-read each
record's three pointers AFTER the call to see what the OUTPUT pointer
ended up containing (post-encryption).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from collections import defaultdict, Counter

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, os.path.join(ROOT, "src"))

import requests

from hcaptcha.log import Logger
from hcaptcha.tools.js_runtime import JsRuntime
from hcaptcha.tools.wasm_disasm import WasmModule, decode_uleb, decode_sleb
from hcaptcha.tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from hcaptcha import version as _v
from hcaptcha.hsw_bridge import HSWAnalyzer


# Record layout
REC_HEADER = 16                              # counter + 3 ptrs
REC_BUF0   = 32                              # bytes at *arg0 (key)
REC_BUF1   = 256                             # bytes at *arg1
REC_BUF2   = 256                             # bytes at *arg2
REC_SIZE   = REC_HEADER + REC_BUF0 + REC_BUF1 + REC_BUF2   # 560
MAX_RECS   = 64                              # 64 calls per record

# Memory layout in wasm scratch (above usual heap usage)
COUNTER_ADDR = 60_000                        # u32 counter (PRE)
BUF_ADDR     = 60_004                        # 64 * 560 = 35,840 bytes
GATE_ADDR    = COUNTER_ADDR + 4 + MAX_RECS * REC_SIZE + 16  # well past ring

# Tmp slots
TMP_C    = GATE_ADDR + 16
TMP_A    = TMP_C + 4
TMP_OFF  = TMP_A + 4

# --------------------------------------------------------------------
# WebAssembly.instantiate hook (same as hsw_n_key_capture)
# --------------------------------------------------------------------
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
            if (v && typeof v === "object" && v.buffer &&
                typeof v.grow === "function") {
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


# --------------------------------------------------------------------
# KS fingerprint (fixslice32)
# --------------------------------------------------------------------
def _is_fixslice_ks(mod: WasmModule, fi: int) -> bool:
    """Functions with sig (i32,i32)->() , body >= 1000B, lots of XOR
    and mask constants 0x0F000F00 / 0x55555555 / 0x33333333."""
    f = next((x for x in mod.functions if x["func_idx"] == fi), None)
    if f is None:
        return False
    if mod.types[f["type_idx"]] != (["i32", "i32"], []):
        return False
    body_len = f["code_end"] - f["code_start"]
    if body_len < 1000:
        return False
    instrs = mod.decode_function(fi) or []
    op = Counter(n for n, _, _, _ in instrs)
    if op.get("i32.xor", 0) < 80:
        return False
    consts = {ops[0] & 0xFFFFFFFF for n, ops, _, _ in instrs
              if n == "i32.const" and ops}
    return bool(
        0x0F000F00 in consts or 0x55555555 in consts
        or 0x33333333 in consts or 251662080 in consts
    )


def _find_ks_set(mod: WasmModule) -> set[int]:
    return {f["func_idx"] for f in mod.functions
            if _is_fixslice_ks(mod, f["func_idx"])}


def _parse_elements(mod: WasmModule) -> set[int]:
    """Parse element section -> set of table func indices."""
    table_funcs: set[int] = set()
    sec = None
    for x in mod.sections:
        if x[0] == 9:
            sec = x; break
    if not sec:
        return table_funcs
    raw = mod.raw
    _, _, off, plen = sec
    count, n = decode_uleb(raw, off); off += n
    for _ in range(count):
        flag, n = decode_uleb(raw, off); off += n
        if flag == 0:
            if raw[off] == 0x41:
                _, m = decode_sleb(raw, off + 1); off += 1 + m
            while raw[off] != 0x0b:
                off += 1
            off += 1
            n_init, m = decode_uleb(raw, off); off += m
            for _ in range(n_init):
                fi, m = decode_uleb(raw, off); off += m
                table_funcs.add(fi)
        else:
            break
    return table_funcs


def _build_call_graph(mod: WasmModule) -> dict[int, set[int]]:
    """Build call graph including call_indirect via element-table funcs."""
    table_funcs = _parse_elements(mod)
    type_to_funcs: dict[int, list[int]] = defaultdict(list)
    for fi in table_funcs:
        f = next((x for x in mod.functions if x["func_idx"] == fi), None)
        if f:
            type_to_funcs[f["type_idx"]].append(fi)
    g: dict[int, set[int]] = defaultdict(set)
    for f in mod.functions:
        fi = f["func_idx"]
        for n, ops, _, _ in (mod.decode_function(fi) or []):
            if n == "call" and ops:
                g[fi].add(ops[0])
            elif n == "call_indirect" and ops:
                for tgt in type_to_funcs.get(ops[0], []):
                    g[fi].add(tgt)
    return g


def _bfs_reach(g: dict[int, set[int]], src: int, depth: int = 12) -> set[int]:
    visited = {src}; layer = {src}
    for _ in range(depth):
        nxt = set()
        for n in layer:
            for c in g.get(n, ()):
                if c not in visited:
                    visited.add(c); nxt.add(c)
        layer = nxt
        if not layer: break
    visited.discard(src)
    return visited


def _count_calls_to(mod: WasmModule, caller: int, targets: set[int]) -> int:
    n = 0
    for name, ops, _, _ in (mod.decode_function(caller) or []):
        if name == "call" and ops and ops[0] in targets:
            n += 1
    return n


def _find_encrypt_entry(mod: WasmModule, log: Logger) -> int:
    """Discover ENTRY_FN with sig (i32,i32,i32)->(i32) that calls a
    fixslice KS function 3+ times.

    The workflow says it should be reachable from one of {ec, pc, kc};
    we verify by computing reachability with call-graph that INCLUDES
    call_indirect (the wbg-bindgen Promise dispatchers use table
    dispatch heavily). If no candidate satisfies strict reachability,
    we fall back to the structural fingerprint alone (it's specific
    enough to be unique).
    """
    ks_set = _find_ks_set(mod)
    log.info(f"  fixslice KS candidates: {sorted(ks_set)}", start=0, end=0)

    exp = {e["name"]: e["idx"] for e in mod.exports if e["kind"] == "func"}
    g   = _build_call_graph(mod)

    reach_union: set[int] = set()
    for ex in ("ec", "pc", "kc"):
        if ex in exp:
            r = _bfs_reach(g, exp[ex], depth=20)
            reach_union |= r
            log.info(f"    reachable from {ex}: {len(r)} funcs",
                     start=0, end=0)

    # Sig fingerprint candidates
    sig3 = (["i32", "i32", "i32"], ["i32"])
    fingerprint_cands = []
    for f in mod.functions:
        fi = f["func_idx"]
        if mod.types[f["type_idx"]] != sig3:
            continue
        ncalls = _count_calls_to(mod, fi, ks_set)
        if ncalls >= 3:
            fingerprint_cands.append(
                (fi, ncalls, fi in reach_union))

    fingerprint_cands.sort(key=lambda x: (-x[1], not x[2], x[0]))
    log.info(f"  encrypt-entry fingerprint hits "
             f"(fn, n_ks_calls, in_ec_pc_kc_reach): {fingerprint_cands}",
             start=0, end=0)
    if not fingerprint_cands:
        raise RuntimeError("no encrypt entry candidate found")

    # Prefer (most KS calls, AND reachable). The reachability test
    # may fail because the encrypt entry sits behind a wbg-bindgen
    # dispatcher that's only reachable via a non-{ec,pc,kc} export
    # (it can sit under {ic, lc, oc, qc} on this build).
    chosen = fingerprint_cands[0][0]
    log.info(f"  -> chose ENTRY_FN = {chosen}", start=0, end=0)
    return chosen


# --------------------------------------------------------------------
# Prologue codegen
# --------------------------------------------------------------------
def _build_prologue(n_args_total: int) -> bytes:
    """Build a prologue that records (counter, arg0, arg1, arg2,
    buf0[32], buf1[256], buf2[256]) into the ring.

    Each arg is local[arg_idx]. n_args_total is the count of i32 params
    (should be 3) but we only ever dump exactly 3.
    """
    out = bytearray()

    # if (*GATE) {
    out += b"\x41" + encode_sleb(GATE_ADDR)
    out += b"\x28\x02\x00"          # i32.load
    out += b"\x04\x40"              # if (empty)

    # tmp_c = *COUNTER
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x41" + encode_sleb(COUNTER_ADDR)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"

    # if (tmp_c < MAX_RECS) {
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_RECS)
    out += b"\x49"                  # i32.lt_u
    out += b"\x04\x40"              # if (empty)

    # tmp_a = BUF + tmp_c * REC_SIZE
    out += b"\x41" + encode_sleb(TMP_A)
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(REC_SIZE)
    out += b"\x6c"                  # i32.mul
    out += b"\x41" + encode_sleb(BUF_ADDR)
    out += b"\x6a"                  # i32.add
    out += b"\x36\x02\x00"

    # Helpers — store a u32 v at tmp_a + off
    def _store_u32_at_off(off: int, push_value_bytes: bytes):
        # base addr (tmp_a)
        nonlocal out
        out += b"\x41" + encode_sleb(TMP_A)
        out += b"\x28\x02\x00"
        # value
        out += push_value_bytes
        # i32.store align=2 off
        out += b"\x36\x02" + encode_uleb(off)

    # *(tmp_a + 0)  = counter
    _store_u32_at_off(0,  b"\x41" + encode_sleb(TMP_C) + b"\x28\x02\x00")
    # *(tmp_a + 4)  = arg0
    _store_u32_at_off(4,  b"\x20" + encode_uleb(0))
    # *(tmp_a + 8)  = arg1
    _store_u32_at_off(8,  b"\x20" + encode_uleb(1))
    # *(tmp_a + 12) = arg2
    _store_u32_at_off(12, b"\x20" + encode_uleb(2))

    # ---- copy bytes from *argX to record buffer X --------------------
    def _copy_bytes_from_arg(arg_local: int, dst_field_off: int,
                              n_bytes: int):
        """memcpy n_bytes from local[arg_local] (pointer) to
        record_base + dst_field_off. Inlined as i64 blocks (8 bytes
        each), then byte-by-byte tail. We guard with: if (arg != 0).
        """
        nonlocal out
        # if (arg != 0) {
        out += b"\x20" + encode_uleb(arg_local)        # local.get arg
        out += b"\x04\x40"                             # if (empty)
        # Block copy: n_bytes is always a small constant (32 or 256).
        n_q = n_bytes // 8
        n_r = n_bytes - n_q * 8
        for q in range(n_q):
            # dst addr
            out += b"\x41" + encode_sleb(TMP_A)
            out += b"\x28\x02\x00"
            # value: i64.load *(arg + q*8)
            out += b"\x20" + encode_uleb(arg_local)
            out += b"\x29\x00" + encode_uleb(q * 8)    # align=0 (unaligned-safe)
            # i64.store align=0 off=(dst_field_off + q*8)
            out += b"\x37\x00" + encode_uleb(dst_field_off + q * 8)
        for r in range(n_r):
            out += b"\x41" + encode_sleb(TMP_A)
            out += b"\x28\x02\x00"
            out += b"\x20" + encode_uleb(arg_local)
            out += b"\x2d\x00" + encode_uleb(n_q * 8 + r)  # i32.load8_u
            out += b"\x3a\x00" + encode_uleb(dst_field_off + n_q * 8 + r)  # i32.store8
        out += b"\x0b"                                 # end if (arg!=0)

    # arg0 -> buf0 (32B), arg1 -> buf1 (256B), arg2 -> buf2 (256B)
    _copy_bytes_from_arg(0, REC_HEADER, REC_BUF0)
    _copy_bytes_from_arg(1, REC_HEADER + REC_BUF0, REC_BUF1)
    _copy_bytes_from_arg(2, REC_HEADER + REC_BUF0 + REC_BUF1, REC_BUF2)

    # counter++
    out += b"\x41" + encode_sleb(COUNTER_ADDR)
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"

    out += b"\x0b"   # end if (counter < MAX)
    out += b"\x0b"   # end if (gate)
    return bytes(out)


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
def run(version: str | None = None) -> dict:
    log = Logger()
    version = version or _v.latest_version()

    info = HSWAnalyzer(version, log=log).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod  = WasmModule(wasm)
    log.info(f"wasm {len(wasm)}B sha256={info['wasm_sha256']}",
             start=0, end=0)

    # Discover ENTRY_FN
    entry_fn = _find_encrypt_entry(mod, log)
    log.info(f"ENTRY_FN = {entry_fn}", start=0, end=0)

    # Patch the module
    writer = ModuleWriter(mod)
    prologue = _build_prologue(n_args_total=3)
    writer.code.splice_code(entry_fn, 0, n_replace=0, new_bytes=prologue)

    # peek/poke exports
    t_i32_to_i32 = next(
        (i for i, (p, r) in enumerate(mod.types)
         if p == ["i32"] and r == ["i32"]), None)
    if t_i32_to_i32 is None:
        t_i32_to_i32 = writer.add_type(["i32"], ["i32"])
    t_i32i32_to_void = next(
        (i for i, (p, r) in enumerate(mod.types)
         if p == ["i32", "i32"] and r == []), None)
    if t_i32i32_to_void is None:
        t_i32i32_to_void = writer.add_type(["i32", "i32"], [])
    writer.add_function(
        t_i32_to_i32, [],
        bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]),
        export_name="__peek32")
    writer.add_function(
        t_i32i32_to_void, [],
        bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]),
        export_name="__poke32")

    patched = writer.emit()
    log.info(f"patched wasm: {len(patched)}B (+{len(patched)-len(wasm)}B)",
             start=0, end=0)

    # Run
    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64 = '"
                f"{base64.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js"))
        r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)
        # Warmup
        rt.eval(
            """(async () => {
                try { await window.hsw(1, new Uint8Array(0)); }
                catch (e) { globalThis.__warmup_err = String(e); }
            })();""",
            suppress=True,
        )
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break
        if rt.eval("globalThis.__hsw_exports") is None:
            raise RuntimeError("WASM never instantiated")

        # Build JWT
        now = int(time.time())
        def b64u(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        jwt = (
            b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            + "."
            + b64u(json.dumps(
                {"s": "00000000", "d": 1, "t": now, "exp": now + 600}
            ).encode())
            + ".fake"
        )

        # Reset counter, enable gate, run hsw(jwt)
        rt.eval(
            f"""
            globalThis.__done = 0;
            globalThis.__tok = '';
            globalThis.__err = '';
            (async () => {{
                const e = globalThis.__hsw_exports;
                e.__poke32({COUNTER_ADDR}, 0);
                e.__poke32({GATE_ADDR}, 1);
                try {{
                    const r = await window.hsw('{jwt}');
                    globalThis.__tok = (typeof r === 'string') ? r : '';
                }} catch (ex) {{
                    globalThis.__err = String(ex);
                }} finally {{
                    e.__poke32({GATE_ADDR}, 0);
                }}
                globalThis.__done = 1;
            }})();
            """,
            suppress=True,
        )
        for _ in range(400):
            if rt.eval("globalThis.__done"):
                break
            time.sleep(0.25)

        err = rt.eval("globalThis.__err") or ""
        if err:
            log.info(f"hsw raised: {err[:300]}", start=0, end=0)
        token = rt.eval("globalThis.__tok") or ""
        log.info(f"token len={len(token)}", start=0, end=0)

        n_recs_total = (rt.eval(
            f"globalThis.__hsw_exports.__peek32({COUNTER_ADDR})"
        ) or 0) & 0xFFFFFFFF
        n_recs = min(n_recs_total, MAX_RECS)
        log.info(f"records captured: {n_recs} (counter={n_recs_total})",
                 start=0, end=0)

        # Read raw ring
        total_bytes = n_recs * REC_SIZE
        arr = rt.eval(
            f"""(function() {{
                const mem = new Uint8Array(
                    globalThis.__hsw_memory.buffer, {BUF_ADDR}, {total_bytes});
                return Array.from(mem);
            }})()"""
        ) or []
        arr = bytes(arr)

        # Parse + AFTER-call snapshot from each unique arg pointer
        records = []
        unique_ptrs = set()
        for i in range(n_recs):
            base = i * REC_SIZE
            counter = int.from_bytes(arr[base:base+4],     "little")
            a0      = int.from_bytes(arr[base+4:base+8],   "little")
            a1      = int.from_bytes(arr[base+8:base+12],  "little")
            a2      = int.from_bytes(arr[base+12:base+16], "little")
            buf0    = arr[base+REC_HEADER:base+REC_HEADER+REC_BUF0]
            buf1    = arr[base+REC_HEADER+REC_BUF0:
                         base+REC_HEADER+REC_BUF0+REC_BUF1]
            buf2    = arr[base+REC_HEADER+REC_BUF0+REC_BUF1:
                         base+REC_HEADER+REC_BUF0+REC_BUF1+REC_BUF2]
            records.append({
                "counter": counter, "arg0": a0, "arg1": a1, "arg2": a2,
                "buf0_pre_hex": buf0.hex(),
                "buf1_pre_hex": buf1.hex(),
                "buf2_pre_hex": buf2.hex(),
            })
            unique_ptrs.update([a0, a1, a2])

        # AFTER-call snapshot for unique ptrs (so we can identify the
        # output buffer — its memory should have changed and now look
        # like ciphertext).
        after = {}
        for p in unique_ptrs:
            if p == 0 or p > 16 * 1024 * 1024:
                continue
            try:
                hex_bytes = rt.eval(
                    f"""(function() {{
                        const mem = new Uint8Array(
                            globalThis.__hsw_memory.buffer, {p}, 512);
                        return Array.from(mem);
                    }})()"""
                ) or []
                after[p] = bytes(hex_bytes).hex()
            except Exception as e:
                after[p] = f"ERR:{e}"

        return {
            "wasm_sha256":   info["wasm_sha256"],
            "entry_fn":      entry_fn,
            "jwt":           jwt,
            "token":         token,
            "n_recs":        n_recs,
            "n_recs_total":  n_recs_total,
            "records":       records,
            "after_snapshot": after,
        }
    finally:
        try:
            rt.close()
        except Exception:
            pass


# --------------------------------------------------------------------
# Analysis: identify key/plaintext/output among arg0,arg1,arg2
# --------------------------------------------------------------------
def analyze(out: dict, expected_n_key_hex: str | None) -> dict:
    recs = out["records"]
    if not recs:
        return {"verdict": "no records captured"}

    arg_label = {0: "arg0", 1: "arg1", 2: "arg2"}
    buf_label = {0: "buf0_pre_hex", 1: "buf1_pre_hex", 2: "buf2_pre_hex"}

    # Distinguish pointers from small integers (lengths). Valid wasm
    # linear-memory pointers in this build live at >= ~64K (memory base
    # offset). A value < 65536 is overwhelmingly likely a length, an
    # enum tag, or an offset.
    PTR_THRESHOLD = 65536
    arg_kind = {}     # 0=>'ptr', 0=>'small_int', etc
    for i in (0, 1, 2):
        vals = [r[arg_label[i]] for r in recs]
        all_ptr  = all(v >= PTR_THRESHOLD for v in vals)
        all_small = all(v < PTR_THRESHOLD for v in vals)
        if all_ptr:    arg_kind[i] = "pointer"
        elif all_small: arg_kind[i] = "small_int"
        else:          arg_kind[i] = "mixed"

    def slice32_hex(rec, i):
        return rec[buf_label[i]][:64]

    # For pointer args, check first-32B constancy
    static_first32 = {}
    for i in (0, 1, 2):
        if arg_kind[i] != "pointer":
            continue
        vals = {slice32_hex(r, i) for r in recs}
        if len(vals) == 1:
            static_first32[i] = next(iter(vals))

    expected_norm = (expected_n_key_hex or "").lower()
    matches_expected = {i: (sl == expected_norm)
                        for i, sl in static_first32.items()}

    # KEY ARG: pointer whose first 32B is constant across calls
    # (the AES master key is build-static)
    key_arg = None
    # First try expected-key match; fall back to "any static 32B"
    for i, sl in static_first32.items():
        if sl == expected_norm:
            key_arg = i
            break
    if key_arg is None:
        # No expected-key match (n_key has rotated from data/keys.json).
        # Pick the SHORTEST-named ptr-arg with static first-32B —
        # canonical Rust calling convention puts the &self / &key first.
        for i in (0, 1, 2):
            if i in static_first32:
                key_arg = i
                break

    # Text-score heuristic for plaintext detection
    def text_score_of(hex_str: str) -> int:
        try:
            b = bytes.fromhex(hex_str)
        except Exception:
            return 0
        return sum(1 for x in b[:128] if 0x20 <= x < 0x7f
                   or x in (9, 10, 13))

    text_score: dict[int, int] = {}
    for i in (0, 1, 2):
        if arg_kind[i] != "pointer":
            text_score[i] = -1  # not a buffer
        else:
            text_score[i] = sum(text_score_of(r[buf_label[i]]) for r in recs)

    # PLAINTEXT_ARG: pointer arg other than key whose buffer varies
    # across calls AND has text-like content (JSON/msgpack/serde bytes)
    pt_arg = None
    pt_candidates = []
    for i in (0, 1, 2):
        if arg_kind[i] != "pointer" or i == key_arg:
            continue
        varies = len({slice32_hex(r, i) for r in recs}) > 1
        pt_candidates.append((i, text_score[i], varies))
    pt_candidates.sort(key=lambda x: (-x[1], not x[2]))
    if pt_candidates:
        pt_arg = pt_candidates[0][0]

    # OUTPUT_ARG: the remaining ptr arg (if any). Compare its PRE buffer
    # to AFTER snapshot to confirm post-encrypt change.
    out_arg = None
    for i in (0, 1, 2):
        if arg_kind[i] == "pointer" and i not in (key_arg, pt_arg):
            out_arg = i
            break

    # Verify output: did the bytes at out_arg's pointer change from PRE
    # to AFTER? (true = it was an output)
    output_changed = None
    if out_arg is not None:
        # Take FIRST record (after snapshot is from the live mem at the
        # end of all 226 calls, so only meaningful for the latest call).
        last = recs[-1]
        ptr = last[arg_label[out_arg]]
        pre = last[buf_label[out_arg]]
        after = out.get("after_snapshot", {}).get(str(ptr)) \
                or out.get("after_snapshot", {}).get(ptr)
        if after:
            # Compare the leading bytes that overlapped (256B PRE vs
            # 512B AFTER)
            n = min(len(pre), len(after))
            output_changed = pre[:n] != after[:n]

    return {
        "arg_kind":       {arg_label[i]: arg_kind[i] for i in (0, 1, 2)},
        "arg_values_per_call": [
            {arg_label[i]: r[arg_label[i]] for i in (0, 1, 2)}
            for r in recs
        ],
        "static_first_32B_per_arg":  {arg_label[i]: sl
                                       for i, sl in static_first32.items()},
        "first_32B_matches_expected_n_key": {
            arg_label[i]: v for i, v in matches_expected.items()},
        "text_score_per_arg":        {arg_label[i]: text_score[i]
                                       for i in (0, 1, 2)},
        "assignment": {
            "key_arg":       arg_label.get(key_arg) if key_arg is not None else None,
            "plaintext_arg": arg_label.get(pt_arg)  if pt_arg  is not None else None,
            "output_arg":    arg_label.get(out_arg) if out_arg is not None else None,
        },
        "output_pre_eq_after":   not output_changed if output_changed is not None else None,
        "key_first_32B_hex":     (static_first32.get(key_arg)
                                  if key_arg is not None else None),
        "expected_n_key_hex":    expected_n_key_hex,
    }


def main():
    out = run()
    log = Logger()
    # Load expected n_key
    try:
        with open(os.path.join(ROOT, "data", "keys.json")) as f:
            keys = json.load(f)
        expected_n_key = keys["hsw"]["n_key"]
    except Exception:
        expected_n_key = None
    out["analysis"] = analyze(out, expected_n_key)

    save = os.path.join(THIS, "instrument_encrypt_entry.last.json")
    # Truncate records for save (keep only first 16 records for size)
    save_out = dict(out)
    save_out["records"] = out["records"][:16]
    with open(save, "w") as f:
        json.dump(save_out, f, indent=2)
    print(json.dumps(out["analysis"], indent=2))
    print(f"saved -> {save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
