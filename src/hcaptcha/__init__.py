"""hCaptcha hsj/hsw master-key extraction + pure-Python crypto + PoW.

Public surface (current era (d) builds):

  * KeyFetcher().fetch() — single call, returns up to 7 keys (3 HSJ,
    4 HSW: encrypt, decrypt, n_key partial, fingerprint_blob_key).
  * HSJKeyFetcher, HSWKeyFetcher — direct per-bundle extractors.
  * HSWBridge, HSWAnalyzer, HSW — bundle wrappers + analyzer.
  * hsw_crypto, hsw_pow — pure-Python crypto + Hashcash PoW.

Opt-in N-key derivation modules (advanced; usually invoked via
KeyFetcher.fetch() automatically):

  * hsw_n_key          — legacy LCG-based N-key derivation for
                         archived builds (eras a-c). Raises on era (d)
                         where constants are emitted by a deobf helper.
                         See docs/09 and docs/10.
  * hsw_n_key_runtime  — APPROACH A: runtime trace of the byte-store
                         helper inside vc that emits the LCG-derived
                         N-key bytes. Recovers a partial N-key on era
                         (d) builds (12 contiguous bytes on the
                         currently inspected build).
  * hsw_deobf_emulator — APPROACH B: pure-Python WASM emulator covering
                         the opcode subset used by the deobf helpers.
                         Scaffolded for any future build that returns
                         to a fully build-static N-key derivation.
"""
from .keyfetcher import KeyFetcher
from .hsj import HSJKeyFetcher
from .hsw import HSWKeyFetcher
from .hsw_bridge import HSWBridge, HSWAnalyzer
from .hsw_client import HSW
from . import hsw_crypto
from . import hsw_pow
# N-key extractors (see module docstrings for build-era applicability):
from . import hsw_n_key
from . import hsw_n_key_runtime
from . import hsw_n_key_full
from . import hsw_n_key_capture          # FINAL extractor: direct AES-site capture
from . import hsw_deobf_emulator

__all__ = [
    "KeyFetcher",
    "HSJKeyFetcher",
    "HSWKeyFetcher",
    "HSWBridge",
    "HSWAnalyzer",
    "HSW",
    "hsw_crypto",
    "hsw_pow",
    "hsw_n_key",
    "hsw_n_key_runtime",
    "hsw_n_key_full",
    "hsw_n_key_capture",
    "hsw_deobf_emulator",
]
__version__ = "1.5.0"
