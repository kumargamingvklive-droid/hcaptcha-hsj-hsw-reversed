"""Deeper probe: scan biggest functions for magic-eq-if patterns calling
the two fixslice candidates (func 279 + 120)."""
import base64, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions

JS_PATH = r"C:\Users\ADMINI~1\AppData\Local\Temp\2\1_40_31_hsw_bind.js"
js = open(JS_PATH, "r", encoding="utf-8", errors="replace").read()
m = re.search(r'CUSTOMWASM\s*=\s*"([A-Za-z0-9+/=]+)"', js)
wasm = base64.b64decode(m.group(1))
mod = WasmModule(wasm)

# strict canonical-mask fixslice candidates
canonical = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
fixslice_with_size = {}
for sc, fi, masks in find_fixslice_functions(mod, top_n=60):
    overlap = canonical & set(masks.keys())
    if len(overlap) >= 3:
        f = next((f for f in mod.functions if f["func_idx"] == fi), None)
        if f is None: continue
        params, results = mod.types[f["type_idx"]]
        if params == ["i32", "i32"] and results == []:
            fixslice_with_size[fi] = f["code_end"] - f["code_start"]
print("strict fixslice (i32,i32)->() candidates:", fixslice_with_size)

# Look at top-12 largest funcs: scan for magic-eq-if patterns whose if-block
# calls one of those fixslice funcs.
sized = sorted(
    [(f["code_end"]-f["code_start"], f["func_idx"], f["type_idx"]) for f in mod.functions],
    reverse=True,
)[:20]

for sz, fi, ti in sized:
    params, results = mod.types[ti]
    instrs = mod.decode_function(fi)
    if not instrs:
        continue
    magic_to_ks = {}
    for i in range(len(instrs) - 2):
        n, ops, _, _ = instrs[i]
        if n != "i32.const" or not ops: continue
        n1, _, _, _ = instrs[i+1]
        n2, _, _, _ = instrs[i+2]
        if n1 != "i32.eq" or n2 != "if": continue
        depth = 1
        j = i + 3
        while j < len(instrs):
            nj, opsj, _, _ = instrs[j]
            if nj in ("block", "loop", "if"): depth += 1
            elif nj == "end":
                depth -= 1
                if depth == 0: break
            elif nj == "call" and opsj and opsj[0] in fixslice_with_size:
                magic_to_ks.setdefault(ops[0], opsj[0])
                break
            j += 1
    if magic_to_ks:
        sig = f"({','.join(params)})->({','.join(results)})"
        print(f"func {fi:4d}  body={sz:7d}B  type={sig:40s}  magic->ks={magic_to_ks}")

# Also check: does ANY function call BOTH 279 and 120? Or one of them
# multiple times? That's a stronger dispatcher signature.
from collections import Counter
print("\n--- callers of fixslice funcs ---")
for tgt in fixslice_with_size:
    callers = []
    for f in mod.functions:
        instrs = mod.decode_function(f["func_idx"])
        cnt = sum(1 for n, ops, _, _ in instrs
                  if n == "call" and ops and ops[0] == tgt)
        if cnt: callers.append((cnt, f["func_idx"]))
    callers.sort(reverse=True)
    print(f"  func {tgt} called by: {callers[:10]}")
