"""Backtest HSW key extractor against archived 1_40_34 bundle.

Static structural identification only. Locates the dispatcher (export 'vc'
or by signature shape) and scans for candidate magic-number gates that
guard calls into AES fixslice32 key-schedule functions.
"""
import os
import sys
import time
from collections import Counter

ROOT = r"C:\Users\Administrator\Desktop\HSJ"
sys.path.insert(0, os.path.join(ROOT, "src"))

from hcaptcha.tools.wasm_disasm import (
    WasmModule,
    FIXSLICE_MASKS,
    find_fixslice_functions,
)

WASM_PATH = r"C:\Users\ADMINI~1\AppData\Local\Temp\2\1_40_34.wasm"

t0 = time.time()
with open(WASM_PATH, "rb") as f:
    wasm = f.read()

mod = WasmModule(wasm)
n_imports = sum(1 for i in mod.imports if i["kind"] == "func")

print(f"WASM bytes: {len(wasm)}")
print(f"sections   : {[s[1] for s in mod.sections]}")
print(f"types      : {len(mod.types)}")
print(f"imports    : {len(mod.imports)} ({n_imports} funcs)")
print(f"functions  : {len(mod.functions)}  (local)")
print(f"exports    : {len(mod.exports)}")
print(f"data segs  : {len(mod.data_segments)}")

# ------------------------------------------------------------------
# Step 3 — find dispatcher
# ------------------------------------------------------------------
vc_export = None
for e in mod.exports:
    if e["kind"] == "func" and e["name"] == "vc":
        vc_export = e
        break

print()
if vc_export:
    fi = vc_export["idx"]
    f = mod.functions[fi - n_imports]
    params, results = mod.types[f["type_idx"]]
    print(f"Found export 'vc' -> func {fi}  sig=({','.join(params)})->({','.join(results)})")
    dispatcher_idx = fi
    dispatcher_found = True
else:
    print("No export 'vc' — looking at all exported funcs ordered by arity:")
    rows = []
    for e in mod.exports:
        if e["kind"] != "func" or e["idx"] < n_imports:
            continue
        f = mod.functions[e["idx"] - n_imports]
        params, results = mod.types[f["type_idx"]]
        rows.append((len(params), e["name"], e["idx"], tuple(params), tuple(results)))
    rows.sort(reverse=True)
    for n_params, name, fi, params, results in rows:
        print(f"  export {name!r:6s}  func {fi:4d}  ({','.join(params)})->({','.join(results)})")
    # pick the highest-arity all-i32 export (or fall back to any i32 of arity >=5)
    dispatcher_idx = None
    for n_params, name, fi, params, results in rows:
        if n_params >= 5 and all(p == "i32" for p in params):
            dispatcher_idx = fi
            dispatcher_name = name
            print(f"\nPicked dispatcher: export {name!r}  func {fi}  arity={n_params}")
            break
    dispatcher_found = dispatcher_idx is not None

# Also: maybe wbg_dispatcher / __wbg_vc / similar named export?
print()
print("Sampling of export names (first 30):")
for e in mod.exports[:30]:
    print(f"  [{e['kind']:6s}] {e['name']!r:30s} idx={e['idx']}")

# ------------------------------------------------------------------
# Step 4 — find candidate magics in dispatcher
# ------------------------------------------------------------------
# Pre-score every function for fixslice32 mask usage.
fixslice_scores = find_fixslice_functions(mod, top_n=999)
fixslice_func_ids = {fi for score, fi, _ in fixslice_scores if score >= 2}
print()
print(f"Functions using fixslice32 masks (score>=2): {len(fixslice_func_ids)}")
print(f"Top 8: {fixslice_scores[:8]}")

if not dispatcher_found:
    print("\nNo dispatcher — skipping magic scan.")
    sys.exit(0)

# Pull all calls transitively reachable from each call site inside dispatcher,
# up to depth 3, to detect indirect routing into a fixslice function.
def calls_reach_fixslice(func_idx, depth=3, seen=None):
    if seen is None:
        seen = set()
    if func_idx in seen:
        return False
    seen.add(func_idx)
    if func_idx in fixslice_func_ids:
        return True
    if depth == 0:
        return False
    if func_idx < n_imports:
        return False
    instrs = mod.decode_function(func_idx) or []
    for name, ops, _, _ in instrs:
        if name == "call" and ops and calls_reach_fixslice(ops[0], depth - 1, seen):
            return True
    return False

instrs = mod.decode_function(dispatcher_idx) or []
print(f"\nDispatcher func {dispatcher_idx} has {len(instrs)} instructions")

# Walk: for each i32.const C followed by i32.eq then if, mark C as candidate
# magic. Then within the if-block, look for any call whose target reaches a
# fixslice function within a small depth.
candidate_magics = []
for i in range(len(instrs) - 2):
    n0, ops0, _, _ = instrs[i]
    n1, _,    _, _ = instrs[i + 1]
    n2, _,    _, _ = instrs[i + 2]
    if n0 != "i32.const" or n1 != "i32.eq" or n2 != "if":
        continue
    magic = ops0[0] & 0xffffffff
    # scan forward from i+2 until matching `end` at depth 0
    depth = 0
    calls_in_block = []
    for j in range(i + 2, len(instrs)):
        nn, oo, _, _ = instrs[j]
        if nn in ("block", "loop", "if"):
            depth += 1
        elif nn == "end":
            depth -= 1
            if depth == 0:
                break
        elif nn == "call" and oo:
            calls_in_block.append(oo[0])
    reaches_fixslice = any(calls_reach_fixslice(c, depth=3) for c in calls_in_block)
    candidate_magics.append({
        "magic": magic,
        "magic_hex": f"0x{magic:08x}",
        "num_calls_in_block": len(calls_in_block),
        "calls": calls_in_block[:8],
        "reaches_fixslice": reaches_fixslice,
    })

print()
print(f"Total `i32.const C; i32.eq; if` gates in dispatcher: {len(candidate_magics)}")
gates_with_fixslice = [c for c in candidate_magics if c["reaches_fixslice"]]
print(f"Gates whose if-body reaches a fixslice function   : {len(gates_with_fixslice)}")
for c in gates_with_fixslice[:20]:
    print(f"  magic={c['magic_hex']}  calls={c['calls']}")

# ------------------------------------------------------------------
# Step 5 — schema booleans
# ------------------------------------------------------------------
encrypt_found = len(gates_with_fixslice) >= 1
decrypt_found = len(gates_with_fixslice) >= 2

# "Extract" key bytes statically: we don't actually have the keys without
# running patched WASM; but we can at least surface the magic numbers as
# the structural fingerprint of where the keys live.
encrypt_key_hex = ""
decrypt_key_hex = ""

print()
print(f"encrypt-key schedule identified: {encrypt_found}")
print(f"decrypt-key schedule identified: {decrypt_found}")

elapsed = time.time() - t0
print(f"\nElapsed: {elapsed:.2f}s")

# Emit a tiny machine-readable line for the parent.
import json
print("RESULT_JSON=" + json.dumps({
    "wasm_size_bytes": len(wasm),
    "dispatcher_found": dispatcher_found,
    "n_gates": len(candidate_magics),
    "n_gates_fixslice": len(gates_with_fixslice),
    "top_gates": [c["magic_hex"] for c in gates_with_fixslice[:6]],
    "elapsed": elapsed,
}))
