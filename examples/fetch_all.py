"""Fetch all five hCaptcha master keys for the current build."""
import json
import sys
from pathlib import Path

# Add the repo's src/ to sys.path so this runs without `pip install`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hcaptcha import KeyFetcher


def main() -> None:
    keys = KeyFetcher().fetch()
    print(json.dumps(keys, indent=2))


if __name__ == "__main__":
    main()
