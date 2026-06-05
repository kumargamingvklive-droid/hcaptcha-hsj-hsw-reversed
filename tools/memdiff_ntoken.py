"""memdiff_ntoken.py — pure memory-diff approach to locate the n-token PLAINTEXT.

Strategy
--------
1. Boot the HSWBridge sandbox (loads hsw.js + WASM, ~1.2 MB linear memory).
2. Snapshot the entire `__hsw_memory.buffer` BEFORE calling window.hsw(jwt).
3. Call `window.hsw(jwt)` and capture the resulting token (base64 string).
4. Snapshot memory AFTER the call.
5. Diff byte-by-byte. Coalesce changed bytes into contiguous "dirty regions"
   (with a small gap-tolerance so a region with 1-2 unchanged bytes in the
   middle isn't split into many tiny pieces).
6. For each dirty region: compute Shannon byte-entropy + look for msgpack/JSON
   shape markers + check overlap with the ciphertext bytes of the returned
   token.

Lowest-entropy dirty regions that look structured (msgpack/JSON marker, ASCII
density, etc.) are the prime candidates for the encrypt INPUT plaintext.

For efficiency: the diff is computed *inside JS* so we only ship the dirty
regions back to Python (typical: a few KB instead of 1.2 MB each direction).
The full pre-snapshot is also kept in JS as `globalThis.__snap_pre` and can
be re-streamed back if needed (we stream the largest dirty region as hex).
"""
from __future__ import annotations
import base64
import json
import math
import os
import sys
import time
from collections import Counter

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, os.path.join(ROOT, "src"))

from hcaptcha.hsw_bridge import HSWBridge, _wait_done  # noqa: E402
from hcaptcha import version as _v  # noqa: E402


# ---------------------------------------------------------------------------
# JS snippets
# ---------------------------------------------------------------------------
_SNAP_PRE_JS = r"""
(() => {
    if (!globalThis.__hsw_memory) throw new Error("no __hsw_memory");
    const u8 = new Uint8Array(globalThis.__hsw_memory.buffer);
    // Stash a *copy* — buffer is live and changes during hsw(jwt).
    globalThis.__snap_pre = new Uint8Array(u8.length);
    globalThis.__snap_pre.set(u8);
    return globalThis.__snap_pre.length;
})();
"""


# Compute the diff inside JS so we don't ship 1.2 MB twice.
# Returns: { size, n_dirty_regions, regions: [{off, len, entropy*1000, head_hex}] }
# We do gap-merge so two changed runs separated by <= gap_tolerance unchanged
# bytes coalesce into one region.
_DIFF_JS_TPL = r"""
(() => {
    if (!globalThis.__snap_pre) throw new Error("no __snap_pre");
    if (!globalThis.__hsw_memory) throw new Error("no __hsw_memory");
    const a = globalThis.__snap_pre;
    const b = new Uint8Array(globalThis.__hsw_memory.buffer);
    if (a.length !== b.length) throw new Error(
        "buffer grew: pre=" + a.length + " post=" + b.length
    );
    const N = a.length;
    const GAP = __GAP__;
    const MAX_HEAD = 64;

    // First pass: produce a bitmap-like array of dirty offsets, but more
    // efficiently — walk and emit regions in one pass.
    const regions = [];
    let i = 0;
    while (i < N) {
        if (a[i] !== b[i]) {
            // start of a region
            let start = i;
            let end = i;
            i++;
            // extend while we keep finding diffs within GAP unchanged bytes
            while (i < N) {
                if (a[i] !== b[i]) {
                    end = i;
                    i++;
                } else {
                    // look ahead up to GAP for another diff
                    let look = 1;
                    let found = false;
                    while (look <= GAP && i + look < N) {
                        if (a[i + look] !== b[i + look]) {
                            found = true;
                            break;
                        }
                        look++;
                    }
                    if (!found) break;
                    // jump forward to that diff
                    i += look;
                    end = i;
                    i++;
                }
            }
            const len = end - start + 1;
            // Compute byte histogram + sha-style head from POST buffer
            const hist = new Uint32Array(256);
            for (let k = 0; k < len; k++) hist[b[start + k]]++;
            // Shannon entropy * 1000 (so we can ship as int)
            let H = 0;
            for (let v = 0; v < 256; v++) {
                if (hist[v] === 0) continue;
                const p = hist[v] / len;
                H -= p * Math.log2(p);
            }
            // head_hex of POST bytes
            const headLen = Math.min(MAX_HEAD, len);
            let head = "";
            for (let k = 0; k < headLen; k++) {
                head += b[start + k].toString(16).padStart(2, "0");
            }
            // tail_hex of POST bytes
            const tailLen = Math.min(MAX_HEAD, len);
            let tail = "";
            for (let k = len - tailLen; k < len; k++) {
                tail += b[start + k].toString(16).padStart(2, "0");
            }
            regions.push({
                off: start,
                len: len,
                entropy_milli: Math.round(H * 1000),
                head_hex: head,
                tail_hex: tail,
            });
        } else {
            i++;
        }
    }
    return JSON.stringify({
        size: N,
        n_dirty_regions: regions.length,
        regions: regions,
    });
})();
"""


# Fetch a specific region of the POST snapshot back as hex.
_FETCH_REGION_JS_TPL = r"""
(() => {
    const b = new Uint8Array(globalThis.__hsw_memory.buffer);
    const off = __OFF__;
    const len = __LEN__;
    let h = "";
    for (let i = 0; i < len; i++) h += b[off + i].toString(16).padStart(2, "0");
    return h;
})();
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _entropy(buf: bytes) -> float:
    if not buf:
        return 0.0
    c = Counter(buf)
    n = len(buf)
    return -sum((v / n) * math.log2(v / n) for v in c.values())


def _looks_msgpack(buf: bytes) -> tuple[bool, str]:
    """Detect msgpack root container in the first few bytes."""
    if not buf:
        return False, ""
    b0 = buf[0]
    # fixmap (0x80–0x8f) — 0–15 pairs
    if 0x80 <= b0 <= 0x8f:
        return True, f"fixmap[{b0 & 0x0f}]"
    # fixarray (0x90–0x9f)
    if 0x90 <= b0 <= 0x9f:
        return True, f"fixarray[{b0 & 0x0f}]"
    # map16 / map32
    if b0 == 0xde:
        return True, "map16"
    if b0 == 0xdf:
        return True, "map32"
    # array16/array32
    if b0 == 0xdc:
        return True, "array16"
    if b0 == 0xdd:
        return True, "array32"
    return False, ""


def _looks_json(buf: bytes) -> bool:
    if not buf:
        return False
    return buf[0] in (0x7b, 0x5b, 0x22)  # { [ "


def _ascii_density(buf: bytes) -> float:
    if not buf:
        return 0.0
    return sum(1 for c in buf if 0x20 <= c < 0x7f or c in (0x09, 0x0a, 0x0d)) / len(buf)


def _find_ct_overlap(region_bytes: bytes, ct_bytes: bytes) -> int | None:
    """Locate ct_bytes as a substring of region_bytes; return offset within
    the region, or None."""
    if not ct_bytes or len(ct_bytes) < 16:
        return None
    pos = region_bytes.find(ct_bytes[:32])
    return pos if pos >= 0 else None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    out: dict = {
        "success": False,
        "mem_size_bytes": 0,
        "n_dirty_regions": 0,
        "largest_dirty_region": None,
        "plaintext_candidate_offsets": [],
        "capture_script_path": os.path.abspath(__file__),
        "notes": "",
    }

    notes_lines: list[str] = []

    # 1. Load a JWT. Prefer the one in capture_ntoken_key.last.json (saved
    #    by an earlier capture run). Fall back to fetching a fresh one.
    cap_path = os.path.join(THIS, "capture_ntoken_key.last.json")
    jwt = None
    if os.path.exists(cap_path):
        try:
            with open(cap_path) as f:
                cap = json.load(f)
            jwt = cap.get("jwt")
        except Exception as e:
            notes_lines.append(f"capture_ntoken_key load failed: {e}")
    if not jwt:
        # Fallback: fetch fresh
        try:
            import requests
            r = requests.get(
                "https://hcaptcha.com/checksiteconfig",
                params={
                    "host": "accounts.hcaptcha.com",
                    "sitekey": "00000000-0000-0000-0000-000000000000",
                    "sc": "1", "swa": "1",
                },
                timeout=10,
            )
            jwt = r.json()["c"]["req"]
            notes_lines.append("used fresh checksiteconfig jwt")
        except Exception as e:
            notes_lines.append(f"fresh jwt fetch failed: {e}")

    if not jwt:
        out["notes"] = "; ".join(notes_lines) + " | no jwt available"
        return out

    notes_lines.append(f"jwt[{len(jwt)}b]")

    # 2. Boot the bridge (loads hsw.js + WASM)
    t0 = time.time()
    bridge = HSWBridge(version=None)
    rt = bridge.runtime
    notes_lines.append(f"bridge ready t={time.time()-t0:.1f}s")

    # 3. Pre-snapshot
    t1 = time.time()
    mem_size = rt.eval(_SNAP_PRE_JS)
    out["mem_size_bytes"] = int(mem_size)
    notes_lines.append(f"pre-snap {mem_size}B t={time.time()-t1:.2f}s")

    # 4. Call window.hsw(jwt) — solve PoW. We use the bridge's wait
    #    machinery directly so we can do the diff in the SAME runtime
    #    immediately after.
    token_esc = jwt.replace("\\", "\\\\").replace("'", "\\'")
    rt.eval(rf"""
        globalThis.__r = null; globalThis.__rerr = null;
        globalThis.__rdone = 0;
        (async () => {{
            try {{
                const o = await window.hsw('{token_esc}');
                globalThis.__r = typeof o === 'string' ? o : JSON.stringify(o);
            }} catch(e) {{ globalThis.__rerr = String(e); }}
            globalThis.__rdone = 1;
        }})();
    """, suppress=True)
    _wait_done(rt, "__rdone", max_seconds=45.0)
    err = rt.eval("globalThis.__rerr || ''")
    if err:
        notes_lines.append(f"hsw(jwt) error: {err}")
        out["notes"] = "; ".join(notes_lines)
        return out
    token = rt.eval("globalThis.__r") or ""
    notes_lines.append(f"token[{len(token)}c]")

    # Decode the token to get the ciphertext bytes for overlap-finding.
    ct_bytes = b""
    try:
        s = token.strip()
        pad = "=" * (-len(s) % 4)
        ct_bytes = base64.b64decode(s + pad)
    except Exception as e:
        notes_lines.append(f"token b64 decode failed: {e}")
    notes_lines.append(f"ct_bytes={len(ct_bytes)}")

    # 5. Diff (in JS) — use a small gap tolerance so a single unchanged
    #    byte inside a region doesn't split it.
    GAP = 8
    diff_js = _DIFF_JS_TPL.replace("__GAP__", str(GAP))
    t2 = time.time()
    diff_raw = rt.eval(diff_js)
    notes_lines.append(f"diff t={time.time()-t2:.2f}s")
    diff = json.loads(diff_raw)
    regions = diff["regions"]
    out["n_dirty_regions"] = int(diff["n_dirty_regions"])
    notes_lines.append(f"dirty_regions={out['n_dirty_regions']}")

    # 6. Score regions
    scored = []
    for r in regions:
        off = r["off"]; ln = r["len"]
        H = r["entropy_milli"] / 1000.0
        head = bytes.fromhex(r["head_hex"])
        is_msgpack, mp_kind = _looks_msgpack(head)
        is_json = _looks_json(head)
        ascii_d = _ascii_density(head)
        scored.append({
            "off": off,
            "len": ln,
            "entropy": round(H, 3),
            "head_hex": r["head_hex"],
            "tail_hex": r["tail_hex"],
            "msgpack": mp_kind,
            "json": is_json,
            "ascii_density": round(ascii_d, 3),
        })

    # Sort by length desc — find the largest first
    scored_by_len = sorted(scored, key=lambda x: -x["len"])
    largest = scored_by_len[0] if scored_by_len else None
    if largest:
        out["largest_dirty_region"] = {
            "offset_hex": hex(largest["off"]),
            "size_bytes": int(largest["len"]),
            "head_hex": largest["head_hex"][:64],
        }

    # 7. Find candidate "plaintext" regions:
    #    a) low entropy (< 5.0 bits/byte) and length between 16B and 4KB
    #    b) msgpack/JSON marker
    #    c) ASCII-density > 0.5
    pt_candidates = []
    for r in scored:
        if r["len"] < 8 or r["len"] > 16384:
            continue
        score = 0
        reasons = []
        if r["msgpack"]:
            score += 4; reasons.append(f"mp:{r['msgpack']}")
        if r["json"]:
            score += 4; reasons.append("json")
        if r["entropy"] < 5.0:
            score += 2; reasons.append(f"H={r['entropy']}")
        if r["ascii_density"] > 0.5:
            score += 2; reasons.append(f"ascii={r['ascii_density']}")
        if r["entropy"] < 3.0:
            score += 2; reasons.append("low-H")
        if score > 0:
            pt_candidates.append({**r, "score": score, "why": ",".join(reasons)})

    pt_candidates.sort(key=lambda x: -x["score"])
    out["plaintext_candidate_offsets"] = [int(c["off"]) for c in pt_candidates[:20]]

    # 8. Find regions whose first ~32B look like the ciphertext (high-entropy
    #    bytes from the returned token). These are likely the GCM output
    #    buffers — the plaintext buffer should be nearby in memory.
    ct_overlaps = []
    if ct_bytes:
        ct_head = ct_bytes[:32]
        for r in scored:
            # We have only the head_hex in JS — fetch full region (capped)
            # only for regions plausible to contain ct
            if r["len"] < 32:
                continue
            # Quick check: does the head start within ct?
            if ct_head.hex() in r["head_hex"]:
                ct_overlaps.append({"off": r["off"], "len": r["len"], "match": "head"})

    # 9. Top-N pretty dump for notes
    note_lines2 = []
    note_lines2.append(f"top-10 dirty regions by size:")
    for r in scored_by_len[:10]:
        marker = ""
        if r["msgpack"]:
            marker += f" mp={r['msgpack']}"
        if r["json"]:
            marker += " json"
        if r["entropy"] < 4.0:
            marker += " LOW-H"
        if r["ascii_density"] > 0.6:
            marker += " ASCII"
        note_lines2.append(
            f"  off=0x{r['off']:x} len={r['len']} H={r['entropy']} ad={r['ascii_density']}{marker} head={r['head_hex'][:32]}"
        )
    note_lines2.append(f"top plaintext-candidate regions ({len(pt_candidates)} total):")
    for c in pt_candidates[:10]:
        note_lines2.append(
            f"  off=0x{c['off']:x} len={c['len']} score={c['score']} why={c['why']} head={c['head_hex'][:48]}"
        )
    if ct_overlaps:
        note_lines2.append(f"ct-head overlaps found: {ct_overlaps}")
    notes_lines.append(" || ".join(note_lines2))

    out["success"] = True
    out["notes"] = "; ".join(notes_lines)

    # Persist a full report for offline inspection
    rep_path = os.path.join(THIS, "memdiff_ntoken.last.json")
    with open(rep_path, "w") as f:
        json.dump({
            "summary": out,
            "all_regions": scored,
            "pt_candidates": pt_candidates,
            "ct_overlaps": ct_overlaps,
            "token_head_hex": ct_bytes[:64].hex() if ct_bytes else "",
        }, f, indent=2)
    out["notes"] += f" | full report -> {rep_path}"

    return out


if __name__ == "__main__":
    res = main()
    print(json.dumps(res, indent=2))
