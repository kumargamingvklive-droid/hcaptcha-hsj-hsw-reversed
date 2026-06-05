# Deobfuscated HSW + HSJ bundles

Frozen, fully-deobfuscated snapshots of `hsw.js` and `hsj.js` for the
build hash in [`MANIFEST.json`](MANIFEST.json). Refreshed when the
underlying build is rotated — by design this folder mirrors what was
live at the moment the snapshot was taken, not whatever hCaptcha is
currently serving.

## Contents

| File                  | Size   | What it is |
| --------------------- | -----: | ---------- |
| [`hsw_deobf.js`](./hsw_deobf.js)              | ~250 KB  | The WebAssembly-backed proof-of-work + n-token bundle, run through the 12-pass deobfuscation pipeline. Every wbg wrapper, every dispatcher branch, and every JS-side helper is in its post-rename form (single- and two-letter identifiers preserved as emitted; structurally clean). |
| [`hsw_deobf.blobs.bin`](./hsw_deobf.blobs.bin)   | ~596 KB  | The raw WebAssembly blob extracted from `hsw_deobf.js` (concatenation of every `jn(...)` base64 chunk). This is the byte-perfect input to `WasmModule(wasm_bytes)` — load it directly to skip the bundle download. |
| [`hsw_deobf.blobs.json`](./hsw_deobf.blobs.json) | ~2.5 KB  | Metadata for the blob: chunk order, sha256, vaddr layout. |
| [`hsj_deobf.js`](./hsj_deobf.js)              | ~1.6 MB  | The asm.js-style "inspekt_client" / fingerprint bundle, after the same deobfuscation pipeline. Hand-rolled compiled JS (not Rust→WASM) — most of the size is the fingerprint surface plus the in-JS AES-256 implementation. |
| [`hsj_deobf.blobs.bin`](./hsj_deobf.blobs.bin)   | ~57 KB   | The string-table + constant blobs extracted from `hsj_deobf.js` (the asm.js-style equivalent of WASM's data section). |
| [`hsj_deobf.blobs.json`](./hsj_deobf.blobs.json) | ~1.4 KB  | Metadata for the HSJ blobs. |
| [`MANIFEST.json`](./MANIFEST.json)            | small    | Build hash + per-file size + sha256, so any rebuild from `src/hcaptcha/tools/deobf.py` is bit-identical. |

## How to regenerate

The whole pipeline is reproducible — there are no manual edits:

```bash
PYTHONPATH=src python -c "
from hcaptcha.tools.deobf import fetch_and_deobfuscate
from hcaptcha import version as v
ver = v.latest_version()
fetch_and_deobfuscate('hsw.js', ver, out_dir='deobfuscated')
fetch_and_deobfuscate('hsj.js', ver, out_dir='deobfuscated')
"
```

`fetch_and_deobfuscate` runs:

  1. Fetch raw bundle from `https://newassets.hcaptcha.com/c/<build>/<name>`.
  2. `js-beautify` pretty-printer (Python wrapper).
  3. `src/hcaptcha/tools/deobf.js` 12-pass deobfuscation (literal folding,
     string-table decryption, dead-code elimination, wbg shim recovery,
     identifier de-mangling, etc — see [`../docs/03-deobfuscation.md`](../docs/03-deobfuscation.md)).
  4. Final pretty-print.

## What you can do with these

  - **Read the bundles** — no other tooling required. The 12 passes
    leave both files structurally clean: `window.hsw = function (a, b)
    { ... }` is at the bottom of `hsw_deobf.js`, the dispatcher table
    is around the middle, and the wbg shim is up top.
  - **Re-derive function indices** — the dispatcher / encrypt / key-
    schedule / SHA-1 PoW function indices used by `hsw_n_key_capture.py`
    are all visible in the WASM at the same byte positions as in
    `hsw_deobf.blobs.bin`.
  - **Validate the Rust-crate inventory** — every algorithm identified
    in [`../docs/13-hsw-rust-crates.md`](../docs/13-hsw-rust-crates.md)
    has its supporting evidence (XxHash3 PRIME64 quadruple, ChaCha20
    sigma words, SHA-1 K constants, fixslice32 masks, PCG LCG
    multiplier) directly inspectable here.
  - **Diff across builds** — keep a copy of the previous build's
    `hsw_deobf.js` and `diff` it against a fresh one to track exactly
    which dispatcher magic / function index / KS site / wrapper name
    rotated between runs.

## Provenance

This is **`hCaptcha` upstream content**, downloaded from
`newassets.hcaptcha.com`, pretty-printed and structurally cleaned only.
No semantic edits, no patches, no instrumentation added — what you
read here is what the bundle does at runtime, just legible. The
deobfuscation pipeline is in [`../src/hcaptcha/tools/deobf.js`](../src/hcaptcha/tools/deobf.js)
and [`../src/hcaptcha/tools/deobf.py`](../src/hcaptcha/tools/deobf.py)
for full audit. The build hash in `MANIFEST.json` lets you re-fetch
the exact upstream bytes for byte-for-byte verification.
