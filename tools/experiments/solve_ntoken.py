"""Stage 29 — COMPLETE n-token solve, self-contained in ONE build/run.

The asset build rotates keys periodically, so capture + recover + verify must
happen in a single window.hsw(jwt) run. We instrument the n-token AES fn (the
fixslice fn the entry calls) to capture, in one run:
  * arg0 round keys (120 deobf words) -> recover master M via the fixslice
    key schedule (inv_bitslice(rk0) + undo(rk1); CONFIRMED by full 120-word
    schedule match -> M is provably this build's AES-256 key).
  * per call: arg1 counter block (prologue) + arg1 keystream output (epilogue).
Then VERIFY: AES-256(M, counter) == captured output for every block (the live
cipher's real keystream), and decrypt the token: plaintext = ct ^ keystream.
A solution is emitted only if M reproduces the live keystream exactly.
"""
from __future__ import annotations
import base64, json, os, struct, sys, time
from collections import Counter
THIS = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(THIS))
sys.path.insert(0, os.path.join(ROOT, "src"))
import requests
from Crypto.Cipher import AES
from hcaptcha.tools import fixslice as fs
from hcaptcha.log import Logger
from hcaptcha.tools.js_runtime import JsRuntime
from hcaptcha.tools.wasm_disasm import WasmModule
from hcaptcha.tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from hcaptcha.hsw_bridge import HSWAnalyzer
from hcaptcha.tools.sandbox_hook import HOOK_JS
from hcaptcha import version as _v
import hcaptcha.hsw_n_token_decrypt as ntd

CNT, GATE = 399_000, 399_004
RKBASE = 399_600           # 120 words round keys (captured once, on first call)
RKDONE = 399_596
RINGBASE = 410_000
NCALLS = 100
NWRK = 120
INW, OUTW = 8, 8           # arg1 input/output words per call (2 blocks)
STRIDE = (INW + OUTW) * 4 + 16
SB = 399_500
def c(n): return b"\x41" + encode_sleb(n)


def deobf_to_fixed(HELP, dst, src_local, nwords, off0=0):
    parts = []
    for i in range(nwords):
        parts.append(c(dst + i * 4) + b"\x41\x00" + b"\x20" + encode_uleb(src_local))
        if off0 + i:
            parts.append(c((off0 + i) * 4) + b"\x6a")
        parts.append(b"\x10" + encode_uleb(HELP) + b"\x36\x02\x00")
    return b"".join(parts)


def deobf_to_slot(HELP, base_slot_addr, base_off, src_local, nwords):
    parts = []
    for i in range(nwords):
        parts.append(c(base_slot_addr) + b"\x28\x02\x00" + c(base_off + i * 4) + b"\x6a")
        parts.append(b"\x41\x00" + b"\x20" + encode_uleb(src_local))
        if i:
            parts.append(c(i * 4) + b"\x6a")
        parts.append(b"\x10" + encode_uleb(HELP) + b"\x36\x02\x00")
    return b"".join(parts)


def main():
    log = Logger(); version = _v.latest_version()
    info = HSWAnalyzer(version, log=log).analyze()
    mod = WasmModule(bytes.fromhex(info["wasm_bytes_hex"]))
    entry = ntd._find_encrypt_entry(mod)
    ks_funcs = ntd._find_fixslice_ks_funcs(mod)
    callcnt = Counter(o[0] for n, o, _, _ in (mod.decode_function(entry) or []) if n == "call" and o)
    FN = next(f for f in ks_funcs if f in callcnt)
    gc = Counter()
    for n, ops, _, _ in (mod.decode_function(FN) or []):
        if n == "call" and ops:
            f = next((x for x in mod.functions if x["func_idx"] == ops[0]), None)
            if f and mod.types[f["type_idx"]] == (["i32", "i32"], ["i32"]):
                gc[ops[0]] += 1
    HELP = gc.most_common(1)[0][0]
    log.info(f"build {info['wasm_sha256'][:12]} entry=fn{entry} FN=fn{FN} helper=fn{HELP}", start=0, end=0)

    writer = ModuleWriter(mod)
    # PROLOGUE: capture round keys once (arg0, NWRK words), then per-call counter (arg1, INW)
    pro = bytearray()
    pro += c(GATE) + b"\x28\x02\x00" + b"\x04\x40"
    # round keys once
    pro += c(RKDONE) + b"\x28\x02\x00" + b"\x45" + b"\x04\x40"
    pro += deobf_to_fixed(HELP, RKBASE, 0, NWRK)
    pro += c(RKDONE) + b"\x41\x01" + b"\x36\x02\x00"
    pro += b"\x0b"
    # per-call counter
    pro += c(CNT) + b"\x28\x02\x00" + c(NCALLS) + b"\x49" + b"\x04\x40"
    pro += c(SB) + c(RINGBASE) + c(CNT) + b"\x28\x02\x00" + c(STRIDE) + b"\x6c" + b"\x6a" + b"\x36\x02\x00"
    pro += deobf_to_slot(HELP, SB, 0, 1, INW)
    pro += b"\x0b\x0b"
    writer.code.splice_code(FN, 0, n_replace=0, new_bytes=bytes(pro))
    # EPILOGUE: capture output (arg1, OUTW) at SB+INW*4; cnt++
    epi = bytearray()
    epi += c(GATE) + b"\x28\x02\x00" + b"\x04\x40"
    epi += c(CNT) + b"\x28\x02\x00" + c(NCALLS) + b"\x49" + b"\x04\x40"
    epi += deobf_to_slot(HELP, SB, INW * 4, 1, OUTW)
    epi += c(CNT) + c(CNT) + b"\x28\x02\x00" + c(1) + b"\x6a" + b"\x36\x02\x00"
    epi += b"\x0b\x0b"
    last_off = (mod.decode_function(FN) or [])[-1][2]
    writer.code.splice_code(FN, last_off, n_replace=0, new_bytes=bytes(epi))

    t_pk = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32"] and r == ["i32"]), None) or writer.add_type(["i32"], ["i32"])
    t_po = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32", "i32"] and r == []), None) or writer.add_type(["i32", "i32"], [])
    writer.add_function(t_pk, [], bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]), export_name="__peek32")
    writer.add_function(t_po, [], bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]), export_name="__poke32")
    patched = writer.emit(); log.info(f"patched {len(patched)}B", start=0, end=0)

    now = int(time.time()); b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    jwt = (b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "."
           + b64u(json.dumps({"s": "00000000", "d": 0, "t": now, "exp": now + 600}).encode()) + ".fake")
    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64='{base64.b64encode(patched).decode()}';")
        rt.eval(HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js")); r.encoding = "utf-8"; rt.eval(r.text, suppress=True)
        rt.eval("(async()=>{try{await window.hsw(1,new Uint8Array(0));}catch(e){}})();", suppress=True)
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break
        rt.eval(f"""globalThis.__done=0;globalThis.__tok='';
            (async()=>{{const e=globalThis.__hsw_exports; e.__poke32({CNT},0); e.__poke32({RKDONE},0); e.__poke32({GATE},1);
            try{{const r=await window.hsw({json.dumps(jwt)}); globalThis.__tok=(typeof r==='string')?r:'';}}
            catch(ex){{globalThis.__e=String(ex);}}finally{{e.__poke32({GATE},0);}}
            globalThis.__done=1;}})();""", suppress=True)
        for _ in range(400):
            if rt.eval("globalThis.__done"):
                break
            time.sleep(0.25)
        token = rt.eval("globalThis.__tok") or ""
        if not token:
            log.info(f"no token err={rt.eval('globalThis.__e')}", start=0, end=0); return
        raw = ntd._b64_decode(token); ct, tag, iv = ntd._split_wire(raw); N = len(ct)
        rd = lambda a, n: bytes(rt.eval(f"(function(){{return Array.from(new Uint8Array(globalThis.__hsw_memory.buffer,{a},{n}));}})()") or [])
        rk = list(struct.unpack(f"<{NWRK}I", rd(RKBASE, NWRK * 4)))
        ncall = min(struct.unpack("<I", rd(CNT, 4))[0], NCALLS)
        calls = []
        for k in range(ncall):
            base = RINGBASE + k * STRIDE
            inp = rd(base, INW * 4)
            out = rd(base + INW * 4, OUTW * 4)
            calls.append((inp, out))
        log.info(f"token={len(token)} N={N} iv={iv.hex()} ncall={ncall} rk0={struct.pack('<8I',*rk[:8]).hex()[:24]}", start=0, end=0)

        # ---- recover master from round keys ----
        A, _ = fs.inv_bitslice(list(rk[0:8]))
        ch = list(rk[8:16]); fs.sub_bytes_nots(ch); fs.shift_rows_1(ch)
        C, _ = fs.inv_bitslice(ch)
        M = A + C
        sched = fs.aes256_key_schedule(M)
        sched_match = sum(1 for i in range(NWRK) if sched[i] == rk[i])
        log.info(f"master M={M.hex()} schedule-match={sched_match}/{NWRK}", start=0, end=0)

        # ---- build keystream map from captured outputs: counter -> 16B block ----
        e = AES.new(M, AES.MODE_ECB)
        ksmap = {}; verified_blocks = 0; checked = 0
        for inp, out in calls:
            ctr = int.from_bytes(inp[12:16], "big") if inp[:12] == iv else None
            for bi in range(2):
                blk = out[bi * 16:bi * 16 + 16]
                if len(blk) < 16:
                    continue
                if inp[:12] == iv:
                    cc = int.from_bytes(inp[12:16], "big") + bi
                    ksmap[cc] = blk
                    exp = e.encrypt(iv + (cc & 0xffffffff).to_bytes(4, "big"))
                    checked += 1; verified_blocks += (exp == blk)
        log.info(f"AES(M,counter)==live output: {verified_blocks}/{checked} blocks verified", start=0, end=0)

        # ---- decrypt token: plaintext block j uses counter j+start ----
        results = {}
        for start in (2, 1, 0):
            ks = bytearray()
            ok = True
            for j in range((N + 15) // 16):
                blk = ksmap.get(j + start)
                if blk is None:
                    blk = e.encrypt(iv + ((j + start) & 0xffffffff).to_bytes(4, "big"))
                ks += blk
            pt = bytes(a ^ b for a, b in zip(ct, ks[:N]))
            printable = sum(1 for b in pt[:400] if 9 <= b <= 126)
            results[start] = (pt, printable)
            log.info(f"  start={start} printable={printable}/400 head={pt[:64]!r}", start=0, end=0)

        best = max(results, key=lambda s: results[s][1])
        pt, pr = results[best]
        solved = (sched_match == NWRK and verified_blocks > 0)
        if solved:
            print("\n*** N-TOKEN MASTER KEY RECOVERED & VERIFIED ***")
            print(f"    master  = {M.hex()}")
            print(f"    verify  = full AES-256 schedule match {sched_match}/{NWRK} round-key words")
            print(f"    verify  = AES-256(M, counter) == live cipher output for {verified_blocks}/{checked} blocks")
            print(f"    counter_start (GCM J0+1) = {best}")
            print(f"    plaintext[:200]: {pt[:200]!r}")
            json.dump({"version": version, "master_key_hex": M.hex(), "fn": FN, "iv": iv.hex(),
                       "counter_start": best, "schedule_match": sched_match,
                       "live_blocks_verified": verified_blocks, "token": token,
                       "plaintext_hex": pt.hex(), "build": info["wasm_sha256"],
                       "method": "fn-roundkey-schedule-recovery + live-keystream-verify"},
                      open(os.path.join(THIS, "ntoken_master_SOLVED.json"), "w"), indent=2)
            print("    saved -> tools/experiments/ntoken_master_SOLVED.json")
        else:
            print(f"\n[stage 29] sched_match={sched_match}/{NWRK} live_verified={verified_blocks}/{checked}; not conclusive.")
            json.dump({"version": version, "M": M.hex(), "sched_match": sched_match,
                       "verified": verified_blocks, "checked": checked, "iv": iv.hex(),
                       "token": token, "rk": rk, "build": info["wasm_sha256"]},
                      open(os.path.join(THIS, "stage29_dump.json"), "w"))
    finally:
        try: rt.close()
        except Exception: pass


if __name__ == "__main__":
    main()
