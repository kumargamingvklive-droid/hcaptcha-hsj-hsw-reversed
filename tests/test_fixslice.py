"""fixslice32 bitslice / inv_bitslice round-trip + replication property."""
import os
from hcaptcha.tools.fixslice_inverse import bitslice, inv_bitslice


def test_roundtrip():
    for _ in range(50):
        data = os.urandom(32)
        assert inv_bitslice(bitslice(data)) == data


def test_replication_property():
    # bitslice(K || K) -> inv_bitslice gives K || K (both lanes equal)
    for _ in range(20):
        k = os.urandom(16)
        out = inv_bitslice(bitslice(k + k))
        assert out[:16] == out[16:32] == k


def test_distinct_lanes_preserved():
    a, b = os.urandom(16), os.urandom(16)
    out = inv_bitslice(bitslice(a + b))
    assert out[:16] == a and out[16:32] == b
