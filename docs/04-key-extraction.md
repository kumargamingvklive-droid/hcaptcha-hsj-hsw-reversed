# Key extraction — status and methods

## Status

| Bundle | Key                     | Status         | Method                                       |
| ------ | ----------------------- | -------------- | -------------------------------------------- |
| HSJ    | `n_key`                 | ✅ extracted   | `keyfetcher_hsj.py` — AST patch + run        |
| HSJ    | `response_decrypt_key`  | ✅ extracted   | `keyfetcher_hsj.py`                          |
| HSJ    | `payload_encrypt_key`   | ✅ extracted   | `keyfetcher_hsj.py`                          |
| HSW    | `encrypt_key`           | ✅ extracted   | `keyfetcher_hsw_keys.py` — WASM bytecode patch |
| HSW    | `decrypt_key`           | ✅ extracted   | `keyfetcher_hsw_keys.py`                     |

All five master keys are recovered byte-accurately from any live hCaptcha
build. The HSW encrypt key is mathematically verified per fetch via
AES-256-GCM authentication-tag check against the live bundle's own
encrypt output.

```python
from keyfetcher import KeyFetcher
keys = KeyFetcher().fetch()
# {
#   'version': '...',
#   'hsj': {'n_key': '...', 'response_decrypt_key': '...', 'payload_encrypt_key': '...'},
#   'hsw': {'encrypt_key': '...', 'decrypt_key': '...'},
#   'cipher': 'AES-256-GCM',
#   'wire_format': {
#       'hsj': 'ct(N) || tag(16) || iv(12) || 0x00',
#       'hsw': 'iv(12) || ct(N) || tag(16)',
#   },
#   'verified': {'hsw_encrypt_key': True},
# }
```

## HSJ — three keys via AST patching

`hsj.js` keeps its AES-256 keys in a JS-managed `Int8Array` heap. The
key-schedule function allocates a 480-byte stack frame and the 32-byte
master key sits at offset 0 of that frame.

`keyfetcher_hsj.py`:

1. Locates the AST pattern `<varname> - 480 | 0` — the stack-frame
   computation in the key schedule's prologue.
2. Wraps the function body to push a copy of the 32 bytes at
   `[base + 0 .. base + 32]` into a JS-side array each time the
   function fires.
3. Drives `hsj(siteKey, …)`, `hsj(0, …)`, and `hsj(1, …)` to trigger all
   three key paths and reads back the three captured keys.

The `var - 480 | 0` pattern is structurally stable across version
rotations — it's an artifact of the AES-256 key schedule's stack
layout, not the obfuscation.

## HSW — two keys via WASM bytecode patching

`hsw.js` ships a Rust-compiled `.wasm` using RustCrypto's `aes-soft`
backend with the `fixslice32` bit-sliced AES representation. The master
key is loaded into 8 i32 locals through an XOR-deobfuscation primitive
and immediately bit-permuted — it never sits in linear memory as 32
contiguous plain bytes.

We solve this by patching the WASM bytecode itself. See
[`07-wasm-patching.md`](./07-wasm-patching.md) for the full mechanism;
the high-level flow:

1. **Identify** the AES-256 key-schedule function and the XOR-deobf
   helper structurally from the WASM (no hardcoded indices —
   rotates per build).
2. **Inject** 8 bytecode sequences at the key-schedule's entry point
   that call the deobf helper with `(0, key_ptr + i*4)` for i in 0..7
   and copy each returned i32 to a fixed scratch memory address.
3. **Add** `__peek32` / `__poke32` exports so JS can read scratch
   memory.
4. **Substitute** the patched binary at load time by hooking
   `WebAssembly.instantiate` inside the running bundle.
5. **Drive** one encrypt — the patch executes, scratch holds the
   master key.
6. **Drive** one decrypt (against dummy bytes — auth fails but the key
   schedule runs first) and read the decrypt master key from scratch
   the same way.
7. **Verify** by AES-256-GCM-decrypting the captured encrypt blob.

End-to-end runtime: ~8 seconds. The fetcher works on every build —
the encrypt and decrypt magic numbers, dispatcher / key-schedule /
deobf-helper function indices, and inline constants all rotate, but
the structural signature of each piece is invariant and discoverable
from the binary.

## What was tried before this worked

Documented for completeness; none of these recovered the key on their own:

* Sliding-window brute force over the WASM data section, full binary,
  `i32.const` literal stream, linear memory snapshots, post-encrypt
  memory regions — across AES-128/192/256-GCM, ChaCha20-Poly1305, six
  wire-layout permutations, multiple AAD candidates: ~4 million
  attempts, zero hits.
* WASM globals (none exposed key material).
* JS-side SBOX hook — proves the JS AES isn't on the hot path.
* Import + export call tracing during one encrypt — proves no key
  bytes flow through any JS-side import boundary.
* WASM stack-pointer snapshotting at dispatcher entry/exit, mid-encrypt
  import callbacks — every byte in those captures is either non-key
  rodata or compiled lookup table contents.
* `inv_bitslice` on every 32-byte window — confirms the live memory
  isn't in the standard K‖K fixslice32 layout.

The patching approach skips all of this — we read the key at the
exact instruction where it's still a plain 32-byte sequence in WASM
locals, before any bit-permutation runs.
