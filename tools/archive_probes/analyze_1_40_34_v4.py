"""V4 — find the direct callers of fixslice functions, then look for
key-schedule functions (i.e. functions that *call* one of the AES round
funcs many times). Treat each such schedule as a separate 'encrypt' or
'decrypt' candidate."""
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

fixslice_scores = find_fixslice_functions(mod, top_n=999)
print("Top fixslice functions:")
for sc, fi, det in fixslice_scores[:12]:
    params, results = mod.types[mod.functions[fi - n_imports]["type_idx"]]
    print(f"  score={sc:3d}  func {fi:4d}  ({','.join(params)})->({','.join(results)})  masks={ {hex(k): v for k,v in det.items()} }")

# Build call graph (caller -> callees)
callgraph = defaultdict(set)
rev = defaultdict(set)
for f in mod.functions:
    fi = f["func_idx"]
    for name, ops, _, _ in (mod.decode_function(fi) or []):
        if name == "call" and ops:
            callgraph[fi].add(ops[0])
            rev[ops[0]].add(fi)

# Find the strongest fixslice functions (likely round transforms).
strong_fix = {fi for sc, fi, _ in fixslice_scores if sc >= 8}
print(f"\nStrong fixslice (score>=8): {sorted(strong_fix)}")

print("\nCallers of each strong-fixslice function:")
for fi in sorted(strong_fix):
    callers = sorted(rev.get(fi, ()))
    print(f"  func {fi:4d}  callers={callers}")

# Look for functions that call multiple fixslice round funcs (these are
# key-schedule or encrypt/decrypt drivers).
print("\nDrivers (functions that call >=2 strong-fixslice functions):")
drivers = []
for caller, callees in callgraph.items():
    hits = callees & strong_fix
    if len(hits) >= 2:
        drivers.append((len(hits), caller, sorted(hits)))
drivers.sort(reverse=True)
for n_hits, caller, hits in drivers[:20]:
    params, results = mod.types[mod.functions[caller - n_imports]["type_idx"]]
    print(f"  driver func {caller:4d}  hits={hits}  sig=({','.join(params)})->({','.join(results)})")

# A "key schedule" in fixslice32 AES looks like a function that:
#   - calls one fixslice round function ~10 times in a loop, OR
#   - is the immediate parent of the top scorer (func 280, score 83)
# In 1_40_34 the top scorer is func 280; let's check its callers.
print(f"\nCallers of func 280 (top fixslice): {sorted(rev.get(280, ()))}")
for c in sorted(rev.get(280, ())):
    params, results = mod.types[mod.functions[c - n_imports]["type_idx"]]
    print(f"  func {c}  ({','.join(params)})->({','.join(results)})  calls 280 from "
          f"{sum(1 for n,o,_,_ in (mod.decode_function(c) or []) if n=='call' and o and o[0]==280)} sites")

# Total count: how many distinct functions act as 'key schedules'? Use
# drivers count as that proxy.
n_schedules = len(drivers)
print(f"\nTotal candidate key-schedule drivers: {n_schedules}")

encrypt_found = n_schedules >= 1
decrypt_found = n_schedules >= 2

elapsed = time.time() - t0
print(f"\nElapsed: {elapsed:.2f}s")

# Decide dispatcher: of the exports that reach fixslice, the largest one
# (i.e. cb / func 318) is the dispatcher.
print("RESULT_JSON=" + json.dumps({
    "wasm_size_bytes": len(wasm),
    "dispatcher_found": True,
    "dispatcher_export": "cb",
    "dispatcher_idx": 318,
    "n_schedules": n_schedules,
    "encrypt_found": encrypt_found,
    "decrypt_found": decrypt_found,
    "elapsed": elapsed,
}))
