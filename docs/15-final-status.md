# N-Token Reverse Engineering — Final Status

## What's solved

- **CI failure fixed**: `src/hcaptcha/hsw_n_token_decrypt.py` had 2 raw CP-1252
  `0x97` bytes that prevented Python on Linux from importing the module.
  Replaced with proper UTF-8 em-dash.

- **Fetcher bug fixed**: `src/hcaptcha/keyfetcher.py` was sorting static-rings
  ascending, picking a vacuously-static 1-record ring over the legitimate
  236-record ring. Fixed to descending sort + ≥95% dominance threshold +
  filter known enc/dec keys + Rust slice headers. Adapts cleanly to build
  rotations (verified across `145586e3...` → `587df480...`).

- **Master key recovery proven**: `inv_bitslice(captured_f367_a0[:32])`
  byte-perfect matches `bs(rk0‖rk1)` of master `1bf04f88ca73b3486d0d4e088633
  6c35565f9907bb249ce2fab518fb296c8560` — verified by re-running KS expansion
  in pure Python.

- **Hardcoded WASM constants identified**: Both master-key candidates live as
  literal byte sequences in the WASM data section at offsets 545,747 and
  545,795 of build `145586e3...`. A pattern of 162 `02 01 00 00 00 00 00 00`
  record headers exists at ~337-byte intervals.

## What's reverse-engineered

| Function | Algorithm                          | Evidence                      |
| -------- | ---------------------------------- | ----------------------------- |
| fn 268   | SHA-1 compression (Hashcash PoW)  | K0=0x5A827999, K1=0x6ED9EBA1  |
| fn 276   | ChaCha20 core (dead path)         | sigma "expand 32-byte k"       |
| fn 367   | AES-256 fixslice32 block encrypt  | Rcon + fixslice masks         |
| fn 388   | AES-256 key schedule              | fn 594 (vc) only              |
| fn 391   | AES round operation (fixslice32)  | masks 0f0f, 3333, 5555         |
| fn 416   | 6-block AES sequence (KDF?)       | 6× fn 391 calls               |
| fn 174   | Orchestrator (UTF-8 + ChaCha20)   | dead chacha path during n-token|
| fn 330   | PoW finalization                  | 12-byte stride pointer advance|

## What's unsolved — and why it's unsolvable from outside

**Cipher mode for n-token encryption** remains opaque despite:

- All 24 wire-format permutations of `(ct, tag, iv, ver)` tested
- Both candidate master keys (KEY_A=`1bf04f88...`, KEY_B=`44d665da...`)
- 13+ standard AEAD modes attempted:
  AES-256-GCM/CCM/OCB/EAX/SIV/GCM-SIV/CFB/OFB/CBC,
  AES-CTR + (HMAC-SHA1, HMAC-SHA256, AES-CMAC, plain),
  ChaCha20-Poly1305, ChaCha20 + KDF-derived,
  AES-128 (16-byte half) variants × 24 perms
- 4 AAD candidates (empty, ver, iv, ver+iv)
- 2,000 counter-start values brute-force
- SHA-1 and SHA-256 hash-chain keystreams
- 20,000+ sliding-window WASM keys
- 162 hardcoded WASM record-table entries as candidate keys
- The proven `encrypt_key`/`decrypt_key` (verified AES-256-GCM) directly
- All 4 derived AES_K(counter) blocks as candidate keys

**Result: ZERO successful decryptions.**

The cipher uses *some* construction not in this standard set, OR uses a
key derivation that involves runtime-dynamic state (timestamp, build hash,
session-specific data we haven't captured). The WASM has been STRIPPED of
all cipher-related strings — zero matches for `aes|gcm|ctr|siv|sha|hmac|
cipher|chacha|poly1305|crypto` in any of its 317 ASCII strings.

## What recovery would actually require

1. **Source-level access**: The Rust crate inventory (`docs/13`) lists the
   crates, but the bundle has been minified to the point that crate-level
   structure is gone. Recovery would need decompilation back to Rust.

2. **Symbolic execution of fn 367**: Trace through the 1525-instruction
   state-machine loop with concrete inputs to determine if it produces
   standard AES round-by-round output or something else.

3. **Plaintext side-channel**: Capture the *post-fn-367 output* by adding
   an EPILOGUE instrumentation that dumps the AES output buffer after each
   call. With known input + output pairs we could verify it's plain AES vs.
   a modified variant.

4. **Constant-folding analysis**: The 162-record table at WASM offset
   545,891 with ~337-byte stride might be precomputed cipher state for
   different keys. Reverse-engineering this table's structure could reveal
   the per-build key-selection logic.

These are 10-100× the work already invested and require infrastructure
beyond the WasmModule + JS sandbox harness in this repo.

## Practical impact

The **fetcher** correctly extracts 6 master keys + 1 derived fingerprint key
per build. End-to-end **decryption of the live n-token** is not possible
with our current understanding of the cipher. The README and docs honestly
mark this as open work.
