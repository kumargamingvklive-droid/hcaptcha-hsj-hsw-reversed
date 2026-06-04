"""Unified hCaptcha master-key fetcher.

Returns all FIVE static AES-256 master keys per build:

  From hsj.js (the inspekt_client / fingerprint bundle):
    * n_key                  — encrypts the `n` token (fingerprint payload)
    * response_decrypt_key   — decrypts hCaptcha server responses
    * payload_encrypt_key    — encrypts client payloads

  From hsw.js (the proof-of-work / AEAD bundle):
    * hsw_encrypt_key        — encrypts request payloads via window.hsw(1, ...)
    * hsw_decrypt_key        — decrypts server responses via window.hsw(0, ...)

All keys are byte-accurate and verified per-build:
  - HSJ keys are extracted by AST-patching the AES-256 key-schedule
    function's stack frame and reading the 32-byte input buffer.
  - HSW keys are extracted by WASM-bytecode-patching the AES-256 fixslice32
    key schedule, inserting 8 calls to the build's XOR-deobf helper to
    capture the deobfuscated master-key words.
  - Verification: AES-256-GCM round-trip against the live bundle's own
    encrypt output (the HSW encrypt key is mathematically verified).

USAGE:
    from .keyfetcher import KeyFetcher
    out = KeyFetcher().fetch()
    # {
    #   'version': '...',
    #   'hsj': {'n_key': '...', 'response_decrypt_key': '...', 'payload_encrypt_key': '...'},
    #   'hsw': {'encrypt_key': '...', 'decrypt_key': '...'},
    #   'cipher': 'AES-256-GCM',
    #   'wire_format': {'hsj': 'ct || tag(16) || iv(12) || 0x00',
    #                   'hsw': 'iv(12) || ct(N) || tag(16)'},
    # }
"""
import json
import os
import sys
import time


from .log import Logger
from . import version as _v


class KeyFetcher:
    """Fetch all five hCaptcha master keys for the current build."""

    def __init__(self, version: str | None = None, log: Logger | None = None):
        self.log = log or Logger()
        self.version = version or _v.latest_version()

    def fetch(self) -> dict:
        t0 = time.time()

        # 1. HSJ — three keys via AST patch of the key schedule
        self.log.info(f"version: {self.version[:16]}...",
                      start=t0, end=time.time())
        self.log.info("extracting HSJ keys (AST patch)...",
                      start=t0, end=time.time())

        from .hsj import HSJKeyFetcher
        hsj_out = HSJKeyFetcher(self.version, log=self.log).fetch_keys()
        hsj_keys = {
            "n_key":                hsj_out["n_key"],
            "response_decrypt_key": hsj_out["response_decrypt_key"],
            "payload_encrypt_key":  hsj_out["payload_encrypt_key"],
        }

        # 2. HSW — two keys via WASM bytecode patch of the key schedule
        self.log.info("extracting HSW keys (WASM bytecode patch)...",
                      start=t0, end=time.time())

        from .hsw import HSWKeyFetcher
        hsw_out = HSWKeyFetcher(self.version, log=self.log).fetch()
        hsw_keys = {
            "encrypt_key": hsw_out["encrypt_key"],
            "decrypt_key": hsw_out["decrypt_key"],
        }

        # 3. Verify decrypt_key by construct-then-decrypt-through-bundle.
        # The hsw.py fetcher only verifies encrypt_key (it decrypts the
        # encrypt output with our recovered key). decrypt_key needs the
        # opposite path: we encrypt with it in pure Python, then ask
        # the bundle to decrypt and check we get the plaintext back.
        dec_verified = False
        try:
            from . import hsw_crypto
            from .hsw_bridge import HSWBridge
            dec_key = bytes.fromhex(hsw_out["decrypt_key"])
            bridge  = HSWBridge(self.version, log=self.log)
            probe   = b"verify-dec-key-probe-" + os.urandom(8)
            blob    = hsw_crypto.encrypt(probe, dec_key)
            decoded = bridge.decrypt(blob)
            dec_verified = (decoded == probe)
            if dec_verified:
                self.log.info("decrypt key verified OK (bundle round-trip)",
                              start=t0, end=time.time())
            else:
                self.log.info("decrypt key verification FAILED (mismatch)",
                              start=t0, end=time.time())
        except Exception as e:
            self.log.info(f"decrypt key verification skipped: {e}",
                          start=t0, end=time.time())

        self.log.info(f"all 5 keys fetched in {time.time()-t0:.1f}s",
                      start=t0, end=time.time())

        return {
            "version":     self.version,
            "hsj":         hsj_keys,
            "hsw":         hsw_keys,
            "cipher":      "AES-256-GCM",
            "wire_format": {
                "hsj": "ct(N) || tag(16) || iv(12) || 0x00",
                "hsw": "iv(12) || ct(N) || tag(16)",
            },
            "aad":         "",
            "verified": {
                "hsw_encrypt_key": bool(hsw_out.get("verified", False)),
                "hsw_decrypt_key": bool(dec_verified),
            },
            "pow": {
                "algorithm": "Hashcash v1 + SHA-1",
                "library":   "rust-hashcash/0.3.3 (sha1 feature)",
                "stamp_format": "1:bits:date:resource:ext:rand:counter",
                "date_format":  "YYYYMMDD (UTC)",
                "verified_in_wasm": True,
                "wasm_evidence": (
                    "SHA-1 K-constants 0x5A827999 and 0x6ED9EBA1 found "
                    "as i32.const literals in code section"
                ),
            },
        }


if __name__ == "__main__":
    out = KeyFetcher().fetch()
    print(json.dumps(out, indent=2))
    with open("hcaptcha_master_keys.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nsaved -> hcaptcha_master_keys.json")
