import sys, base64, re
sys.path.insert(0, "C:/Users/Administrator/Desktop/HSJ/src")
from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions
from collections import Counter

js = open(r"C:\Users\Administrator\AppData\Local\Temp\2\1_40_15_hsw_bind.js","rb").read()
m = re.search(rb'"(AGFzbQ[A-Za-z0-9+/=]+)"', js)
wasm = base64.b64decode(m.group(1).decode("ascii"))
mod = WasmModule(wasm)

canonical = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
# Relax: ANY function with >=3 of the canonical masks, ANY signature
fixslice = set()
for score, fi, masks in find_fixslice_functions(mod, top_n=60):
    overlap = canonical & set(masks.keys())
    if len(overlap) >= 3:
        fixslice.add(fi)
print(f"Relaxed fixslice32 candidates: {sorted(fixslice)}")

# Scan ALL funcs for i32.const ; i32.eq ; if -> call(fixslice)
print()
print("Looking for ANY function with 'i32.const ; i32.eq ; if' that routes "
      "into a fixslice function (any sig):")
matches = []
for f in mod.functions:
    fi = f["func_idx"]
    try:
        instrs = mod.decode_function(fi)
    except Exception:
        continue
    triples = []
    for i in range(len(instrs) - 2):
        n, ops, _, _ = instrs[i]
        if n != "i32.const" or not ops:
            continue
        n1, _, _, _ = instrs[i+1]
        n2, _, _, _ = instrs[i+2]
        if n1 == "i32.eq" and n2 == "if":
            magic = ops[0]
            depth = 1
            j = i + 3
            while j < len(instrs):
                nj, opsj, _, _ = instrs[j]
                if nj in ("block","loop","if"): depth += 1
                elif nj == "end":
                    depth -= 1
                    if depth == 0: break
                elif nj == "call" and opsj and opsj[0] in fixslice:
                    triples.append((magic, opsj[0]))
                    break
                j += 1
    if triples:
        p, r = mod.types[f["type_idx"]]
        matches.append((fi, triples, p, r))

print(f"Found {len(matches)} candidate dispatcher functions")
for fi, triples, p, r in matches[:20]:
    print(f"  func {fi}  sig={p}->{r}  magics->KS: {triples[:5]}")

# Just to be 100% sure: search if any function in the wasm calls into the
# fixslice set at all
print()
print("Who calls into fixslice set?")
callers = Counter()
for f in mod.functions:
    fi = f["func_idx"]
    try:
        instrs = mod.decode_function(fi)
    except Exception:
        continue
    for n, ops, _, _ in instrs:
        if n == "call" and ops and ops[0] in fixslice:
            callers[fi] += 1
print(f"  {dict(callers.most_common(15))}")
