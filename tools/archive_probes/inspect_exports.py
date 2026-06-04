import sys, base64, re, os
sys.path.insert(0, "C:/Users/Administrator/Desktop/HSJ/src")
from hcaptcha.tools.wasm_disasm import WasmModule

js = open(r"C:\Users\Administrator\AppData\Local\Temp\2\1_40_15_hsw_bind.js","rb").read()
m = re.search(rb'"(AGFzbQ[A-Za-z0-9+/=]+)"', js)
wasm = base64.b64decode(m.group(1).decode("ascii"))
mod = WasmModule(wasm)

print("EXPORTS:")
for ex in mod.exports:
    if ex["kind"] != "func":
        print(f"  {ex['kind']:8s} {ex['name']}")
        continue
    f = next((f for f in mod.functions if f["func_idx"] == ex["idx"]), None)
    if f is None:
        print(f"  func     {ex['name']:30s} (idx={ex['idx']}, imported)")
        continue
    params, results = mod.types[f["type_idx"]]
    print(f"  func     {ex['name']:30s} (idx={ex['idx']}, "
          f"params={len(params)}, results={len(results)}) {params} -> {results}")
