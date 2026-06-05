"""hCaptcha hsj/hsw master-key extraction + pure-Python crypto + PoW.

Public surface:

  * KeyFetcher().fetch() — single call, returns 6 build-static
    verified AES-256 master keys (3 HSJ + 3 HSW) plus a derived
    7th fingerprint identifier.
  * HSJKeyFetcher, HSWKeyFetcher — direct per-bundle extractors.
  * HSWBridge, HSWAnalyzer, HSW — bundle wrappers + analyzer.
  * hsw_crypto, hsw_pow — pure-Python AES-GCM + Hashcash PoW.
  * hsw_n_key_capture — the working n_token AES key extractor used
    by KeyFetcher. Patches the AES encrypt call site (fn arg0) and
    captures the master key at the moment the bundle uses it.
"""
from .keyfetcher import KeyFetcher
from .hsj import HSJKeyFetcher
from .hsw import HSWKeyFetcher
from .hsw_bridge import HSWBridge, HSWAnalyzer
from .hsw_client import HSW
from . import hsw_crypto
from . import hsw_pow
from . import hsw_n_key_capture
from .hsw_n_token_decrypt import decrypt_n_token, NTokenError

__all__ = [
    "KeyFetcher",
    "HSJKeyFetcher",
    "HSWKeyFetcher",
    "HSWBridge",
    "HSWAnalyzer",
    "HSW",
    "hsw_crypto",
    "hsw_pow",
    "hsw_n_key_capture",
    "decrypt_n_token",
    "NTokenError",
]
__version__ = "1.5.0"
