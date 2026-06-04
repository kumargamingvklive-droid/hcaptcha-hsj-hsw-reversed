"""Pure-Python port of Implex's hcaptcha-reverse / encryptions / fetcher.py.

STATUS — opt-in legacy module, NOT wired into KeyFetcher
========================================================

This is a port of Implex-ltd/hcaptcha-reverse's LCG-based N-key recovery
recipe. It works on legacy HSW WASM builds (eras a-c — pre-vc, call_indirect,
and named-export non-magic dispatcher; see docs/10-architecture-eras.md)
where the six per-build scalars (`key_seed`, `seed`, `memory`, `key_factor1`,
`key_factor2`, `operator`) appear as in-stream i32.const / i64.const
literals in a small dedicated function. On current era (d) builds those
scalars are emitted by a deobf helper call (e.g. ``call 496(0, MAGIC,
byte_idx, base_ptr)``) that reads from rodata tables, so the bytecode
pattern Implex matches no longer exists in-line. The module raises
``RuntimeError`` fast on era (d) builds; it is preserved here as the
reference extractor for archived bundles and as documentation of the
algorithm.

The strong working hypothesis (docs/09-hsw-keys-derivation.md) is that
the 32 bytes this derivation produces equal ``hsj.n_key`` for the same
build — i.e. it is a parallel derivation path inside hsw.js for a key
that is ALREADY exposed by :class:`HSJKeyFetcher`. Until a runtime-drive
fallback or pure-Python deobf-helper emulator lands, the keyfetcher
pipeline intentionally still returns five keys, not six.

ALGORITHM
=========

Implex's algorithm recovers a 32-byte "N key" from hsw.js by:

  1. Identifying a single function in the WASM that contains the LCG
     multiplier 6364136223846793005 (0x5851F42D4C957F2D) along with the
     marker int 8589934624 (== 0x200000020, two i32 ones packed into i64).
  2. Extracting four scalars (key_seed, seed, memory, key_factor1, key_factor2)
     and a +/- operator from the unrolled instruction stream of that function.
  3. Reading a 328-byte rodata blob at virtual address 1075552.
  4. Running 30 iterations of a PCG-XSH-RR-style LCG; each step xors a byte
     read from rodata with a rotated word derived from the PCG state.

This module is a faithful port of that recipe.  It uses the project's
existing WasmModule for parsing (no need for wasm2wat / wasm-decompile),
but where Implex's recipe relied on textual decompiler features
(``label B_``, ``select_if``, ``i32_wrap_i64(``, etc.) we instead match
the equivalent bytecode patterns directly.

USAGE (archived/legacy builds only — current builds will RuntimeError)
----------------------------------------------------------------------
    from hcaptcha.hsw_n_key import fetch_n_key
    n = fetch_n_key("<legacy-version-hash>")  # likely 1.40.x or earlier
    print(n)  # 64-char hex
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Optional

import requests

from .tools.wasm_disasm import WasmModule
from . import version as _v


# ---------------------------------------------------------------------------
# Algorithm constants (from Implex's fetcher.py)
# ---------------------------------------------------------------------------
LCG_MULTIPLIER = 6364136223846793005           # 0x5851F42D4C957F2D
LCG_MULTIPLIER_MASK = 0xFFFFFFFFFFFFFFFF
MEMORY_OFFSET = 1075552                        # virtual address of the rodata blob
MARKER_INT = 8589934624                        # 0x200000020 — distinctive packed-i32 sentinel
N_BYTES = 32                                   # output key length
N_LCG_STEPS = 30                               # = N_BYTES - 2  (first two bytes are key_seed lo)


# ---------------------------------------------------------------------------
# WASM blob extraction from hsw.js
# ---------------------------------------------------------------------------
def extract_wasm_from_hsw_js(version: str) -> bytes:
    """Download hsw.js for the given version and return the raw WASM bytes.

    First tries Implex's pattern (a single base64 blob after `0,null,"`).
    Falls back to the project's HSWAnalyzer (which runs the bundle in a
    Node sandbox and captures the WASM at instantiation) when the simple
    pattern is absent.
    """
    src = requests.get(
        f"https://newassets.hcaptcha.com/c/{version}/hsw.js",
        timeout=30,
    ).text

    # Try the simple legacy pattern first (cheap path).
    if '0,null,"' in src:
        try:
            blob = src.split('0,null,"')[1].split('"')[0]
            wasm = base64.b64decode(blob)
            if wasm[:4] == b"\x00asm":
                return wasm
        except Exception:
            pass

    # Fall back to the full sandbox-based extractor.  This handles every
    # build the project supports (the current one inlines the WASM as
    # multiple jn(...) base64 chunks in undefined order).
    from .hsw_bridge import HSWAnalyzer
    info = HSWAnalyzer(version=version).analyze()
    return bytes.fromhex(info["wasm_bytes_hex"])


# ---------------------------------------------------------------------------
# Constant extraction (bytecode walk, not decompiler text)
# ---------------------------------------------------------------------------
@dataclass
class KeyFactors:
    """The five scalars + one operator Implex's algorithm needs."""
    key_seed:    int   # 16-bit seed → first two bytes of key
    seed:        int   # initial PCG state (i64)
    memory:      int   # base index into the 1075552 rodata blob (i32)
    key_factor1: int   # constant added/subtracted into the PCG step (i64)
    key_factor2: int   # large positive index offset (>10^9)
    operator:    str   # '+' or '-' — how key_factor1 is folded into the LCG step


def _find_n_key_function(mod: WasmModule) -> Optional[int]:
    """Return the index of the function carrying the unrolled N-key
    derivation.  Heuristic: the function contains the highest density
    of i64.const LCG_MULTIPLIER literals.
    """
    best_fi, best_count = None, 0
    for f in mod.functions:
        instrs = mod.decode_function(f["func_idx"]) or []
        c = sum(
            1 for name, ops, _, _ in instrs
            if name == "i64.const" and ops
            and (ops[0] & LCG_MULTIPLIER_MASK) == LCG_MULTIPLIER
        )
        if c > best_count:
            best_fi, best_count = f["func_idx"], c
    return best_fi


def _consts_in_function(mod: WasmModule, fi: int):
    """Yield (idx, name, ops) for every instruction in function `fi`.
    Lets us scan for constant literals + nearby ops without re-decoding."""
    return list(mod.decode_function(fi) or [])


def extract_key_factors(mod: WasmModule, fi: int) -> Optional[KeyFactors]:
    """Walk the bytecode of `fi` and pull Implex's five scalars + operator.

    Detection rules (translated from Implex's text-pattern heuristics):
      * key_seed     — a smallish i32.const (< 65536) immediately preceding
                       the first `call X` that materialises an i64.
      * seed         — the i64.const literal closest to the first LCG-mul.
      * memory       — an i32.const literal feeding the rodata index path
                       (i.e. used as an offset that later combines with
                       key_factor2 + step).  Implex extracts it from a
                       plain `var = NNN;` line right after the marker.
      * key_factor1  — the i64.const that is either added or subtracted
                       immediately AFTER an `i64.mul` with the LCG mult.
      * key_factor2  — a large i32.const (> 10^9) used in the rodata index.
      * operator     — `+` if the post-mul op is i64.add, `-` if i64.sub.

    In the present build (which inlines the LCG step but obfuscates
    key_factor1 / key_seed / memory through helper calls), several of
    these literals are NOT present as in-stream i64.const/i32.const ops
    — they get pushed by `call HELPER` and never appear as constants.
    In that case this function returns None and the caller should record
    "constants unrecoverable in this build".
    """
    instrs = _consts_in_function(mod, fi)

    # --- key_factor1 + operator: the value added/subbed after i64.mul ---
    key_factor1 = None
    operator    = None
    for i in range(len(instrs) - 4):
        # pattern: i64.const LCG; i64.mul; [i64.const K; i64.add|i64.sub]
        n0, ops0, _, _ = instrs[i]
        n1, _,    _, _ = instrs[i + 1]
        n2, ops2, _, _ = instrs[i + 2]
        n3, _,    _, _ = instrs[i + 3]
        if (n0 == "i64.const" and ops0
                and (ops0[0] & LCG_MULTIPLIER_MASK) == LCG_MULTIPLIER
                and n1 == "i64.mul"
                and n2 == "i64.const" and ops2
                and n3 in ("i64.add", "i64.sub")):
            key_factor1 = ops2[0] & LCG_MULTIPLIER_MASK
            operator    = "+" if n3 == "i64.add" else "-"
            break

    # --- key_factor2: large i32.const (> 10^9) in this function ---
    key_factor2 = None
    for n, ops, _, _ in instrs:
        if n == "i32.const" and ops:
            v = ops[0]
            # treat as unsigned-ish: accept v in [10^9, 4*10^9)
            uv = v & 0xFFFFFFFF
            if 10**9 <= uv < 4 * 10**9:
                key_factor2 = uv
                break

    # --- seed: the i64.const literal that is the INITIAL PCG state ---
    # Heuristic: the first i64.const that is NOT the LCG multiplier and
    # not the key_factor1 we already found, evaluated near the top of
    # the function.
    seed = None
    for n, ops, off, _ in instrs:
        if n == "i64.const" and ops:
            v = ops[0] & LCG_MULTIPLIER_MASK
            if v == LCG_MULTIPLIER:
                continue
            if key_factor1 is not None and v == key_factor1:
                continue
            seed = v
            break

    # --- key_seed: smallish i32.const (< 65536) used before any LCG mul ---
    key_seed = None
    for n, ops, _, _ in instrs:
        if n == "i64.const" and ops and (ops[0] & LCG_MULTIPLIER_MASK) == LCG_MULTIPLIER:
            break
        if n == "i32.const" and ops:
            v = ops[0] & 0xFFFFFFFF
            if 0 < v < 65536:
                key_seed = v
                # keep last small one before the LCG section
    # ----- memory: i32.const used as small (< 4096) post-marker index --
    memory = None
    # in Implex's WAT, memory is a small i32 that is NEITHER key_seed NOR
    # one of the obvious mask constants (15/16/4/255/8/0/etc).  We accept
    # the first i32.const in [16, 4095] that is not key_seed.
    for n, ops, _, _ in instrs:
        if n == "i32.const" and ops:
            v = ops[0] & 0xFFFFFFFF
            if 16 <= v < 4096 and v != key_seed:
                memory = v
                break

    if None in (key_seed, seed, memory, key_factor1, key_factor2, operator):
        return None

    return KeyFactors(
        key_seed=key_seed,
        seed=seed,
        memory=memory,
        key_factor1=key_factor1,
        key_factor2=key_factor2,
        operator=operator,
    )


def get_rodata_blob(mod: WasmModule, vaddr: int = MEMORY_OFFSET) -> Optional[bytes]:
    """Return the data segment whose virtual address is exactly `vaddr`.
    Implex's algorithm reads from this single segment; if the build no
    longer has a segment starting there, return None.
    """
    for seg in mod.data_segments:
        if seg["vaddr"] == vaddr:
            return bytes(seg["data"])
    return None


# ---------------------------------------------------------------------------
# Core derivation — verbatim port of Implex's _generate_n_key
# ---------------------------------------------------------------------------
def derive_n_key(factors: KeyFactors, memory: bytes) -> bytes:
    """Run Implex's 30-step PCG-XSH-RR-flavoured derivation.

    Returns a 32-byte key.  Byte 0/1 are the low/high bytes of
    `factors.key_seed`; bytes 2..31 are generated.
    """
    seed = factors.seed
    key_factor1 = factors.key_factor1
    key_factor2 = factors.key_factor2

    # First two bytes are the low 16 bits of key_seed, little-endian.
    out = list(factors.key_seed.to_bytes(4, "little"))[:2]

    mem = memory
    mem_len = len(mem)

    for step in range(N_LCG_STEPS):
        if step != 0:
            seed = (seed * LCG_MULTIPLIER) & 0xFFFFFFFFFFFFFFFF
            if factors.operator == "+":
                seed = (seed + key_factor1) & 0xFFFFFFFFFFFFFFFF
            else:
                seed = (seed - key_factor1) & 0xFFFFFFFFFFFFFFFF

        base_index     = factors.memory + step
        memory_position = base_index + key_factor2

        # Two derived addresses; both are reduced mod mem_len with wrap.
        segment_address = (
            ((memory_position // 320) << 3) + memory_position + 1032 - MEMORY_OFFSET
        )
        mask_address = (memory_position % 96) + 8

        segment_address %= mem_len
        if segment_address + 4 <= mem_len:
            seg_bytes = mem[segment_address:segment_address + 4]
        else:
            wrap = segment_address + 4 - mem_len
            seg_bytes = mem[segment_address:] + mem[:wrap]
        segment_value = int.from_bytes(seg_bytes, "little")

        mask_address %= mem_len
        if mask_address + 8 <= mem_len:
            mask_bytes = mem[mask_address:mask_address + 8]
        else:
            wrap = mask_address + 8 - mem_len
            mask_bytes = mem[mask_address:] + mem[:wrap]
        mask_value = int.from_bytes(mask_bytes, "little")

        hash_value = (segment_value ^ (mask_value & 0xFFFFFFFF)) & 0xFF

        bit45 = (seed >> 45) & 0xFFFFFFFF
        bit27 = (seed >> 27) & 0xFFFFFFFF
        bit59 = (seed >> 59) & 0xFFFFFFFF
        if bit45 & 0x80000000: bit45 -= 0x100000000
        if bit27 & 0x80000000: bit27 -= 0x100000000
        if bit59 & 0x80000000: bit59 -= 0x100000000

        combined = bit45 ^ bit27
        shift = bit59 % 32
        combined &= 0xFFFFFFFF
        rotated = ((combined >> shift) | (combined << (32 - shift))) & 0xFFFFFFFF
        if rotated & 0x80000000: rotated -= 0x100000000

        key_byte = (hash_value ^ rotated) & 0xFF
        out.append(key_byte)

    return bytes(out)


# ---------------------------------------------------------------------------
# Top-level API
# ---------------------------------------------------------------------------
def fetch_n_key(version: Optional[str] = None) -> str:
    """End-to-end port of Implex's `HCaptchaKey(version).run()`.

    Returns the recovered N-key as a 64-char lowercase hex string.

    Raises RuntimeError if any structural step fails (no LCG function
    located, constants unrecoverable, rodata blob missing).
    """
    version = version or _v.latest_version()
    wasm = extract_wasm_from_hsw_js(version)
    mod = WasmModule(wasm)

    fi = _find_n_key_function(mod)
    if fi is None:
        raise RuntimeError(
            "no function in WASM contains the LCG multiplier "
            f"{LCG_MULTIPLIER} — algorithm has changed."
        )

    factors = extract_key_factors(mod, fi)
    if factors is None:
        raise RuntimeError(
            f"could not extract Implex's six scalars from func {fi}; "
            "constants are likely deobfuscated via helper calls in this build."
        )

    blob = get_rodata_blob(mod, MEMORY_OFFSET)
    if blob is None:
        raise RuntimeError(
            f"no data segment at vaddr={MEMORY_OFFSET}; cannot run derivation."
        )

    key = derive_n_key(factors, blob)
    return key.hex()


if __name__ == "__main__":
    import sys
    v = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        print(fetch_n_key(v))
    except Exception as e:
        print(f"error: {e}")
        sys.exit(1)
