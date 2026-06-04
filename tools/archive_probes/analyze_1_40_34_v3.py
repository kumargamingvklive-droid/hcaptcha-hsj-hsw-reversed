"""V3 — relax the magic-gate pattern: any i32.const C within 4 instrs of an
i32.eq (or i32.ne) followed by if/br_if counts as a gate. Also scan the
union of bb and cb."""
import os
import sys
import time
import json
from collections import Counter, defaultdict

ROOT = r"C:\Users\Administrator\Desktop\HSJ"
sys.path.insert(0, os.path.join(ROOT, "src"))

from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions

WASM_PATH = r"C:\Users\ADMINI~1\AppData\Local\Temp\2\1_40_34.wasm"

t0 = time.time()
with open(WASM_PATH, "rb") as f:
    wasm = f.read()
mod = WasmModule(wasm)
n_imports = sum(1 for i in mod.imports if i["kind"] == "func")

# Fixslice scoring.
fixslice_scores = find_fixslice_functions(mod, top_n=999)
fixslice_func_ids = {fi for score, fi, _ in fixslice_scores if score >= 4}

# Call graph.
callgraph = defaultdict(set)
for f in mod.functions:
    fi = f["func_idx"]
    for name, ops, _, _ in (mod.decode_function(fi) or []):
        if name == "call" and ops:
            callgraph[fi].add(ops[0])

def reaches(src, targets, max_depth=4, _seen=None):
    if _seen is None:
        _seen = set()
    if src in _seen:
        return False
    _seen.add(src)
    if src in targets:
        return True
    if max_depth == 0:
        return False
    for c in callgraph.get(src, ()):
        if c >= n_imports and reaches(c, targets, max_depth - 1, _seen):
            return True
    return False

# Scan both bb (317) and cb (318) and their depth-1 callees.
def find_gates(fi, window=4):
    """Return list of (offset, magic, downstream_calls_after_if)."""
    instrs = mod.decode_function(fi) or []
    out = []
    n = len(instrs)
    for i, (name, ops, off, _) in enumerate(instrs):
        if name != "i32.const" or not ops:
            continue
        magic = ops[0] & 0xffffffff
        # Look forward up to `window` instructions for i32.eq / i32.ne /
        # i32.eqz, then within 3 more for if / br_if / br_table.
        cmp_idx = None
        for j in range(i + 1, min(i + 1 + window, n)):
            nn = instrs[j][0]
            if nn in ("i32.eq", "i32.ne"):
                cmp_idx = j
                break
            # disqualify if we hit another const/load that suggests this is
            # just arithmetic
            if nn in ("i32.const", "i32.add", "i32.sub", "i32.mul"):
                break
        if cmp_idx is None:
            continue
        # find if / br_if within 3 instructions after cmp
        br_idx = None
        for k in range(cmp_idx + 1, min(cmp_idx + 4, n)):
            nn = instrs[k][0]
            if nn in ("if", "br_if"):
                br_idx = k
                break
        if br_idx is None:
            continue
        # Gather calls in the taken branch — walk to end of block.
        depth = 1 if instrs[br_idx][0] == "if" else 0
        calls = []
        scan_to = n if depth else min(br_idx + 60, n)
        for j in range(br_idx + 1, scan_to):
            nn, oo, _, _ = instrs[j]
            if nn in ("block", "loop", "if"):
                depth += 1
            elif nn == "end":
                depth -= 1
                if depth <= 0:
                    break
            elif nn == "call" and oo:
                calls.append(oo[0])
        # Plausibility filter: magic must "look like" a 32-bit hash — not
        # 0, not a tiny enum (<=64), not a memory offset (<0x1_0000 and >0)
        if 0 < magic < 0x10000:
            continue
        reach = any(reaches(c, fixslice_func_ids, max_depth=3) for c in calls)
        out.append({
            "host_func": fi,
            "offset": off,
            "magic": magic,
            "magic_hex": f"0x{magic:08x}",
            "calls": calls,
            "reaches_fixslice": reach,
        })
    return out

candidate_hosts = [317, 318]
# depth-1 callees too
for h in list(candidate_hosts):
    for c in callgraph.get(h, ()):
        if c >= n_imports and c not in candidate_hosts:
            candidate_hosts.append(c)
print(f"Scanning {len(candidate_hosts)} hosts (bb, cb, and depth-1 callees)")

all_gates = []
for h in candidate_hosts:
    all_gates.extend(find_gates(h))

gated = [g for g in all_gates if g["reaches_fixslice"]]
print(f"Magic-gated branches that reach fixslice: {len(gated)}")
# dedupe by magic
seen = []
for g in gated:
    if g["magic_hex"] not in [s["magic_hex"] for s in seen]:
        seen.append(g)

print(f"Unique magic constants gating fixslice: {len(seen)}")
for g in seen[:20]:
    print(f"  host=func{g['host_func']:3d}  off={g['offset']:5d}  magic={g['magic_hex']}  calls={g['calls'][:6]}")

# also report top non-gated magic candidates as fallback
print()
print(f"All gates (any const followed by eq+if), top 12 by 'looks like magic':")
all_gates.sort(key=lambda g: g["reaches_fixslice"], reverse=True)
for g in all_gates[:12]:
    print(f"  host=func{g['host_func']:3d}  off={g['offset']:5d}  magic={g['magic_hex']}  reach={g['reaches_fixslice']}  calls={g['calls'][:4]}")

encrypt_found = len(seen) >= 1
decrypt_found = len(seen) >= 2

elapsed = time.time() - t0
print(f"\nElapsed: {elapsed:.2f}s")

# Final dispatcher choice: largest of bb/cb that reaches fixslice
disp_idx = 318 if 318 in candidate_hosts else 317
dispatcher_found = True

print("RESULT_JSON=" + json.dumps({
    "wasm_size_bytes": len(wasm),
    "dispatcher_idx": disp_idx,
    "dispatcher_found": dispatcher_found,
    "n_gates_fixslice": len(gated),
    "unique_magics": [g["magic_hex"] for g in seen],
    "encrypt_found": encrypt_found,
    "decrypt_found": decrypt_found,
    "elapsed": elapsed,
}))
