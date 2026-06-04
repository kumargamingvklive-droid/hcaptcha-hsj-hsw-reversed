"""Backtest HSW key-extractor structural identification on archived 1_40_31."""
import base64
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions


JS_PATH = r"C:\Users\ADMINI~1\AppData\Local\Temp\2\1_40_31_hsw_bind.js"


def extract_wasm_b64(js_text: str) -> bytes:
    """Find the largest base64 blob that decodes to a WASM module
    (starts with magic \\x00asm\\x01\\x00\\x00\\x00)."""
    # 1) Try direct named-CUSTOMWASM regex
    m = re.search(r'CUSTOMWASM\s*=\s*"([A-Za-z0-9+/=]+)"', js_text)
    candidates = []
    if m:
        candidates.append(m.group(1))
    # 2) Otherwise scan for huge base64 strings starting AGFzbQ (= "\0asm" in b64)
    for hit in re.findall(r'"(AGFzbQ[A-Za-z0-9+/=]{4096,})"', js_text):
        candidates.append(hit)
    for cand in candidates:
        try:
            raw = base64.b64decode(cand)
        except Exception:
            continue
        if raw[:4] == b"\x00asm":
            return raw
    raise RuntimeError("no WASM blob found in JS bundle")


def main():
    t0 = time.time()
    result = {
        "version_label": "1_40_31",
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

    # Step 1: file present?
    if not os.path.exists(JS_PATH):
        result["error_summary"] = f"JS file missing at {JS_PATH}"
        result["time_seconds"] = round(time.time() - t0, 3)
        print(json.dumps(result, indent=2))
        return
    result["download_ok"] = True

    js_text = open(JS_PATH, "r", encoding="utf-8", errors="replace").read()
    print(f"[+] JS bundle: {len(js_text)} chars", file=sys.stderr)

    # Step 2: extract WASM
    try:
        wasm = extract_wasm_b64(js_text)
        result["wasm_extracted"] = True
        result["wasm_size_bytes"] = len(wasm)
        print(f"[+] WASM extracted: {len(wasm)} bytes (magic={wasm[:8].hex()})",
              file=sys.stderr)
    except Exception as e:
        result["error_summary"] = f"wasm extract failed: {e}"
        result["time_seconds"] = round(time.time() - t0, 3)
        print(json.dumps(result, indent=2))
        return

    # Step 3: instantiate WasmModule, find dispatcher
    try:
        mod = WasmModule(wasm)
        print(f"[+] WASM module: {len(mod.functions)} funcs, "
              f"{len(mod.exports)} exports, {len(mod.types)} types",
              file=sys.stderr)

        vc_idx = None
        for ex in mod.exports:
            if ex["kind"] == "func" and ex["name"] == "vc":
                vc_idx = ex["idx"]
                break
        if vc_idx is None:
            # signature-shape fallback: exported func with >=8 i32 params, 0 results
            for ex in mod.exports:
                if ex["kind"] != "func":
                    continue
                f = next((f for f in mod.functions
                          if f["func_idx"] == ex["idx"]), None)
                if f is None:
                    continue
                params, results = mod.types[f["type_idx"]]
                if (len(params) >= 8 and len(results) == 0
                        and all(p == "i32" for p in params)):
                    vc_idx = ex["idx"]
                    break

        # Find strict-canonical fixslice32 (i32,i32)->() candidates first;
        # these are the AES key-schedule functions regardless of dispatcher.
        canonical = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
        fixslice_with_size = {}
        for s, fi, masks in find_fixslice_functions(mod, top_n=60):
            overlap = canonical & set(masks.keys())
            if len(overlap) >= 3:
                f = next((f for f in mod.functions
                          if f["func_idx"] == fi), None)
                if f is None:
                    continue
                params, results = mod.types[f["type_idx"]]
                if params == ["i32", "i32"] and results == []:
                    fixslice_with_size[fi] = f["code_end"] - f["code_start"]
        print(f"[+] fixslice32 (i32,i32)->() candidates: {sorted(fixslice_with_size.keys())}",
              file=sys.stderr)

        if vc_idx is None:
            # OLD BUILD: no named 'vc' export, no >=8 i32 args, 0-result
            # dispatcher.  Try a broader fallback: scan EVERY function for
            # magic-eq-if triples that call a fixslice ks func.  The function
            # with the most such triples is the dispatcher.
            best = (-1, None, {})
            for f in mod.functions:
                instrs = mod.decode_function(f["func_idx"]) or []
                magic_map = {}
                for i in range(len(instrs) - 2):
                    n, ops, _, _ = instrs[i]
                    if n != "i32.const" or not ops: continue
                    n1, _, _, _ = instrs[i+1]
                    n2, _, _, _ = instrs[i+2]
                    if n1 != "i32.eq" or n2 != "if": continue
                    depth = 1; j = i + 3
                    while j < len(instrs):
                        nj, opsj, _, _ = instrs[j]
                        if nj in ("block", "loop", "if"): depth += 1
                        elif nj == "end":
                            depth -= 1
                            if depth == 0: break
                        elif nj == "call" and opsj and opsj[0] in fixslice_with_size:
                            magic_map.setdefault(ops[0], opsj[0])
                            break
                        j += 1
                if len(magic_map) > best[0]:
                    best = (len(magic_map), f["func_idx"], magic_map)
            if best[0] >= 1:
                vc_idx = best[1]
                magic_to_ks = best[2]
                result["dispatcher_found"] = True
                print(f"[+] FALLBACK dispatcher = func {vc_idx} "
                      f"({best[0]} magic->ks via brute scan)", file=sys.stderr)
            else:
                # No magic-eq-if dispatcher pattern at all.  Treat as
                # dispatcher_found=False but DON'T early-return — we still
                # have the raw fixslice32 key-schedule candidates.
                magic_to_ks = {}
                print(f"[-] no magic-eq-if->fixslice pattern in any function; "
                      f"raw ks candidates remain: {sorted(fixslice_with_size.keys())}",
                      file=sys.stderr)
        else:
            result["dispatcher_found"] = True
            print(f"[+] dispatcher vc = func {vc_idx}", file=sys.stderr)
            instrs = mod.decode_function(vc_idx)
            magic_to_ks = {}
            for i in range(len(instrs) - 2):
                n, ops, _, _ = instrs[i]
                if n != "i32.const" or not ops: continue
                n1, _, _, _ = instrs[i+1]
                n2, _, _, _ = instrs[i+2]
                if n1 != "i32.eq" or n2 != "if": continue
                depth = 1; j = i + 3
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

        print(f"[+] magic -> key_schedule map: {magic_to_ks}", file=sys.stderr)

        # Also record raw fixslice candidates as a secondary signal of
        # "key schedule found" even if we couldn't tie them to magics.
        raw_ks_count = len(fixslice_with_size)

        # Per task Step 5: set encrypt_key_extracted / decrypt_key_extracted
        # based on whether 1 / 2+ candidate key schedules were found.
        # Prefer magic->ks pairs (precise) but fall back to raw fixslice
        # function count (structural-only) if no magics tied in.
        items = list(magic_to_ks.items())
        ks_found = len(items) if items else raw_ks_count

        if ks_found >= 1:
            result["encrypt_key_extracted"] = True
            if items:
                m_enc, ks_enc = items[0]
                result["encrypt_key_hex"] = (
                    f"<static:magic={m_enc & 0xffffffff:#x},ks_func={ks_enc}>"
                )
            else:
                ks_list = sorted(fixslice_with_size.keys())
                result["encrypt_key_hex"] = (
                    f"<static:ks_func={ks_list[0]} (fixslice candidate)>"
                )
        if ks_found >= 2:
            result["decrypt_key_extracted"] = True
            if len(items) >= 2:
                m_dec, ks_dec = items[1]
                result["decrypt_key_hex"] = (
                    f"<static:magic={m_dec & 0xffffffff:#x},ks_func={ks_dec}>"
                )
            else:
                ks_list = sorted(fixslice_with_size.keys())
                result["decrypt_key_hex"] = (
                    f"<static:ks_func={ks_list[1]} (fixslice candidate)>"
                )

        notes = []
        if not items:
            notes.append(
                f"no magic-eq-if->fixslice triple found in any function; "
                f"raw fixslice32 candidates: {sorted(fixslice_with_size.keys())}"
            )
        else:
            notes.append(f"magic->ks: {magic_to_ks}")
        notes.append("key bytes not extracted (Step 5 skipped: no JS sandbox)")
        result["error_summary"] = "; ".join(notes)

    except Exception as e:
        import traceback
        traceback.print_exc()
        result["error_summary"] = f"disasm failed: {e}"

    result["time_seconds"] = round(time.time() - t0, 3)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
