"""Static call-graph BFS to identify the n-token AES key-schedule / encrypt.

Approach B from the task: from each export (ec, rc, pc, kc, vc), BFS
through `call` targets up to depth N. Then identify which of the 4
fixslice key-schedule candidates (263, 282, 304, 345) is uniquely
reachable from the kc-path (n-token entry) but NOT from vc.

We also note which GCM/CTR encrypt function is the direct caller of
the n-token key-schedule helper.

GHASH constant search: look for any function that loads the
GHASH polynomial constant 0xE100000000000000 (or 0xE1 in a long const)
in its body.
"""
from __future__ import annotations
import json
import os
import sys
from collections import defaultdict, deque

# Wire up imports
THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, os.path.join(ROOT, "src"))

from hcaptcha.tools.wasm_disasm import WasmModule


WASM_PATH_CANDIDATES = [
    "/tmp/hsw-dcmp/hsw.wasm",
    "/tmp/hsw.wasm",
    os.path.join(ROOT, "data", "archive", "hsw.wasm"),
]


def _load_wasm() -> bytes:
    for p in WASM_PATH_CANDIDATES:
        if os.path.exists(p):
            print(f"[+] loading {p}")
            return open(p, "rb").read()
    raise SystemExit("no hsw.wasm found in known locations")


def _build_call_graph(mod: WasmModule) -> dict[int, set[int]]:
    """Forward call graph: caller -> set(direct callees)."""
    g: dict[int, set[int]] = defaultdict(set)
    for f in mod.functions:
        fi = f["func_idx"]
        instrs = mod.decode_function(fi) or []
        for name, ops, _, _ in instrs:
            if name == "call" and ops:
                g[fi].add(ops[0])
    return g


def _reverse_graph(g: dict[int, set[int]]) -> dict[int, set[int]]:
    r: dict[int, set[int]] = defaultdict(set)
    for c, callees in g.items():
        for x in callees:
            r[x].add(c)
    return r


def _bfs_reachable(g: dict[int, set[int]], src: int, max_depth: int = 8) -> set[int]:
    """BFS forward from src up to max_depth. Returns set of all reachable
    func indices (excluding src itself)."""
    visited = {src}
    layer = {src}
    for _ in range(max_depth):
        nxt = set()
        for n in layer:
            for c in g.get(n, ()):
                if c not in visited:
                    visited.add(c)
                    nxt.add(c)
        layer = nxt
        if not layer:
            break
    visited.discard(src)
    return visited


def _find_ghash_funcs(mod: WasmModule) -> list[int]:
    """Find functions that load 0xE100000000000000 as an i64.const."""
    GHASH_HI = 0xE100000000000000
    GHASH_LO = 0x00000000000000E1
    out = []
    for f in mod.functions:
        instrs = mod.decode_function(f["func_idx"]) or []
        for name, ops, _, _ in instrs:
            if name == "i64.const" and ops:
                v = ops[0] & 0xFFFFFFFFFFFFFFFF
                if v == GHASH_HI or v == GHASH_LO:
                    out.append(f["func_idx"])
                    break
            if name == "i32.const" and ops:
                v = ops[0] & 0xFFFFFFFF
                if v == 0xE1000000:
                    out.append(f["func_idx"])
                    break
    return out


def main():
    wasm = _load_wasm()
    mod = WasmModule(wasm)

    # Map export name -> func_idx
    exp_by_name = {e["name"]: e["idx"] for e in mod.exports if e["kind"] == "func"}
    print(f"[+] total funcs={len(mod.functions)} exports={len(exp_by_name)}")
    interesting_exports = ["ec", "rc", "pc", "kc", "vc", "dc", "fc", "gc",
                           "hc", "ic", "lc", "mc", "nc", "oc", "qc", "sc", "tc", "uc"]
    for name in interesting_exports:
        if name in exp_by_name:
            print(f"    {name} -> fn {exp_by_name[name]}")

    g = _build_call_graph(mod)

    # Load labels to know which funcs are key-schedule candidates
    labels_path = os.path.join(ROOT, "docs", "hsw_function_labels.json")
    labels = json.load(open(labels_path))
    ks_candidates = [f["func_idx"] for f in labels["functions"]
                     if f.get("role") == "aes_key_schedule"]
    print(f"[+] aes_key_schedule candidates: {ks_candidates}")

    # Reachable set from each export (depth 10 to capture deep chains)
    reach: dict[str, set[int]] = {}
    for name in interesting_exports:
        if name in exp_by_name:
            reach[name] = _bfs_reachable(g, exp_by_name[name], max_depth=12)

    print(f"\n[+] Key-schedule reachability matrix:")
    print(f"    {'fn':>6} | " + " ".join(f"{n:>4}" for n in reach.keys()))
    for ks in ks_candidates:
        row = []
        for name in reach.keys():
            row.append("YES " if ks in reach[name] else "  . ")
        print(f"    {ks:>6} | " + " ".join(row))

    # Cross-check: which KS funcs are reachable from kc (n-token) but
    # NOT from vc? Those are the n-token-specific key schedule.
    print(f"\n[+] N-TOKEN candidates (reachable from kc but NOT from vc):")
    kc_set = reach.get("kc", set())
    vc_set = reach.get("vc", set())
    ec_set = reach.get("ec", set())
    pc_set = reach.get("pc", set())
    only_kc = [ks for ks in ks_candidates if ks in kc_set and ks not in vc_set]
    print(f"    reachable from kc only: {only_kc}")
    only_vc = [ks for ks in ks_candidates if ks in vc_set and ks not in kc_set]
    print(f"    reachable from vc only: {only_vc}")
    in_both = [ks for ks in ks_candidates if ks in kc_set and ks in vc_set]
    print(f"    reachable from both:    {in_both}")
    in_neither = [ks for ks in ks_candidates if ks not in kc_set and ks not in vc_set]
    print(f"    in neither:             {in_neither}")

    # For each KS candidate, find its direct callers (presumed encrypt-or-decrypt funcs)
    print(f"\n[+] Direct callers of each KS candidate:")
    r = _reverse_graph(g)
    for ks in ks_candidates:
        callers = sorted(r.get(ks, set()))
        sig = next((mod.types[mod.functions[i - len(mod.imports)]["type_idx"] if False else f["type_idx"]]
                    for f in mod.functions if f["func_idx"] == ks), None)
        # safer: just look up by func_idx
        f = next((ff for ff in mod.functions if ff["func_idx"] == ks), None)
        ty = mod.types[f["type_idx"]] if f else None
        print(f"    fn {ks} ({ty}): callers={callers}")

    # GHASH polynomial search
    ghash_funcs = _find_ghash_funcs(mod)
    print(f"\n[+] GHASH polynomial bearers (functions loading 0xE1000...): {ghash_funcs}")

    # For each ghash func, list its callers (probable GCM auth)
    for gf in ghash_funcs:
        callers = sorted(r.get(gf, set()))
        print(f"    fn {gf} callers={callers}")

    # AES-related: also list aes_round funcs and their reachability
    aes_round_funcs = [f["func_idx"] for f in labels["functions"]
                       if f.get("role") == "aes_round"]
    print(f"\n[+] aes_round funcs: {aes_round_funcs}")
    print(f"\n[+] AES-round reachability (kc vs vc):")
    for ar in aes_round_funcs:
        in_kc = ar in kc_set
        in_vc = ar in vc_set
        print(f"    fn {ar}: kc={in_kc} vc={in_vc} ec={ar in ec_set} pc={ar in pc_set}")

    # Final summary: best n-token KS candidate
    print(f"\n[+] CONCLUSION:")
    if len(only_kc) == 1:
        ks_n = only_kc[0]
        callers = sorted(r.get(ks_n, set()))
        print(f"    N-token KS: fn {ks_n}")
        print(f"    Direct caller(s) (= encrypt wrapper): {callers}")
        # The caller of the caller (encrypt entry point):
        for c in callers:
            cc = sorted(r.get(c, set()))
            print(f"    Callers of {c}: {cc}")
    elif len(only_kc) > 1:
        print(f"    AMBIGUOUS: {only_kc} all reachable from kc-only.")
    else:
        print(f"    NO KS unique to kc-path. KS shared with vc or unreachable.")
        # fallback — check pc path (the docstring mentioned pc as the wrapper)
        only_pc = [ks for ks in ks_candidates if ks in pc_set and ks not in vc_set]
        print(f"    reachable from pc but not vc: {only_pc}")
        only_ec = [ks for ks in ks_candidates if ks in ec_set and ks not in vc_set]
        print(f"    reachable from ec but not vc: {only_ec}")


if __name__ == "__main__":
    main()
