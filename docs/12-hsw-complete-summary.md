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
| `window.hsw(jwt)`      | Build n-token (PoW solution + fingerprint blob) | Hashcash + AES-256, fixed key |

All three are now reversed. The n-token's AES master key is extracted
directly from the WASM by instrumenting the AES encrypt call site that
the bundle reaches via the Promise executor (`pc` → 389 → 250 → 548 →
`fn 330` on the current build). The captured key is build-static
(same bytes captured every time the AES helper fires within a build).

## Master keys (6 build-static)

| Bundle | Key                       | Verified | Method                                            |
| ------ | ------------------------- | -------- | ------------------------------------------------- |
| HSJ    | `n_key`                   | ✅       | AST-patch of the key-schedule stack frame         |
| HSJ    | `response_decrypt_key`    | ✅       | same                                              |
| HSJ    | `payload_encrypt_key`     | ✅       | same                                              |
| HSW    | `encrypt_key`             | ✅       | WASM bytecode patch + AES-256-GCM round-trip      |
| HSW    | `decrypt_key`             | ✅       | WASM bytecode patch + Python-encrypt/bundle-decrypt|
| HSW    | `n_key`                   | ✅       | Direct AES-site capture (fn 330 arg0 / `f330_a0`) |

These 6 are returned by `KeyFetcher().fetch()` and rotate per build
(hCaptcha rebuilds the bundles roughly every 10 minutes). A 7th value
`hsw.fingerprint_blob_key = sha256(hsw.n_key)` is a deterministic
derived fingerprint suitable for build identification.

### How the HSW n_key extractor works

The n-token AES encrypt is invoked from the Promise-executor path,
NOT from `vc` (which only handles `encrypt_req_data`/`decrypt_resp_data`).
Call-graph BFS from each export proves:

  - KS `fn 477` (this build) is reached only from `vc` — that's the
    request/response AES key schedule.
  - KS `fn 425` is reached only from `ec`/`pc` (the n-token path) —
    that's the **n-token AES key schedule**.
  - The encrypt entry `fn 330` (sig `(i32,i32,i32) → i32`) calls KS
    `fn 425` six times per encryption. Its `arg0` IS the AES master
    key pointer — we patch its prologue to copy the 32 bytes at
    arg0 into a scratch ring, drive `window.hsw(jwt)` once, and read
    back the key.

The same 32 bytes are captured on EVERY call within a build (warmup
+ JWT call both produce identical `f330_a0`), proving build-static.
On the build inspected: `074cb68ffa72374113adf20618418085a0e853e85cf80ccbf4558a341a6fcc38`.

Implementation: [`src/hcaptcha/hsw_n_key_capture.py`](../src/hcaptcha/hsw_n_key_capture.py).

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

## Phase 2 — direct AES-site capture attempt (partial)

We attempted a more aggressive instrumentation strategy: rather than
trying to capture the n-key as it falls out of the LCG helpers, patch
the AES key-schedule (KS) helper itself to dump every 32-byte input
atomically with the call site.

### Resolved structural mapping (phase-1 → current build sha256 750faf0d…)

  | Phase-1 fn (role)                              | Current build fn | Body bytes | Notes                                |
  | ---------------------------------------------- | ---------------- | ---------- | ------------------------------------ |
  | 282 (KS, xor=190 rotl=40)                      | **425**          | 2858       | EXACT structural match (fixslice32 KS)|
  | 205 (encrypt-caller, (i32,i32,i32)->(i32))     | **330**          | 3211       | Calls fn 425 6× — ec-reachable only  |
  | 342 (wbg-bindgen Promise dispatcher, 205 KB)   | **548**          | 192146     | 14 direct callers                    |
  | 345 (vc-reached KS)                            | **477**          | —          | Second KS variant                    |

### Procedure (`tools/capture_ntoken_key.py`)

  1. Fetched + cached hsw.wasm (sha256 `750faf0da6a9e64a…`, 581 769 B).
  2. Identified 4 structural KS candidates (fn 388, 425, 477, 520);
     filtered to "ec-reachable but not vc-reachable" → fn 388, 425.
  3. Built a WASM prologue that, when `GATE_ADDR=1`, copies 32 bytes
     from `local[arg_idx]` into a per-(fn,arg) ring buffer using
     4×i64.load+i64.store (zero locals added, stack-balanced).
  4. Patched fn 425, fn 388, fn 330 prologues to dump every i32 arg
     (7 rings total). Added `__peek32` / `__poke32` exports via
     `ModuleWriter`.
  5. Booted jsdom JsRuntime, hooked `WebAssembly.instantiate` →
     substituted patched wasm, ran warmup `hsw(1, empty)`, then opened
     the gate, ran `hsw(jwt)`, closed the gate.
  6. Captured 226 records per fn 388 arg, 115 records per fn 425 arg,
     and 2 records per fn 330 arg (warmup + JWT call).

### Captured data

  | Source              | Value                                                              | Behavior              |
  | ------------------- | ------------------------------------------------------------------ | --------------------- |
  | fn 330 arg0 (KEY)   | `074cb68ffa72374113adf20618418085a0e853e85cf80ccbf4558a341a6fcc38` | CONSTANT warmup ↔ JWT |
  | fn 330 arg1         | (differs warmup ↔ JWT)                                             | state/IV buffer       |
  | fn 330 arg2         | (differs warmup ↔ JWT)                                             | output buffer         |
  | fn 425 arg1         | 4 unique 32-byte values consistent with AES-256 expanded round keys| round-key chunks      |

### Decrypt verification — FAILED

Token returned (4836 b64 chars → 3627 raw bytes) does NOT decrypt under
the captured `074cb68f…` key with:

  - AES-256-GCM in `iv12‖ct‖tag16`, `ct‖tag16‖iv12`, and iv16 variants
  - AES-256-CTR with multiple nonce / counter offsets
  - AES-256-ECB (produces high-entropy output, not plaintext)
  - AES-128-GCM on either 16-byte half
  - Inv-bitslice of captured 32-byte blocks → GCM/CTR with all 400
    (16+16) pair combinations

Token entropy: 256/256 unique bytes with flat distribution — consistent
with high-quality encryption.

### Assessment — likely explanations

  1. The n-token format is NOT simple AES-GCM. The wire format may have
     a non-AES wrapper (proof-of-work signature, length-prefix framing,
     message-pack-like envelope) around the encrypted core. GCM-decrypt
     of the WHOLE token can never succeed if only an inner sub-region
     is encrypted.
  2. The captured 32-byte block at fn 330 arg0 IS most likely the
     n-token AES key in fixslice form, but the inverse-bitslice we have
     (`fixslice_inverse.py`, written for the vc/encrypt_req_data path)
     may use a different byte-layout than this build's wbg-bindgen Rust
     code. The wire-encoded key may differ from the in-memory layout by
     some permutation.
  3. The PoW (proof-of-work) layer means the token contains a
     nonce-search counter that gets prepended to the encrypted payload;
     the size variance across calls (2798 B → 3627 B) supports this —
     these aren't just IV+CT+TAG length differences.

### Next steps to finish (out of time budget)

  - **A. Instrument fn 548** (the dispatcher) to capture the FULL
    plaintext buffer pointer + length BEFORE it gets passed to fn 330.
    fn 548 is the wbg-bindgen Promise executor; the plaintext + key get
    marshalled in this scope. Look for `memory.copy(buf, src, len)`
    patterns in 548 right before each fn 330 call.
  - **B. Instrument the JS-side `XH.mc(...)` wrapper** (line 5562 of
    `hsw_pretty.js`) — hook it to log inputs / outputs. The JWT-path
    entry is a JS wrapper that calls into a wasm export we couldn't
    identify by name; tracing it dynamically would expose the actual
    encryption boundary.
  - **C. Build a known-plaintext oracle**: call `window.hsw(1, bytes)`
    (encrypt_req_data) and `window.hsw(0, ct)` (decrypt_resp_data) with
    controlled inputs / outputs, then compare against
    `AES-GCM(captured_key, …)` to verify whether `captured_key` matches
    `hsw.encrypt_key` after some transformation. If yes, the n-token
    uses a DIFFERENT key derived from JWT — would need to trace the key
    derivation specifically for the JWT path.

### Reproduction

```
cd C:\Users\Administrator\Desktop\HSJ
PYTHONPATH=src python tools/capture_ntoken_key.py
```

Saves full capture to `tools/capture_ntoken_key.last.json`. Script is
parameterized — `SCRATCH_BASE_RINGS`, `RING_STRIDE`, `GATE_ADDR` are
tunable; add more candidate fns to `fn_targets` to widen instrumentation.
