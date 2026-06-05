"""recover_keystream.py

Goal: recover the n-token plaintext WITHOUT the master key by treating the
per-block AES encrypt as a black box. The key idea:

  CTR-mode keystream block = AES_encrypt(round_keys, counter_block)

If we observe (counter_block_in, encrypted_counter_out) for every call to
the per-block AES encrypt during one window.hsw(jwt), we have the full
keystream. The plaintext is then ciphertext XOR keystream.

We do NOT need to know the round keys; the encrypt function's *output*
IS the keystream.

Strategy
========
Instrument fn 277 (and a few nearby (i32,i32)->() candidates) so that we
capture BOTH args BOTH before and after each call. Then post-process to
find which function (if any) shows the per-block-encrypt signature:

  * exactly 192 calls during one window.hsw(jwt)
  * arg's contents differ between pre and post (the function mutates it)
  * pre values are highly diverse (different counter per call)

The function arg whose post-values, concatenated, XOR-ed against the
ciphertext yield a printable / msgpack / JSON plaintext IS the keystream.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from collections import Counter, defaultdict

# Bootstrap path
THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, os.path.join(ROOT, "src"))

import requests

from hcaptcha.log import Logger
from hcaptcha.tools.js_runtime import JsRuntime
from hcaptcha.tools.wasm_disasm import WasmModule, decode_uleb, decode_sleb, parse_instruction
from hcaptcha.tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from hcaptcha import version as _v


# ---------------------------------------------------------------------------
# WebAssembly.instantiate hook — substitute patched WASM
# ---------------------------------------------------------------------------
_HOOK_JS = r"""
(function () {
  function _b64ToU8(s) {
    if (typeof Buffer !== "undefined") {
      const b = Buffer.from(s, "base64");
      return new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
    }
    const bin = atob(s);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return u8;
  }
  function _install(t) {
    if (!t || !t.WebAssembly) return;
    const origInstantiate = t.WebAssembly.instantiate;
    t.WebAssembly.instantiate = function (buf, imp) {
      let useBuf = buf;
      if (buf && buf.byteLength != null) {
        useBuf = _b64ToU8(globalThis.__patched_wasm_b64);
      }
      return origInstantiate.call(this, useBuf, imp).then(r => {
        const inst = r.instance || r;
        if (inst && inst.exports) {
          globalThis.__hsw_exports = inst.exports;
          for (const k of Object.keys(inst.exports)) {
            const v = inst.exports[k];
            if (v && typeof v === "object" && v.buffer &&
                typeof v.grow === "function") {
              globalThis.__hsw_memory = v;
              break;
            }
          }
        }
        return r;
      });
    };
    if (t.WebAssembly.instantiateStreaming) {
      t.WebAssembly.instantiateStreaming = async function (source, imp) {
        const resp = await source;
        const buf = await resp.arrayBuffer();
        return t.WebAssembly.instantiate(buf, imp);
      };
    }
  }
  _install(globalThis);
  _install(typeof window !== "undefined" ? window : null);
})();
"""

# ---------------------------------------------------------------------------
# Layout: each ring stores up to MAX_RECS records of REC_SIZE bytes.
# We use one ring per (fn, arg, phase) — phase in {pre, post}.
#
#   counter_addr  : i32  (call counter)
#   buf_addr      : MAX_RECS * REC_SIZE byte ring
#   tmp_c / tmp_a : scratch
# ---------------------------------------------------------------------------
REC_SIZE  = 16
MAX_RECS  = 256

SCRATCH_BASE = 60_000
RING_STRIDE  = 6_000
TMP_BASE     = 300_000
TMP_STRIDE   = 16
GATE_ADDR    = 320_000

MAX_SLOTS    = 48                  # bytes used: 48 * 6000 = 288_000 (<320_000)


def _slot(slot_idx: int) -> tuple[int, int, int, int]:
    counter = SCRATCH_BASE + slot_idx * RING_STRIDE
    buf     = counter + 4
    tmp_c   = TMP_BASE + slot_idx * TMP_STRIDE
    tmp_a   = tmp_c + 4
    return counter, buf, tmp_c, tmp_a


# ---------------------------------------------------------------------------
# Dump snippet — stack-neutral, gated, optional counter-increment
# ---------------------------------------------------------------------------
def _build_dump16_snippet(
    counter_addr: int, buf_addr: int,
    tmp_c: int, tmp_a: int,
    src_local: int,
    increment_counter: bool,
) -> bytes:
    out = bytearray()
    # if (*GATE != 0) {
    out += b"\x41" + encode_sleb(GATE_ADDR)
    out += b"\x28\x02\x00"
    out += b"\x04\x40"
    # tmp_c = *counter
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    # if (tmp_c < MAX_RECS) {
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_RECS)
    out += b"\x49"
    out += b"\x04\x40"
    # tmp_a = buf + counter * 16
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(REC_SIZE)
    out += b"\x6c"
    out += b"\x41" + encode_sleb(buf_addr)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    # *(tmp_a+0)  = i64 *(src+0)
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x20" + encode_uleb(src_local)
    out += b"\x29\x03\x00"
    out += b"\x37\x03\x00"
    # *(tmp_a+8) = i64 *(src+8)
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x20" + encode_uleb(src_local)
    out += b"\x29\x03\x08"
    out += b"\x37\x03\x08"
    if increment_counter:
        out += b"\x41" + encode_sleb(counter_addr)
        out += b"\x41" + encode_sleb(tmp_c)
        out += b"\x28\x02\x00"
        out += b"\x41" + encode_sleb(1)
        out += b"\x6a"
        out += b"\x36\x02\x00"
    out += b"\x0b"
    out += b"\x0b"
    return bytes(out)


# ---------------------------------------------------------------------------
# Function-body walker: precise opcode positions
# ---------------------------------------------------------------------------
def _parse_instr_positions(mod: WasmModule, func_idx: int):
    f = next((x for x in mod.functions if x["func_idx"] == func_idx), None)
    if f is None:
        raise KeyError(func_idx)
    raw = mod.raw
    code_start = f["code_start"]
    code_end   = f["code_end"]
    off = code_start
    out = []
    while off < code_end:
        name, _ops, n = parse_instruction(raw, off)
        out.append((off - code_start, name, n))
        off += n
    return out


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------
def _enumerate_candidates(mod: WasmModule) -> list[tuple[int, int]]:
    fns = []
    for f in mod.functions:
        if mod.types[f["type_idx"]] != (["i32", "i32"], []):
            continue
        body_len = f["code_end"] - f["code_start"]
        if 100 <= body_len <= 6000:
            fns.append((f["func_idx"], body_len))
    return fns


# ---------------------------------------------------------------------------
# Instrument one fn — splice dumps for arg0 and arg1, before and after.
#
# Order:
#   PRE  : at code_off = 0
#   POST : before every `return` AND before final `end`
#
# Each `slot_pre_a0`, `slot_pre_a1`, `slot_post_a0`, `slot_post_a1` is
# distinct. Only ONE of them (slot_post_a1, conventionally) increments the
# call counter; the others share the SAME counter via a shared address...
# actually simpler: each gets its own counter. They should all be in lockstep
# because they all live in the same gated block and execute exactly once
# per call (post snippets execute once per call path).
# ---------------------------------------------------------------------------
def _instrument_one_fn(writer: ModuleWriter, mod: WasmModule,
                       fn_idx: int,
                       slots: dict[str, int],
                       log: Logger) -> dict:
    f = next(x for x in mod.functions if x["func_idx"] == fn_idx)
    instrs = _parse_instr_positions(mod, fn_idx)

    snippets = {}
    for key, slot in slots.items():
        # key example: "pre_a0", "post_a1"
        phase, arg = key.split("_a")
        arg = int(arg)
        c, b, tc, ta = _slot(slot)
        snippets[key] = _build_dump16_snippet(
            c, b, tc, ta, src_local=arg,
            increment_counter=(phase == "post" and arg == 1),
        )

    # PRE: splice arg0 dump then arg1 dump at code_off=0
    pre = b"".join(snippets[k] for k in ("pre_a0", "pre_a1"))
    writer.code.splice_code(fn_idx, 0, n_replace=0, new_bytes=pre)

    # POST: combine arg0 + arg1
    post = b"".join(snippets[k] for k in ("post_a0", "post_a1"))

    splice_points = []
    for off, name, n in instrs:
        if name == "return":
            splice_points.append(off)
    last_off, last_name, last_n = instrs[-1]
    assert last_name == "end", f"fn {fn_idx} doesn't end with `end`: {last_name}"
    splice_points.append(last_off)
    for off in splice_points:
        writer.code.splice_code(fn_idx, off, n_replace=0, new_bytes=post)

    body_len = f["code_end"] - f["code_start"]
    log.info(f"  fn {fn_idx}: body={body_len}B, "
             f"slots pre_a0={slots['pre_a0']} pre_a1={slots['pre_a1']} "
             f"post_a0={slots['post_a0']} post_a1={slots['post_a1']}, "
             f"post-splices={len(splice_points)}", start=0, end=0)
    return {
        "fn": fn_idx,
        "slots": dict(slots),
        "body_len": body_len,
        "n_post_splices": len(splice_points),
    }


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _make_fake_jwt() -> str:
    now = int(time.time())
    return (
        _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        + "."
        + _b64u(json.dumps(
            {"s": "00000000", "d": 1, "t": now, "exp": now + 600}
        ).encode())
        + ".fake"
    )


# ---------------------------------------------------------------------------
# Main capture
# ---------------------------------------------------------------------------
def capture(version: str | None = None, log: Logger | None = None,
            extra_fns: list[int] | None = None) -> dict:
    log = log or Logger()
    version = version or _v.latest_version()

    from hcaptcha.hsw_bridge import HSWAnalyzer
    info = HSWAnalyzer(version, log=log).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)
    log.info(f"wasm {len(wasm)}B sha256={info['wasm_sha256'][:16]}",
             start=0, end=0)

    cands = _enumerate_candidates(mod)
    log.info(f"  (i32,i32)->() candidates: {len(cands)}", start=0, end=0)

    # Build priority list: fn 277 first, then nearby candidates,
    # then any per orchestrator hint (226 = caller).
    HINT_FNS = [277]
    if extra_fns:
        HINT_FNS = list(HINT_FNS) + list(extra_fns)

    priority = []
    cand_dict = {fi: bl for fi, bl in cands}
    for fi in HINT_FNS:
        if fi in cand_dict:
            priority.append(fi)
    # Then add any candidate with body size 200..2000 (per-block AES range)
    for fi, bl in sorted(cands, key=lambda x: x[1]):
        if fi in priority:
            continue
        if 150 <= bl <= 2200:
            priority.append(fi)
    # Cap so we fit in 48 slots (4 slots per fn => max 12 fns)
    MAX_FNS = 10
    priority = priority[:MAX_FNS]
    log.info(f"  priority list: {priority}", start=0, end=0)

    writer = ModuleWriter(mod)
    instrumented = []
    next_slot = 0
    for fi in priority:
        slots = {
            "pre_a0":  next_slot + 0,
            "pre_a1":  next_slot + 1,
            "post_a0": next_slot + 2,
            "post_a1": next_slot + 3,
        }
        if slots["post_a1"] >= MAX_SLOTS:
            log.info(f"  WARN: out of slots before fn {fi}",
                     start=0, end=0)
            break
        try:
            rec = _instrument_one_fn(writer, mod, fi, slots, log)
            instrumented.append(rec)
            next_slot += 4
        except Exception as e:
            log.info(f"  WARN: could not instrument fn {fi}: {e}",
                     start=0, end=0)

    # peek/poke
    t_i32_to_i32 = next(
        (i for i, (p, r) in enumerate(mod.types)
         if p == ["i32"] and r == ["i32"]), None)
    if t_i32_to_i32 is None:
        t_i32_to_i32 = writer.add_type(["i32"], ["i32"])
    t_i32i32_to_void = next(
        (i for i, (p, r) in enumerate(mod.types)
         if p == ["i32", "i32"] and r == []), None)
    if t_i32i32_to_void is None:
        t_i32i32_to_void = writer.add_type(["i32", "i32"], [])
    writer.add_function(
        t_i32_to_i32, [],
        bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]),
        export_name="__peek32")
    writer.add_function(
        t_i32i32_to_void, [],
        bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]),
        export_name="__poke32")

    patched = writer.emit()
    log.info(f"  patched wasm: {len(patched)}B (+{len(patched)-len(wasm)}B)",
             start=0, end=0)
    try:
        WasmModule(patched)  # re-parse for sanity
        log.info("  patched wasm: re-parse OK", start=0, end=0)
    except Exception as e:
        log.info(f"  WARN: patched wasm fails to re-parse: {e}",
                 start=0, end=0)

    # Save patched WASM next to script for debugging
    dbg_path = os.path.join(THIS, "recover_keystream.patched.wasm")
    with open(dbg_path, "wb") as f:
        f.write(patched)
    log.info(f"  patched wasm saved -> {dbg_path}", start=0, end=0)

    # Run
    log.info("  starting JsRuntime...", start=0, end=0)
    rt = JsRuntime()
    log.info("  JsRuntime ready", start=0, end=0)
    try:
        rt.eval(f"globalThis.__patched_wasm_b64 = '"
                f"{base64.b64encode(patched).decode()}';")
        log.info("  pushed patched wasm to JS realm", start=0, end=0)
        rt.eval(_HOOK_JS)
        log.info("  installed hook", start=0, end=0)
        r = requests.get(_v.asset_url(version, "hsw.js"))
        r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)
        log.info("  evaluated hsw.js", start=0, end=0)

        rt.eval(
            """(async () => {
                try { await window.hsw(1, new Uint8Array(0)); }
                catch (e) { globalThis.__warmup_err = String(e); }
            })();""", suppress=True,
        )
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break
        log.info("  hsw_exports populated", start=0, end=0)
        warmup_err = rt.eval("globalThis.__warmup_err") or ""
        if warmup_err:
            log.info(f"  warmup err: {warmup_err[:200]}", start=0, end=0)

        jwt = _make_fake_jwt()

        # Reset all counters before run
        poke_lines = []
        for rec in instrumented:
            for slot in rec["slots"].values():
                c, _, _, _ = _slot(slot)
                poke_lines.append(f"e.__poke32({c}, 0);")
        poke_init = "\n".join(poke_lines)

        rt.eval(
            f"""
            globalThis.__rk_done = 0;
            globalThis.__rk_token = '';
            globalThis.__rk_err = '';
            (async () => {{
                const e = globalThis.__hsw_exports;
                {poke_init}
                e.__poke32({GATE_ADDR}, 1);
                try {{
                    const r = await window.hsw('{jwt}');
                    globalThis.__rk_token = (typeof r === 'string') ? r : '';
                }} catch (ex) {{
                    globalThis.__rk_err = String(ex);
                }} finally {{
                    e.__poke32({GATE_ADDR}, 0);
                }}
                globalThis.__rk_done = 1;
            }})();
            """, suppress=True,
        )
        for _ in range(400):
            if rt.eval("globalThis.__rk_done"):
                break
            time.sleep(0.25)

        err = rt.eval("globalThis.__rk_err") or ""
        if err:
            log.info(f"  hsw raised: {err[:300]}", start=0, end=0)
        token_b64 = rt.eval("globalThis.__rk_token") or ""
        log.info(f"  token len={len(token_b64)}", start=0, end=0)

        # Read each ring
        captured: dict[int, dict] = {}
        for rec in instrumented:
            fn_idx = rec["fn"]
            captured[fn_idx] = {"rings": {}}
            for key, slot in rec["slots"].items():
                c, b, _, _ = _slot(slot)
                n = (rt.eval(f"globalThis.__hsw_exports.__peek32({c})") or 0) & 0xFFFFFFFF
                n = min(n, MAX_RECS)
                total = n * REC_SIZE
                if total > 0:
                    arr = rt.eval(
                        f"""(function() {{
                            const mem = new Uint8Array(
                                globalThis.__hsw_memory.buffer, {b}, {total});
                            return Array.from(mem);
                        }})()"""
                    ) or []
                    blocks = [bytes(arr[i*REC_SIZE:(i+1)*REC_SIZE]) for i in range(n)]
                else:
                    blocks = []
                captured[fn_idx]["rings"][key] = {
                    "n": n,
                    "blocks": blocks,
                }
            n_post_a1 = captured[fn_idx]["rings"]["post_a1"]["n"]
            n_post_a0 = captured[fn_idx]["rings"]["post_a0"]["n"]
            log.info(f"  fn {fn_idx}: post a0={n_post_a0} a1={n_post_a1}",
                     start=0, end=0)

        return {
            "wasm_sha256": info["wasm_sha256"],
            "instrumented": instrumented,
            "jwt": jwt,
            "token_b64": token_b64,
            "captured": captured,
        }
    finally:
        try: rt.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _b64_to_bytes(s: str) -> bytes:
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return dec(s + pad)
        except Exception:
            continue
    raise ValueError("b64 decode failed")


def _looks_msgpack(b: bytes) -> bool:
    if not b: return False
    b0 = b[0]
    if 0x80 <= b0 <= 0xdf: return True
    if b0 in (0xc4, 0xc5, 0xc6, 0xd9, 0xda, 0xdb,
              0xde, 0xdf, 0xdc, 0xdd): return True
    return False


def _looks_json(b: bytes) -> bool:
    if not b: return False
    return b[0] in (0x7b, 0x5b)


def _printable_ratio(b: bytes, n: int = 128) -> float:
    if not b: return 0.0
    s = b[:n]
    cnt = sum(1 for x in s if 0x20 <= x < 0x7f or x in (0x09, 0x0a, 0x0d))
    return cnt / len(s)


def _byte_entropy(b: bytes) -> float:
    if not b: return 0.0
    from math import log2
    cnt = Counter(b)
    n = len(b)
    return -sum((c/n) * log2(c/n) for c in cnt.values())


def analyze(out: dict, log: Logger) -> dict:
    token_b64 = out.get("token_b64") or ""
    if not token_b64:
        return {"success": False, "reason": "no token"}
    raw_token = _b64_to_bytes(token_b64)
    log.info(f"  token raw bytes: {len(raw_token)}", start=0, end=0)
    log.info(f"  token head: {raw_token[:32].hex()}", start=0, end=0)
    log.info(f"  token tail: {raw_token[-32:].hex()}", start=0, end=0)

    # Summarize each fn ring
    summaries = []
    for fn_idx, rec in out["captured"].items():
        rings = rec["rings"]
        s = {"fn": fn_idx}
        for key, r in rings.items():
            n = r["n"]
            blocks = r["blocks"]
            unique = len(set(blocks))
            s[f"{key}_n"]      = n
            s[f"{key}_uniq"]   = unique
        # Mutation detection: does post differ from pre?
        for arg in (0, 1):
            pre = rings.get(f"pre_a{arg}",  {}).get("blocks", [])
            post = rings.get(f"post_a{arg}", {}).get("blocks", [])
            mutates = any(p != q for p, q in zip(pre, post))
            s[f"a{arg}_mutates"] = mutates
        summaries.append(s)
        log.info(f"  fn {fn_idx}: " +
                 " ".join(f"{k}={v}" for k, v in s.items() if k != "fn"),
                 start=0, end=0)

    # For each (fn, arg), build keystream = concat(post_blocks); XOR with
    # ciphertext under several layouts; score.
    attempts = []
    for fn_idx, rec in out["captured"].items():
        rings = rec["rings"]
        for arg in (0, 1):
            post = rings.get(f"post_a{arg}", {}).get("blocks", [])
            if not post:
                continue
            ks = b"".join(post)
            for label, ct in [
                ("strip28",  raw_token[:-28]),
                ("strip16",  raw_token[:-16]),
                ("from0",    raw_token),
                ("from1",    raw_token[1:-27]),
                ("from12",   raw_token[12:-16]),
                ("from16",   raw_token[16:-12]),
                ("strip29",  raw_token[:-29]),
            ]:
                n = min(len(ct), len(ks))
                if n < 16: continue
                pt = bytes(ct[i] ^ ks[i] for i in range(n))
                score = {
                    "msgpack": _looks_msgpack(pt),
                    "json":    _looks_json(pt),
                    "print":   _printable_ratio(pt),
                    "entropy": _byte_entropy(pt[:256]),
                }
                attempts.append({
                    "fn": fn_idx,
                    "arg": arg,
                    "ct_layout": label,
                    "ks_len": len(ks),
                    "ct_len": len(ct),
                    "n_xor": n,
                    "pt_head_hex": pt[:64].hex(),
                    "ks_head_hex": ks[:64].hex(),
                    "pt_full": pt,
                    "score": score,
                })

    def _rank(a):
        s = a["score"]
        # Prefer msgpack > json > high-printable > anything
        if s["msgpack"]: return (3, s["print"])
        if s["json"]:    return (2, s["print"])
        return (1, s["print"])

    attempts.sort(key=_rank, reverse=True)

    if attempts:
        best = attempts[0]
        log.info(f"  BEST: fn {best['fn']} arg{best['arg']} "
                 f"layout {best['ct_layout']} print={best['score']['print']:.2f} "
                 f"msgpack={best['score']['msgpack']} json={best['score']['json']} "
                 f"entropy={best['score']['entropy']:.2f}",
                 start=0, end=0)
        log.info(f"  pt head: {best['pt_head_hex']}", start=0, end=0)
        log.info(f"  prev:    {best['pt_full'][:200]!r}", start=0, end=0)

    has_winner = any(a["score"]["msgpack"] or a["score"]["json"]
                     or a["score"]["print"] > 0.6
                     for a in attempts)
    return {
        "success": has_winner,
        "summaries": summaries,
        "best": attempts[:5] if attempts else [],
        "raw_token_hex": raw_token.hex(),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    log = Logger()
    log.info("=== recover_keystream: capture phase ===", start=0, end=0)
    cap = capture(log=log)

    def _scrub_cap(c):
        s = dict(c)
        new_cap = {}
        for fn, rec in s["captured"].items():
            ring_summ = {}
            for k, r in rec["rings"].items():
                ring_summ[k] = {
                    "n": r["n"],
                    "first": r["blocks"][0].hex() if r["blocks"] else None,
                    "last":  r["blocks"][-1].hex() if r["blocks"] else None,
                    "unique": len(set(r["blocks"])),
                    "sample_5": [b.hex() for b in r["blocks"][:5]],
                }
            new_cap[str(fn)] = {"rings": ring_summ}
        s["captured"] = new_cap
        return s

    cap_path = os.path.join(THIS, "recover_keystream.capture.json")
    with open(cap_path, "w") as f:
        json.dump(_scrub_cap(cap), f, indent=2)
    log.info(f"  saved -> {cap_path}", start=0, end=0)

    log.info("\n=== recover_keystream: analysis ===", start=0, end=0)
    ana = analyze(cap, log)
    ana_dump = {
        "success": ana.get("success"),
        "summaries": ana.get("summaries"),
        "best": [
            {k: v for k, v in a.items() if k != "pt_full"}
            for a in ana.get("best", [])
        ],
        "raw_token_hex": ana.get("raw_token_hex"),
    }
    ana_path = os.path.join(THIS, "recover_keystream.analysis.json")
    with open(ana_path, "w") as f:
        json.dump(ana_dump, f, indent=2)
    log.info(f"  saved -> {ana_path}", start=0, end=0)
    return 0 if ana.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
