# Architecture overview

This document gives the 30,000-foot view of how `hsj.js` and `hsw.js` fit
into hCaptcha's challenge-response flow, and how the tools in this
repo line up against them.

## The hCaptcha client wire flow

A browser loading hCaptcha downloads three things from
`newassets.hcaptcha.com`:

```
https://newassets.hcaptcha.com/c/<version>/hcaptcha.js   ← visible UI loader
https://newassets.hcaptcha.com/c/<version>/hsj.js        ← inspekt_client (fingerprint + n token)
https://newassets.hcaptcha.com/c/<version>/hsw.js        ← PoW + request/response AEAD
```

`<version>` is a 64-char hex hash that **rotates roughly every 10
minutes** — hCaptcha rebuilds the bundles continuously, the function
indices and magic numbers inside them rotate, and the master keys
rotate with them. The freshest hash is broadcast inside a JWT served
by `hcaptcha.com/checksiteconfig`. [`version.py`](../version.py) does
that discovery and constructs the asset URL.

Once loaded, the page calls into both bundles:

| Browser-side call          | Bundle | Effect                                                  |
| -------------------------- | ------ | ------------------------------------------------------- |
| `hsj(siteKey, ...)`        | hsj    | Collects fingerprint, encrypts as the `n` token         |
| `hsj(0, response)`         | hsj    | Decrypts an hCaptcha server response                    |
| `hsj(1, payload)`          | hsj    | Encrypts a client payload                               |
| `window.hsw(jwt)`          | hsw    | Computes the proof-of-work token from the JWT `req`     |
| `window.hsw(0, response)`  | hsw    | Decrypts an hCaptcha server response                    |
| `window.hsw(1, payload)`   | hsw    | Encrypts a client request                               |

Both bundles run as IIFEs to keep their internals (string tables, AES
keys, decoder functions) closure-private.

## Two bundles, two compile targets, five master keys

|                  | **hsj.js** (`inspekt_client`)                          | **hsw.js**                                                  |
| ---------------- | ------------------------------------------------------ | ----------------------------------------------------------- |
| Compile target   | asm.js-style hand-rolled compiled JS                    | wasm-bindgen wrapper over Rust → WebAssembly                |
| Crypto location  | JS-side AES in linear `Int8Array` heap                  | WASM-side AES with `aes-soft` fixslice32                    |
| Keys extracted   | `n_key`, `response_decrypt_key`, `payload_encrypt_key` | `encrypt_key`, `decrypt_key`                                |
| Extraction       | AST patch + drive (`hcaptcha.hsj`)                     | WASM bytecode patch + drive (`hcaptcha.hsw`)                |
| Verification     | Direct decrypt round-trip                               | AES-256-GCM authentication-tag check                        |

```python
from keyfetcher import KeyFetcher
keys = KeyFetcher().fetch()
# → 5 master AES-256 keys + cipher / wire-format metadata
```

## Repo layout

```
hcaptcha-hsj-hsw-reversed/
├── README.md
├── LICENSE
├── pyproject.toml
├── package.json + package-lock.json   Node deps (acorn, astring, jsdom, canvas)
│
├── docs/                              ← you are here
│   ├── 00-architecture.md
│   ├── 01-hsj-bundle.md
│   ├── 02-hsw-bundle.md
│   ├── 03-deobfuscation.md
│   ├── 04-key-extraction.md
│   ├── 05-wasm-internals.md
│   ├── 06-fixslice32.md
│   └── 07-wasm-patching.md
│
├── examples/
│   └── fetch_all.py                   ← drop-in usage example
│
└── src/hcaptcha/                      ← Python package
    ├── __init__.py                    exports KeyFetcher, HSJKeyFetcher, ...
    ├── __main__.py                    `python -m hcaptcha`
    ├── keyfetcher.py                  unified entry: all 5 keys
    ├── hsj.py                         HSJ keys (AST-patch method)
    ├── hsw.py                         HSW keys (bytecode-patch method)
    ├── hsw_bridge.py                  HSWBridge + HSWAnalyzer (encrypt/decrypt as a service)
    ├── algorithm.py                   AES-256-GCM + xxhash + msgpack helpers (HSJ-compat)
    ├── log.py                         minimal Logger
    ├── version.py                     asset-version discovery
    │
    └── tools/                         ← internal infrastructure
        ├── __init__.py
        ├── wasm_disasm.py             WASM 1.0 disassembler + structural locators
        ├── wasm_writer.py             WASM 1.0 byte-perfect re-emitter / patcher
        ├── fixslice_inverse.py        RustCrypto fixslice32 reference
        ├── deobf.py + deobf.js        12-pass deobfuscation pipeline
        ├── js_runtime.py              Python side of the Node + jsdom sandbox
        └── _js_runner.js              Node side of the sandbox
```

## End-to-end pipeline

```bash
# Install deps (once)
pip install pycryptodome xxhash msgpack jsbeautifier requests
npm install

# Extract all five keys
PYTHONPATH=src python -m hcaptcha
```

Internally:

1. `version.py` discovers the current `<version>` from
   `hcaptcha.com/checksiteconfig`.
2. `keyfetcher_hsj.HSJKeyFetcher` downloads `hsj.js`, AST-patches the
   key schedule, runs the bundle three times to capture all three
   keys.
3. `keyfetcher_hsw_keys.HSWKeyFetcher` downloads `hsw.js`, runs
   `deobf.py` to extract the encrypt/decrypt magic numbers,
   structurally locates the key-schedule and XOR-deobf functions in
   the WASM, builds a patched binary via `wasm_writer.py` that copies
   the master key bytes to a known scratch region, hooks
   `WebAssembly.instantiate` to substitute the patched binary, runs
   one encrypt + one decrypt, reads back both master keys, and
   verifies the encrypt key by AES-256-GCM round-trip.
4. All 5 keys returned in one dict plus cipher / wire-format metadata.

## Documentation route

Continue in [`01-hsj-bundle.md`](./01-hsj-bundle.md) for the HSJ
internals, then [`02-hsw-bundle.md`](./02-hsw-bundle.md) for HSW. The
deobf pipeline is in [`03-deobfuscation.md`](./03-deobfuscation.md).
Key-extraction status is in [`04-key-extraction.md`](./04-key-extraction.md).
WASM format reference is in [`05-wasm-internals.md`](./05-wasm-internals.md).
The bit-sliced AES math (why JS-observation alone can't recover the
HSW key) is in [`06-fixslice32.md`](./06-fixslice32.md). The bytecode
patching technique that solves it is in
[`07-wasm-patching.md`](./07-wasm-patching.md).
