import sys, base64, re
sys.path.insert(0, "C:/Users/Administrator/Desktop/HSJ/src")
from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions
from collections import Counter

js = open(r"C:\Users\Administrator\AppData\Local\Temp\2\1_40_15_hsw_bind.js","rb").read()
m = re.search(rb'"(AGFzbQ[A-Za-z0-9+/=]+)"', js)
wasm = base64.b64decode(m.group(1).decode("ascii"))
mod = WasmModule(wasm)

# Find fixslice32 functions first (any signature)
print("Top fixslice32 mask-bearing functions:")
canonical = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
fixslice_all = []
for score, fi, masks in find_fixslice_functions(mod, top_n=30):
    overlap = canonical & set(masks.keys())
    if len(overlap) >= 3:
        f = next((f for f in mod.functions if f["func_idx"] == fi), None)
        if f is None: continue
        p, r = mod.types[f["type_idx"]]
        print(f"  func {fi:4d}  score={score:3d}  sig={p}->{r}  masks={list(masks.items())[:5]}")
        fixslice_all.append(fi)

print()
print("Now scan ALL functions for those with many 'i32.const ; i32.eq ; if'")
print("triples, where the if-block contains a call into the fixslice set.")

# Look for the dispatcher: any function with many magic compare patterns
def count_magic_pattern(fi):
    try:
        instrs = mod.decode_function(fi)
    except Exception:
        return 0, 0
    triples = 0
    hits = 0
    for i in range(len(instrs) - 2):
        n, ops, _, _ = instrs[i]
        if n != "i32.const" or not ops:
            continue
        n1, _, _, _ = instrs[i+1]
        n2, _, _, _ = instrs[i+2]
        if n1 == "i32.eq" and n2 == "if":
            triples += 1
            depth = 1
            j = i + 3
            while j < len(instrs):
                nj, opsj, _, _ = instrs[j]
                if nj in ("block","loop","if"): depth += 1
                elif nj == "end":
                    depth -= 1
                    if depth == 0: break
                elif nj == "call" and opsj and opsj[0] in fixslice_all:
                    hits += 1
                    break
                j += 1
    return triples, hits

candidates = []
for f in mod.functions:
    fi = f["func_idx"]
    triples, hits = count_magic_pattern(fi)
    if triples >= 5 or hits >= 2:
        p, r = mod.types[f["type_idx"]]
        candidates.append((hits, triples, fi, p, r))

candidates.sort(reverse=True)
print("Top dispatcher candidates (by hits, triples):")
for hits, triples, fi, p, r in candidates[:10]:
    print(f"  func {fi:4d}  triples={triples:3d}  hits-to-fixslice={hits:2d}  "
          f"params={len(p)}({p}) results={len(r)}")

# Also find any exported function or any function that calls 'client'
# Looking at the export 'client' (func 388), peek at its body
print()
print("client (func 388) body analysis:")
client_instrs = mod.decode_function(388)
print(f"  {len(client_instrs)} instructions")
# How many i32.const ; i32.eq ; if triples?
t, h = count_magic_pattern(388)
print(f"  triples={t}, hits-to-fixslice={h}")
# What does it call?
call_counts = Counter()
for n, ops, _, _ in client_instrs:
    if n == "call" and ops:
        call_counts[ops[0]] += 1
print(f"  Top calls: {call_counts.most_common(10)}")
