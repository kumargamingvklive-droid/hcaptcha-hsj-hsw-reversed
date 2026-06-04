"""Refined classifier for HSW unknown functions.

Key insights from inspection:
- A handful of "deobf core" functions (444, 449, 355, 578, 496, 477, 257) implement
  the runtime XOR-based decryption of rodata blobs. They share the constants
  0x140/0x148/0x26b/0x408/0x60 and (for the XOR step) 0xe586d82b.
  Every function that calls these IS A DEOBF CONSUMER.
- The classic deobf XOR-key constants 0x69169f2b / 0xbd31f8f6 / 0xfb42e581 /
  0x8304f247 / 0x77782831 / 0xe586d82b appear inline as compile-time decryption
  keys in deobf-reading callers.
- Fixslice32 masks (0x55555555 etc) mark AES rounds.
- call_indirect + a populated table marks Rust vtable dispatch (trait objects).
- A small body that ends with `unreachable` and calls something is the panic
  prologue Rust emits for `unwrap`/`expect`/`format!` macro paths.
"""
import json
import os
import sys
from collections import Counter

REPO = r"C:/Users/Administrator/Desktop/HSJ"
sys.path.insert(0, os.path.join(REPO, "src"))

from hcaptcha.tools.wasm_disasm import WasmModule  # noqa: E402

LABELS_PATH = os.path.join(REPO, "docs", "hsw_function_labels.json")
WASM_PATH   = r"C:/tmp/hsw-dcmp/hsw.wasm"


# Core "runtime deobf reader" helpers — calling any of these is a strong
# signal that the caller is mid-decryption of an obfuscated constant blob.
DEOBF_CORE = {444, 449, 355, 578, 496, 477, 257}
# Static deobf XOR keys observed inline
DEOBF_KEYS = {0x8304f247, 0x77782831, 0x69169f2b, 0xbd31f8f6, 0xfb42e581,
              0xe586d82b}
# fixslice32 masks (AES rounds)
FIXSLICE_MASKS = {0x55555555, 0xaaaaaaaa, 0x33333333, 0xcccccccc,
                  0x0f0f0f0f, 0xf0f0f0f0, 0x00ff00ff, 0xff00ff00,
                  0x0000ffff, 0xffff0000}
# PCG/LCG multiplier
PCG_LO = 0x4c957f2d
PCG_HI = 0x5851f42d
PCG64  = 0x5851f42d4c957f2d
# Wasm-bindgen reflect ops we already know
WBG_REFLECT = {12, 379}  # add to taste


def classify(mod, fn, fn_meta):
    """Return refined role label and a short description."""
    idx = fn["func_idx"]
    instrs = mod.decode_function(idx) or []
    body_size = fn_meta.get("body_bytes", 0)
    callees = fn_meta.get("callees", 0)
    callers = fn_meta.get("callers", 0)
    sig = fn_meta.get("signature", "")

    counts = Counter()
    consts32 = []
    consts64 = []
    static_calls = []
    has_call_indirect = False
    has_unreachable = False
    has_memory_copy = False
    has_memory_fill = False
    has_load8u = 0
    has_store8 = 0
    has_load32 = 0
    has_store32 = 0
    has_loop = False
    has_br_table = False
    n_blocks = 0
    n_select = 0
    last_ops = []  # tail-of-body
    for name, ops, _, _ in instrs:
        counts[name] += 1
        if name == "i32.const":
            consts32.append(ops[0] & 0xffffffff)
        elif name == "i64.const":
            consts64.append(ops[0] & 0xffffffffffffffff)
        elif name == "call" and ops:
            static_calls.append(ops[0])
        elif name == "call_indirect":
            has_call_indirect = True
        elif name == "unreachable":
            has_unreachable = True
        elif name == "memory.copy":
            has_memory_copy = True
        elif name == "memory.fill":
            has_memory_fill = True
        elif name == "i32.load8_u":
            has_load8u += 1
        elif name == "i32.store8":
            has_store8 += 1
        elif name == "i32.load":
            has_load32 += 1
        elif name == "i32.store":
            has_store32 += 1
        elif name == "loop":
            has_loop = True
        elif name == "br_table":
            has_br_table = True
        elif name == "block":
            n_blocks += 1
        elif name == "select":
            n_select += 1
        last_ops.append(name)
    last5 = last_ops[-5:]

    cset32 = set(consts32)
    cset64 = set(consts64)
    call_set = set(static_calls)
    n_instr = sum(counts.values())

    n_xor   = counts.get("i32.xor", 0) + counts.get("i64.xor", 0)
    n_shl   = counts.get("i32.shl", 0) + counts.get("i64.shl", 0)
    n_shr   = counts.get("i32.shr_u", 0) + counts.get("i64.shr_u", 0)
    n_rotl  = counts.get("i32.rotl", 0) + counts.get("i64.rotl", 0)
    n_rotr  = counts.get("i32.rotr", 0) + counts.get("i64.rotr", 0)
    n_mul   = counts.get("i32.mul", 0) + counts.get("i64.mul", 0)
    n_or    = counts.get("i32.or", 0) + counts.get("i64.or", 0)
    n_and   = counts.get("i32.and", 0) + counts.get("i64.and", 0)
    n_add   = counts.get("i32.add", 0) + counts.get("i64.add", 0)
    n_i64   = sum(v for k, v in counts.items() if k.startswith("i64."))
    n_i32   = sum(v for k, v in counts.items() if k.startswith("i32."))

    # ---- High-confidence buckets ----

    # PCG/LCG
    if PCG_LO in cset32 or PCG_HI in cset32 or PCG64 in cset64:
        return "rng_or_lcg", "carries PCG-32 multiplier"

    # Fixslice32 AES round
    fs_hits = sum(1 for c in consts32 if c in FIXSLICE_MASKS)
    if fs_hits >= 3 and n_xor >= 4:
        return "aes_round", f"fixslice masks={fs_hits} xor={n_xor}"

    # SHA-1-ish: lots of rotl + i32-only + xor
    if n_rotl >= 6 and n_xor >= 6 and n_i64 == 0:
        return "sha_round", f"rotl={n_rotl} xor={n_xor}"

    # Hash mixing: 64-bit rotations + xors
    if (n_rotl + n_rotr) >= 4 and n_xor >= 4:
        return "hash_round", f"rot={n_rotl + n_rotr} xor={n_xor}"

    # ---- Tiny stubs / trivials ----
    if has_unreachable and n_instr <= 4:
        return "panic_unreachable", "bare unreachable trap"

    # Very tiny wrapper around a single call
    if body_size <= 35 and len(static_calls) == 1 and not has_unreachable:
        return "thunk", f"forwards to f_{static_calls[0]}"

    # Tiny wrapper, no calls -> accessor/iterator advance
    if body_size <= 20 and not static_calls and not has_unreachable:
        return "accessor", f"trivial accessor (size={body_size})"

    # ---- Vtable / trait-object dispatch ----
    if has_call_indirect:
        # Has both static calls and an indirect -> likely a Rust trait dispatch
        if len(static_calls) >= 1:
            return "vtable_dispatch", f"call_indirect + {len(static_calls)} direct"
        return "vtable_dispatch", "pure call_indirect"

    # ---- Deobf consumers ----
    # The "deobf core" functions (444, 449, 355, 578, 496, 477, 257) are
    # actually general-purpose runtime helpers (String push, byte copy,
    # u64 store, etc.) so calling them is NOT specific to deobf.
    # The hard signal is the inline XOR-key constants from the obfuscator.
    deobf_keys_inline = len(cset32 & DEOBF_KEYS)
    if idx in DEOBF_CORE:
        # These are actually generic byte/string runtime helpers, not deobf-only
        return "runtime_string_io", "shared byte/string runtime helper (called everywhere)"
    if deobf_keys_inline >= 2:
        return "deobf_consumer", f"inline XOR keys present ({deobf_keys_inline})"

    # ---- Memory ops ----
    if has_memory_copy and counts.get("memory.copy", 0) >= 1:
        if counts.get("memory.copy", 0) >= 2 or body_size < 200:
            return "vec_copy", f"memory.copy x{counts.get('memory.copy', 0)}"
    if has_memory_fill:
        return "vec_fill", f"memory.fill x{counts.get('memory.fill', 0)}"

    # ---- Panic / format / unwrap ----
    # Rust's `unwrap_failed` / `panic_fmt` pattern: ends with unreachable
    # AND calls one of a handful of imports.
    if has_unreachable:
        if body_size < 200 and len(static_calls) <= 4:
            return "panic_unwrap", f"unreachable, calls={len(static_calls)}"
        if body_size < 400:
            return "panic_format", f"format! + unreachable (size={body_size})"
        # Larger ones ending in unreachable are still panic helpers
        return "panic_format", f"large panic helper (size={body_size})"

    # ---- Drop glue ----
    # Small fn, calls Box-dealloc-style helper(s) only
    if body_size < 80 and len(static_calls) <= 3 and not has_loop and not has_br_table:
        return "drop_glue", f"small drop helper (calls={len(static_calls)})"

    # ---- Result/Option helpers ----
    if body_size < 150 and len(static_calls) <= 3 and n_select <= 4 and not has_loop:
        return "small_helper", f"small helper (size={body_size})"

    # ---- i64-heavy big-int / serialize ----
    if n_i64 > n_i32 and body_size > 200:
        return "i64_arith", f"i64-heavy (i64={n_i64} i32={n_i32})"

    # ---- Byte-level (de)serializer ----
    if has_store8 >= 5 or has_load8u >= 8:
        return "byte_serializer", f"byte serializer (loads={has_load8u} stores={has_store8})"

    # ---- Large body w/ many calls = format/render orchestrator ----
    if body_size >= 800:
        # If it calls deobf core, it's a deobf-consuming "decode + use" routine
        if deobf_calls >= 1:
            return "deobf_consumer", f"large deobf consumer (size={body_size})"
        return "render_orchestrator", f"large orchestrator (size={body_size}, calls={len(static_calls)})"
    if body_size >= 300 and len(static_calls) >= 8:
        return "format_helper", f"medium format helper (size={body_size}, calls={len(static_calls)})"

    # ---- mid-size with loops -> parser/iter ----
    if has_loop and body_size < 600:
        return "loop_iter", f"loop-based iter/parser (size={body_size})"

    # ---- Big switch / br_table ----
    if has_br_table and n_blocks >= 4:
        return "branch_table_dispatch", f"br_table dispatch (size={body_size})"

    # Fallback
    return "utility", f"size={body_size} calls={len(static_calls)} blocks={n_blocks}"


def main():
    with open(LABELS_PATH, "rb") as f:
        labels = json.load(f)

    with open(WASM_PATH, "rb") as f:
        wasm_bytes = f.read()
    mod = WasmModule(wasm_bytes)

    before = sum(1 for fn in labels["functions"] if fn["role"] == "unknown")

    new_role_counts = Counter()
    samples = []
    for fn in labels["functions"]:
        if fn["role"] != "unknown":
            continue
        if fn.get("is_import"):
            new_role = "wbg_marshal"
            desc = "import shim"
        else:
            try:
                ms = next((f for f in mod.functions if f["func_idx"] == fn["func_idx"]), None)
                if ms is None:
                    new_role = "utility"; desc = "no body found"
                else:
                    new_role, desc = classify(mod, ms, fn)
            except Exception as e:
                new_role = "utility"; desc = f"classify error: {e}"
        fn["role"] = new_role
        fn["role_note"] = desc
        new_role_counts[new_role] += 1
        if len(samples) < 80:
            samples.append((fn["func_idx"], fn.get("name", ""), new_role, desc,
                            fn.get("body_bytes", 0), fn.get("signature", "")))

    after = sum(1 for fn in labels["functions"] if fn["role"] == "unknown")

    # Update top-level label_counts (full snapshot)
    full_counts = Counter(fn["role"] for fn in labels["functions"])
    labels["label_counts"] = dict(sorted(full_counts.items(), key=lambda x: -x[1]))

    with open(LABELS_PATH, "w", encoding="utf-8") as f:
        json.dump(labels, f, indent=2)

    print(f"unknown before: {before}")
    print(f"unknown after:  {after}")
    print(f"new buckets (newly-assigned only): {dict(new_role_counts)}")
    print()
    print(f"FULL label_counts:")
    for k, v in labels["label_counts"].items():
        print(f"  {k:25s} {v}")

    with open(r"C:/tmp/hsw_samples.md", "w", encoding="utf-8") as f:
        f.write("| idx | name | role | body | sig | description |\n")
        f.write("|---:|---|---|---:|---|---|\n")
        for idx, name, role, desc, body, sig in samples:
            f.write(f"| {idx} | `{name}` | `{role}` | {body} | `{sig}` | {desc} |\n")


if __name__ == "__main__":
    main()
