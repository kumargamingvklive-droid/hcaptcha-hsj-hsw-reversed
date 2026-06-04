"""Live n-token decrypt probe.

Question we answer:
  "Which of our 5 candidate keys (if any) AES-GCM decrypts the n-token
   that window.hsw(jwt) returns?"

How:
  1. Boot HSWBridge (which loads hsw.js inside the jsdom sandbox + the
     sandbox_polyfill so window.hsw(jwt) works to completion).
  2. Synthesize a minimal JWT for a d=1 hashcash challenge.
  3. Call bridge.solve(jwt) -> capture the returned string (base64).
  4. Base64-decode -> raw bytes; classify with two wire-format
     hypotheses:
        H1: iv(12) || ct(N) || tag(16)
        H2: ct(N) || tag(16) || iv(12)
  5. For each candidate key x each wire-format hypothesis, attempt
     AES-256-GCM decrypt (empty AAD).
  6. Also try a 6th key built from the captured n-key trace bytes
     (12 bytes zero-padded to 32) — to see how close the partial
     n_key is to the real key.

Run:
  PYTHONPATH=src python tools/n_token_decrypt_probe.py
"""
from __future__ import annotations

import base64
import json
import sys
import time

from Crypto.Cipher import AES


def b64u_nopad(d: bytes) -> str:
    return base64.urlsafe_b64encode(d).rstrip(b"=").decode("ascii")


def synth_jwt(d: int = 1) -> str:
    now = int(time.time())
    header  = b64u_nopad(json.dumps(
        {"alg": "HS256", "typ": "JWT"},
        separators=(",", ":"),
    ).encode())
    payload = b64u_nopad(json.dumps(
        {"s": "00000000", "d": d, "t": now, "exp": now + 600},
        separators=(",", ":"),
    ).encode())
    return f"{header}.{payload}.fake"


def try_decrypt_gcm(key: bytes, iv: bytes, ct: bytes, tag: bytes):
    """Returns (ok, plaintext or error str)."""
    try:
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        pt = cipher.decrypt_and_verify(ct, tag)
        return True, pt
    except Exception as e:                                       # noqa: BLE001
        return False, str(e)


def make_padded_key(raw: bytes) -> bytes:
    """Right-pad with zeros to 32 bytes (or truncate)."""
    if len(raw) >= 32:
        return raw[:32]
    return raw + b"\x00" * (32 - len(raw))


def main():
    sys.path.insert(0, "src")
    from hcaptcha import HSWBridge, KeyFetcher                     # noqa: WPS433

    # --- 1. fetch keys
    print("[1/4] fetching keys ...")
    keys = KeyFetcher().fetch()
    candidates: dict[str, bytes] = {
        "hsj.n_key":                 bytes.fromhex(keys["hsj"]["n_key"]),
        "hsj.payload_encrypt_key":   bytes.fromhex(keys["hsj"]["payload_encrypt_key"]),
        "hsj.response_decrypt_key":  bytes.fromhex(keys["hsj"]["response_decrypt_key"]),
        "hsw.encrypt_key":           bytes.fromhex(keys["hsw"]["encrypt_key"]),
        "hsw.decrypt_key":           bytes.fromhex(keys["hsw"]["decrypt_key"]),
    }
    # 6th-attempt: the partial trace bytes zero-padded to 32. This is NOT
    # the real n-key (we know it isn't — see KeyFetcher) but is a useful
    # negative control to see how badly an off-key fails.
    if keys["hsw"].get("n_key"):
        partial = bytes.fromhex(keys["hsw"]["n_key"])
        candidates["hsw.n_key_partial+zero_pad"] = make_padded_key(partial)

    # --- 2. boot bridge + solve JWT
    print("[2/4] booting HSWBridge ...")
    bridge = HSWBridge(keys["version"])
    jwt = synth_jwt(d=1)
    print(f"[2/4] solving JWT (d=1)\n      jwt={jwt}")
    n_token = bridge.solve(jwt)
    print(f"[2/4] got n_token ({len(n_token)} chars): {n_token[:120]}...")

    # --- 3. classify wire format
    print("[3/4] decoding n_token ...")
    # try base64 (standard) then urlsafe
    raw = None
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(n_token + "=" * (-len(n_token) % 4))
            break
        except Exception:                                          # noqa: BLE001
            continue
    if raw is None:
        raise RuntimeError(f"could not base64-decode: {n_token!r}")
    print(f"[3/4] raw bytes ({len(raw)}): {raw.hex()[:200]}...")

    # If the n-token is too short to fit IV+TAG, GCM is impossible.
    if len(raw) < 12 + 16:
        print(f"WARNING: n_token only {len(raw)} bytes — too short for any "
              f"iv(12)+tag(16) GCM framing")

    wires = {}
    if len(raw) >= 12 + 16:
        # H1: iv(12) || ct(N) || tag(16)
        wires["H1: iv(12) || ct(N) || tag(16)"] = {
            "iv":  raw[:12],
            "ct":  raw[12:-16],
            "tag": raw[-16:],
        }
        # H2: ct(N) || tag(16) || iv(12)
        wires["H2: ct(N) || tag(16) || iv(12)"] = {
            "iv":  raw[-12:],
            "ct":  raw[:-28],
            "tag": raw[-28:-12],
        }

    fmt_lines = []
    for name, parts in wires.items():
        fmt_lines.append(
            f"  {name}: "
            f"iv={parts['iv'].hex()[:24]}.. ct({len(parts['ct'])})B "
            f"tag={parts['tag'].hex()[:16]}..")
    wire_format_detected = (
        f"raw_len={len(raw)} bytes\n" + "\n".join(fmt_lines)
        if fmt_lines else f"raw_len={len(raw)} bytes (too short for GCM)"
    )
    print(wire_format_detected)

    # --- 4. cross-attempt decrypt
    print("\n[4/4] decrypt attempts:")
    attempts = []
    for key_label, key in candidates.items():
        for wire_name, parts in wires.items():
            ok, res = try_decrypt_gcm(key, parts["iv"], parts["ct"], parts["tag"])
            if ok:
                preview = res[:100].hex() + (" (" + repr(res[:60]) + ")" if any(0x20 <= b < 0x7f for b in res[:8]) else "")
                attempts.append({
                    "key_label":         f"{key_label} | {wire_name}",
                    "key_hex":           key.hex(),
                    "result":            f"SUCCESS ({len(res)} bytes plaintext)",
                    "plaintext_preview": preview[:100],
                })
                print(f"  [HIT] {key_label:<32} | {wire_name:<32} -> {len(res)}B plaintext")
            else:
                attempts.append({
                    "key_label":         f"{key_label} | {wire_name}",
                    "key_hex":           key.hex(),
                    "result":            f"MAC failed: {res}",
                    "plaintext_preview": "",
                })
                print(f"  [MISS] {key_label:<32} | {wire_name:<32} -> {res}")

    # Summary
    hits = [a for a in attempts if a["result"].startswith("SUCCESS")]
    print(f"\n=== SUMMARY: {len(hits)} hits out of {len(attempts)} attempts ===")
    for h in hits:
        print(f"  WINNER: {h['key_label']}")
        print(f"          plaintext preview: {h['plaintext_preview']}")
    if not hits:
        print("  No key/wire-format combination decrypted the n-token.")
        print("  => The n-token is NOT a simple AES-GCM blob under any of "
              "these 5 master keys with either iv|ct|tag or ct|tag|iv "
              "framing.")

    return {
        "jwt":                  jwt,
        "n_token":              n_token,
        "n_token_raw":          raw,
        "wire_format_detected": wire_format_detected,
        "attempts":             attempts,
    }


if __name__ == "__main__":
    out = main()
    # Persist the capture+attempts as JSON next to this script.
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    dump = {
        "jwt": out["jwt"],
        "n_token_b64": out["n_token"],
        "n_token_hex": out["n_token_raw"].hex(),
        "n_token_len_bytes": len(out["n_token_raw"]),
        "wire_format_detected": out["wire_format_detected"],
        "attempts": out["attempts"],
    }
    with open(os.path.join(here, "n_token_decrypt_probe.last.json"), "w") as f:
        json.dump(dump, f, indent=2)
    print(f"\nsaved capture+attempts -> tools/n_token_decrypt_probe.last.json")
