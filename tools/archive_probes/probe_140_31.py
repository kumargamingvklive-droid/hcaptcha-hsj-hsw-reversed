"""Probe 1_40_31 WASM exports & function shapes."""
import base64, os, re, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions

JS_PATH = r"C:\Users\ADMINI~1\AppData\Local\Temp\2\1_40_31_hsw_bind.js"
js = open(JS_PATH, "r", encoding="utf-8", errors="replace").read()
m = re.search(r'CUSTOMWASM\s*=\s*"([A-Za-z0-9+/=]+)"', js)
wasm = base64.b64decode(m.group(1))
mod = WasmModule(wasm)

print(f"funcs={len(mod.functions)} exports={len(mod.exports)} types={len(mod.types)}")
print("\n--- ALL EXPORTS ---")
for ex in mod.exports:
    if ex["kind"] != "func":
        print(f"  {ex['kind']:8s} {ex['name']!r:30s} idx={ex['idx']}")
        continue
    f = next((f for f in mod.functions if f["func_idx"] == ex["idx"]), None)
    if f is None:
        print(f"  func     {ex['name']!r:30s} idx={ex['idx']}  (no body)")
        continue
    params, results = mod.types[f["type_idx"]]
    sz = f["code_end"] - f["code_start"]
    print(f"  func     {ex['name']!r:30s} idx={ex['idx']}  type=({','.join(params)})->({','.join(results)})  body={sz}B")

print("\n--- Largest functions (top 12) ---")
sized = sorted(
    [(f["code_end"]-f["code_start"], f["func_idx"], f["type_idx"]) for f in mod.functions],
    reverse=True,
)[:12]
for sz, fi, ti in sized:
    params, results = mod.types[ti]
    print(f"  func {fi:4d}  body={sz:7d}B  type=({','.join(params)})->({','.join(results)})")

print("\n--- find_fixslice_functions(top 15) ---")
for sc, fi, masks in find_fixslice_functions(mod, top_n=15):
    f = next(ff for ff in mod.functions if ff["func_idx"] == fi)
    params, results = mod.types[f["type_idx"]]
    mask_str = "{" + ",".join(f"{k:#010x}:{v}" for k, v in masks.items()) + "}"
    print(f"  score={sc:4d}  func={fi:4d}  type=({','.join(params)})->({','.join(results)})  masks={mask_str}")
