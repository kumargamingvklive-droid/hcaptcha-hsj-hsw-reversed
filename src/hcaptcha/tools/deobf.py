"""End-to-end pipeline: download → beautify → deobfuscate hsj.js / hsw.js.

The actual deobfuscator is the Node script `deobf.js` in this directory —
this Python wrapper handles the fetch + beautify so it's a single command.

Requires:
    pip install jsbeautifier requests
    npm install acorn astring
"""
import os
import subprocess
import sys
import jsbeautifier
import requests

from .. import version as _v


def fetch_and_deobfuscate(name: str, version: str | None = None,
                          out_dir: str = ".", quiet: bool = False) -> str:
    """Fetch hsj.js or hsw.js for the given version, beautify, deobfuscate.
    Returns the path to the deobfuscated output file. Set quiet=True to
    silence progress output."""
    def _p(s):
        if not quiet: print(s)

    version = version or _v.latest_version()
    url = _v.asset_url(version, name)
    _p(f"fetching {url}")
    r = requests.get(url)
    r.encoding = "utf-8"
    raw_path = os.path.join(out_dir, f"{name.replace('.js', '')}_raw.js")
    open(raw_path, "w", encoding="utf-8").write(r.text)

    opts = jsbeautifier.default_options()
    opts.indent_size = 2
    pretty = jsbeautifier.beautify(r.text, opts)
    pretty_path = os.path.join(out_dir, f"{name.replace('.js', '')}_b.js")
    open(pretty_path, "w", encoding="utf-8").write(pretty)

    deobf_path = os.path.join(out_dir, f"{name.replace('.js', '')}_deobf.js")
    deobf_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deobf.js")
    cmd = ["node", deobf_js, pretty_path, deobf_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not quiet:
        # Emit only summary lines from the deobf node process
        for line in result.stderr.splitlines():
            if line.startswith("[deobf] ") and ("decoder-calls" in line or
                                                  "renamed locals" in line or
                                                  "wrote" in line):
                continue  # skip verbose per-pass counters
    if result.returncode != 0:
        raise RuntimeError(f"deobf failed: {result.stderr}")
    _p(f"deobfuscated -> {deobf_path}")

    os.remove(raw_path)
    os.remove(pretty_path)
    return deobf_path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "both"
    version = sys.argv[2] if len(sys.argv) > 2 else None
    if target == "both":
        for name in ("hsj.js", "hsw.js"):
            fetch_and_deobfuscate(name, version)
    else:
        fetch_and_deobfuscate(target, version)
