"""Backtest the HSW key extractor's STATIC structural identification
against the archived 1_40_15_hsw_bind.js bundle.

Steps:
  1. Read the JS file
  2. Extract the embedded base64 WASM blob (starts with `AGFzbQEAAAAB`)
  3. Decode it; verify it starts with the WASM magic '\x00asm'
  4. Instantiate WasmModule
  5. Locate dispatcher `vc` (by export name or signature shape)
  6. For each i32.const constant in vc's instructions followed by
     i32.eq + if, treat as candidate magic. If the if-block contains a
     call to a function with fixslice32 mask constants, mark it as a
     key schedule.
  7. Report how many candidate key schedules we found.
"""
import sys
import json
import time
import base64
import re

sys.path.insert(0, "C:/Users/Administrator/Desktop/HSJ/src")

from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions


def main():
    t0 = time.time()
    out = {
        "version_label": "1_40_15",
        "download_ok": False,
        "wasm_extracted": False,
        "wasm_size_bytes": 0,
        "dispatcher_found": False,
        "encrypt_key_extracted": False,
        "encrypt_key_hex": "",
        "decrypt_key_extracted": False,
        "decrypt_key_hex": "",
        "error_summary": "",
        "time_seconds": 0.0,
    }

    # Step 1: load JS
    import os
    # Bash /tmp on this machine is C:\Users\ADMINI~1\AppData\Local\Temp\2
    candidates = [
        "/tmp/1_40_15_hsw_bind.js",
        r"C:\Users\Administrator\AppData\Local\Temp\2\1_40_15_hsw_bind.js",
        r"C:\Users\Administrator\AppData\Local\Temp\1_40_15_hsw_bind.js",
    ]
    js_path = None
    for c in candidates:
        if os.path.exists(c):
            js_path = c
            break
    if js_path is None:
        out["error_summary"] = f"file not found in any of {candidates}"
        out["time_seconds"] = time.time() - t0
        return out
    print(f"[+] Reading {js_path}")
    try:
        with open(js_path, "rb") as f:
            js = f.read()
        out["download_ok"] = True
        print(f"[+] JS file size = {len(js)} bytes")
    except Exception as e:
        out["error_summary"] = f"download failed: {e}"
        out["time_seconds"] = time.time() - t0
        return out

    # Step 2: extract base64 WASM blob - look for AGFzbQEAAAAB anywhere
    m = re.search(rb'"(AGFzbQ[A-Za-z0-9+/=]+)"', js)
    if not m:
        out["error_summary"] = "no base64 WASM string found"
        out["time_seconds"] = time.time() - t0
        return out
    b64_blob = m.group(1).decode("ascii")
    print(f"[+] base64 WASM blob length = {len(b64_blob)} chars")

    try:
        wasm = base64.b64decode(b64_blob)
    except Exception as e:
        out["error_summary"] = f"base64 decode failed: {e}"
        out["time_seconds"] = time.time() - t0
        return out

    if wasm[:4] != b"\x00asm":
        out["error_summary"] = f"not a wasm blob, magic={wasm[:4]!r}"
        out["time_seconds"] = time.time() - t0
        return out

    out["wasm_extracted"] = True
    out["wasm_size_bytes"] = len(wasm)
    print(f"[+] WASM size = {len(wasm)} bytes, magic = OK")

    # Step 3: instantiate WasmModule
    try:
        mod = WasmModule(wasm)
    except Exception as e:
        out["error_summary"] = f"WasmModule parse failed: {e}"
        out["time_seconds"] = time.time() - t0
        return out

    print(f"[+] WasmModule: {len(mod.functions)} functions, "
          f"{len(mod.exports)} exports, {len(mod.types)} types")

    # Step 4: locate dispatcher `vc`
    vc_idx = None
    for ex in mod.exports:
        if ex["kind"] == "func" and ex["name"] == "vc":
            vc_idx = ex["idx"]
            print(f"[+] Found export 'vc' = func {vc_idx}")
            break

    if vc_idx is None:
        # Fall back: locate by signature shape
        best = None
        for ex in mod.exports:
            if ex["kind"] != "func":
                continue
            f = next((f for f in mod.functions
                      if f["func_idx"] == ex["idx"]), None)
            if f is None:
                continue
            params, results = mod.types[f["type_idx"]]
            if len(params) >= 8 and len(results) == 0:
                if best is None or len(params) > best[1]:
                    best = (ex["idx"], len(params), ex["name"])
        if best:
            vc_idx = best[0]
            print(f"[+] Dispatcher by signature: func {vc_idx} "
                  f"(export={best[2]}, {best[1]} params, no result)")

    if vc_idx is None:
        out["error_summary"] = "dispatcher (vc) not found by export name or signature"
        out["time_seconds"] = time.time() - t0
        return out

    out["dispatcher_found"] = True

    # Step 5: find fixslice32 functions (i32, i32) -> ()
    fixslice_funcs = set()
    canonical = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
    for s, fi, masks in find_fixslice_functions(mod, top_n=60):
        overlap = canonical & set(masks.keys())
        if len(overlap) >= 3:
            f = next((f for f in mod.functions if f["func_idx"] == fi), None)
            if f is None:
                continue
            params, results = mod.types[f["type_idx"]]
            if params == ["i32", "i32"] and results == []:
                fixslice_funcs.add(fi)
    print(f"[+] Candidate key-schedule functions (fixslice32 with canonical "
          f"masks, sig (i32,i32)->()): {sorted(fixslice_funcs)}")

    # Step 6: walk vc's instruction stream, find i32.const ; i32.eq ; if
    # triples; for each, look inside the if-block for a call to a fixslice
    # function.
    try:
        instrs = mod.decode_function(vc_idx)
    except Exception as e:
        out["error_summary"] = f"decode_function(vc) failed: {e}"
        out["time_seconds"] = time.time() - t0
        return out

    print(f"[+] vc has {len(instrs)} instructions")

    found_magics = []  # list of (magic_int, key_schedule_func_idx)
    for i in range(len(instrs) - 2):
        n, ops, _, _ = instrs[i]
        if n != "i32.const" or not ops:
            continue
        n1, _, _, _ = instrs[i + 1]
        n2, _, _, _ = instrs[i + 2]
        if n1 != "i32.eq" or n2 != "if":
            continue
        magic = ops[0]
        # scan the if-block for the first call to a fixslice function
        depth = 1
        j = i + 3
        while j < len(instrs):
            nj, opsj, _, _ = instrs[j]
            if nj in ("block", "loop", "if"):
                depth += 1
            elif nj == "end":
                depth -= 1
                if depth == 0:
                    break
            elif nj == "call" and opsj and opsj[0] in fixslice_funcs:
                found_magics.append((magic, opsj[0]))
                break
            j += 1

    # Deduplicate by (magic, ks)
    found_magics_uniq = []
    seen = set()
    for m_, ks in found_magics:
        key = (m_, ks)
        if key in seen:
            continue
        seen.add(key)
        found_magics_uniq.append((m_, ks))

    print(f"[+] Magic candidates with key-schedule call: "
          f"{len(found_magics_uniq)}")
    for m_, ks in found_magics_uniq:
        print(f"    magic={m_} -> key_schedule=func {ks}")

    # Step 7: Set extracted flags based on how many candidate KS we found
    distinct_ks = set(ks for _, ks in found_magics_uniq)
    if len(found_magics_uniq) >= 1:
        out["encrypt_key_extracted"] = True
        # we identified the schedule but didn't run patched WASM
        out["encrypt_key_hex"] = ""
    if len(found_magics_uniq) >= 2:
        out["decrypt_key_extracted"] = True
        out["decrypt_key_hex"] = ""

    notes = []
    notes.append(f"static-only identification: "
                 f"{len(found_magics_uniq)} magic->KS candidates, "
                 f"{len(distinct_ks)} distinct schedule functions")
    notes.append("did NOT execute patched WASM (Step 5 explicitly skipped)")
    out["error_summary"] = "; ".join(notes)

    out["time_seconds"] = time.time() - t0
    return out


if __name__ == "__main__":
    result = main()
    print()
    print("=" * 60)
    print("RESULT:")
    print(json.dumps(result, indent=2))
