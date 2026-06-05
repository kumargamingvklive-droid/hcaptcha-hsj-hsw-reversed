# HSW bundle

`hsw.js` is the second piece of hCaptcha's client side — it computes
the proof-of-work challenge answer and provides AEAD encrypt/decrypt
for client requests / server responses using *different* keys than
HSJ.

## Compile target — wasm-bindgen + Rust

The bundle ships a `wasm-bindgen`-generated JS wrapper around a
~596 KB Rust-compiled `.wasm` module. The wrapper handles the
JS↔WASM marshaling: it boxes JS values into wbg handle-table entries
(`a3(value)` boxes, `t3(handle)` unboxes), copies bytes between JS
typed-arrays and WASM linear memory, and dispatches method calls.

A typical wbg-generated entry looks like:

```js
// from hsw_deobf.js — encrypt_req_data wrapper:
encrypt_req_data: function (a) {
    try {
        var b = j12.qc(-16);                            // alloc 16 bytes on WASM stack
        j12.vc(1579470607, 0, 0, b, 0, a3(a), 0, 0);    // call dispatcher
        var c = p3().getInt32(b + 0, !0);               // read result ptr
        var d = p3().getInt32(b + 4, !0);               // read result len
        if (p3().getInt32(b + 8, !0)) throw b4(d);      // check error flag
        return b4(c);                                    // unbox JS string
    } finally {
        j12.qc(16);                                     // free stack
    }
},
```

The key WASM exports are:

| Export | Role                                                                            |
| -----: | ------------------------------------------------------------------------------- |
|   `vc` | Universal dispatcher — first arg is a magic-number opcode                       |
|   `qc` | Stack allocator — `qc(-N)` allocs N bytes, `qc(N)` frees, `qc(0)` reads SP      |
|   `jc` | The Memory export — `WebAssembly.Memory` instance                                |
|   `kc`, `nc`, `pc`, `uc`, … | wbg typed-array marshaling helpers                                |

Two-letter export names are stable in *kind* (there's always a
dispatcher, always a stack-alloc, always a Memory) but rotate in
*spelling* per build. `keyfetcher_hsw_keys.py` auto-discovers the
current spelling from the deobfuscated source — that's why it works on
any version without code changes.

## The dispatcher pattern

The `vc` export is a giant `match` on its first i32 argument:

```rust
// Conceptually, vc looks like this internally:
fn vc(magic: u32, a: u32, b: u32, c: u32, d: u32, e: u32, f: u32, g: u32) {
    match magic {
        1579470607 => encrypt_req_data_impl(/* args */),
        1011143743 => decrypt_resp_data_impl(/* args */),
        1936079124 => typed_array_view_u8(/* args */),
        1336883443 => typed_array_view_u16(/* args */),
        ...
    }
}
```

The magic numbers are stable for a given build but rotate with
`<version>`. They're effectively *opcode IDs*. The deobf source makes
this clear — every wbg-style entry calls `vc(MAGIC, …)`.

## Three crypto paths

| `window.hsw(…)`     | Dispatch        | Operation                                       | AES master key |
| ------------------- | --------------- | ----------------------------------------------- | -------------- |
| `(0, ciphertext)`   | `vc` magic `1011143743`* | AES-256-GCM decrypt of server response | `hsw.decrypt_key` |
| `(1, plaintext)`    | `vc` magic `1579470607`* | AES-256-GCM encrypt of client request payload | `hsw.encrypt_key` |
| `(jwt)` (no mode)   | `ec`/`pc` (separate exports from `vc`) | Proof-of-work (Hashcash + SHA-1) + AES-256 encrypt of the n-token | `hsw.n_key` |

\* This version. Magics rotate per build but the dispatcher pattern is invariant.

The third path does **not** go through `vc`. It has its own Promise
executor exports (`ec` / `pc`) and its own AES key schedule
(`fn 425` on the current build) reached from `ec`/`pc` only — never
from `vc`. See [`10-architecture-eras.md`](./10-architecture-eras.md)
sub-section *Sub-architecture (d.1)* for the call graph and
[`09-hsw-keys-derivation.md`](./09-hsw-keys-derivation.md) for the
direct AES-site capture procedure that recovers the n-token AES
master key.

## Key inventory — 6 build-static AES-256 master keys + fingerprint

| Key                          | Bundle    | Method                                             | Verified |
| ---------------------------- | --------- | -------------------------------------------------- | -------- |
| `hsj.n_key`                  | hsj.js    | AST patch on key-schedule stack frame              | ✅       |
| `hsj.response_decrypt_key`   | hsj.js    | AST patch                                          | ✅       |
| `hsj.payload_encrypt_key`    | hsj.js    | AST patch                                          | ✅       |
| `hsw.encrypt_key`            | hsw.js    | WASM bytecode patch + AES-256-GCM round-trip       | ✅       |
| `hsw.decrypt_key`            | hsw.js    | WASM bytecode patch + Python-encrypt / bundle-decrypt | ✅    |
| `hsw.n_key`                  | hsw.js    | Direct AES-site capture (fn 330/352 `arg0`, build-static across calls) | ✅ |
| `hsw.fingerprint_blob_key`   | hsw.js    | `sha256(hsw.n_key)` — derived identifier           | ✅       |

All six AES keys are 32 bytes (AES-256). The fingerprint blob key is
deterministic given the n-key — it is a per-build identifier rather
than an independent extraction.

> **Caveat on `hsw.n_key`:** the bytes are the correct AES master key
> (build-static, captured at the actual AES.encrypt invocation), but
> the live n-token does not decrypt under the captured key with any
> standard AES wire format we have tried. The n-token's outer
> envelope is non-standard (likely PoW-stamp framing around an inner
> AEAD). The extraction layer is complete; the envelope decode is a
> separate, consumer-side question. See
> [`12-hsw-complete-summary.md`](./12-hsw-complete-summary.md).

## Wire formats

Empirically determined by encrypting plaintexts of known length on
the `vc` path (`hsw.encrypt_key` / `hsw.decrypt_key`):

| Plaintext size | Output size | Layout                                |
| --------------:| -----------:| ------------------------------------- |
|              0 |          28 | `ct(0) ‖ tag(16) ‖ iv(12)`            |
|             16 |          44 | `ct(16) ‖ tag(16) ‖ iv(12)`           |
|             32 |          60 | `ct(32) ‖ tag(16) ‖ iv(12)`           |

This is standard AES-GCM with a 12-byte random IV and 16-byte tag.

Note that **HSW's wire format has no trailing version byte** (HSJ has
a `0x00` trailer). This is the cheapest way to fingerprint which
bundle produced a given blob.

### n-token envelope (unresolved)

The **third path** (`window.hsw(jwt)`) does NOT use this wire format.
Token sizes vary call-to-call (2798 B → 4203 B in samples), token
entropy is 256/256 unique bytes with flat distribution, and
brute-force decryption with the (now correctly extracted) `hsw.n_key`
against every standard AES wire variant fails. The token almost
certainly wraps an inner AEAD in PoW-stamp / length-prefix framing.
See [`09-hsw-keys-derivation.md`](./09-hsw-keys-derivation.md) and
[`12-hsw-complete-summary.md`](./12-hsw-complete-summary.md).

## What's inside the WASM

The compiled `.wasm`:

* ~596 KB total
* 186 data segments totalling ~61 KB
* 20 exports (19 functions + 1 `Memory`)
* 0 exported `WebAssembly.Global`s
* **No AES SBOX, no AES Rcon, no crypto crate strings** (fully stripped)
* The crypto code uses `aes-soft` fixslice32 — see
  [`06-fixslice32.md`](./06-fixslice32.md)

The lack of AES lookup tables is the defining characteristic. A
"normal" AES implementation has a 256-byte SBOX and 10-byte Rcon table
in its rodata. This one has neither because fixslice32 computes them
via bit-parallel boolean circuits.

## What's in `hsw_deobf.js`

After running `python deobf.py hsw.js`, the output is ~6k lines.
Notable regions:

| Lines (approx) | Contents                                                             |
| --------------:| -------------------------------------------------------------------- |
|        1 –  200 | IIFE init, string-decoder setup                                     |
|      200 –  700 | WASM loader + base64-decoded blob references                        |
|      700 – 3500 | wbg helpers, JsValue handle table, memory marshalling               |
|     3500 – 4090 | Proof-of-work + fingerprint payload assembly                        |
|     4090 – 4096 | The JS-side AES tables — `w10` (SBOX), `x10`/`y10`/`z10` (T-tables) |
|     4500 – 5300 | The wbg imports object (m12) — `encrypt_req_data`, `decrypt_resp_data` and ~200 helpers |
|     5300 – 5957 | `window.hsw` dispatcher entry point                                  |

## The JS-side AES — and why it's a dead end

`hsw_deobf.js` contains a full JS-side AES implementation: SBOX
(`w10`), forward T-tables (`x10`, `y10`, `z10`), inverse T-table
(`b11`), and round-encoded constants baked into an obfuscated
arithmetic switch (`z2`). When called as `z2(47, plaintext_block, 76)`
it encrypts one 16-byte block under a key that is *embedded as
obfuscated arithmetic* — never stored as plain bytes.

It's also **not on the `window.hsw(0, …) / (1, …)` hot path**.
`check_sbox_hits.py` patches `w10` with a `Proxy` counter; a clean
encrypt produces **0 SBOX reads**. The JS AES is used for some other
internal step (likely PoW intermediate transforms or fingerprint
shuffling) but not for AEAD.

## Bridge — `HSWBridge`

[`keyfetcher_hsw.py`](../keyfetcher_hsw.py) ships `HSWBridge` which
boots a jsdom/Node sandbox, loads the live `hsw.js`, and exposes
`encrypt`/`decrypt`/`solve` as Python methods that round-trip bytes
through the actual bundle. This is what to use when you need the
results, not the keys.

```python
from keyfetcher_hsw import HSWBridge

b = HSWBridge()                          # boots once, ~2s
ct = b.encrypt(b"hello-from-python")     # AES-256-GCM(unknown_key, hello-...)
pt = b.decrypt(server_response_bytes)
tok = b.solve(req_jwt_from_checksiteconfig)
```

Bytes round-trip cleanly through hCaptcha's servers — for any wire-
compatible task this is functionally equivalent to having the raw key.

Continue in [`03-deobfuscation.md`](./03-deobfuscation.md).
