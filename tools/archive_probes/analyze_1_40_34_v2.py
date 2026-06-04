"""Deeper analysis: enumerate calls from each export and look for one whose
call-tree fans into the fixslice cluster with magic-gated branches."""
import os
import sys
import time
import json
from collections import Counter, defaultdict

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

# Pre-score fixslice functions.
fixslice_scores = find_fixslice_functions(mod, top_n=999)
fixslice_func_ids = {fi for score, fi, _ in fixslice_scores if score >= 4}
print(f"fixslice-rich functions (score>=4): {sorted(fixslice_func_ids)}")

# Build call graph.
callgraph = defaultdict(set)   # caller -> set(callee)
for f in mod.functions:
    fi = f["func_idx"]
    for name, ops, _, _ in (mod.decode_function(fi) or []):
        if name == "call" and ops:
            callgraph[fi].add(ops[0])

def reaches(src, targets, max_depth=4):
    seen = {src}
    frontier = {src}
    for _ in range(max_depth):
        nxt = set()
        for s in frontier:
            for c in callgraph.get(s, ()):
                if c in targets:
                    return True
                if c not in seen and c >= n_imports:
                    seen.add(c)
                    nxt.add(c)
        frontier = nxt
        if not frontier:
            break
    return False

# Enumerate every export func and check which can reach a fixslice cluster.
print()
print("Export -> reaches fixslice?")
ranked = []
for e in mod.exports:
    if e["kind"] != "func" or e["idx"] < n_imports:
        continue
    fi = e["idx"]
    params, results = mod.types[mod.functions[fi - n_imports]["type_idx"]]
    reach = reaches(fi, fixslice_func_ids, max_depth=4)
    n_instrs = len(mod.decode_function(fi) or [])
    print(f"  {e['name']!r:6s} func {fi:4d}  ({','.join(params)})->({','.join(results)})"
          f"  reach_fixslice={reach}  body_instrs={n_instrs}")
    if reach:
        ranked.append((n_instrs, fi, e["name"], params, results))

ranked.sort(reverse=True)

# Pick the export with the largest body that reaches fixslice — that's the
# dispatcher.
if not ranked:
    print("No exported function reaches fixslice.")
    dispatcher_idx = None
else:
    biggest = ranked[0]
    print(f"\nPicked dispatcher: export {biggest[2]!r}  func {biggest[1]}  "
          f"body_instrs={biggest[0]}")
    dispatcher_idx = biggest[1]

dispatcher_found = dispatcher_idx is not None

# Scan dispatcher (and possibly its direct call tree at depth 1) for
# i32.const C; i32.eq; if pattern.
def scan_magics(fi):
    instrs = mod.decode_function(fi) or []
    out = []
    for i in range(len(instrs) - 2):
        n0, ops0, _, _ = instrs[i]
        n1, _,    _, _ = instrs[i + 1]
        n2, _,    _, _ = instrs[i + 2]
        if n0 != "i32.const" or n1 != "i32.eq" or n2 != "if":
            continue
        magic = ops0[0] & 0xffffffff
        # forward scan to find the if-block calls
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
        reaches_fix = any(reaches(c, fixslice_func_ids, max_depth=3) or c in fixslice_func_ids
                          for c in calls_in_block)
        out.append({
            "host_func": fi,
            "magic": magic,
            "magic_hex": f"0x{magic:08x}",
            "calls": calls_in_block,
            "reaches_fixslice": reaches_fix,
        })
    return out

all_gates = []
if dispatcher_found:
    # scan dispatcher itself
    all_gates.extend(scan_magics(dispatcher_idx))
    # plus depth-1 callees (since the dispatcher often delegates to a switch
    # implementation function)
    for c in sorted(callgraph.get(dispatcher_idx, ())):
        if c < n_imports:
            continue
        all_gates.extend(scan_magics(c))

print()
print(f"Total magic gates (dispatcher + depth-1 callees): {len(all_gates)}")
gates_with_fixslice = [g for g in all_gates if g["reaches_fixslice"]]
print(f"Gates whose if-body reaches a fixslice func    : {len(gates_with_fixslice)}")
seen_magics = []
for g in gates_with_fixslice[:30]:
    if g["magic_hex"] not in seen_magics:
        seen_magics.append(g["magic_hex"])
    print(f"  host=func{g['host_func']:3d}  magic={g['magic_hex']}  calls={g['calls'][:6]}")

print()
print(f"Unique magics that gate fixslice work: {seen_magics}")

encrypt_found = len(seen_magics) >= 1
decrypt_found = len(seen_magics) >= 2

elapsed = time.time() - t0
print(f"\nElapsed: {elapsed:.2f}s")

print("RESULT_JSON=" + json.dumps({
    "wasm_size_bytes": len(wasm),
    "dispatcher_found": dispatcher_found,
    "n_gates_fixslice": len(gates_with_fixslice),
    "unique_magics": seen_magics,
    "encrypt_found": encrypt_found,
    "decrypt_found": decrypt_found,
    "elapsed": elapsed,
}))
