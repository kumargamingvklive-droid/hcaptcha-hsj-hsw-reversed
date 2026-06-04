"""Backtest harness for the HSW key extractor's STATIC identification logic.

For each archived HSW bundle in
    https://github.com/Implex-ltd/hcaptcha-reverse/tree/main/archive/hsw

this script:
  1. Downloads the JS file (raw, via gh CLI).
  2. Extracts the embedded base64 WASM blob (matches `AGFzbQ[base64]+`).
  3. Decodes + parses the WASM via `hcaptcha.tools.wasm_disasm.WasmModule`.
  4. Runs the structural identification logic from `hcaptcha.hsw`:
       a. Find the `vc` export (or fall back to >=8-arg, no-result signature).
       b. Score every function for fixslice32 AES masks (canonical:
          0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0); restrict to
          (i32,i32)->() — those are the key-schedule candidates.
       c. Walk `vc`'s instruction stream; find every
          `i32.const MAGIC ; i32.eq ; if` triple, then inside the if-block
          look for a `call N` where N is a key-schedule candidate.
  5. Records per-version:
        wasm_size, vc_export_present, fixslice_candidate_count,
        magic_if_fixslice_triples, distinct_key_schedule_funcs,
        identifiable (final boolean).

We do NOT run the patched WASM. The point is to document which archived
builds the modern extractor would WORK on structurally vs which would
need a different strategy.

Outputs:
  C:/Users/Administrator/Desktop/HSJ/tools/backtest_report.json
  C:/Users/Administrator/Desktop/HSJ/tools/backtest_report.md
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout

# Make hcaptcha importable.
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from hcaptcha.tools.wasm_disasm import WasmModule, find_fixslice_functions


def _find_gh_cli() -> str:
    # /tmp/gh-cli/bin/gh.exe under git-bash maps to %TEMP%/gh-cli/bin/gh.exe.
    # Python (Windows) can't see the /tmp/ path directly, so resolve it.
    candidates = [
        r"/tmp/gh-cli/bin/gh.exe",
        os.path.join(os.environ.get("TEMP", ""), "gh-cli", "bin", "gh.exe"),
        os.path.join(os.environ.get("TMP",  ""), "gh-cli", "bin", "gh.exe"),
        r"C:\Users\Administrator\AppData\Local\Temp\gh-cli\bin\gh.exe",
        r"C:\Users\Administrator\AppData\Local\Temp\2\gh-cli\bin\gh.exe",
        "gh.exe",
        "gh",
    ]
    for c in candidates:
        if c and (os.path.isfile(c) or c in ("gh", "gh.exe")):
            return c
    raise RuntimeError(f"gh CLI not found in any of: {candidates}")


GH_CLI = _find_gh_cli()
REPO = "Implex-ltd/hcaptcha-reverse"
ARCHIVE_DIR = "archive/hsw"

CACHE_DIR = os.path.join(HERE, "_archive_cache")
REPORT_JSON = os.path.join(HERE, "backtest_report.json")
REPORT_MD = os.path.join(HERE, "backtest_report.md")

CANONICAL_FIXSLICE_MASKS = {0x55555555, 0x33333333, 0x0F0F0F0F, 0xF0F0F0F0}
PER_VERSION_TIMEOUT_S = 90.0
DOWNLOAD_TIMEOUT_S = 60.0


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def list_archive_files() -> list[str]:
    """List all *_hsw_bind.js entries in the archive via the gh CLI."""
    raw = subprocess.check_output(
        [GH_CLI, "api", f"repos/{REPO}/contents/{ARCHIVE_DIR}"],
        timeout=DOWNLOAD_TIMEOUT_S,
    )
    entries = json.loads(raw)
    names = sorted(
        e["name"] for e in entries
        if e.get("type") == "file" and e["name"].endswith("_hsw_bind.js")
    )
    return names


def download_file(name: str) -> str:
    """Download `archive/hsw/<name>` via gh CLI to cache; return local path."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    local = os.path.join(CACHE_DIR, name)
    if os.path.exists(local) and os.path.getsize(local) > 1024:
        return local
    out = subprocess.check_output(
        [GH_CLI, "api", f"repos/{REPO}/contents/{ARCHIVE_DIR}/{name}",
         "--jq", ".content"],
        timeout=DOWNLOAD_TIMEOUT_S,
    )
    # gh returns base64; some files may be too large for the contents API.
    b64 = out.decode("ascii", errors="ignore").strip().replace("\n", "")
    if not b64:
        # Fall back: raw URL via gh's raw endpoint
        raw_url = (f"repos/{REPO}/contents/{ARCHIVE_DIR}/{name}"
                   "?ref=main")
        # Try the download_url-based fetch
        meta = json.loads(subprocess.check_output(
            [GH_CLI, "api", raw_url], timeout=DOWNLOAD_TIMEOUT_S))
        dl = meta.get("download_url")
        if not dl:
            raise RuntimeError("no content + no download_url")
        # Use gh's `api` for the raw URL via curl-like fallback.
        raw = subprocess.check_output(
            [GH_CLI, "api", f"--hostname", "raw.githubusercontent.com",
             dl.replace("https://raw.githubusercontent.com/", "")],
            timeout=DOWNLOAD_TIMEOUT_S,
        )
        with open(local, "wb") as f:
            f.write(raw)
        return local
    try:
        data = base64.b64decode(b64)
    except Exception as e:
        raise RuntimeError(f"base64 decode of contents API failed: {e}")
    with open(local, "wb") as f:
        f.write(data)
    return local


# ---------------------------------------------------------------------------
# WASM extraction from the JS shim
# ---------------------------------------------------------------------------
WASM_B64_RE = re.compile(rb'"(AGFzbQ[A-Za-z0-9+/]+=*)"')


def extract_wasm(js_bytes: bytes) -> bytes:
    """Find and base64-decode the embedded WASM blob.

    Older archives use `const CUSTOMWASM = "AGFzbQ..."`. Some variants
    may use a different variable name, so we just look for the base64
    prefix `AGFzbQ` (== `\\x00asm\\x01`) inside any double-quoted string.
    """
    # Pick the LONGEST candidate — there can be smaller base64 strings
    # elsewhere; the WASM is by far the biggest.
    candidates = WASM_B64_RE.findall(js_bytes)
    if not candidates:
        raise RuntimeError("no base64 WASM string (AGFzbQ...) found in JS")
    b64 = max(candidates, key=len).decode("ascii")
    wasm = base64.b64decode(b64)
    if wasm[:4] != b"\x00asm":
        raise RuntimeError(f"decoded blob is not WASM, magic={wasm[:4]!r}")
    return wasm


# ---------------------------------------------------------------------------
# Structural identification (mirrors hcaptcha.hsw)
# ---------------------------------------------------------------------------
def find_dispatcher(mod: WasmModule) -> tuple[int | None, str]:
    """Return (func_idx, mode) where mode is one of:
        'vc-export'   — found the literal vc export
        'signature'   — fallback by export signature (>=8 i32 args, no result)
        ''            — not found
    """
    for ex in mod.exports:
        if ex["kind"] == "func" and ex["name"] == "vc":
            return ex["idx"], "vc-export"
    # Fall back by signature
    for ex in mod.exports:
        if ex["kind"] != "func":
            continue
        f = next((f for f in mod.functions if f["func_idx"] == ex["idx"]), None)
        if f is None:
            continue
        params, results = mod.types[f["type_idx"]]
        if len(params) >= 8 and len(results) == 0:
            return ex["idx"], f"signature:{ex['name']}"
    return None, ""


def find_key_schedule_candidates(mod: WasmModule) -> dict[int, dict]:
    """Return {func_idx: {masks: {hex_mask: count}, mask_count: N}}.

    A candidate must:
      * have canonical fixslice32 mask overlap >= 3
      * be of type (i32, i32) -> ()
    """
    out = {}
    for s, fi, masks in find_fixslice_functions(mod, top_n=60):
        overlap = CANONICAL_FIXSLICE_MASKS & set(masks.keys())
        if len(overlap) < 3:
            continue
        f = next((f for f in mod.functions if f["func_idx"] == fi), None)
        if f is None:
            continue
        params, results = mod.types[f["type_idx"]]
        if params == ["i32", "i32"] and results == []:
            out[fi] = {
                "score":        s,
                "masks":        {f"0x{k:08x}": v for k, v in masks.items()},
                "canonical_overlap": len(overlap),
            }
    return out


def find_magic_triples(mod: WasmModule, vc_idx: int,
                       ks_candidates: set[int]) -> list[dict]:
    """Walk vc and collect every (i32.const MAGIC; i32.eq; if) triple
    whose if-block contains a `call N` for N in ks_candidates."""
    try:
        instrs = mod.decode_function(vc_idx)
    except Exception:
        return []
    if not instrs:
        return []

    found = []
    seen = set()
    for i in range(len(instrs) - 2):
        n, ops, _, _ = instrs[i]
        if n != "i32.const" or not ops:
            continue
        n1, _, _, _ = instrs[i + 1]
        n2, _, _, _ = instrs[i + 2]
        if n1 != "i32.eq" or n2 != "if":
            continue
        magic = ops[0]
        # Walk the if-block to its matching end; record the first call to
        # a key-schedule candidate.
        depth = 1
        j = i + 3
        ks_hit = None
        while j < len(instrs):
            nj, opsj, _, _ = instrs[j]
            if nj in ("block", "loop", "if"):
                depth += 1
            elif nj == "end":
                depth -= 1
                if depth == 0:
                    break
            elif nj == "call" and opsj and opsj[0] in ks_candidates:
                ks_hit = opsj[0]
                break
            j += 1
        if ks_hit is None:
            continue
        key = (magic, ks_hit)
        if key in seen:
            continue
        seen.add(key)
        # Unsigned representation as well, for readability.
        u_magic = magic & 0xFFFFFFFF
        found.append({
            "magic_signed":   magic,
            "magic_unsigned": u_magic,
            "magic_hex":      f"0x{u_magic:08x}",
            "key_schedule":   ks_hit,
            "vc_offset":      instrs[i][2],
        })
    return found


# ---------------------------------------------------------------------------
# Per-version backtest
# ---------------------------------------------------------------------------
def backtest_one(name: str) -> dict:
    label = name.replace("_hsw_bind.js", "")
    rec = {
        "version_label":          label,
        "archive_name":           name,
        "download_ok":            False,
        "js_size":                0,
        "wasm_extracted":         False,
        "wasm_size":              0,
        "wasm_parsed":            False,
        "n_functions":            0,
        "n_exports":              0,
        "vc_export_present":      False,
        "dispatcher_mode":        "",
        "dispatcher_idx":         None,
        "key_sched_candidates":   0,
        "key_sched_func_idxs":    [],
        "magic_if_fixslice_triples": 0,
        "distinct_key_sched_called": 0,
        "triples_detail":         [],
        "identifiable":           False,
        "error":                  "",
        "elapsed_s":              0.0,
    }
    t0 = time.time()

    try:
        # 1. Download
        path = download_file(name)
        rec["download_ok"] = True
        with open(path, "rb") as f:
            js = f.read()
        rec["js_size"] = len(js)

        # 2. Extract WASM
        wasm = extract_wasm(js)
        rec["wasm_extracted"] = True
        rec["wasm_size"] = len(wasm)

        # 3. Parse
        mod = WasmModule(wasm)
        rec["wasm_parsed"] = True
        rec["n_functions"] = len(mod.functions)
        rec["n_exports"] = len(mod.exports)

        # 4a. Dispatcher — try vc export first, fall back to signature shape
        vc_idx, mode = find_dispatcher(mod)
        rec["dispatcher_mode"] = mode
        rec["dispatcher_idx"] = vc_idx
        rec["vc_export_present"] = mode == "vc-export"

        # Diagnostic: max-arg export, so we can tell pre-dispatcher builds
        # apart from post-dispatcher ones at a glance.
        max_args = 0
        max_args_name = ""
        for ex in mod.exports:
            if ex["kind"] != "func":
                continue
            f = next((f for f in mod.functions if f["func_idx"] == ex["idx"]), None)
            if f is None:
                continue
            params, results = mod.types[f["type_idx"]]
            if len(params) > max_args and len(results) == 0:
                max_args = len(params)
                max_args_name = ex["name"]
        rec["max_export_arg_count_void_ret"] = max_args
        rec["max_export_name_void_ret"] = max_args_name

        # 4b. Key-schedule candidates — works regardless of dispatcher.
        ks_map = find_key_schedule_candidates(mod)
        rec["key_sched_candidates"] = len(ks_map)
        rec["key_sched_func_idxs"] = sorted(ks_map.keys())

        if vc_idx is None:
            # No dispatcher of the modern shape. Report fixslice candidates
            # so the failure mode is clear: the build uses a different
            # entry-point architecture; the magic-table approach doesn't
            # apply.
            rec["error"] = (
                "no dispatcher: largest export takes "
                f"{max_args} args (modern HSW vc takes >=8). "
                "This pre-dispatcher build needs a different extractor strategy."
            )
            return rec

        # 4c. Magic / fixslice triples in vc
        triples = find_magic_triples(mod, vc_idx, set(ks_map.keys()))
        rec["magic_if_fixslice_triples"] = len(triples)
        rec["distinct_key_sched_called"] = len({t["key_schedule"] for t in triples})
        # Keep the top 8 to bound output
        rec["triples_detail"] = triples[:8]

        rec["identifiable"] = (
            rec["vc_export_present"]
            and rec["key_sched_candidates"] >= 1
            and rec["magic_if_fixslice_triples"] >= 2
            and rec["distinct_key_sched_called"] >= 1
        )
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        rec["_traceback"] = traceback.format_exc(limit=4)
    finally:
        rec["elapsed_s"] = round(time.time() - t0, 3)
    return rec


def backtest_one_with_timeout(name: str) -> dict:
    """Run backtest_one with a per-version timeout."""
    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(backtest_one, name)
        try:
            return fut.result(timeout=PER_VERSION_TIMEOUT_S)
        except FutTimeout:
            return {
                "version_label": name.replace("_hsw_bind.js", ""),
                "archive_name":  name,
                "error":         f"timeout after {PER_VERSION_TIMEOUT_S}s",
                "elapsed_s":     PER_VERSION_TIMEOUT_S,
                "identifiable":  False,
            }
        except Exception as e:
            return {
                "version_label": name.replace("_hsw_bind.js", ""),
                "archive_name":  name,
                "error":         f"executor: {type(e).__name__}: {e}",
                "identifiable":  False,
            }


# ---------------------------------------------------------------------------
# Markdown summary
# ---------------------------------------------------------------------------
def render_markdown(report: dict) -> str:
    rows = report["versions"]
    headers = [
        "version", "wasm KB", "funcs", "vc export",
        "max-arg void export", "AES KS candidates",
        "magic+if+fixslice triples", "distinct KS called",
        "identifiable",
    ]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for r in rows:
        wasm_kb = f"{r.get('wasm_size', 0) / 1024:.1f}" if r.get("wasm_size") else "-"
        max_arg_disp = "-"
        if r.get("max_export_arg_count_void_ret"):
            # Wasm-bindgen mangled names can be 100+ chars; clip to keep
            # the table legible.
            nm = r["max_export_name_void_ret"]
            if len(nm) > 22:
                nm = nm[:10] + "..." + nm[-8:]
            max_arg_disp = f"{nm}({r['max_export_arg_count_void_ret']})"
        cells = [
            r["version_label"],
            wasm_kb,
            str(r.get("n_functions", 0)),
            "yes" if r.get("vc_export_present") else "no",
            max_arg_disp,
            str(r.get("key_sched_candidates", 0)),
            str(r.get("magic_if_fixslice_triples", 0)),
            str(r.get("distinct_key_sched_called", 0)),
            "PASS" if r.get("identifiable") else "FAIL",
        ]
        lines.append("| " + " | ".join(cells) + " |")

    # Add a notes/failure-mode column as a separate block (some errors are long)
    lines.append("")
    lines.append("### Failure notes\n")
    for r in rows:
        if not r.get("identifiable") and r.get("error"):
            lines.append(f"- **{r['version_label']}**: {r['error']}")

    summary = (
        f"\n**Summary**: {report['n_pass']} / {report['n_total']} archives "
        f"are structurally identifiable by the modern extractor "
        f"(WASM extracted + vc export + >=1 KS candidate + >=2 magic/if/fixslice triples).\n"
    )
    return (
        "# HSW backtest report\n\n"
        f"Generated: {report['generated_at']}\n"
        f"Source: github.com/{REPO}/tree/main/{ARCHIVE_DIR}\n"
        + summary + "\n"
        + "\n".join(lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    t0 = time.time()
    print(f"[+] Listing archive files via gh CLI...")
    try:
        names = list_archive_files()
    except subprocess.CalledProcessError as e:
        print(f"[!] gh CLI failed: {e}")
        sys.exit(2)
    print(f"[+] Found {len(names)} archive(s): "
          f"{', '.join(n.replace('_hsw_bind.js','') for n in names)}")

    results = []
    for i, name in enumerate(names, 1):
        label = name.replace("_hsw_bind.js", "")
        print(f"\n[{i:>2}/{len(names)}] {label}  ...")
        rec = backtest_one_with_timeout(name)
        if rec.get("identifiable"):
            print(f"        PASS  wasm={rec.get('wasm_size',0)}B "
                  f"KS={rec.get('key_sched_candidates',0)} "
                  f"triples={rec.get('magic_if_fixslice_triples',0)}")
        else:
            print(f"        FAIL  err={rec.get('error','-')[:90]}")
        results.append(rec)

    n_pass = sum(1 for r in results if r.get("identifiable"))
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repo":         REPO,
        "archive_dir":  ARCHIVE_DIR,
        "n_total":      len(results),
        "n_pass":       n_pass,
        "elapsed_s":    round(time.time() - t0, 2),
        "versions":     results,
    }

    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    md = render_markdown(report)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"\n[+] {n_pass}/{len(results)} archives structurally identifiable")
    print(f"[+] JSON report -> {REPORT_JSON}")
    print(f"[+] Markdown    -> {REPORT_MD}")
    print()
    print(md)


if __name__ == "__main__":
    main()
