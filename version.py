"""Discover the current hsj/hsw asset version served by hCaptcha.

Both bundles ship at the same /c/{version}/ path, so one lookup gives you both.
"""
import base64, json
import requests

CHECKSITECONFIG = "https://hcaptcha.com/checksiteconfig"
DUMMY_SITEKEY = "00000000-0000-0000-0000-000000000000"


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def latest_version(host: str = "accounts.hcaptcha.com",
                   sitekey: str = DUMMY_SITEKEY,
                   timeout: float = 10.0) -> str:
    """Return the current asset version hash, e.g. '5d10e16e...'."""
    r = requests.get(
        CHECKSITECONFIG,
        params={"host": host, "sitekey": sitekey, "sc": "1", "swa": "1"},
        timeout=timeout,
    )
    r.raise_for_status()
    req_jwt = r.json()["c"]["req"]
    payload = json.loads(_b64url_decode(req_jwt.split(".")[1]))
    return payload["l"].rsplit("/", 1)[-1]


def asset_url(version: str, name: str) -> str:
    return f"https://newassets.hcaptcha.com/c/{version}/{name}"


if __name__ == "__main__":
    v = latest_version()
    print(f"latest version: {v}")
    print(f"hsj url: {asset_url(v, 'hsj.js')}")
    print(f"hsw url: {asset_url(v, 'hsw.js')}")
