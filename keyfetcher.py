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
    from keyfetcher import KeyFetcher
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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from log import Logger
import version as _v


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

        from keyfetcher_hsj import HSJKeyFetcher
        hsj_out = HSJKeyFetcher(self.version, log=self.log).fetch_keys()
        hsj_keys = {
            "n_key":                hsj_out["n_key"],
            "response_decrypt_key": hsj_out["response_decrypt_key"],
            "payload_encrypt_key":  hsj_out["payload_encrypt_key"],
        }

        # 2. HSW — two keys via WASM bytecode patch of the key schedule
        self.log.info("extracting HSW keys (WASM bytecode patch)...",
                      start=t0, end=time.time())

        from keyfetcher_hsw_keys import HSWKeyFetcher
        hsw_out = HSWKeyFetcher(self.version, log=self.log).fetch()
        hsw_keys = {
            "encrypt_key": hsw_out["encrypt_key"],
            "decrypt_key": hsw_out["decrypt_key"],
        }

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
                "hsw_encrypt_key": hsw_out.get("verified", False),
            },
        }


if __name__ == "__main__":
    out = KeyFetcher().fetch()
    print(json.dumps(out, indent=2))
    with open("hcaptcha_master_keys.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nsaved -> hcaptcha_master_keys.json")
