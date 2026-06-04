"""Capture the n-token AES key by instrumenting the WASM key-schedule
function(s) and dumping their argument buffers.

Background
==========
Phase 1 (call-graph BFS over the hsw.wasm module) identified the AES
fixslice key-schedule helpers reachable from the n-token JWT path.
On this build the candidates are:

  * fn 425 — sig (i32,i32)->(), body 2858B, opcode profile xor=190,
    and=48, rotl=40, mask 0x0F00_0F00 (=251662080) — classic AES
    fixslice32 key schedule. Reached from ec via fn 548 (the giant
    wbg-bindgen Promise state machine).
  * fn 314 — sig (i32,i32)->(), body 4508B — bigger fixslice-style
    helper, also reached from ec via fn 548. Most likely the
    bit-orthogonalization helper (called before/after KS).
  * fn 477 — the vc-dispatcher KS (= encrypt_req_data path).

We instrument 425 and 314 simultaneously, gated by a memory flag so
recording only happens while window.hsw(jwt) is in flight. Each
prologue dumps 32 bytes from arg0 AND 32 bytes from arg1 into a
distinct scratch ring with the call counter. After hsw returns we
read every captured 32-byte block, then for each block attempt
AES-256-GCM decrypt of the n-token (in both wire-format hypotheses).
The block that decrypts is the n-token AES key.

This script is the "capture" half of the phase-2 task. The wbg-bindgen
helper fn 548 also reaches the GCM encrypt routine, but observing its
buffers cleanly is harder (it's a 192-KB Rust state machine). Instead
we read the plaintext directly from the AES-GCM decrypt output once we
have the key — that bytes-for-bytes IS the plaintext that fed the
encrypt call.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections import defaultdict

# Make the in-repo hcaptcha package importable
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, "src"))

import requests
from Crypto.Cipher import AES

from hcaptcha.log import Logger
from hcaptcha.tools.js_runtime import JsRuntime
from hcaptcha.tools.wasm_disasm import WasmModule, decode_uleb, decode_sleb
from hcaptcha.tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from hcaptcha.hsw_n_key_runtime import _HOOK_JS
from hcaptcha import version as _v


# ---------------------------------------------------------------------------
# Scratch layout — one ring per (fn, arg) for up to MAX_RECS calls each.
# Record = (counter_index_u32) + 32 raw bytes per arg.
# ---------------------------------------------------------------------------
REC_SIZE = 36                                # 4 (counter) + 32 (key bytes)
MAX_RECS = 256

# Per-function-arg scratch rings. Each gets a (counter, buffer, tmp_c, tmp_a)
# quadruple. Indexed by (func_idx, arg_local_index).
SCRATCH_BASE_RINGS = 60_000
RING_STRIDE = 10_000                         # 256 * 36 = 9216 + slack
TMP_BASE = 200_000
TMP_STRIDE = 16

# We support up to 16 (fn,arg) ring slots in the layout above.
# That covers (425 a0, 425 a1, 314 a0, 314 a1, 330 a0, 330 a1, 330 a2,
# 388 a0, 388 a1, 520 a0, 520 a1, 477 a0, 477 a1, ...).
GATE_ADDR = 200_000 + 16 * TMP_STRIDE        # = 200_256


def _ring_slots(slot_idx: int) -> tuple[int, int, int, int]:
    counter = SCRATCH_BASE_RINGS + slot_idx * RING_STRIDE
    buf     = counter + 4
    tmp_c   = TMP_BASE + slot_idx * TMP_STRIDE
    tmp_a   = tmp_c + 4
    return counter, buf, tmp_c, tmp_a


def _build_key_dump_prologue(
    counter_addr: int, buf_addr: int,
    tmp_c: int, tmp_a: int,
    src_local: int,
    gated: bool = True,
) -> bytes:
    """Prologue bytecode (stack-balanced):

        if (*GATE != 0) {
          c = *counter
          if (c < MAX_RECS) {
            addr = buf + c * REC_SIZE
            *(addr) = c                             // store counter index
            // memcpy 32 bytes from local[src_local] -> addr+4
            // Inlined as 4 x i64.load + i64.store (8 bytes each)
            *(addr+4)  = *(src+0)   (i64)
            *(addr+12) = *(src+8)   (i64)
            *(addr+20) = *(src+16)  (i64)
            *(addr+28) = *(src+24)  (i64)
            (*counter)++
          }
        }

    Stack-neutral; the original function body runs unchanged.
    """
    out = bytearray()
    if gated:
        out += b"\x41" + encode_sleb(GATE_ADDR)
        out += b"\x28\x02\x00"                                 # i32.load
        out += b"\x04\x40"                                     # if (empty)
    # tmp_c = *counter
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    # if counter < MAX_RECS:
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_RECS)
    out += b"\x49"                                             # i32.lt_u
    out += b"\x04\x40"                                         # if (empty)
    # tmp_a = buf + counter * REC_SIZE
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(REC_SIZE)
    out += b"\x6c"                                             # i32.mul
    out += b"\x41" + encode_sleb(buf_addr)
    out += b"\x6a"                                             # i32.add
    out += b"\x36\x02\x00"
    # *(tmp_a+0) = counter (so we can correlate ordering)
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    # Copy 32 bytes from *src to addr+4 as 4 x i64
    for chunk in range(4):
        src_off = chunk * 8
        dst_off = 4 + chunk * 8
        # dst addr
        out += b"\x41" + encode_sleb(tmp_a)
        out += b"\x28\x02\x00"                                 # i32.load tmp_a
        # value: i64.load *(src + src_off)
        out += b"\x20" + encode_uleb(src_local)                # local.get src
        out += b"\x29\x03" + encode_uleb(src_off)              # i64.load align=3 off=src_off
        # i64.store align=3 off=dst_off
        out += b"\x37\x03" + encode_uleb(dst_off)
    # counter++
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    out += b"\x0b"                                             # end (if counter<MAX)
    if gated:
        out += b"\x0b"                                         # end (if gated)
    return bytes(out)


# ---------------------------------------------------------------------------
# Locate KS candidates by structural fingerprint
# ---------------------------------------------------------------------------
def _find_ks_candidates(mod: WasmModule, log: Logger) -> list[int]:
    """Find functions matching the AES fixslice32 key-schedule fingerprint:
    sig (i32,i32)->() , body >= 1000B, lots of XOR + ROTL, mask 0x0F000F00
    or 0x55555555/0x33333333.
    """
    out = []
    for f in mod.functions:
        if mod.types[f["type_idx"]] != (["i32", "i32"], []):
            continue
        body_len = f["code_end"] - f["code_start"]
        if body_len < 1000:
            continue
        instrs = mod.decode_function(f["func_idx"]) or []
        from collections import Counter
        op = Counter(n for n, _, _, _ in instrs)
        if op.get("i32.xor", 0) < 80:
            continue
        # confirm mask presence
        consts = {ops[0] & 0xFFFFFFFF for n, ops, _, _ in instrs
                  if n == "i32.const" and ops}
        if not (0x0F000F00 in consts or 0x55555555 in consts
                or 0x33333333 in consts or 251662080 in consts):
            continue
        out.append(f["func_idx"])
        log.info(f"  KS candidate fn {f['func_idx']}: body={body_len}B "
                 f"xor={op['i32.xor']} rotl={op.get('i32.rotl', 0)}",
                 start=0, end=0)
    return out


# ---------------------------------------------------------------------------
# Active-segment element parser (returns table_funcs ordered)
# ---------------------------------------------------------------------------
def _parse_elements(mod: WasmModule) -> set[int]:
    table_funcs = set()
    sec = None
    for x in mod.sections:
        if x[0] == 9:
            sec = x; break
    if not sec:
        return table_funcs
    raw = mod.raw
    _, _, off, plen = sec
    count, n = decode_uleb(raw, off); off += n
    for _ in range(count):
        flag, n = decode_uleb(raw, off); off += n
        if flag == 0:
            if raw[off] == 0x41:
                _, m = decode_sleb(raw, off + 1); off += 1 + m
            while raw[off] != 0x0b:
                off += 1
            off += 1
            n_init, m = decode_uleb(raw, off); off += m
            for _ in range(n_init):
                fi, m = decode_uleb(raw, off); off += m
                table_funcs.add(fi)
        else:
            break
    return table_funcs


# ---------------------------------------------------------------------------
# Reachability (used to confirm which candidates lie on the n-token path)
# ---------------------------------------------------------------------------
def _reach_from(mod: WasmModule, table_funcs: set[int], src: int,
                depth: int = 12) -> set[int]:
    type_to_funcs: dict[int, list[int]] = defaultdict(list)
    for fi in table_funcs:
        f = next((x for x in mod.functions if x["func_idx"] == fi), None)
        if f:
            type_to_funcs[f["type_idx"]].append(fi)
    g: dict[int, set[int]] = defaultdict(set)
    for f in mod.functions:
        fi = f["func_idx"]
        for n, ops, _, _ in (mod.decode_function(fi) or []):
            if n == "call" and ops:
                g[fi].add(ops[0])
            elif n == "call_indirect" and ops:
                for tgt in type_to_funcs.get(ops[0], []):
                    g[fi].add(tgt)
    visited = {src}; layer = {src}
    for _ in range(depth):
        nxt = set()
        for n in layer:
            for c in g.get(n, ()):
                if c not in visited:
                    visited.add(c); nxt.add(c)
        layer = nxt
        if not layer: break
    visited.discard(src)
    return visited


# ---------------------------------------------------------------------------
# Main capture
# ---------------------------------------------------------------------------
def capture(version: str | None = None, log: Logger | None = None) -> dict:
    log = log or Logger()
    version = version or _v.latest_version()

    from hcaptcha.hsw_bridge import HSWAnalyzer
    info = HSWAnalyzer(version, log=log).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)

    log.info(f"capture_ntoken_key: wasm {len(wasm)}B sha256={info['wasm_sha256'][:16]}",
             start=0, end=0)

    table = _parse_elements(mod)
    exp = {e["name"]: e["idx"] for e in mod.exports if e["kind"] == "func"}
    log.info(f"  exports: pc={exp.get('pc')} ec={exp.get('ec')} "
             f"kc={exp.get('kc')} vc={exp.get('vc')}", start=0, end=0)

    # Find KS candidates
    ks_all = _find_ks_candidates(mod, log)

    # Filter to ones reachable from ec/pc/kc but NOT vc (we want the
    # n-token-path KS, not the vc/encrypt_req_data KS).
    reach_ec = _reach_from(mod, table, exp["ec"]) if "ec" in exp else set()
    reach_vc = _reach_from(mod, table, exp["vc"]) if "vc" in exp else set()

    ks_targets = [k for k in ks_all if k in reach_ec and k not in reach_vc]
    log.info(f"  KS candidates reachable from ec but not vc: {ks_targets}",
             start=0, end=0)
    if not ks_targets:
        # fall back: instrument ALL KS candidates
        log.info("  no ec-only KS — falling back to ALL KS candidates",
                 start=0, end=0)
        ks_targets = ks_all

    # Build patched module. For each function we want to instrument, dump
    # all of its i32 arguments. Slot allocator: one slot per (fn,arg).
    writer = ModuleWriter(mod)

    # Targets: the KS candidates + their probable "encrypt entry"
    # callers (sig (i32,i32,i32)->(i32)) that may carry the master-key
    # pointer in arg0.
    fn_targets: list[int] = list(ks_targets)
    # Add the known encrypt-entry caller fn 330 (= phase-1 fn 205).
    if 330 in {f["func_idx"] for f in mod.functions}:
        fn_targets.append(330)
    # Also include fn 388 if present and reachable from ec but not vc
    # (sometimes the actual KS entry).
    reach_ec_set = reach_ec
    for cand in (388, 520, 350, 352):
        if cand in {f["func_idx"] for f in mod.functions} and cand in reach_ec_set:
            if cand not in fn_targets:
                fn_targets.append(cand)

    instrumented: list[dict] = []
    slot_assignments: dict[tuple[int, int], int] = {}
    next_slot = 0

    for fi in fn_targets:
        f = next((x for x in mod.functions if x["func_idx"] == fi), None)
        if f is None:
            continue
        params, _ = mod.types[f["type_idx"]]
        # Build one prologue chunk per i32 arg
        chunks = []
        for arg_idx, pt in enumerate(params):
            if pt != "i32":
                continue
            if next_slot >= 16:
                log.info(f"  WARN: out of ring slots, stopping at fn {fi}",
                         start=0, end=0)
                break
            counter, buf, tmp_c, tmp_a = _ring_slots(next_slot)
            chunks.append(_build_key_dump_prologue(
                counter, buf, tmp_c, tmp_a,
                src_local=arg_idx, gated=True))
            slot_assignments[(fi, arg_idx)] = next_slot
            next_slot += 1
        if chunks:
            prologue = b"".join(chunks)
            writer.code.splice_code(fi, 0, n_replace=0, new_bytes=prologue)
            instrumented.append({"fn": fi, "n_args_i32": len(chunks)})
            log.info(f"  instrumented fn {fi} (i32 args: {len(chunks)})",
                     start=0, end=0)

    # Add peek/poke exports
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

    # Sandbox run
    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64 = '"
                f"{base64.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js"))
        r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)
        rt.eval(
            """(async () => {
                try { await window.hsw(1, new Uint8Array(0)); }
                catch (e) { globalThis.__warmup_err = String(e); }
            })();""",
            suppress=True,
        )
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break

        # Build JWT + run gated
        now = int(time.time())
        def b64u(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        jwt = (
            b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            + "."
            + b64u(json.dumps(
                {"s": "00000000", "d": 1, "t": now, "exp": now + 600}
            ).encode())
            + ".fake"
        )

        # Build counter / buf addr lookup from slot_assignments
        counters: dict[str, int] = {}
        bufs: dict[str, int] = {}
        for (fi, ai), slot in slot_assignments.items():
            c, b, _, _ = _ring_slots(slot)
            name = f"f{fi}_a{ai}"
            counters[name] = c
            bufs[name] = b
        poke_init = "\n".join(
            f"e.__poke32({c}, 0);" for c in counters.values()
        )
        rt.eval(
            f"""
            globalThis.__nkey_done = 0;
            globalThis.__nkey_token = '';
            globalThis.__nkey_err = '';
            (async () => {{
                const e = globalThis.__hsw_exports;
                {poke_init}
                e.__poke32({GATE_ADDR}, 1);
                try {{
                    const r = await window.hsw('{jwt}');
                    globalThis.__nkey_token = (typeof r === 'string') ? r : '';
                }} catch (ex) {{
                    globalThis.__nkey_err = String(ex);
                }} finally {{
                    e.__poke32({GATE_ADDR}, 0);
                }}
                globalThis.__nkey_done = 1;
            }})();
            """,
            suppress=True,
        )
        for _ in range(400):
            if rt.eval("globalThis.__nkey_done"):
                break
            time.sleep(0.25)

        err = rt.eval("globalThis.__nkey_err") or ""
        if err:
            log.info(f"  hsw() raised: {err[:300]}", start=0, end=0)
        token = rt.eval("globalThis.__nkey_token") or ""
        log.info(f"  token len={len(token)}", start=0, end=0)

        # Read each ring buffer
        captured = {}
        for name, c_addr in counters.items():
            n_recs = (rt.eval(f"globalThis.__hsw_exports.__peek32({c_addr})") or 0) & 0xFFFFFFFF
            n_recs = min(n_recs, MAX_RECS)
            log.info(f"  ring {name}: {n_recs} records", start=0, end=0)
            if n_recs == 0:
                captured[name] = []
                continue
            buf_addr = bufs[name]
            total_bytes = n_recs * REC_SIZE
            # Read as bytes
            arr = rt.eval(
                f"""(function() {{
                    const mem = new Uint8Array(
                        globalThis.__hsw_memory.buffer, {buf_addr}, {total_bytes});
                    return Array.from(mem);
                }})()"""
            ) or []
            recs = []
            for i in range(n_recs):
                base = i * REC_SIZE
                counter = (arr[base] | (arr[base+1] << 8) |
                           (arr[base+2] << 16) | (arr[base+3] << 24))
                key32 = bytes(arr[base+4:base+36])
                recs.append({"counter": counter, "key32_hex": key32.hex()})
            captured[name] = recs

        return {
            "wasm_sha256": info["wasm_sha256"],
            "instrumented_ks_fns": instrumented,
            "jwt": jwt,
            "token": token,
            "captured": captured,
        }
    finally:
        try:
            rt.close()
        except Exception:
            pass


def try_decrypt(token_b64: str, key: bytes) -> tuple[bool, bytes | str]:
    """Try AES-256-GCM decrypt of `token_b64` under `key`, both wire formats."""
    try:
        raw = base64.b64decode(token_b64 + "=" * (-len(token_b64) % 4))
    except Exception:
        try:
            raw = base64.urlsafe_b64decode(token_b64 + "=" * (-len(token_b64) % 4))
        except Exception as e:
            return False, f"b64 decode failed: {e}"
    if len(raw) < 12 + 16:
        return False, "too short"
    # H1: iv(12) || ct(N) || tag(16)
    iv, ct, tag = raw[:12], raw[12:-16], raw[-16:]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        pt = cipher.decrypt_and_verify(ct, tag)
        return True, pt
    except Exception:
        pass
    # H2: ct(N) || tag(16) || iv(12)
    iv, ct, tag = raw[-12:], raw[:-28], raw[-28:-12]
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        pt = cipher.decrypt_and_verify(ct, tag)
        return True, pt
    except Exception as e:
        return False, str(e)


def main() -> int:
    log = Logger()
    out = capture(log=log)
    token = out["token"]
    print(f"token: {token[:80]}... (len {len(token)})")

    # Iterate every captured 32-byte block; first one to decrypt wins.
    tried = 0
    for ring_name, recs in out["captured"].items():
        for rec in recs:
            key = bytes.fromhex(rec["key32_hex"])
            ok, res = try_decrypt(token, key)
            tried += 1
            if ok:
                print(f"\n*** WINNER ***")
                print(f"  ring={ring_name}")
                print(f"  counter={rec['counter']}")
                print(f"  key (hex)={rec['key32_hex']}")
                print(f"  plaintext ({len(res)} bytes): {res[:80].hex()}...")
                out["winner"] = {
                    "ring": ring_name,
                    "counter": rec["counter"],
                    "key_hex": rec["key32_hex"],
                    "plaintext_hex": res.hex(),
                    "plaintext_len": len(res),
                }
                # Save
                save_path = os.path.join(THIS_DIR, "capture_ntoken_key.last.json")
                with open(save_path, "w") as f:
                    json.dump(out, f, indent=2)
                print(f"  saved -> {save_path}")
                return 0
    print(f"\nno key out of {tried} candidates decrypted the n-token")
    save_path = os.path.join(THIS_DIR, "capture_ntoken_key.last.json")
    with open(save_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"saved capture -> {save_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
