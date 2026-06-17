"""Hashcash-v1 + SHA-1 proof-of-work solver (hsw_pow): mint -> check."""
import pytest
from hcaptcha import hsw_pow


@pytest.mark.parametrize("bits", [0, 4, 8])
def test_mint_then_check(bits):
    stamp = hsw_pow.mint("example-resource", bits)["stamp"]
    assert hsw_pow.check(stamp, expected_resource="example-resource")["valid"]


def test_stamp_format():
    stamp = hsw_pow.mint("res", 4)["stamp"]
    parts = stamp.split(":")
    # 1:bits:date:resource:ext:rand:counter
    assert parts[0] == "1"
    assert parts[1] == "4"
    assert parts[3] == "res"


def test_check_rejects_wrong_resource():
    stamp = hsw_pow.mint("res-a", 4)["stamp"]
    assert not hsw_pow.check(stamp, expected_resource="res-b")["valid"]


def test_check_rejects_tampered():
    stamp = hsw_pow.mint("res", 8)["stamp"]
    # break the counter so the leading-zero-bits property fails
    parts = stamp.split(":")
    parts[-1] = str(int(parts[-1]) + 1)
    assert not hsw_pow.check(":".join(parts), expected_resource="res")["valid"]
