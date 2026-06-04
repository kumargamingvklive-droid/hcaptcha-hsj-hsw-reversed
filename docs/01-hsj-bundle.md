# HSJ bundle — `inspekt_client`

`hsj.js` is the **inspekt_client** module: a hand-rolled
asm.js-style compiled blob that runs a fingerprint collection routine
and encrypts the resulting payload as the "n token" that gets attached
to every hCaptcha challenge submission. It also provides an AES-GCM
decrypt path for hCaptcha server responses.

## Compile target

The bundle imports a `Int8Array`-like JS-managed heap and operates on
it with asm.js-typed integer arithmetic — every memory access is
`f[(a |= 0) >> 2]` or similar, with explicit `|0` type annotations.
This style was popular when hand-writing fast compiled JS before WASM
became universal.

The crucial consequence: **all crypto state, including the AES master
key, lives in this JS-side `Int8Array` heap as plain bytes**. There is
no bit-slicing — the implementation uses textbook AES with the SBOX
and T-tables visible in the source (these are the `w10`, `x10`, `y10`,
`z10` arrays in `hsj_deobf.js`).

That is what makes key extraction tractable in HSJ but not in HSW.

## Public entry shapes

```js
// Generate the n-token for a given siteKey + challenge data
hsj(siteKey, challengeJson)   // → base64 token string

// AES-GCM helpers exposed for the bundle's internal use,
// but callable from the page once you hold a reference to hsj():
hsj(0, ciphertext)            // → plaintext (decrypt response)
hsj(1, plaintext)             // → ciphertext (encrypt payload)
```

## Wire formats

| Operation                     | Layout                                                  |
| ----------------------------- | ------------------------------------------------------- |
| `hsj(1, pt)` (encrypt)        | `ct(len(pt)) ‖ tag(16) ‖ iv(12) ‖ 0x00` — base64        |
| `hsj(0, ct)` (decrypt)        | `ct(N) ‖ tag(16) ‖ iv(12) ‖ 0x00` — base64              |

The trailing `0x00` byte is a version marker. AES-256-GCM with
12-byte IV and 16-byte tag is the standard NIST construction —
see [`algorithm.py`](../algorithm.py) `HSJEncryption` / `ResponseEncryption`.

## Key extraction — `keyfetcher_hsj.py`

The AES-256 key schedule allocates a 480-byte stack frame on the
asm.js heap. The 32-byte input key sits at offset 0 of that frame:

```
heap layout during AES-256 key expansion:
  [stack_top - 480]  ← the 32-byte master key (what we want)
  [stack_top - 480 + 32 .. stack_top - 480 + 480]
                      ← 240 bytes of round keys + scratch
  stack_top
```

The source contains a recognisable AST pattern in the key-schedule
function: a `ve - 480 | 0` expression that computes the start of the
stack frame. `keyfetcher_hsj.py` AST-patches the function around this
expression to additionally **push a copy of those 32 bytes into a JS
array**, then runs the bundle and calls `hsj(siteKey, …)` twice
(once to populate the n-token path, once to populate the response
path). Two distinct 32-byte keys come out:

```python
from keyfetcher_hsj import HSJKeyFetcher

keys = HSJKeyFetcher().fetch_keys()
# {
#   'version':              '5695589...',
#   'n_key':                'fe1ba43f...',  ← AES-256-GCM, encrypts client payload
#   'response_decrypt_key': '2fb5e0f6...',  ← AES-256-GCM, decrypts server response
# }
```

Both keys plug directly into [`algorithm.py`](../algorithm.py).

## Decoder side

The fingerprint payload returned by `hsj(siteKey, …)` is AES-256-GCM
under `n_key` plus a small wire envelope. `decrypted_n.json` is a
captured sample. The payload itself is msgpack-encoded — a dictionary
of dozens of browser-detected fields (`screen`, `navigator`, `timing`,
WebGL fingerprints, canvas hashes, etc.) plus answers to anti-bot
challenges.

## Deobfuscated layout

Run `python deobf.py hsj.js` and the resulting `hsj_deobf.js` is
~24k lines of cleaned-up source. The interesting regions:

| Lines (approx) | Contents                                                       |
| --------------:| -------------------------------------------------------------- |
|       100 –  500 | IIFE init, string-decoder setup, helper bindings              |
|       500 – 3500 | Fingerprint collection probes (one per browser surface)        |
|     3500 – 5500 | Crypto helpers — SBOX, T-tables, encrypt/decrypt entrypoints   |
|     5500 –24000 | The asm.js-compiled internal Rust-ish state machine             |

The "compiled" lower section is the original asm.js output — renaming
+ sequence flattening makes it readable but it's still machine-style
code (single-letter locals, integer arithmetic, switch-flattened
control flow). See [`03-deobfuscation.md`](./03-deobfuscation.md) for
what each pass does to it.

## Caveats — version rotation

The `<version>` hash rotates ~every 10 minutes. The hsj bundle gets
recompiled, the local variable names change, the string-table indices
shuffle. **The `ve - 480 | 0` AST pattern is stable across rotations**
(it's a structural property of the AES-256 key schedule in this
implementation) — that's why `keyfetcher_hsj.py` doesn't need
re-tuning for new versions.

Continue in [`02-hsw-bundle.md`](./02-hsw-bundle.md).
