"""instrument_encrypt_entry.py — dump (arg0, arg1, arg2, *arg*) at the
n-token AES encrypt entry function, BOTH at prologue AND at epilogue.

We dynamically discover ENTRY_FN on the current build:
  * signature (i32,i32,i32) -> (i32)
  * calls a fixslice32 KS function 3+ times
  * reachable from one of {ec, pc, kc}

We splice TWO instrumentation hooks into ENTRY_FN:

  PROLOGUE (at code_off = 0): records a fixed-size PRE record
      struct rec_pre {
          u32 counter;          // index in ring
          u32 arg0;
          u32 arg1;
          u32 arg2;
          u8  buf0[32];         // bytes at *arg0 (KEY candidate)
          u8  buf1[3072];       // bytes at *arg1 (PT/CT/AAD candidate, FULL)
          u8  buf2[256];        // bytes at *arg2 (PT/CT/AAD candidate)
      };

  EPILOGUE (spliced just before the function's final 0x0b 'end'):
  records a fixed-size POST record with the same layout, re-reading
  the buffers from the SAVED arg pointers. This lets us see whether
  fn 226 mutates *arg1 in-place (e.g. plaintext -> ciphertext) or
  leaves it untouched.

The epilogue is stack-neutral so it does not disturb the function's
return value (left on the stack right before the final 'end').

NOTE: The epilogue only fires on the function's normal return path.
If fn 226 has early `return` opcodes elsewhere in its body, those
calls will produce a PRE record but no POST record. The pre and post
counters are reported separately so the caller can detect this.
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
REC_BUF0   = 512                             # bytes at *arg0 (cipher ctx / round keys)
REC_BUF1   = 4096                            # bytes at *arg1 (FULL hCaptcha buf)
REC_BUF2   = 256                             # bytes at *arg2 (or length, if scalar)
REC_SIZE   = REC_HEADER + REC_BUF0 + REC_BUF1 + REC_BUF2   # 3376
MAX_RECS   = 16                              # 16 calls per record set

# Memory layout in wasm scratch (above usual heap usage)
# We pick a high address that's well past the typical heap but inside
# the initial memory.size (hCaptcha wasm has ~1MB initial pages).
PRE_COUNTER_ADDR  = 60_000                                  # u32 PRE counter
PRE_BUF_ADDR      = 60_004                                  # MAX_RECS * REC_SIZE
POST_COUNTER_ADDR = PRE_BUF_ADDR + MAX_RECS * REC_SIZE + 16  # u32 POST counter
POST_BUF_ADDR     = POST_COUNTER_ADDR + 4                    # MAX_RECS * REC_SIZE
GATE_ADDR         = POST_BUF_ADDR + MAX_RECS * REC_SIZE + 16

# Tmp slots for codegen (scalar scratch)
TMP_C        = GATE_ADDR + 16                # current PRE counter
TMP_A        = TMP_C + 4                     # current PRE record base addr
TMP_SAVED_A1 = TMP_A + 4                     # saved arg1 ptr  (read back in post)
TMP_SAVED_A2 = TMP_SAVED_A1 + 4              # saved arg2      (read back in post)
TMP_SAVED_A0 = TMP_SAVED_A2 + 4              # saved arg0 ptr
TMP_PC       = TMP_SAVED_A0 + 4              # current POST counter
TMP_PA       = TMP_PC + 4                    # current POST record base addr

# Total scratch end (sanity)
SCRATCH_END  = TMP_PA + 4

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

    chosen = fingerprint_cands[0][0]
    log.info(f"  -> chose ENTRY_FN = {chosen}", start=0, end=0)
    return chosen


# --------------------------------------------------------------------
# Helpers for emitting "copy bytes from src ptr -> dst region" inline.
# Stack-neutral (everything is store-style: addr/value pushes followed
# by store pops in equal measure).
# --------------------------------------------------------------------
def _emit_copy_inline(src_addr_push: bytes, dst_base_push: bytes,
                      dst_off_base: int, n_bytes: int) -> bytes:
    """Emit wasm bytes that copy `n_bytes` from address (src_addr_push)
    to (dst_base_push + dst_off_base..). Each push is a sequence of
    wasm bytes that leaves a single i32 (address) on the stack.

    Uses i64 block copies for 8B chunks, byte tail for remainder.
    Stack-neutral overall.
    """
    out = bytearray()
    n_q = n_bytes // 8
    n_r = n_bytes - n_q * 8
    for q in range(n_q):
        # dst addr
        out += dst_base_push
        # i64.load (unaligned) from src + q*8
        out += src_addr_push
        out += b"\x29\x00" + encode_uleb(q * 8)
        # i64.store align=0 off=(dst_off_base + q*8)
        out += b"\x37\x00" + encode_uleb(dst_off_base + q * 8)
    for r in range(n_r):
        out += dst_base_push
        out += src_addr_push
        out += b"\x2d\x00" + encode_uleb(n_q * 8 + r)        # i32.load8_u
        out += b"\x3a\x00" + encode_uleb(dst_off_base + n_q * 8 + r)  # i32.store8
    return bytes(out)


# --------------------------------------------------------------------
# PRE prologue codegen
# --------------------------------------------------------------------
def _build_prologue() -> bytes:
    """Build PRE prologue.

    Records (counter, arg0, arg1, arg2, buf0[32], buf1[3072], buf2[256])
    into the PRE ring, and ALSO saves arg0/arg1/arg2 to TMP slots so
    the EPILOGUE can re-read the same pointers.
    """
    out = bytearray()

    # Save arg0/arg1/arg2 to TMP slots unconditionally (so even if the
    # gate is closed we don't read stale values from a prior call).
    def _save_local(addr: int, local_idx: int) -> bytes:
        return (b"\x41" + encode_sleb(addr) +
                b"\x20" + encode_uleb(local_idx) +
                b"\x36\x02\x00")   # i32.store

    out += _save_local(TMP_SAVED_A0, 0)
    out += _save_local(TMP_SAVED_A1, 1)
    out += _save_local(TMP_SAVED_A2, 2)

    # if (*GATE) {
    out += b"\x41" + encode_sleb(GATE_ADDR)
    out += b"\x28\x02\x00"          # i32.load
    out += b"\x04\x40"              # if (empty)

    # tmp_c = *PRE_COUNTER
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x41" + encode_sleb(PRE_COUNTER_ADDR)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"

    # if (tmp_c < MAX_RECS) {
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_RECS)
    out += b"\x49"                  # i32.lt_u
    out += b"\x04\x40"              # if (empty)

    # tmp_a = PRE_BUF + tmp_c * REC_SIZE
    out += b"\x41" + encode_sleb(TMP_A)
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(REC_SIZE)
    out += b"\x6c"                  # i32.mul
    out += b"\x41" + encode_sleb(PRE_BUF_ADDR)
    out += b"\x6a"                  # i32.add
    out += b"\x36\x02\x00"

    # Helpers to push tmp_a (record base addr)
    push_base = (b"\x41" + encode_sleb(TMP_A) + b"\x28\x02\x00")

    # *(tmp_a + 0)  = tmp_c
    out += push_base
    out += b"\x41" + encode_sleb(TMP_C) + b"\x28\x02\x00"
    out += b"\x36\x02" + encode_uleb(0)
    # *(tmp_a + 4)  = arg0
    out += push_base
    out += b"\x20" + encode_uleb(0)
    out += b"\x36\x02" + encode_uleb(4)
    # *(tmp_a + 8)  = arg1
    out += push_base
    out += b"\x20" + encode_uleb(1)
    out += b"\x36\x02" + encode_uleb(8)
    # *(tmp_a + 12) = arg2
    out += push_base
    out += b"\x20" + encode_uleb(2)
    out += b"\x36\x02" + encode_uleb(12)

    # ---- copy bytes from *argX to record buffer X --------------------
    def _push_local_ptr(local_idx: int) -> bytes:
        return b"\x20" + encode_uleb(local_idx)

    def _copy_local_buf(arg_local: int, dst_field_off: int,
                        n_bytes: int) -> bytes:
        # if (arg != 0) { copy }
        body = bytearray()
        body += b"\x20" + encode_uleb(arg_local)        # local.get arg
        body += b"\x04\x40"                              # if (empty)
        body += _emit_copy_inline(
            src_addr_push=_push_local_ptr(arg_local),
            dst_base_push=push_base,
            dst_off_base=dst_field_off,
            n_bytes=n_bytes,
        )
        body += b"\x0b"                                  # end if
        return bytes(body)

    # arg0 -> buf0 (32B)
    out += _copy_local_buf(0, REC_HEADER, REC_BUF0)
    # arg1 -> buf1 (3072B)
    out += _copy_local_buf(1, REC_HEADER + REC_BUF0, REC_BUF1)
    # arg2 -> buf2 (256B)  -- note arg2 may be a small int (a length),
    # in which case the deref pointer is small and reading would either
    # trap or grab random low memory. The function only runs if
    # arg2 != 0; small-int args (lengths) of 0 won't run. For non-zero
    # small-int args we'd just read invalid memory. That's fine; we
    # report the bytes regardless.
    out += _copy_local_buf(2, REC_HEADER + REC_BUF0 + REC_BUF1, REC_BUF2)

    # counter++
    out += b"\x41" + encode_sleb(PRE_COUNTER_ADDR)
    out += b"\x41" + encode_sleb(TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"

    out += b"\x0b"   # end if (counter < MAX)
    out += b"\x0b"   # end if (gate)
    return bytes(out)


# --------------------------------------------------------------------
# POST epilogue codegen
# --------------------------------------------------------------------
def _build_epilogue() -> bytes:
    """Build POST epilogue, to be spliced JUST BEFORE the function's
    final 0x0b 'end'. Stack-neutral (the return value sits underneath
    on the operand stack; we never touch it).

    Re-reads buffers from the SAVED arg pointers (TMP_SAVED_A0/1/2).
    """
    out = bytearray()

    # if (*GATE) {
    out += b"\x41" + encode_sleb(GATE_ADDR)
    out += b"\x28\x02\x00"
    out += b"\x04\x40"

    # tmp_pc = *POST_COUNTER
    out += b"\x41" + encode_sleb(TMP_PC)
    out += b"\x41" + encode_sleb(POST_COUNTER_ADDR)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"

    # if (tmp_pc < MAX_RECS) {
    out += b"\x41" + encode_sleb(TMP_PC)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_RECS)
    out += b"\x49"
    out += b"\x04\x40"

    # tmp_pa = POST_BUF + tmp_pc * REC_SIZE
    out += b"\x41" + encode_sleb(TMP_PA)
    out += b"\x41" + encode_sleb(TMP_PC)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(REC_SIZE)
    out += b"\x6c"
    out += b"\x41" + encode_sleb(POST_BUF_ADDR)
    out += b"\x6a"
    out += b"\x36\x02\x00"

    push_base = (b"\x41" + encode_sleb(TMP_PA) + b"\x28\x02\x00")

    # *(tmp_pa + 0)  = tmp_pc
    out += push_base
    out += b"\x41" + encode_sleb(TMP_PC) + b"\x28\x02\x00"
    out += b"\x36\x02" + encode_uleb(0)
    # *(tmp_pa + 4)  = saved arg0
    out += push_base
    out += b"\x41" + encode_sleb(TMP_SAVED_A0) + b"\x28\x02\x00"
    out += b"\x36\x02" + encode_uleb(4)
    # *(tmp_pa + 8)  = saved arg1
    out += push_base
    out += b"\x41" + encode_sleb(TMP_SAVED_A1) + b"\x28\x02\x00"
    out += b"\x36\x02" + encode_uleb(8)
    # *(tmp_pa + 12) = saved arg2
    out += push_base
    out += b"\x41" + encode_sleb(TMP_SAVED_A2) + b"\x28\x02\x00"
    out += b"\x36\x02" + encode_uleb(12)

    # ---- copy bytes from *saved_arg{0,1,2} ---------------------------
    def _push_saved(addr: int) -> bytes:
        return b"\x41" + encode_sleb(addr) + b"\x28\x02\x00"

    def _copy_saved_buf(saved_addr: int, dst_field_off: int,
                        n_bytes: int) -> bytes:
        body = bytearray()
        body += _push_saved(saved_addr)                   # push saved val
        body += b"\x04\x40"                                # if (val != 0)
        body += _emit_copy_inline(
            src_addr_push=_push_saved(saved_addr),
            dst_base_push=push_base,
            dst_off_base=dst_field_off,
            n_bytes=n_bytes,
        )
        body += b"\x0b"
        return bytes(body)

    out += _copy_saved_buf(TMP_SAVED_A0, REC_HEADER, REC_BUF0)
    out += _copy_saved_buf(TMP_SAVED_A1, REC_HEADER + REC_BUF0, REC_BUF1)
    out += _copy_saved_buf(TMP_SAVED_A2,
                           REC_HEADER + REC_BUF0 + REC_BUF1, REC_BUF2)

    # counter++
    out += b"\x41" + encode_sleb(POST_COUNTER_ADDR)
    out += b"\x41" + encode_sleb(TMP_PC)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"

    out += b"\x0b"   # end if (tmp_pc < MAX)
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

    # Find code-relative offset of the FINAL 0x0b byte in fn 226's body
    entry_f = next(f for f in mod.functions if f["func_idx"] == entry_fn)
    body_size = entry_f["code_end"] - entry_f["code_start"]
    final_end_code_off = body_size - 1
    if mod.raw[entry_f["code_end"] - 1] != 0x0b:
        raise RuntimeError(
            f"fn {entry_fn} body does not end with 0x0b "
            f"(last byte = {mod.raw[entry_f['code_end']-1]:#x})")
    log.info(f"  body_size={body_size}B, final 'end' at code_off="
             f"{final_end_code_off}", start=0, end=0)

    # Count early-return opcodes in the body for diagnostic purposes
    # (these skip the epilogue, so the POST counter may lag PRE).
    instrs = mod.decode_function(entry_fn) or []
    early_returns = sum(1 for n, _, _, _ in instrs if n == "return")
    log.info(f"  early `return` opcodes in body: {early_returns}",
             start=0, end=0)

    # Patch the module: PRE prologue at code_off=0, POST epilogue
    # spliced (a) just before the final 0x0b 'end' AND (b) immediately
    # before every `return` opcode in the body. The epilogue is
    # stack-neutral so it doesn't disturb the return value sitting on
    # the operand stack just before the `return`.
    writer = ModuleWriter(mod)
    prologue = _build_prologue()
    epilogue = _build_epilogue()

    # Insertion points for the epilogue: every `return` (code_off) plus
    # the final-end position.
    return_offsets = [off for n, _, off, _ in instrs if n == "return"]
    epilogue_sites = sorted(set(return_offsets + [final_end_code_off]))
    log.info(f"  epilogue insertion sites (code_off): {epilogue_sites}",
             start=0, end=0)
    for code_off in epilogue_sites:
        writer.code.splice_code(entry_fn, code_off,
                                n_replace=0, new_bytes=epilogue)
    writer.code.splice_code(entry_fn, 0,
                            n_replace=0, new_bytes=prologue)

    log.info(f"  prologue size = {len(prologue)}B, "
             f"epilogue size = {len(epilogue)}B, "
             f"sites = {len(epilogue_sites)}", start=0, end=0)

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

        # Reset counters, enable gate, run hsw(jwt)
        rt.eval(
            f"""
            globalThis.__done = 0;
            globalThis.__tok = '';
            globalThis.__err = '';
            (async () => {{
                const e = globalThis.__hsw_exports;
                e.__poke32({PRE_COUNTER_ADDR}, 0);
                e.__poke32({POST_COUNTER_ADDR}, 0);
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

        n_pre_total = (rt.eval(
            f"globalThis.__hsw_exports.__peek32({PRE_COUNTER_ADDR})"
        ) or 0) & 0xFFFFFFFF
        n_post_total = (rt.eval(
            f"globalThis.__hsw_exports.__peek32({POST_COUNTER_ADDR})"
        ) or 0) & 0xFFFFFFFF
        n_pre  = min(n_pre_total,  MAX_RECS)
        n_post = min(n_post_total, MAX_RECS)
        log.info(f"pre  records: {n_pre} (counter={n_pre_total})",
                 start=0, end=0)
        log.info(f"post records: {n_post} (counter={n_post_total})",
                 start=0, end=0)

        # Helper to bulk-read a contiguous region as bytes
        def _read_region(addr: int, n_bytes: int) -> bytes:
            if n_bytes <= 0:
                return b""
            # Read in chunks to avoid creating huge JSON arrays in one
            # eval call (3072*16 = 49152 bytes is fine; do it in one).
            arr = rt.eval(
                f"""(function() {{
                    const mem = new Uint8Array(
                        globalThis.__hsw_memory.buffer, {addr}, {n_bytes});
                    return Array.from(mem);
                }})()"""
            ) or []
            return bytes(arr)

        pre_blob  = _read_region(PRE_BUF_ADDR,  n_pre  * REC_SIZE)
        post_blob = _read_region(POST_BUF_ADDR, n_post * REC_SIZE)

        def _parse_blob(blob: bytes, n_recs: int) -> list[dict]:
            recs = []
            for i in range(n_recs):
                base = i * REC_SIZE
                counter = int.from_bytes(blob[base:base+4],     "little")
                a0      = int.from_bytes(blob[base+4:base+8],   "little")
                a1      = int.from_bytes(blob[base+8:base+12],  "little")
                a2      = int.from_bytes(blob[base+12:base+16], "little")
                buf0    = blob[base+REC_HEADER:base+REC_HEADER+REC_BUF0]
                buf1    = blob[base+REC_HEADER+REC_BUF0:
                              base+REC_HEADER+REC_BUF0+REC_BUF1]
                buf2    = blob[base+REC_HEADER+REC_BUF0+REC_BUF1:
                              base+REC_HEADER+REC_BUF0+REC_BUF1+REC_BUF2]
                recs.append({
                    "counter": counter, "arg0": a0, "arg1": a1, "arg2": a2,
                    "buf0_hex": buf0.hex(),
                    "buf1_hex": buf1.hex(),
                    "buf2_hex": buf2.hex(),
                })
            return recs

        pre_records  = _parse_blob(pre_blob,  n_pre)
        post_records = _parse_blob(post_blob, n_post)

        return {
            "wasm_sha256":   info["wasm_sha256"],
            "entry_fn":      entry_fn,
            "body_size":     body_size,
            "early_returns": early_returns,
            "jwt":           jwt,
            "token":         token,
            "n_pre":         n_pre,
            "n_pre_total":   n_pre_total,
            "n_post":        n_post,
            "n_post_total":  n_post_total,
            "pre_records":   pre_records,
            "post_records":  post_records,
        }
    finally:
        try:
            rt.close()
        except Exception:
            pass


# --------------------------------------------------------------------
# Diff / msgpack analysis
# --------------------------------------------------------------------
def analyze_diff(out: dict) -> dict:
    """Compare pre vs post buffers for each matching call. Classify
    each buf as plaintext / ciphertext / unchanged. Try msgpack on the
    plaintext-looking side."""
    pre  = out.get("pre_records",  [])
    post = out.get("post_records", [])

    try:
        import msgpack  # type: ignore
        have_msgpack = True
    except Exception:
        msgpack = None
        have_msgpack = False

    def _text_score(b: bytes, n: int = 256) -> int:
        return sum(1 for x in b[:n] if 0x20 <= x < 0x7f
                   or x in (9, 10, 13))

    def _entropy(b: bytes) -> float:
        if not b:
            return 0.0
        from collections import Counter
        cnt = Counter(b)
        L = len(b)
        import math
        return -sum((c / L) * math.log2(c / L) for c in cnt.values())

    results = []
    for i in range(min(len(pre), len(post))):
        rp = pre[i]
        rq = post[i]
        # Match by arg1 pointer + arg2 length (the relevant buf addr)
        buf1_pre  = bytes.fromhex(rp["buf1_hex"])
        buf1_post = bytes.fromhex(rq["buf1_hex"])
        # Only compare the first `arg2` bytes (the actual buf length)
        # if arg2 looks like a small length (< REC_BUF1). Otherwise
        # compare the whole 3072.
        arg2 = rp["arg2"]
        if 0 < arg2 <= REC_BUF1:
            cmp_len = arg2
        else:
            cmp_len = REC_BUF1
        n_diff = sum(1 for a, b in zip(buf1_pre[:cmp_len],
                                        buf1_post[:cmp_len]) if a != b)
        # First diff offset
        first_diff = -1
        for j in range(cmp_len):
            if buf1_pre[j] != buf1_post[j]:
                first_diff = j; break
        pre_text  = _text_score(buf1_pre[:cmp_len])
        post_text = _text_score(buf1_post[:cmp_len])
        pre_ent   = _entropy(buf1_pre[:cmp_len])
        post_ent  = _entropy(buf1_post[:cmp_len])

        # Try msgpack on plaintext-looking side
        msgpack_side = None
        msgpack_pre  = None
        msgpack_post = None
        if have_msgpack:
            try:
                msgpack_pre = msgpack.unpackb(buf1_pre[:cmp_len],
                                              raw=False, strict_map_key=False)
            except Exception as e:
                msgpack_pre = f"ERR:{type(e).__name__}:{str(e)[:80]}"
            try:
                msgpack_post = msgpack.unpackb(buf1_post[:cmp_len],
                                               raw=False, strict_map_key=False)
            except Exception as e:
                msgpack_post = f"ERR:{type(e).__name__}:{str(e)[:80]}"

        # Decide which side is "plaintext-shaped"
        plaintext_side = None
        if isinstance(msgpack_pre, (dict, list)) and \
           not isinstance(msgpack_post, (dict, list)):
            plaintext_side = "pre"
        elif isinstance(msgpack_post, (dict, list)) and \
             not isinstance(msgpack_pre, (dict, list)):
            plaintext_side = "post"
        elif pre_ent < post_ent - 0.5:
            plaintext_side = "pre"
        elif post_ent < pre_ent - 0.5:
            plaintext_side = "post"

        results.append({
            "call":            i,
            "arg0":            rp["arg0"],
            "arg1":            rp["arg1"],
            "arg2":            rp["arg2"],
            "cmp_len":         cmp_len,
            "buf1_pre_head":   buf1_pre[:32].hex(),
            "buf1_post_head":  buf1_post[:32].hex(),
            "buf1_pre_eq_post": n_diff == 0,
            "n_diff_bytes":    n_diff,
            "first_diff_off":  first_diff,
            "pre_text_score":  pre_text,
            "post_text_score": post_text,
            "pre_entropy":     round(pre_ent, 3),
            "post_entropy":    round(post_ent, 3),
            "msgpack_pre":     str(msgpack_pre)[:500],
            "msgpack_post":    str(msgpack_post)[:500],
            "plaintext_side":  plaintext_side,
        })
    return {
        "have_msgpack": have_msgpack,
        "per_call":     results,
    }


def main():
    out = run()
    out["diff_analysis"] = analyze_diff(out)
    save = os.path.join(THIS, "instrument_encrypt_entry.last.json")
    with open(save, "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out["diff_analysis"], indent=2))
    print(f"saved -> {save}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
