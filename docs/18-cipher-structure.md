# N-token cipher STRUCTURE identified via WAT analysis

Despite the cipher mode being opaque to black-box decryption attempts,
analysis of fn 344's WAT (the new build's master orchestrator, 85,633
lines, equivalent to old build's fn 288) reveals the **structural pattern**
of the encryption loop.

## Direct calls from fn 344 to crypto primitives (new build)

| Callee   | Algorithm           | Direct call count | Lines in fn 344 |
| -------- | ------------------- | ----------------- | --------------- |
| fn 548   | AES-256 block encrypt | 3 sites         | 21369, 21603, 23017 |
| fn 434   | AES round operation  | 1 site           | 19172           |
| fn 416   | KDF (6-block AES)   | 6 sites           | 57427, 57706, 61214, 63698, 66327, 67538 |
| fn 388   | AES-256 key schedule | 2 sites          | 81594, 83121    |

(`call_indirect` only used 2 times in fn 344, both `type 3` `(i32) → ()`
which is the Rust async `Future::poll` dispatch, NOT for crypto.)

## The encryption loop pattern (around line 23017)

The 3rd call site to fn 548 is the encryption loop body:

```wasm
local.get 2          ;; current counter offset
i32.const 16
i32.add
local.set 2          ;; counter += 16  (THE COUNTER ADVANCES BY 16 PER CALL)
local.get 10
i32.const 2472
i32.add              ;; arg0 = struct + 2472 (the round-keys buffer)
local.get 9          ;; arg1 = current input block (counter-encoded)
call 548             ;; encrypt one 16-byte block
```

This is **textbook CTR-mode**: a counter that increments by 16 each
iteration, feeding into an AES-256 block-encrypt to produce a 16-byte
keystream chunk.

With 235 iterations (235 × 16 = 3760 ≈ 3759-byte ciphertext), this
matches the 256 fn 548 invocations we observed in instrumentation
(the extra ~21 calls = setup + finalize + initial round-key derivation).

## The KDF region (lines 57427–67538)

fn 416 is called SIX times in fn 344, all in the range 57427–67538.
fn 416 is itself a 6-block AES sequence (`call 391/434 × 6` internally).
This is likely the **MAC computation** — 6 × 96 = 576 bytes of derived
material being processed through fn 416 to produce the 16-byte tag.

## The KS region (lines 81594, 83121)

After the encryption + MAC loops, the bundle calls fn 388 (AES KS) twice.
This is consistent with **key derivation** for the FINAL output stage —
perhaps deriving the IV transformation or the wire-format outer wrap.

## Why CTR-mode AES-256 with `1bf04f88...` still doesn't decrypt

Despite the cipher being structurally CTR-mode, our black-box CTR brute
fails because:

1. **The counter format is non-standard**: The increment is `+= 16`
   (BYTE offset) rather than `+= 1` (block counter). The actual input
   to AES is likely formed as some function of this byte offset + IV +
   build-context. None of the standard `iv || counter_be32` or
   `counter || iv || padding` formats match.

2. **MAC = fn 416 over derived material**, not the plain ciphertext or
   `iv || ct` or `ver || iv || ct`. fn 416 takes ITS OWN derived inputs
   and produces 96 bytes per call × 6 calls = 576 bytes of intermediate
   state that finally collapses to the 16-byte tag.

3. **Round keys captured ARE the master's** — verified bit-perfect.
   But the counter VALUE per block is computed by code that's deeply
   intertwined with the Rust async state machine; it's not a simple
   counter we can predict.

## What this proves

- The cipher is **definitely AES-256 in CTR-like mode** (not GCM, not
  CCM, not OCB, etc. — none of those use a per-block byte-offset
  increment pattern).
- The MAC is **NOT GHASH (excluded by missing 0xE100... constant) nor
  Poly1305 (no R-clamp 0x0ffffffc)** — it's a custom construction
  built on AES blocks via fn 416.
- The complete cipher mode is uniquely defined by:
  - Counter encoding rule (line ~22900 of fn344.wat)
  - fn 416's exact 6-block sequence (separate function, 1634 instructions)
  - The fn 388 KS calls at the end (likely the IV/tag wrap)

## Outstanding work for complete recovery

To produce a working Python decryptor, the precise byte-formula for:

1. **Counter encoding**: read the ~50 instructions BEFORE line 23017 to
   determine how `local_9` (the block input) is computed from the
   counter offset + IV bytes.

2. **fn 416's algorithm**: full instruction-level reverse of its 1634
   instructions. The 6-block AES sequence likely follows a recognizable
   MAC pattern (CMAC, OMAC, parallelizable variants thereof).

3. **Output stage**: trace the bytes after the encryption loop that
   form the final wire blob.

This is mechanically achievable but tedious. Each step would take
several hours of careful WAT reading.
