"""One-pass test runner for the full n-key tracer."""
import json, sys
from hcaptcha.hsw_n_key_full import trace_full_n_key

r = trace_full_n_key(two_pass=True, instrument_i32=False, instrument_i64=True)
print("=== RESULT ===")
print("full_hex:", r.get("full_hex"))
print("base_ptr:", r.get("base_ptr_hex"))
print("filled:", r.get("bytes_captured"))
print("repeatable:", r.get("repeatable"))
print("debug pass1:", r.get("_pass1_debug"))
