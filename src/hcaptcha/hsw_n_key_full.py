"""Full 32-byte N-key runtime extractor for hCaptcha era (d) HSW builds.

Background
==========
The legacy module :mod:`hcaptcha.hsw_n_key_runtime` patched a single
helper (call_340 in the build it was written against) to capture only
the 12 LCG-derived bytes of the N-key (bytes 2..13 of a 32-byte
buffer). On current builds the 32-byte key is fully assembled by a
mix of THREE per-byte/per-i32/per-i64 store helpers — discovered by
their structural fingerprint (the `320 / 328 / 1032` scattered-mapping
arithmetic pattern). This module extends the trace to all three so
ALL 32 bytes are captured (when statically possible).

The 32-byte N-key layout in vc
------------------------------
On every era-(d) build we have inspected, vc materialises the N-key
into a logical 32-byte buffer indexed by ``(base_param, byte_offset)``
where:

  * ``base_param`` is one of the helper-call's i32 args (the FIRST i32
    arg in every helper we have seen, but discovered structurally).
  * The byte offset is the SECOND i32 arg in the call.

Three helpers contribute writes:

  - **byte-store** (3 args ``(i32, i32, i32) -> ()``): writes 1 byte,
    used by the 12-iteration LCG block immediately preceding the
    "post-LCG" section. Provides 12 bytes (offsets 0..11 in the
    LCG-buffer's coordinates; these end up at n_key[2..13] when the
    LCG buffer is +2 from the n_key base).

  - **i64-store** (4 args ``(i32, i32, i64, i32) -> ()``): writes
    8 bytes (XORed with a rodata mask). About 24 of these calls
    appear immediately AFTER the LCG block in vc; collectively they
    populate the i64-aligned slots of a larger encryption state, and
    one of them lands exactly on the n_key buffer.

  - **i32-store** (3 args ``(i32, i32, i32) -> ()`` but with `i32.store16`
    / `i32.store` opcodes — distinguished from the byte-store by
    opcode profile rather than signature alone).

For each helper we patch the prologue to push a record
``(base_u32, offset_u32, value_u64-or-u32)`` to a unique ring buffer
in linear memory. After ``window.hsw(jwt)`` returns, we re-derive every
byte each helper wrote (un-XORing the i64/i32 store helpers' rodata
mask, which we read statically from the WASM data section), then
pivot every (virtual_address -> byte) pair to find the 32-byte run
that covers offsets ``0..31`` contiguously and pick the buffer that
overlaps the LCG-write region.

Two passes
----------
We run ``window.hsw(jwt)`` with two different ``t`` (timestamp) values
to determine whether the recovered 32 bytes are **repeatable** (i.e.
build-static — the same across calls) or **per-call** (e.g. mixed
with a runtime entropy source). The result type's ``repeatable`` flag
reports this verdict.
"""
from __future__ import annotations

import base64
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests

from .log import Logger
from .tools.js_runtime import JsRuntime
from .tools.wasm_disasm import WasmModule, find_data_segment_at
from .tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from .hsw_n_key_runtime import _HOOK_JS
from . import version as _v


# ---------------------------------------------------------------------------
# Scratch layout: three ring buffers + temp slots, in distinct regions.
# ---------------------------------------------------------------------------
SCRATCH_COUNTER_BYTE = 60_000          # ring 1: byte-store helper
SCRATCH_BUF_BYTE     = 60_004
REC_BYTE             = 12              # (base, off, byte_val) — 3 x u32
MAX_BYTE             = 4000

SCRATCH_COUNTER_I64  = 110_000         # ring 2: i64-store helper
SCRATCH_BUF_I64      = 110_004
REC_I64              = 24              # (base, off, val_i64) — 2 x u32 + 1 x i64
MAX_I64              = 2000

SCRATCH_COUNTER_I32  = 170_000         # ring 3: i32-store helper
SCRATCH_BUF_I32      = 170_004
REC_I32              = 16              # (base, off, val_i32, pad) — 4 x u32
MAX_I32              = 2000

# Temp scratch slots (per-helper, no shared state)
TMP_C_BYTE = 200_000
TMP_A_BYTE = 200_004
TMP_C_I64  = 200_016
TMP_A_I64  = 200_020
TMP_C_I32  = 200_032
TMP_A_I32  = 200_036

N_KEY_LCG_MULT = 6364136223846793005     # 0x5851F42D4C957F2D


# ---------------------------------------------------------------------------
# Helper discovery — find vc, LCG block, and the three store-helper indices
# ---------------------------------------------------------------------------
def _find_vc(mod: WasmModule) -> int:
    for ex in mod.exports:
        if ex["kind"] == "func" and ex["name"] == "vc":
            return ex["idx"]
    raise RuntimeError("vc dispatcher export not found")


def _lcg_positions(mod: WasmModule, vc_idx: int) -> list[int]:
    instrs = mod.decode_function(vc_idx)
    return [
        i for i, (n, ops, _, _) in enumerate(instrs)
        if n == "i64.const" and ops
        and (ops[0] & 0xFFFFFFFFFFFFFFFF) == N_KEY_LCG_MULT
    ]


def _find_byte_store(mod: WasmModule, vc_idx: int) -> int:
    """The (i32,i32,i32)->() callee invoked between successive LCG iterations.
    Identified by counting: it appears once per LCG iteration."""
    instrs = mod.decode_function(vc_idx)
    lcgs = _lcg_positions(mod, vc_idx)
    callees = Counter()
    for pos in lcgs:
        for j in range(pos, min(pos + 30, len(instrs))):
            nj, opsj, _, _ = instrs[j]
            if nj == "call" and opsj:
                f = next((f for f in mod.functions
                          if f["func_idx"] == opsj[0]), None)
                if f and mod.types[f["type_idx"]] == (
                        ["i32", "i32", "i32"], []):
                    callees[opsj[0]] += 1
                    break
    if not callees:
        raise RuntimeError("byte-store helper not found near LCG")
    best, n_hits = callees.most_common(1)[0]
    if n_hits < 3:
        raise RuntimeError(f"byte-store {best} only hit {n_hits} times")
    return best


def _find_post_lcg_stores(mod: WasmModule, vc_idx: int
                          ) -> tuple[Optional[int], Optional[int]]:
    """After the last LCG iteration, vc emits a sequence of calls that
    fall into 2 categories: (i32,i32,i64,i32) sigs (i64-store) and
    (i32,i32,i32) sigs (i32-store / byte-store). Return the most-
    common (i32,i32,i64,i32) callee and the most-common
    (i32,i32,i32) callee that is NOT the LCG byte-store.

    Returns (i64_store_idx, i32_store_idx) — either may be None.
    """
    instrs = mod.decode_function(vc_idx)
    lcgs = _lcg_positions(mod, vc_idx)
    if not lcgs:
        return (None, None)
    start = lcgs[-1]
    byte_store = _find_byte_store(mod, vc_idx)

    callees_i64 = Counter()
    callees_i32 = Counter()
    for j in range(start, min(start + 800, len(instrs))):
        n, ops, _, _ = instrs[j]
        if n != "call" or not ops:
            continue
        cf = next((f for f in mod.functions if f["func_idx"] == ops[0]), None)
        if cf is None:
            continue
        sig = mod.types[cf["type_idx"]]
        if sig == (["i32", "i32", "i64", "i32"], []):
            callees_i64[ops[0]] += 1
        elif sig == (["i32", "i32", "i32"], []) and ops[0] != byte_store:
            callees_i32[ops[0]] += 1

    i64_store = callees_i64.most_common(1)[0][0] if callees_i64 else None
    i32_store = callees_i32.most_common(1)[0][0] if callees_i32 else None
    return (i64_store, i32_store)


def _detect_mask_vaddr(mod: WasmModule, helper_idx: int) -> Optional[int]:
    """Find the rodata base used for the XOR mask in helper `helper_idx`.
    Pattern: `i32.const 96; i32.rem_u; i32.const VADDR; i32.add; i64.load`.
    Returns VADDR or None."""
    instrs = mod.decode_function(helper_idx) or []
    for i in range(len(instrs) - 5):
        names = [instrs[i + k][0] for k in range(6)]
        if (names[0] == "i32.const" and instrs[i][1] == [96]
                and names[1] == "i32.rem_u"
                and names[2] == "i32.const"
                and names[3] == "i32.add"
                and names[4] in ("i64.load", "i32.load")):
            return instrs[i + 2][1][0] & 0xFFFFFFFF
    return None


# ---------------------------------------------------------------------------
# Prologue builders
# ---------------------------------------------------------------------------
def _build_prologue_3args(counter_addr: int, buf_addr: int, max_recs: int,
                          rec_size: int, tmp_c: int, tmp_a: int,
                          base_local: int, off_local: int, val_local: int,
                          val_is_i64: bool) -> bytes:
    """Generic prologue for a 3-or-4-arg helper that stores
    (base, off, val) where val is i32 or i64. Doesn't use any wasm
    locals — only scratch memory slots."""
    out = bytearray()
    # tmp_c = *counter
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    # if counter < max_recs:
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(max_recs)
    out += b"\x49"                                       # i32.lt_u
    out += b"\x04\x40"                                   # if (empty)
    # tmp_a = buf + counter * rec_size
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(rec_size)
    out += b"\x6c"                                       # i32.mul
    out += b"\x41" + encode_sleb(buf_addr)
    out += b"\x6a"                                       # i32.add
    out += b"\x36\x02\x00"
    # *(tmp_a+0) = base (i32)
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x20" + encode_uleb(base_local)
    out += b"\x36\x02\x00"
    # *(tmp_a+4) = off (i32)
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x20" + encode_uleb(off_local)
    out += b"\x36\x02\x04"
    # *(tmp_a+8) = val
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x20" + encode_uleb(val_local)
    if val_is_i64:
        out += b"\x37\x03\x08"                           # i64.store align=3 off=8
    else:
        out += b"\x36\x02\x08"                           # i32.store align=2 off=8
    # counter++
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    out += b"\x0b"                                       # end (if)
    return bytes(out)


# ---------------------------------------------------------------------------
# Top-level trace
# ---------------------------------------------------------------------------
@dataclass
class FullTraceResult:
    full_hex:        str
    bytes_captured:  int
    base_ptr_hex:    str
    repeatable:      str
    trace_records:   list = field(default_factory=list)


def _instantiate(rt: JsRuntime, patched: bytes, hsw_src: str,
                 log: Optional[Logger] = None):
    rt.eval(f"globalThis.__patched_wasm_b64 = "
            f"'{base64.b64encode(patched).decode()}';")
    rt.eval(_HOOK_JS)
    rt.eval(hsw_src, suppress=True)
    rt.eval(
        """(async () => {
            try { await window.hsw(1, new Uint8Array(0)); } catch (e) {
              globalThis.__hsw_warmup_err = String(e) + ' | stack=' +
                  (e && e.stack ? e.stack : 'none');
            }
        })();""",
        suppress=True,
    )
    for _ in range(80):
        time.sleep(0.1)
        if rt.eval("globalThis.__hsw_exports") is not None:
            break
    if log:
        err = rt.eval("globalThis.__hsw_warmup_err")
        ok = rt.eval("globalThis.__hsw_exports") is not None
        log.info(f"n-key trace: instantiate ok={ok} err={(err or '')[:300]}",
                 start=0, end=0)


def _make_jwt(timestamp_offset: int = 0) -> str:
    now = int(time.time()) + timestamp_offset
    def b64u(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    return (
        b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "."
        + b64u(json.dumps(
            {"s": f"{timestamp_offset:08x}",
             "d": 1, "t": now, "exp": now + 600}).encode())
        + ".fake"
    )


def _run_and_capture(rt: JsRuntime, jwt: str, log: Logger,
                     have_i64: bool, have_i32: bool) -> dict:
    rt.eval(
        f"""
        globalThis.__nkey_done = 0;
        globalThis.__nkey_err = '';
        (async () => {{
            const e = globalThis.__hsw_exports;
            e.__poke32({SCRATCH_COUNTER_BYTE}, 0);
            e.__poke32({SCRATCH_COUNTER_I64}, 0);
            e.__poke32({SCRATCH_COUNTER_I32}, 0);
            try {{
                const r = await window.hsw('{jwt}');
                globalThis.__nkey_result = String(r);
            }} catch (ex) {{
                globalThis.__nkey_err = String(ex);
            }}
            globalThis.__nkey_done = 1;
        }})();
        """,
        suppress=True,
    )
    for _ in range(400):
        if rt.eval("globalThis.__nkey_done"):
            break
        time.sleep(0.25)
    err = rt.eval("globalThis.__nkey_err") or ""
    if err:
        log.info(f"n-key trace: hsw() raised {err[:200]}", start=0, end=0)

    n_byte = (rt.eval(f"globalThis.__hsw_exports.__peek32({SCRATCH_COUNTER_BYTE})") or 0) & 0xFFFFFFFF
    n_i64  = (rt.eval(f"globalThis.__hsw_exports.__peek32({SCRATCH_COUNTER_I64})") or 0) & 0xFFFFFFFF if have_i64 else 0
    n_i32  = (rt.eval(f"globalThis.__hsw_exports.__peek32({SCRATCH_COUNTER_I32})") or 0) & 0xFFFFFFFF if have_i32 else 0

    # Batched: read entire ring buffer as one big array per ring.
    def _read_ring(addr_start: int, rec_size: int, count: int) -> list:
        if count <= 0:
            return []
        n_u32 = (rec_size * count) // 4
        arr = rt.eval(
            f"""(function() {{
                const mem = new Uint8Array(globalThis.__hsw_memory.buffer,
                                            {addr_start}, {rec_size * count});
                const out = new Uint32Array(mem.buffer, mem.byteOffset,
                                            {n_u32});
                return Array.from(out);
            }})()"""
        )
        return arr or []

    byte_words = _read_ring(SCRATCH_BUF_BYTE, REC_BYTE, min(n_byte, MAX_BYTE))
    byte_rec = []
    for i in range(0, len(byte_words), 3):
        bp, off, bv = byte_words[i], byte_words[i + 1], byte_words[i + 2]
        if off & 0x80000000:
            off -= 0x100000000
        byte_rec.append((bp & 0xFFFFFFFF, off, bv & 0xFF))

    i64_words = _read_ring(SCRATCH_BUF_I64, REC_I64, min(n_i64, MAX_I64))
    i64_rec = []
    for i in range(0, len(i64_words), 6):
        bp, off, lo, hi = (i64_words[i], i64_words[i + 1],
                           i64_words[i + 2], i64_words[i + 3])
        if off & 0x80000000:
            off -= 0x100000000
        i64_rec.append((bp & 0xFFFFFFFF, off,
                        ((hi & 0xFFFFFFFF) << 32) | (lo & 0xFFFFFFFF)))

    i32_words = _read_ring(SCRATCH_BUF_I32, REC_I32, min(n_i32, MAX_I32))
    i32_rec = []
    for i in range(0, len(i32_words), 4):
        bp, off, val = i32_words[i], i32_words[i + 1], i32_words[i + 2]
        if off & 0x80000000:
            off -= 0x100000000
        i32_rec.append((bp & 0xFFFFFFFF, off, val & 0xFFFFFFFF))

    return {
        "byte": byte_rec,
        "i64":  i64_rec,
        "i32":  i32_rec,
    }


def _build_n_key_from_captures(captures: dict,
                               mask_i64: Optional[bytes],
                               mask_i32: Optional[bytes],
                               log: Logger,
                               ) -> tuple[Optional[bytes], Optional[int], dict]:
    """From the captures, find the 32-byte n_key buffer.

    Strategy
    --------
    1. The byte-store records define one or more "base_ptr" values
       where bytes have been written at offsets 0..N (typically 0..11
       for the 12-iter LCG). Pick the base_ptr with the longest
       contiguous run starting at 0 — that's the LCG buffer's base.
    2. The n_key's logical base is presumed equal to that LCG base
       (since the LCG writes are the only direct byte-level writes
       into the n_key during vc on era-(d) builds).
    3. Walk i64 / i32 records: for each one, compute virtual
       (base_ptr + off) → 8 (or 4) bytes after XORing with the rodata
       mask. If those bytes land in the range
       [LCG_base, LCG_base + 32), record them at the right offsets.

    Returns (n_key_bytes_or_None, lcg_base, debug_info).
    """
    byte_rec = captures["byte"]
    i64_rec  = captures["i64"]
    i32_rec  = captures["i32"]

    per_base = defaultdict(dict)
    for bp, off, bv in byte_rec:
        if 0 <= off < 32:
            per_base[bp][off] = bv

    if not per_base:
        return (None, None, {"error": "no byte-store records"})

    # Pick base with most contiguous coverage starting at 0
    def _contig_run(m: dict) -> int:
        n = 0
        while n in m:
            n += 1
        return n
    candidates = sorted(per_base.items(),
                        key=lambda x: -_contig_run(x[1]))
    lcg_base, byte_map = candidates[0]
    log.info(f"n-key trace: LCG base 0x{lcg_base:x} "
             f"bytes={sorted(byte_map.keys())}", start=0, end=0)

    # Try two layouts for n_key:
    #   (a) n_key starts at lcg_base, LCG bytes are n_key[0..11]
    #   (b) n_key starts at lcg_base - 2, LCG bytes are n_key[2..13]
    #   (c) n_key starts at lcg_base + K for some other K
    # We rank each layout by how many of the i64/i32 writes land
    # inside [n_key_base, n_key_base + 32) and pick the layout that
    # maximises that count.

    def _try_layout(n_key_base: int) -> tuple[bytearray, int]:
        kb = bytearray(32)
        filled = bytearray(32)
        # 1. Fill i64 writes
        if mask_i64 is not None:
            for bp, off, val in i64_rec:
                virt = bp + off
                mask = int.from_bytes(
                    mask_i64[virt % 96 : virt % 96 + 8], "little")
                xored = (val ^ mask) & 0xFFFFFFFFFFFFFFFF
                for k in range(8):
                    addr = virt + k
                    idx = addr - n_key_base
                    if 0 <= idx < 32:
                        kb[idx] = (xored >> (8 * k)) & 0xFF
                        filled[idx] = 1
        # 2. Fill i32 writes
        if mask_i32 is not None:
            for bp, off, val in i32_rec:
                virt = bp + off
                mask = int.from_bytes(
                    mask_i32[virt % 96 : virt % 96 + 4], "little")
                xored = (val ^ mask) & 0xFFFFFFFF
                for k in range(4):
                    addr = virt + k
                    idx = addr - n_key_base
                    if 0 <= idx < 32:
                        kb[idx] = (xored >> (8 * k)) & 0xFF
                        filled[idx] = 1
        # 3. Fill LCG bytes LAST so they win (they are the actual key bytes)
        for off, b in byte_map.items():
            virt = lcg_base + off
            idx = virt - n_key_base
            if 0 <= idx < 32:
                kb[idx] = b
                filled[idx] = 1
        return kb, sum(filled)

    best_layout = None
    best_filled = 0
    # Exhaustive search: any delta in -32..32 (the LCG bytes must fit
    # inside the 32-byte n_key window).
    for delta in range(-32, 33):
        # LCG bytes occupy [delta, delta + max_step]; must all fit in [0, 32)
        max_step = max(byte_map.keys()) if byte_map else 0
        if not (0 <= 0 - delta and (max_step - delta) < 32):
            continue
        # Equivalent: 0 <= -delta and max_step - delta < 32
        # i.e. delta <= 0 AND delta > max_step - 32
        n_key_base = lcg_base + delta
        kb, filled = _try_layout(n_key_base)
        if filled > best_filled:
            best_filled = filled
            best_layout = (n_key_base, kb)

    if best_layout is None:
        return (None, lcg_base,
                {"byte_map": byte_map, "filled": 0,
                 "i64_count": len(i64_rec), "i32_count": len(i32_rec)})

    n_key_base, kb = best_layout
    log.info(f"n-key trace: best layout n_key_base=0x{n_key_base:x} "
             f"filled={best_filled}/32", start=0, end=0)

    # Diagnostic: count which i64 writes landed in [n_key_base, +32)
    matching_i64 = []
    if mask_i64 is not None:
        for bp, off, val in i64_rec:
            virt = bp + off
            if n_key_base <= virt < n_key_base + 32:
                mask_off = virt % 96
                mask = int.from_bytes(
                    mask_i64[mask_off:mask_off + 8], "little")
                xored = (val ^ mask) & 0xFFFFFFFFFFFFFFFF
                matching_i64.append((bp, off, virt, val, xored))

    matching_i32 = []
    if mask_i32 is not None:
        for bp, off, val in i32_rec:
            virt = bp + off
            if n_key_base <= virt < n_key_base + 32:
                mask_off = virt % 96
                mask = int.from_bytes(
                    mask_i32[mask_off:mask_off + 4], "little")
                xored = (val ^ mask) & 0xFFFFFFFF
                matching_i32.append((bp, off, virt, val, xored))

    return bytes(kb), n_key_base, {
        "byte_map": byte_map,
        "filled": best_filled,
        "i64_count": len(i64_rec),
        "i32_count": len(i32_rec),
        "matching_i64": matching_i64,
        "matching_i32": matching_i32,
        "lcg_base": lcg_base,
        "n_key_base": n_key_base,
    }


def trace_full_n_key(version: Optional[str] = None,
                     log: Optional[Logger] = None,
                     two_pass: bool = True,
                     instrument_i32: bool = True,
                     instrument_i64: bool = True) -> dict:
    """End-to-end: instrument the three store helpers in vc, run
    ``window.hsw(jwt)`` once (or twice for repeatability check), and
    return the full 32-byte N-key (or as much of it as is statically
    determinable).

    Returns a dict::

        {
          "full_hex":        "<64-char hex>" or "<partial>",
          "bytes_captured":  int,
          "base_ptr_hex":    "0xNNNNNN",
          "repeatable":      "SAME" or "DIFFERENT" or "UNKNOWN",
          "trace_records":   [ ... debug ... ],
        }
    """
    log = log or Logger()
    version = version or _v.latest_version()

    # 1. Parse WASM
    from .hsw_bridge import HSWAnalyzer
    info = HSWAnalyzer(version, log=log).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)

    vc_idx = _find_vc(mod)
    byte_store = _find_byte_store(mod, vc_idx)
    i64_store, i32_store = _find_post_lcg_stores(mod, vc_idx)
    log.info(f"n-key full trace: vc={vc_idx} byte_store={byte_store} "
             f"i64_store={i64_store} i32_store={i32_store}", start=0, end=0)

    # Read XOR-mask base vaddrs and the rodata bytes
    mask_i64_vaddr = _detect_mask_vaddr(mod, i64_store) if i64_store else None
    mask_i32_vaddr = _detect_mask_vaddr(mod, i32_store) if i32_store else None
    mask_i64 = find_data_segment_at(mod, mask_i64_vaddr, 256) if mask_i64_vaddr else None
    mask_i32 = find_data_segment_at(mod, mask_i32_vaddr, 256) if mask_i32_vaddr else None
    log.info(f"n-key full trace: mask_i64@{mask_i64_vaddr}({len(mask_i64) if mask_i64 else 'none'}B) "
             f"mask_i32@{mask_i32_vaddr}({len(mask_i32) if mask_i32 else 'none'}B)",
             start=0, end=0)

    # 2. Build patched module
    writer = ModuleWriter(mod)
    # byte-store params: (base i32, val i32, step i32)  — per the helper bodies
    # we have inspected; the FIRST arg (local 0) is the base, SECOND
    # (local 1) is the byte value, THIRD (local 2) is the step.
    writer.code.splice_code(
        byte_store, 0, n_replace=0,
        new_bytes=_build_prologue_3args(
            SCRATCH_COUNTER_BYTE, SCRATCH_BUF_BYTE,
            MAX_BYTE, REC_BYTE,
            TMP_C_BYTE, TMP_A_BYTE,
            base_local=0, off_local=2, val_local=1,
            val_is_i64=False))
    if i64_store is not None and instrument_i64:
        # i64-store params: (base i32, off i32, val i64, ???_i32)
        writer.code.splice_code(
            i64_store, 0, n_replace=0,
            new_bytes=_build_prologue_3args(
                SCRATCH_COUNTER_I64, SCRATCH_BUF_I64,
                MAX_I64, REC_I64,
                TMP_C_I64, TMP_A_I64,
                base_local=0, off_local=1, val_local=2,
                val_is_i64=True))
    else:
        i64_store = None
    if i32_store is not None and instrument_i32:
        # i32-store params: (base i32, off i32, val i32)
        writer.code.splice_code(
            i32_store, 0, n_replace=0,
            new_bytes=_build_prologue_3args(
                SCRATCH_COUNTER_I32, SCRATCH_BUF_I32,
                MAX_I32, REC_I32,
                TMP_C_I32, TMP_A_I32,
                base_local=0, off_local=1, val_local=2,
                val_is_i64=False))
    else:
        i32_store = None

    # Peek / poke exports
    t_i32_to_i32 = next((i for i, (p, r) in enumerate(mod.types)
                         if p == ["i32"] and r == ["i32"]), None)
    if t_i32_to_i32 is None:
        t_i32_to_i32 = writer.add_type(["i32"], ["i32"])
    t_i32i32_to_void = next((i for i, (p, r) in enumerate(mod.types)
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
    log.info(f"n-key full trace: patched wasm {len(patched)}B "
             f"(+{len(patched) - len(wasm)}B)", start=0, end=0)

    # 3. Sandbox run
    rt = JsRuntime()
    try:
        r = requests.get(_v.asset_url(version, "hsw.js"))
        r.encoding = "utf-8"
        hsw_src = r.text
        _instantiate(rt, patched, hsw_src, log=log)

        jwt1 = _make_jwt(0)
        cap1 = _run_and_capture(rt, jwt1, log,
                                have_i64=i64_store is not None,
                                have_i32=i32_store is not None)
        log.info(f"n-key full trace: pass1 captures "
                 f"byte={len(cap1['byte'])} i64={len(cap1['i64'])} "
                 f"i32={len(cap1['i32'])}", start=0, end=0)
        n_key1, base1, debug1 = _build_n_key_from_captures(
            cap1, mask_i64, mask_i32, log)

        repeatable = "UNKNOWN"
        if two_pass:
            jwt2 = _make_jwt(7200)         # +2h different timestamp
            cap2 = _run_and_capture(rt, jwt2, log,
                                    have_i64=i64_store is not None,
                                    have_i32=i32_store is not None)
            log.info(f"n-key full trace: pass2 captures "
                     f"byte={len(cap2['byte'])} i64={len(cap2['i64'])} "
                     f"i32={len(cap2['i32'])}", start=0, end=0)
            n_key2, base2, debug2 = _build_n_key_from_captures(
                cap2, mask_i64, mask_i32, log)
            if n_key1 is not None and n_key2 is not None:
                repeatable = "SAME" if n_key1 == n_key2 else "DIFFERENT"
                log.info(f"n-key full trace: pass1={n_key1.hex()}", start=0, end=0)
                log.info(f"n-key full trace: pass2={n_key2.hex()}", start=0, end=0)
                if repeatable == "DIFFERENT":
                    # Identify static bytes (same across both passes)
                    static_mask = bytes(
                        a if a == b else 0
                        for a, b in zip(n_key1, n_key2)
                    )
                    log.info(f"n-key full trace: static-bytes-mask={static_mask.hex()}",
                             start=0, end=0)
        else:
            n_key2 = None
            debug2 = {}

        result_key = n_key1 or b""
        return {
            "full_hex":       result_key.hex(),
            "bytes_captured": debug1.get("filled", 0),
            "base_ptr_hex":   f"0x{base1:x}" if base1 else "",
            "repeatable":     repeatable,
            "trace_records":  [
                {"location": f"vc instr after LCG @ helper={byte_store}",
                 "bytes_provided": "0..11 (LCG steps)",
                 "construction": "LCG-derived i32 byte values"},
                {"location": f"vc post-LCG @ helper={i64_store}",
                 "bytes_provided": "varies",
                 "construction": "i64.const literal XOR rodata mask @"
                                 f"vaddr={mask_i64_vaddr}, "
                                 "split into 8 LE bytes"},
                {"location": f"vc post-LCG @ helper={i32_store}",
                 "bytes_provided": "varies",
                 "construction": "i32.const literal XOR rodata mask @"
                                 f"vaddr={mask_i32_vaddr}, "
                                 "split into 4 LE bytes"},
            ],
            "_pass1_debug": debug1,
            "_pass2_debug": debug2 if two_pass else None,
            "_n_key_pass2": n_key2.hex() if n_key2 else "",
        }
    finally:
        try:
            rt.close()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    v_arg = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        r = trace_full_n_key(v_arg)
        print(json.dumps(r, indent=2, default=str))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nerror: {e}")
        sys.exit(1)
