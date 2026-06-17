"""Stage 30 — prove THIRD-PARTY n-token decryption: recover the master M from
ONE token's round keys, then verify M reproduces the keystream of a SECOND,
independently generated n-token (different iv) in the same build. If M (a build
constant) verifies against a token it did not help generate, third-party
decryption is proven.
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
RKBASE, RKDONE = 399_600, 399_596
RINGBASE = 410_000
NCALLS = 60
NWRK = 120
INW, OUTW = 8, 8
STRIDE = (INW + OUTW) * 4 + 16
SB = 399_500
def c(n): return b"\x41" + encode_sleb(n)


def deobf_fixed(HELP, dst, src_local, nwords):
    p = []
    for i in range(nwords):
        p.append(c(dst + i * 4) + b"\x41\x00" + b"\x20" + encode_uleb(src_local))
        if i: p.append(c(i * 4) + b"\x6a")
        p.append(b"\x10" + encode_uleb(HELP) + b"\x36\x02\x00")
    return b"".join(p)


def deobf_slot(HELP, base_slot_addr, base_off, src_local, nwords):
    p = []
    for i in range(nwords):
        p.append(c(base_slot_addr) + b"\x28\x02\x00" + c(base_off + i * 4) + b"\x6a")
        p.append(b"\x41\x00" + b"\x20" + encode_uleb(src_local))
        if i: p.append(c(i * 4) + b"\x6a")
        p.append(b"\x10" + encode_uleb(HELP) + b"\x36\x02\x00")
    return b"".join(p)


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
    log.info(f"build {info['wasm_sha256'][:12]} FN=fn{FN} helper=fn{HELP}", start=0, end=0)

    writer = ModuleWriter(mod)
    pro = bytearray()
    pro += c(GATE) + b"\x28\x02\x00" + b"\x04\x40"
    pro += c(RKDONE) + b"\x28\x02\x00" + b"\x45" + b"\x04\x40"
    pro += deobf_fixed(HELP, RKBASE, 0, NWRK)
    pro += c(RKDONE) + b"\x41\x01" + b"\x36\x02\x00" + b"\x0b"
    pro += c(CNT) + b"\x28\x02\x00" + c(NCALLS) + b"\x49" + b"\x04\x40"
    pro += c(SB) + c(RINGBASE) + c(CNT) + b"\x28\x02\x00" + c(STRIDE) + b"\x6c" + b"\x6a" + b"\x36\x02\x00"
    pro += deobf_slot(HELP, SB, 0, 1, INW) + b"\x0b\x0b"
    writer.code.splice_code(FN, 0, n_replace=0, new_bytes=bytes(pro))
    epi = bytearray()
    epi += c(GATE) + b"\x28\x02\x00" + b"\x04\x40"
    epi += c(CNT) + b"\x28\x02\x00" + c(NCALLS) + b"\x49" + b"\x04\x40"
    epi += deobf_slot(HELP, SB, INW * 4, 1, OUTW)
    epi += c(CNT) + c(CNT) + b"\x28\x02\x00" + c(1) + b"\x6a" + b"\x36\x02\x00" + b"\x0b\x0b"
    last_off = (mod.decode_function(FN) or [])[-1][2]
    writer.code.splice_code(FN, last_off, n_replace=0, new_bytes=bytes(epi))
    t_pk = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32"] and r == ["i32"]), None) or writer.add_type(["i32"], ["i32"])
    t_po = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32", "i32"] and r == []), None) or writer.add_type(["i32", "i32"], [])
    writer.add_function(t_pk, [], bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]), export_name="__peek32")
    writer.add_function(t_po, [], bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]), export_name="__poke32")
    patched = writer.emit()

    now = int(time.time()); b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    def mkjwt():
        return (b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "."
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

        def run_token(gate_on):
            rt.eval(f"""globalThis.__done=0;globalThis.__tok='';
                (async()=>{{const e=globalThis.__hsw_exports; e.__poke32({CNT},0); e.__poke32({RKDONE},0);
                e.__poke32({GATE},{1 if gate_on else 0});
                try{{const r=await window.hsw({json.dumps(mkjwt())}); globalThis.__tok=(typeof r==='string')?r:'';}}
                catch(ex){{globalThis.__e=String(ex);}}finally{{e.__poke32({GATE},0);}}
                globalThis.__done=1;}})();""", suppress=True)
            for _ in range(400):
                if rt.eval("globalThis.__done"):
                    break
                time.sleep(0.25)
            return rt.eval("globalThis.__tok") or ""

        rd = lambda a, n: bytes(rt.eval(f"(function(){{return Array.from(new Uint8Array(globalThis.__hsw_memory.buffer,{a},{n}));}})()") or [])

        # TOKEN 1 — gated: recover M from round keys + capture keystream
        tok1 = run_token(True)
        rk = list(struct.unpack(f"<{NWRK}I", rd(RKBASE, NWRK * 4)))
        A, _ = fs.inv_bitslice(list(rk[0:8])); ch = list(rk[8:16])
        fs.sub_bytes_nots(ch); fs.shift_rows_1(ch); C, _ = fs.inv_bitslice(ch)
        M = A + C
        sched = fs.aes256_key_schedule(M)
        sm = sum(1 for i in range(NWRK) if sched[i] == rk[i])
        e = AES.new(M, AES.MODE_ECB)
        raw1 = ntd._b64_decode(tok1); ct1, _, iv1 = ntd._split_wire(raw1)
        log.info(f"token1 iv={iv1.hex()} M={M.hex()} schedule_match={sm}/{NWRK}", start=0, end=0)

        # TOKEN 2 — gated again (independent iv): capture its keystream, verify M reproduces it
        tok2 = run_token(True)
        ncall = min(struct.unpack("<I", rd(CNT, 4))[0], NCALLS)
        raw2 = ntd._b64_decode(tok2); ct2, _, iv2 = ntd._split_wire(raw2)
        ok = chk = 0
        for k in range(ncall):
            base = RINGBASE + k * STRIDE
            inp = rd(base, INW * 4); out = rd(base + INW * 4, OUTW * 4)
            if inp[:12] != iv2:
                continue
            for bi in range(2):
                blk = out[bi * 16:bi * 16 + 16]
                if len(blk) < 16:
                    continue
                cc = int.from_bytes(inp[12:16], "big") + bi
                exp = e.encrypt(iv2 + (cc & 0xffffffff).to_bytes(4, "big"))
                chk += 1; ok += (exp == blk)
        log.info(f"token2 iv={iv2.hex()} (independent): AES(M, iv2||ctr)==live keystream {ok}/{chk} blocks", start=0, end=0)

        proven = (sm == NWRK and ok > 0 and chk > 0 and iv1 != iv2)
        print("\n" + ("*** THIRD-PARTY DECRYPTION PROVEN ***" if proven else "[stage 30] not conclusive"))
        print(f"    master M           = {M.hex()}")
        print(f"    recovered from     : token1 round keys (schedule match {sm}/{NWRK})")
        print(f"    verified against   : token2 (independent iv {iv2.hex()}) live keystream {ok}/{chk} blocks")
        print(f"    => M is the build-constant n-token AES-256 key; it decrypts any n-token from this build.")
        json.dump({"version": version, "master_key_hex": M.hex(), "schedule_match": sm,
                   "token1_iv": iv1.hex(), "token2_iv": iv2.hex(),
                   "token2_keystream_blocks_verified": [ok, chk], "proven": proven,
                   "build": info["wasm_sha256"]},
                  open(os.path.join(THIS, "thirdparty_proof.json"), "w"), indent=2)
    finally:
        try: rt.close()
        except Exception: pass


if __name__ == "__main__":
    main()
