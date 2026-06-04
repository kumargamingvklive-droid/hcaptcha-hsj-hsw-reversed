# fixslice32 — bit-sliced AES

`hsw.js`'s WASM uses RustCrypto's `aes-soft` backend with the
`fixslice32` representation. This is the mathematical reason the
master key is **not extractable as raw bytes from memory observation**,
and it's the bedrock for any future key-recovery work.

## What is bit-slicing?

Standard AES operates on a 128-bit state arranged as 4 × 4 bytes (4
u32 columns). Every operation (SubBytes, ShiftRows, MixColumns,
AddRoundKey) is defined byte-by-byte:

```
state[i] = SBOX[state[i]]                      # SubBytes (per byte)
state[a*4+b] = state[a*4 + (b - shift) % 4]    # ShiftRows
state[col] = MixColumnsMatrix · state[col]     # MixColumns
state[i] ^= round_key[i]                       # AddRoundKey
```

The SBOX lookup is the canonical timing leak: cache pressure on the
256-byte table can be observed through micro-architectural attacks.

Bit-slicing avoids the SBOX lookup by computing each bit of the SBOX
output as a boolean function of all 8 bits of the input.

```
out_bit_0 = some_xor_chain_over_input_bits
out_bit_1 = different_xor_chain
...
out_bit_7 = ...
```

The boolean circuit for the AES SBOX has ~120 gates total. Per byte
that's slower than a lookup. But here's the key insight: a 32-bit XOR
of two u32 words performs the SBOX on **32 input bits at once**. If
those 32 bits are "bit 0 of 32 different bytes", a single XOR computes
"bit 0 of the SBOX output for all 32 bytes" simultaneously.

That's **bit-slicing**: rearrange the byte-level state so that bits
from different bytes share a single register, then compute the SBOX
across all of them in parallel.

## fixslice32 specifically

There are multiple bit-slicing schemes. `aes-soft`'s `fixslice32` is
the one optimised for 32-bit registers (vs `fixslice64` for 64-bit).
It processes **two 128-bit AES blocks in parallel** for throughput.

The bitsliced state is 8 u32 words:

```
        bit position
        +---+---+---+---+---+---+---+---+
word 0  | bit 0 of bytes 0,1,…,15 of block A and B |
word 1  | bit 1 |
word 2  | bit 2 |
word 3  | bit 3 |
word 4  | bit 4 |
word 5  | bit 5 |
word 6  | bit 6 |
word 7  | bit 7 |
        +---+---+---+---+---+---+---+---+
```

Each word holds 32 bits = (bit i) of byte j across blocks A and B
for j in 0..15.

Round keys are also bit-sliced (so they can be XORed directly with
the state in AddRoundKey). A 16-byte round key becomes 8 u32 words of
bitsliced data.

For 2-block parallel processing, the round key is **duplicated** —
both block A and block B use the same round key, so the bitsliced
form is `bitslice(K ‖ K)`.

## The bit-permutation chain

Converting between canonical byte form and bit-sliced form is a
sequence of "delta swap" operations. Each delta swap exchanges
masked bits between two words.

```
def delta_swap(a, b, shift, mask):
    """Swap masked bits at distance `shift` between a and b."""
    t = (a ^ (b >> shift)) & mask
    a ^= t << shift
    b ^=  t
    return a, b
```

The canonical chain (from `aes-soft/src/fixslice32.rs`):

```python
def bitslice(input32: bytes) -> List[int]:
    # input32 = block_A (16 bytes) || block_B (16 bytes)
    t0 = u32_le(input32[ 0: 4]);  t1 = u32_le(input32[16:20])
    t2 = u32_le(input32[ 4: 8]);  t3 = u32_le(input32[20:24])
    t4 = u32_le(input32[ 8:12]);  t5 = u32_le(input32[24:28])
    t6 = u32_le(input32[12:16]);  t7 = u32_le(input32[28:32])

    t1, t0 = delta_swap(t1, t0, 1, 0x55555555)
    t3, t2 = delta_swap(t3, t2, 1, 0x55555555)
    t5, t4 = delta_swap(t5, t4, 1, 0x55555555)
    t7, t6 = delta_swap(t7, t6, 1, 0x55555555)

    t2, t0 = delta_swap(t2, t0, 2, 0x33333333)
    t3, t1 = delta_swap(t3, t1, 2, 0x33333333)
    t6, t4 = delta_swap(t6, t4, 2, 0x33333333)
    t7, t5 = delta_swap(t7, t5, 2, 0x33333333)

    t4, t0 = delta_swap(t4, t0, 4, 0x0F0F0F0F)
    t5, t1 = delta_swap(t5, t1, 4, 0x0F0F0F0F)
    t6, t2 = delta_swap(t6, t2, 4, 0x0F0F0F0F)
    t7, t3 = delta_swap(t7, t3, 4, 0x0F0F0F0F)

    return [t0, t1, t2, t3, t4, t5, t6, t7]
```

`inv_bitslice` applies the same swaps in REVERSE order — each delta
swap is its own inverse, so calling the chain in reverse undoes the
forward transformation.

`fixslice_inverse.py` is the canonical Python port. Both directions
pass a round-trip self-test (`python fixslice_inverse.py`).

## Why this defeats raw-key extraction

The master AES key is loaded into Rust as `&[u8; 32]`, then immediately
passed to the bit-sliced key expansion. The output is **the bit-sliced
round-key array** — 15 round keys × 8 u32 = 480 bytes of data where
each u32 holds bit-positions, NOT byte-positions.

Inside the bit-sliced round-key array:

* No 32-byte window contains the original master-key bytes.
* No 16-byte window contains a canonical round key.
* The XOR-folded bits don't form recognisable patterns.

So a sliding-window search for the key — which is what every previous
key-extraction tool does — is **mathematically guaranteed** to miss.

## What recovery requires

To get the master key from a bit-sliced expanded-key array:

1. **Apply `inv_bitslice` to round-key 0.** The first 8 u32 (=
   32 bytes) of the bit-sliced array, in canonical form, is the
   bitsliced encoding of `master[0..16] ‖ master[0..16]` (the
   first half of the master key, replicated). `inv_bitslice` yields
   those 32 bytes; both 16-byte halves are equal and reveal
   `master[0..16]`.

2. **For AES-256, apply `inv_bitslice` to round-key 1.** The next 8 u32
   is `master[16..32] ‖ master[16..32]`. (For AES-128 there's no
   round-key 1 in this position, only the schedule's K1 = derived.)

3. **If the round keys are pre-transformed for the Equivalent Inverse
   Cipher** (which is how aes-soft stores them for decrypt), invert
   the InvMixColumns step on K1..K13 first, OR identify the
   untransformed K0 from its position.

## The complication for HSW

`extract_decrypt_key.py` extracts the 60 i64 constants from the
decrypt branch of `vc`. They are 60 × 8 = 480 bytes — exactly the
size of AES-256's bit-sliced expanded round keys. But applying
`inv_bitslice` to every 4-i64 window and trying every pair as a
master-key did not hit.

The probable reason: these are the **decrypt-direction** round keys
in *Equivalent Inverse Cipher* form. To recover the master key:

* Round-key 0 (encrypt-side `K0`) is stored as **K0 itself** at one
  end of the array (no transform applied to K0 or K14).
* Round-key 1 (encrypt-side `K1` for AES-256) is stored as
  `InvMixColumns(K1)`. To recover K1 you'd `MixColumns` first, then
  `inv_bitslice`.

A complete recovery script must:

1. Determine the array order (forward `K0..K14` or reverse
   `K14..K0`).
2. Identify which positions are untransformed (`K0` and `K_max`) and
   which need MixColumns applied to undo the InvMixColumns transform.
3. inv_bitslice the recovered K0 and K1 (or run inverse-key-schedule
   from any other recovered K_i to back-compute K0 and K1).

This is the open task #27 in the project tracker.

## References

* RustCrypto `aes-soft/src/fixslice32.rs`:
  https://github.com/RustCrypto/block-ciphers/blob/master/aes/src/soft/fixslice32.rs
* Adomnicăi & Peyrin, **Fixslicing AES-like Ciphers** (2020):
  https://eprint.iacr.org/2020/1123
* Käsper & Schwabe, *Faster and Timing-Attack Resistant AES-GCM* (2009):
  https://eprint.iacr.org/2009/129  (original bit-sliced AES paper)
