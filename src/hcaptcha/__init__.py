"""hCaptcha hsj/hsw master-key extraction + pure-Python crypto + PoW."""
from .keyfetcher import KeyFetcher
from .hsj import HSJKeyFetcher
from .hsw import HSWKeyFetcher
from .hsw_bridge import HSWBridge, HSWAnalyzer
from .hsw_client import HSW
from . import hsw_crypto
from . import hsw_pow
# hsw_n_key is OPT-IN: legacy LCG-based N-key derivation for archived
# builds (eras a-c). Raises RuntimeError on current builds where the
# constants are emitted by a deobf helper. See docs/09 + docs/10.
from . import hsw_n_key

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
]
__version__ = "1.2.0"
