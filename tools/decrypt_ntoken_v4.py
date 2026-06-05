"""decrypt_ntoken_v4.py — final attempt

Now we KNOW:
  * fn 334 IS the fixslice32 KS (constants 0x0F000F00, 0x55555555, 0x33333333)
  * fn 334 takes (output_round_keys_ptr, input_master_key_ptr) — arg1 is the key
  * f334_a1 has 4 distinct 32-byte values (the 4 master keys scheduled
    during n-token production)
  * fn 226 is the encrypt entry (sig i32,i32,i32 -> i32) that calls 334
  * The 96-byte contiguous buffer reconstructed from rings looks like
    consecutive AES key-schedule scratch (NOT pure round-key array)

Strategy:
  * Use ALL 4 distinct f334_a1 values as primary candidates
  * Also use all 6 distinct buf-merged 32B sliding windows
  * For each, try as AES-256 key under MANY wire formats including
    PoW-stamp/header prepended formats and AES-256-CTR mode
  * Try AES-256-CTR + manual GMAC (some Rust crypto libs do GCM in
    pieces that yield different framing)
  * Try AES-128-GCM (in case it's 128-bit key + counter)
"""
from __future__ import annotations
import base64, hashlib, hmac, json, os, struct, sys, time

THIS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS)
sys.path.insert(0, os.path.join(ROOT, "src"))

from Crypto.Cipher import AES
from Crypto.Util import Counter as _Ctr
from hcaptcha.tools.fixslice_inverse import inv_bitslice


def _try_gcm(key, iv, ct, tag, aad=b""):
    if len(key) not in (16, 24, 32): return None
    if len(iv) < 1 or len(tag) not in (12, 16): return None
    try:
        c = AES.new(key, AES.MODE_GCM, nonce=iv, mac_len=len(tag))
        if aad: c.update(aad)
        return c.decrypt_and_verify(ct, tag)
    except Exception:
        return None


def _try_ctr(key, iv, ct):
    """Try AES-CTR decrypt and check if plaintext looks like JSON/msgpack."""
    if len(key) not in (16, 24, 32): return None
    try:
        if len(iv) == 16:
            ctr = _Ctr.new(128, initial_value=int.from_bytes(iv, "big"))
        elif len(iv) == 12:
            ctr = _Ctr.new(32, prefix=iv, initial_value=1)
        else:
            return None
        c = AES.new(key, AES.MODE_CTR, counter=ctr)
        return c.decrypt(ct)
    except Exception:
        return None


def looks_like_pt(pt: bytes) -> bool:
    """Heuristic: msgpack starts with 0x82-0x8f / 0xc4-0xc6 etc., JSON starts with { or ["""
    if not pt: return False
    b0 = pt[0]
    # JSON
    if b0 in (0x7b, 0x5b, 0x22):  # { [ "
        return True
    # msgpack map prefix
    if 0x80 <= b0 <= 0x8f or 0x90 <= b0 <= 0x9f:
        # check first few bytes look ASCII
        return any(0x20 <= b < 0x7f for b in pt[:10])
    return False


def b64_to_bytes(s):
    s = s.strip()
    pad = "=" * (-len(s) % 4)
    for dec in (base64.urlsafe_b64decode, base64.b64decode):
        try: return dec(s + pad)
        except Exception: continue
    raise ValueError("b64 decode failed")


def main():
    with open(os.path.join(THIS, "capture_ntoken_key.last.json")) as f:
        cap = json.load(f)

    raw = b64_to_bytes(cap["token"])
    print(f"[+] raw token: {len(raw)} bytes")
    print(f"    head: {raw[:32].hex()}")
    print(f"    tail: {raw[-32:].hex()}")

    # Primary candidates: distinct values of f334_a1 (= master_key arg to KS)
    primary = []
    for name in ["f334_a1", "f334_a0", "f277_a0", "f277_a1", "f330_a0", "f330_a1", "f330_a2"]:
        from collections import Counter as C
        recs = cap["captured"].get(name, [])
        if not recs: continue
        c = C(r["key32_hex"] for r in recs)
        for v, count in c.most_common():
            primary.append((f"{name}/x{count}", bytes.fromhex(v)))

    # Dedupe
    seen = set(); uniq = []
    for lbl, k in primary:
        if k in seen: continue
        seen.add(k); uniq.append((lbl, k))
    primary = uniq
    print(f"[+] {len(primary)} unique primary candidates")

    # Derived: every 32B sliding window across the merged buffer
    merged = bytes.fromhex(
        "5ba849c2809699d41d2dbbb3b3ad84c4f742712ccdeaf9caa30136c3ae02d8d6"
        "e41d4c4a25467f922d5597de9a379a7fbbbfdd1b6f92fa17a1e93e35a2d9e41f"
        "3bd9f067af35b548c46bb33b53b0aa8662e20570f3cc807debec67e02fac83ba"
    )
    for off in range(0, len(merged) - 32 + 1):
        k = merged[off:off+32]
        if k in seen: continue
        seen.add(k); primary.append((f"win@{off}", k))

    # Apply derived transforms
    transforms = []
    for lbl, k in list(primary):
        for tname, tk in [
            ("sha256", hashlib.sha256(k).digest()),
            ("rev", k[::-1]),
            ("xor_blocks", bytes(a ^ b for a, b in zip(k[:16], k[16:32])) * 2),
        ]:
            if tk in seen: continue
            seen.add(tk); transforms.append((f"{lbl}/{tname}", tk))
        # inv_bitslice
        try:
            bs = list(struct.unpack("<8I", k))
            inv = inv_bitslice(bs)
            for tlbl, tk in [("inv_bs", inv), ("inv_bs_dup", inv[:16] + inv[:16])]:
                if tk in seen or len(tk) != 32: continue
                seen.add(tk); transforms.append((f"{lbl}/{tlbl}", tk))
        except Exception:
            pass
    primary.extend(transforms)

    print(f"[+] {len(primary)} total candidates (incl. transforms)")

    # Layouts: extensive
    layouts = []
    # Standard 3-piece layouts with various header/trailer lengths
    for hdr in [0, 1, 2, 4, 8, 12, 16, 24, 32]:
        for trl in [0, 1, 2, 4, 8, 12, 16, 24]:
            inner = raw[hdr:len(raw)-trl] if trl else raw[hdr:]
            if len(inner) < 1 + 12 + 16: continue
            # All 6 piece orderings with 12-byte IV
            for nm, ivp, ctp, tagp in [
                ("ct||tag||iv", (-12, None), (None, -28), (-28, -12)),
                ("iv||ct||tag", (None, 12), (12, -16), (-16, None)),
                ("ct||iv||tag", (-28, -16), (None, -28), (-16, None)),
                ("tag||ct||iv", (-12, None), (16, -12), (None, 16)),
                ("iv||tag||ct", (None, 12), (28, None), (12, 28)),
                ("tag||iv||ct", (16, 28), (28, None), (None, 16)),
            ]:
                def _sl(s, e):
                    if s is None and e is None: return inner[:]
                    if s is None: return inner[:e]
                    if e is None: return inner[s:]
                    return inner[s:e]
                iv = _sl(*ivp); ct = _sl(*ctp); tag = _sl(*tagp)
                if len(iv) == 12 and len(tag) == 16 and len(ct) > 0:
                    layouts.append((f"H{hdr}T{trl}/{nm}", iv, ct, tag))

    print(f"[+] {len(layouts)} envelope layouts")

    # AAD candidates
    aads = [b""]
    if cap.get("jwt"):
        jwt = cap["jwt"]
        aads.append(jwt.encode())
        parts = jwt.split(".")
        if len(parts) >= 2:
            aads.append((parts[0] + "." + parts[1]).encode())  # signing input
            for p in parts[:2]:
                aads.append(p.encode())
                try: aads.append(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
                except: pass
    if cap.get("wasm_sha256"):
        aads.append(cap["wasm_sha256"].encode())
        try: aads.append(bytes.fromhex(cap["wasm_sha256"]))
        except: pass

    # Dedupe aads
    seen_aad = set(); uniq_aads = []
    for a in aads:
        if a in seen_aad: continue
        seen_aad.add(a); uniq_aads.append(a)
    aads = uniq_aads
    print(f"[+] {len(aads)} AAD candidates")

    # GCM sweep
    print(f"[+] starting GCM sweep: {len(primary)} keys x {len(layouts)} layouts "
          f"x {len(aads)} aads = {len(primary)*len(layouts)*len(aads)} attempts")
    t0 = time.time()
    n = 0
    for klbl, k in primary:
        for llbl, iv, ct, tag in layouts:
            for aad in aads:
                n += 1
                pt = _try_gcm(k, iv, ct, tag, aad)
                if pt is not None:
                    print(f"\n*** GCM HIT after {n} attempts ***")
                    print(f"    key: {k.hex()}")
                    print(f"    key_label: {klbl}")
                    print(f"    layout: {llbl}")
                    print(f"    aad: {aad[:50].hex()}")
                    print(f"    pt[{len(pt)}]: {pt[:200]!r}")
                    out = {"success": True, "key_hex": k.hex(), "key_label": klbl,
                           "layout": llbl, "aad_hex": aad.hex() if aad else "",
                           "plaintext_hex": pt.hex(), "plaintext_head": pt[:512].hex(),
                           "attempts": n, "elapsed_sec": time.time()-t0,
                           "method": "AES-256/192/128-GCM"}
                    with open(os.path.join(THIS, "decrypt_ntoken_v4.last.json"), "w") as f:
                        json.dump(out, f, indent=2)
                    return 0
    elapsed = time.time() - t0
    print(f"\n[+] no GCM hit. {n} attempts in {elapsed:.1f}s")

    # CTR sweep — try AES-CTR with various IV layouts and key candidates
    # Plaintext should look like msgpack or JSON
    print(f"\n[+] trying AES-CTR sweep ({len(primary)} keys × few layouts)")
    n_ctr = 0
    for klbl, k in primary[:50]:  # only top candidates
        if len(k) not in (16, 24, 32): continue
        for iv_off in [0, 1, 2, 4, 8, 12, 16, -12, -16]:
            if iv_off < 0:
                iv = raw[iv_off-16:iv_off] if iv_off != -16 else raw[-16:]
            else:
                iv = raw[iv_off:iv_off+16]
            if len(iv) != 16: continue
            # Try ct as the remainder
            for ct_start, ct_end in [(0, None), (16, None), (-16, None), (12, -12)]:
                ct = raw[ct_start:ct_end] if ct_end else raw[ct_start:]
                pt = _try_ctr(k, iv, ct)
                n_ctr += 1
                if pt and looks_like_pt(pt):
                    print(f"\n*** CTR HIT after {n_ctr} ctr attempts ***")
                    print(f"    key: {k.hex()}")
                    print(f"    iv_off={iv_off} ct_range=({ct_start},{ct_end})")
                    print(f"    pt[{len(pt)}]: {pt[:200]!r}")
                    out = {"success": True, "key_hex": k.hex(), "key_label": klbl + "/CTR",
                           "iv": iv.hex(), "ct_range": [ct_start, ct_end],
                           "plaintext_hex": pt.hex(), "method": "AES-256-CTR"}
                    with open(os.path.join(THIS, "decrypt_ntoken_v4.last.json"), "w") as f:
                        json.dump(out, f, indent=2)
                    return 0
    print(f"[+] no CTR hit ({n_ctr} attempts)")

    with open(os.path.join(THIS, "decrypt_ntoken_v4.last.json"), "w") as f:
        json.dump({"success": False, "n_gcm": n, "n_ctr": n_ctr,
                   "elapsed_sec": time.time()-t0}, f, indent=2)
    return 1


if __name__ == "__main__":
    sys.exit(main())
