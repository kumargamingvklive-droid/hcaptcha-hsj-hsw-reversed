"""Entry point: `python -m hcaptcha` prints all 5 keys as JSON."""
import json
from .keyfetcher import KeyFetcher


def main() -> None:
    out = KeyFetcher().fetch()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
