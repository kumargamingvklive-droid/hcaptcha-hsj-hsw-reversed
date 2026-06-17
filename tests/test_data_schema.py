"""Schema validation for data/keys.json and the data/archive snapshots.

These catch silent corruption / schema drift in the auto-refreshed key
data (the refresh-keys workflow commits here every 12h)."""
import json
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

HEX64 = re.compile(r"^[0-9a-f]{64}$")
HSJ_KEYS = {"n_key", "response_decrypt_key", "payload_encrypt_key"}
HSW_KEYS = {"encrypt_key", "decrypt_key"}


def _snapshots():
    out = []
    kj = os.path.join(DATA, "keys.json")
    if os.path.exists(kj):
        out.append(kj)
    arc = os.path.join(DATA, "archive")
    if os.path.isdir(arc):
        out += [os.path.join(arc, f) for f in os.listdir(arc) if f.endswith(".json")]
    return out


@pytest.mark.parametrize("path", _snapshots(), ids=lambda p: os.path.basename(p))
def test_snapshot_valid(path):
    with open(path) as f:
        d = json.load(f)                        # parses without error
    assert HEX64.match(d["version"]), f"bad version in {path}"
    assert HSJ_KEYS <= set(d.get("hsj", {})), f"missing HSJ keys in {path}"
    assert HSW_KEYS <= set(d.get("hsw", {})), f"missing HSW keys in {path}"
    for section in ("hsj", "hsw"):
        for name, val in d[section].items():
            if val is None:
                continue
            assert isinstance(val, str) and HEX64.match(val), \
                f"{section}.{name} not 64-hex in {path}"


def test_keys_json_present():
    assert os.path.exists(os.path.join(DATA, "keys.json")), "data/keys.json missing"
