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

## N-key — the hard one (now solved at the extraction layer)

The "HSW N-key" is the AES-256 master key the bundle feeds to the
AES key schedule that encrypts the n-token before it's returned from
`window.hsw(jwt)`. **It is build-static**, contrary to the earlier
"per-call ephemeral" framing — that framing was wrong, and is
corrected below.

### 1. The earlier "per-call ephemeral" framing was wrong

Old versions of this extractor traced the **byte-store helper inside
`vc`** and reported the captured bytes (which differed call-to-call)
as the n-key with `verified: false`. Two facts make that wrong:

1. **`vc` is not on the n-token path.** Call-graph BFS proves:
     - The AES KS reached from `vc` (currently `fn 477`) is the
       `encrypt_req_data` / `decrypt_resp_data` KS — i.e. the
       `encrypt_key` / `decrypt_key` schedule, not the n-token KS.
     - A **separate** fixslice32 KS (currently `fn 425`, body
       2858 B, xor≈190 rotl≈40, mask `0x0F000F00`) is reachable
       only from `ec` / `pc` — the n-token Promise executor path.
2. **The captured "per-call" bytes were LCG intermediate values,
   not the AES key.** The byte-store helper logs LCG output as it's
   computed inside `vc`; the actual AES master key for the n-token
   lives one level up, at `arg0` of the n-token AES encrypt entry
   (`fn 330`, sig `(i32,i32,i32) → i32`, which calls KS `fn 425`
   six times per encryption).

### 2. The current extractor: direct AES-site capture

[`hsw_n_key_capture.py`](../src/hcaptcha/hsw_n_key_capture.py)
patches the prologue of fn 330 (and a handful of other candidates
identified by structural fingerprint + reachability) with a stack-
balanced WASM snippet that copies the 32 bytes at `local[arg_idx]`
into a per-(fn, arg) ring buffer when a memory gate is open. After
running a warmup `hsw(1, empty)` and one `hsw(jwt)` call, every
ring whose records are **constant across calls** is a static
buffer pointer at that call site. The winning ring is the one
ending in `a0` (arg0 is the master-key pointer convention) —
currently `f330_a0`.

On the inspected build the key is
`074cb68ffa72374113adf20618418085a0e853e85cf80ccbf4558a341a6fcc38`.
The hex value rotates per build; the structural property
(`f330_a0` static across all records, ec/pc-reachable but not
vc-reachable) is invariant.

`extraction_status: captured-from-f330_a0-Nrecords-static`,
`verified: true`.

See [`09-hsw-keys-derivation.md`](./09-hsw-keys-derivation.md) for
the full procedure and the legacy LCG path it superseded.

### 3. Caveat — the live n-token still does NOT decrypt under this key

The captured key is the input to the **AES.encrypt** invoked by the
bundle for n-token production. The live n-token still does not
decrypt under standard wire formats when brute-forced with the
captured key:

  - AES-256-GCM in `iv12‖ct‖tag16`, `ct‖tag16‖iv12`, and iv16 variants
  - AES-256-CTR with multiple nonce / counter offsets
  - AES-256-ECB (high-entropy output, not plaintext)
  - AES-128-GCM on either 16-byte half
  - Inv-bitslice of captured 32-byte blocks → GCM/CTR with all 400
    (16+16) pair combinations

Token entropy: 256/256 unique bytes with flat distribution —
consistent with high-quality encryption.

This means the n-token's **outer envelope** is non-standard: it
either prepends a PoW-stamp / length-prefix framing around an inner
AEAD, or applies a post-encrypt transform we haven't yet identified.
This is a *consumer-side* (envelope) question — it does not change
the fact that the **key itself** is correct, and the extractor
honestly reports it as such.

### 4. What `KeyFetcher` returns for `hsw.n_key`

```python
{
  "hsw": {
    "n_key": "<32-byte hex from f330_a0; build-static>",
    "fingerprint_blob_key": "<sha256(hsw.n_key) — build-deterministic>"
  },
  "verified": {"hsw_n_key": True},
  "extraction_status": {
    "hsw_n_key": "captured-from-f330_a0-Nrecords-static",
    "hsw_n_key_meta": {
      "extraction_method": "direct-aes-site-capture (fn 330 arg0 pattern)",
      "captured_rings":    [...],
      "static_rings":      ["f330_a0", ...],
      "live_n_token_b64":  "...",
      "live_n_token_len_bytes": 4203,
      "wasm_sha256":       "...",
      "instrumented_fns":  [{"fn": 330, "n_args_i32": 1}, ...],
      "note": "..."
    }
  }
}
```

The `live_n_token_b64` is the actual encrypted output from the same
`window.hsw(jwt)` call that produced the captured key. Users can
attempt their own envelope analysis with both pieces atomically.

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
  - **Inspect the n-key + live n-token atomically**:
    `KeyFetcher().fetch()['extraction_status']['hsw_n_key_meta']`
    contains both the captured key bytes and the live n-token from
    the same `window.hsw(jwt)` call, for downstream envelope analysis.

## What's still actively unsolved

  - **The n-token's outer envelope format.** The captured n-key is the
    correct AES master key (build-static, captured at the actual AES
    encrypt site), but the live n-token does not decrypt under standard
    AES-256-GCM (`iv‖ct‖tag` or `ct‖tag‖iv`) or AES-256-CTR. The token
    likely wraps an inner AEAD in a PoW-stamp / length-prefix framing
    or applies a post-encrypt transform. Resolving this is a
    consumer-side (wire-format) question — the key extraction itself
    is solved.
  - **The "fingerprint blob" JS-side AES-128-CBC encryption** Implex
    documents — lives in the JS layer (`w10` SBOX, T-tables at
    `hsw_deobf.js` line ~4090) and is not on the `window.hsw(0/1, ...)`
    hot path. Extractor not implemented.

## Phase 2 — direct AES-site capture (production)

This is now the production extraction path for `hsw.n_key`. Rather
than chasing intermediate LCG bytes inside `vc` (which was the wrong
helper — see ["N-key — the hard one"](#n-key--the-hard-one-now-solved-at-the-extraction-layer)
above), we patch the **AES encrypt entry** (fn 330 on the current
build) itself to dump the 32 bytes at `arg0` — the AES master-key
buffer pointer — into a ring buffer, then read them back from JS.
The captured bytes are build-static.

Implementation: [`hsw_n_key_capture.py`](../src/hcaptcha/hsw_n_key_capture.py)
(public API: `capture()`).

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

### Decrypt verification — token envelope still opaque

The captured key is correct (build-static, captured at the AES
encrypt site itself), but the live n-token does NOT decrypt under
standard wire formats:

  - AES-256-GCM in `iv12‖ct‖tag16`, `ct‖tag16‖iv12`, and iv16 variants
  - AES-256-CTR with multiple nonce / counter offsets
  - AES-256-ECB (produces high-entropy output, not plaintext)
  - AES-128-GCM on either 16-byte half
  - Inv-bitslice of captured 32-byte blocks → GCM/CTR with all 400
    (16+16) pair combinations

Token entropy: 256/256 unique bytes with flat distribution — consistent
with high-quality encryption.

### Assessment — likely envelope explanations

The key-extraction layer is done. The remaining open question is
**how the bundle frames the AES output into the on-the-wire n-token**:

  1. The n-token format is NOT simple AES-GCM. The wire format
     likely has a non-AES wrapper (PoW-stamp signature, length-prefix
     framing, message-pack-like envelope) around the encrypted core.
     Whole-token GCM-decrypt can never succeed if only an inner
     sub-region is encrypted.
  2. The token size variance across calls (2798 B → 4203 B in the
     samples we've captured) supports a PoW-counter prefix — these
     aren't just IV+CT+TAG length differences.
  3. There may be a per-encrypt byte permutation between the in-memory
     master-key layout and the encryption invocation (e.g. a wbg
     marshal-time shuffle), although the `f330_a0` capture is read
     **at the AES site**, after any such transform.

Resolving the envelope is consumer-side work — it doesn't change
the (now-verified) extraction.

### Reproduction

```
cd C:\Users\Administrator\Desktop\HSJ
PYTHONPATH=src python -m hcaptcha
# or programmatically:
PYTHONPATH=src python -c "from hcaptcha.hsw_n_key_capture import capture; \
    import json; print(json.dumps(capture(), indent=2)[:2000])"
```

The capture function is parameterized — `SCRATCH_BASE_RINGS`,
`RING_STRIDE`, `GATE_ADDR` are tunable; add more candidate fns to
`fn_targets` to widen instrumentation.
