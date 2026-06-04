# HSW — complete reverse engineering summary

This document is the canonical reference for everything we know about
`hsw.js` after the full reverse engineering effort.

## TL;DR — the complete answer

`hsw.js` is a wasm-bindgen Rust module compiled to WebAssembly. The
JS-side surface is `window.hsw(...)`, which dispatches three operations:

| Call form              | Operation                                       | Crypto                    |
| ---------------------- | ----------------------------------------------- | ------------------------- |
| `window.hsw(0, blob)`  | Decrypt server response                         | AES-256-GCM, fixed key    |
| `window.hsw(1, bytes)` | Encrypt request payload                         | AES-256-GCM, fixed key    |
| `window.hsw(jwt)`      | Build n-token (PoW solution + fingerprint blob) | Hashcash + custom envelope|

We have **completely reversed** the first two and the PoW part of the
third. The fingerprint-blob envelope inside the n-token uses a
per-call ephemeral key whose derivation we can observe but cannot
reproduce statically because hCaptcha intentionally mixes runtime
input (`Math.round(Date.now()/1000)` from the JS-side `rc(...)`
wrapper) into the key-derivation LCG seed on every call.

## Master keys (5 build-static)

| Bundle | Key                       | Verified | Method                                            |
| ------ | ------------------------- | -------- | ------------------------------------------------- |
| HSJ    | `n_key`                   | ✅       | AST-patch of the key-schedule stack frame         |
| HSJ    | `response_decrypt_key`    | ✅       | same                                              |
| HSJ    | `payload_encrypt_key`     | ✅       | same                                              |
| HSW    | `encrypt_key`             | ✅       | WASM bytecode patch + AES-256-GCM round-trip      |
| HSW    | `decrypt_key`             | ✅       | WASM bytecode patch + Python-encrypt/bundle-decrypt|

These 5 are returned by `KeyFetcher().fetch()` and rotate per build
(hCaptcha rebuilds the bundles roughly every 10 minutes).

## HSW dispatcher inventory

The `vc` WASM export multiplexes 10 distinct magic numbers:

  - 5 typed-array setter magics (`setInt8`/`setInt16`/`setInt32` etc.)
  - 2 AES magics (`encrypt_req_data`, `decrypt_resp_data`)
  - 3 reserved magics (currently unused by the wbg JS layer)

The n-token entry is its own export (current build: `ec`; the name
rotates per build). Reachable only via `window.hsw(jwt)` (the third
path). See [`08-hsw-dispatch-table.md`](./08-hsw-dispatch-table.md).

## PoW algorithm

**Hashcash v1 + SHA-1** (`rust-hashcash/0.3.3` with the `sha1` feature).

  - Stamp format: `1:bits:date:resource:ext:rand:counter`
  - Date format:  `YYYYMMDD` (UTC)
  - Difficulty:   from the JWT payload's `d` field
  - Verified in WASM: SHA-1 K-constants `0x5A827999` and `0x6ED9EBA1`
    appear as `i32.const` literals in the code section

Pure-Python solver in [`hsw_pow.py`](../src/hcaptcha/hsw_pow.py) — bits=18
in 0.4 s, bits=20 in ~1.2 s. The WASM does it in ~10 ms thanks to JIT,
but the algorithm is identical.

## N-key — the hard one

The "N-key" inside HSW is the AES-256 key used by `vc` to encrypt the
n-token before returning it from `window.hsw(jwt)`. We've established:

### 1. It's per-call ephemeral, not per-build static

Two-pass extraction with different JWT timestamps confirms:

```
pass1: e185ed60 00...00 48176f69 5d334695 c5a1b094   (12 LCG bytes captured)
pass2: 8158c415 00...00 62f05b6d 8dd0ec19 c26a2e12   (12 LCG bytes captured)
static_bytes_mask: 00...00  (ZERO bytes match across passes)
```

Every captured byte changes between calls. The JS-side wrapper:

```js
return d1().then(function (a) {
  return a.<EXPORT>(JSON.stringify(payload),  // input
                    Math.round(Date.now()/1e3), // <-- runtime seed
                    jwt, x1);
});
```

The runtime timestamp is mixed into the LCG seed inside `vc`. This is
not a quirk — it's hCaptcha's design choice. There is no fixed
"HSW n-key" to extract.

### 2. The n-token doesn't decrypt with any of our 5 master keys

We capture a live n-token (typically 2500–3700 bytes after base64-decode)
and brute-force AES-256-GCM decrypt with:

  - All 5 master keys (3 HSJ + 2 HSW)
  - Both standard wire formats (`iv‖ct‖tag` and `ct‖tag‖iv`)
  - All 32-byte windows of every base address we capture in memory
    (72+ candidates per run)

Zero decrypts succeed. The n-token envelope is either:

  - AES-256-GCM with a key not present in any traceable memory region
    (most likely: a session key derived inside `vc` from the per-call
    LCG output we partially capture, immediately freed after use)
  - A non-GCM AEAD (AES-256-CTR + HMAC, ChaCha20-Poly1305) where our
    GCM wire-format hypothesis is wrong by construction
  - A custom envelope that doesn't match any standard AEAD wire format

Disproving each requires either capturing **all 32 bytes** of the
per-call key atomically with the token (currently blocked — see below)
or finding the encryption call site inside `vc` and reading the args
passed to it.

### 3. What `KeyFetcher` returns for `hsw.n_key`

We ship what we can capture honestly:

```python
{
  "hsw": {
    "n_key": "<16-byte hex from this extraction run; per-call ephemeral>",
    "fingerprint_blob_key": "<sha256(static_bytes_mask) — build-deterministic>"
  },
  "verified": {"hsw_n_key": false},                # always false on era (d)
  "extraction_status": {
    "hsw_n_key": "per-call-ephemeral-N-of-32-captured",
    "hsw_n_key_meta": {
      "pass1_hex": "...",       # bytes captured this run
      "pass2_hex": "...",       # bytes captured with different JWT timestamp
      "repeatable": "DIFFERENT",
      "static_bytes_count": 0,
      "live_n_token_b64": "...",       # the actual n-token from pass1
      "live_n_token_len_bytes": 3627,
      "note": "..."
    }
  }
}
```

The `live_n_token_b64` is the actual encrypted output from the same
`window.hsw(jwt)` call that produced the captured key bytes. Users can
attempt their own decryption analysis with both pieces atomically.

### 4. The remaining instrumentation gap

The LCG block in `vc` emits 12 of 32 n-key bytes via a "byte-store"
helper that we successfully patch. The remaining 20 bytes come from:

  - i64-store helper writes (8 bytes per call; ~1 call lands in the
    n-key window on the builds we've inspected — captures ~8 bytes)
  - i32-store helper writes (4 bytes per call; this helper fires from
    **many non-vc paths** during JS warm-up + polyfill init + microtask
    callbacks, and instrumenting it without a vc-gate either floods the
    ring buffer or perturbs timing enough to trip a missing-API path
    in jsdom that we haven't polyfilled yet)

The current code adds a `gated=True` flag to the i32_store prologue
and toggles a memory gate from Python around the `window.hsw(jwt)`
call, but the WASM still crashes with
`TypeError: Cannot set properties of undefined (setting 'f')` after
~90 s. That crash is from a wbg helper trying to attach a property to
a JS object that resolves to `undefined` through one of our polyfills.
Further polyfill work would be needed to push past it.

## Architectural eras (the backtest)

We tested the modern extractor against 12 archived HSW bundles
(versions 1.39.0 – 1.40.34):

| Era | Builds                | Dispatcher                    | Modern extractor works? |
| --- | --------------------- | ----------------------------- | ----------------------- |
| (a) | 1.39.0, 1.40.0–14     | None (direct calls)           | ❌                      |
| (b) | 1.40.15–16            | call_indirect table           | ❌                      |
| (c) | 1.40.21–34            | named export, no magic        | ❌                      |
| (d) | current (1.41+)       | `vc` + magic multiplex        | ✅                      |

Every era needs its own extractor strategy. We ship the era (d) one in
`hsw.py` + `hsw_n_key_full.py`. The structural identification works on
all 12 archive bundles (fixslice key-schedule candidates are findable)
but the magic-dispatcher heuristic doesn't apply pre-era-(d).

See [`10-architecture-eras.md`](./10-architecture-eras.md) for the
decision tree.

## Function map — 594 / 594 labeled

[`11-hsw-function-map.md`](./11-hsw-function-map.md) +
[`hsw_function_labels.json`](./hsw_function_labels.json) cover every
function in the current build:

  | Role                  | Count |
  | --------------------- | ----: |
  | wbg_marshal           | 196 |
  | deobf_consumer        | 139 |
  | panic_format          | 87 |
  | vtable_dispatch       | 32 |
  | drop_glue             | 20 |
  | panic_unwrap          | 19 |
  | thunk                 | 18 |
  | utility               | 14 |
  | aes_round             | 14 |
  | wbindgen_helpers      | 12 |
  | loop_iter             | 8 |
  | runtime_string_io     | 7 |
  | aes_key_schedule      | 6 |
  | hash_round            | 5 |
  | format_helper         | 5 |
  | fingerprint_collect   | 3 |
  | deobf_helper          | 2 |
  | accessor              | 2 |
  | byte_serializer       | 2 |
  | small_helper          | 1 |
  | sha1_pow              | 1 |
  | rng_or_lcg            | 1 |
  | **unknown**           | **0** |

Re-classifier at [`../tools/refine_hsw_labels.py`](../tools/refine_hsw_labels.py).

## The 29 jsdom API gaps we polyfilled

To make `window.hsw(jwt)` complete in pure Node + jsdom (no Playwright,
no real browser), [`sandbox_polyfill.js`](../src/hcaptcha/tools/sandbox_polyfill.js)
fills:

  - `performance.getEntries(ByType|ByName)`, `mark`, `measure`,
    `timing`, `navigation`, `PerformanceObserver`
  - `navigator.userAgentData`, `connection`, `permissions`,
    `serviceWorker`, `gpu`, `javaEnabled`, `maxTouchPoints`
  - `document.fonts` (FontFaceSet)
  - `OffscreenCanvas` (delegates to node-canvas)
  - `HTMLCanvasElement.getContext('webgl'|'webgl2')` stub with
    `UNMASKED_VENDOR_WEBGL` / `UNMASKED_RENDERER_WEBGL` + extension list
  - `window.chrome.app/runtime/csi/loadTimes`
  - `matchMedia`, `indexedDB`, `requestIdleCallback`, `Notification`,
    `BroadcastChannel`, `visualViewport`
  - `screen.availLeft/availTop/colorDepth/pixelDepth`
  - `window.outerWidth/outerHeight/devicePixelRatio`

End result: `HSWBridge.solve(jwt)` completes in ~0.25 s.

## What you can do with this

  - **Encrypt/decrypt traffic byte-perfect**: use `hsw.encrypt_key` +
    `hsw.decrypt_key` with [`hsw_crypto.py`](../src/hcaptcha/hsw_crypto.py)
    or the high-level [`HSW`](../src/hcaptcha/hsw_client.py) class.
  - **Generate valid Hashcash stamps**: use [`hsw_pow.py`](../src/hcaptcha/hsw_pow.py).
  - **Generate live n-tokens**: use [`HSWBridge.solve(jwt)`](../src/hcaptcha/hsw_bridge.py)
    — runs the actual WASM via the polyfilled jsdom sandbox, returns
    the n-token base64 string in ~0.25 s per call.
  - **Inspect the per-call n-key**: `KeyFetcher().fetch()['extraction_status']['hsw_n_key_meta']`
    contains both the captured key bytes and the live n-token from
    the same call, atomically.

## What's still actively unsolved

  - Capturing all 32 bytes of the per-call n-key atomically — blocked
    by the wbg-side crash that triggers when we instrument the
    i32-store helper. Would need polyfill expansion or a fundamentally
    different instrumentation strategy.
  - The exact format of the n-token envelope — not AES-256-GCM with any
    of our 5 master keys (proven by brute-force over 146 (key, format)
    combinations). Probably uses the per-call session key whose first
    16 bytes we capture; ruling that out requires the full 32 bytes.
  - The "fingerprint blob" JS-side AES-128-CBC encryption Implex
    documents — lives in the JS layer (`w10` SBOX, T-tables at
    `hsw_deobf.js` line ~4090) and is not on the `window.hsw(0/1, ...)`
    hot path. Extractor not implemented.
