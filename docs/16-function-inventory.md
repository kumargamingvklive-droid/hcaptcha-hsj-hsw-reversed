# hCaptcha hsw.wasm — Function-by-Function Inventory

Complete reverse-engineering of every n-token-reachable function in
`hsw.wasm`. Function indices are for build `587df480b9aaa94c...`; the
underlying algorithms are stable across builds (only indices rotate).

## Cryptographic primitives

### fn 548 — AES-256 fixslice32 block encrypt
- **Body**: 3165 bytes, 1528 instructions
- **Signature**: `(i32, i32) -> ()` — `(round_keys_buf, input_block_buf)`
- **Profile**: 96 XOR, 87 SHL, 132 AND, 0 ROTL — bitsliced fixslice32
- **Structure**: outer loop → 4-target br_table state machine
  - State 0 body (instr 71–727, 656 inst): initial setup + first AES rounds
  - State 1 body (instr 728–1367, 640 inst): middle rounds + key load
  - State 2 body (instr 1368–1497, 130 inst): final round + epilogue
  - Default state: exit handler
- **Constants**: 0x0f0f0f0f×30, 0x33333333×30, 0x55555555×23 (fixslice
  delta-swap masks); 0x11111111×3, 0x22222222×3, 0x44444444×3 (MixColumns);
  Rcon values 0x01, 0x02, 0x04, …, 0x36
- **Callees**: fn 193×36 (XOR-deobfuscated memory store), fn 203×45 (load),
  fn 487, fn 307×2, fn 432×2
- **n-token invocations**: 256× per token (build 587df480), 236× on prior
  build — variation per build's PoW difficulty + KS counts
- **Role**: AES round function called repeatedly to advance the bitsliced
  state through all 14 AES-256 rounds. Each call may be one round, one
  half-round, or the full block depending on internal state.

### fn 434 — AES round operation (fixslice32 variant)
- **Body**: 2865 bytes, 1432 instructions (identical size to old build's
  fn 391)
- **Signature**: `(i32, i32) -> ()` — `(state_buf, round_key_buf)`
- **Profile**: 190 XOR, 40 ROTL, 48 AND — same as old fn 391
- **Structure**: outer loop → br_table state machine (6 cases)
- **Constants**: 0x0f0f0f0f×16, 0x0f000f00×8 (MixColumns), 0x55555555×8,
  0x33333333×8, 0x11111111×8
- **Callees**: fn 193×96 (mem store), fn 203×56 (mem load), fn 354, fn 547,
  fn 585
- **n-token invocations**: 140× per token
- **Role**: SubBytes + ShiftRows + MixColumns + AddRoundKey decomposed
  into fine-grained state-machine steps. Each invocation handles one
  sub-round operation.

### fn 268 (build `145586e3...`) → fn ? (build `587df480...`) — SHA-1
- **Body**: 4508 bytes, 2602 instructions
- **Signature**: `(i32, i32) -> ()` — `(state_out_buf, message_block_buf)`
- **Profile**: 352 XOR, 224 ROTL, 88 AND — classic SHA-1 compression
- **Structure**: FULLY UNROLLED — 80 rounds inlined (no inner loop)
- **Constants**: K0=0x5A827999×20, K1=0x6ED9EBA1×20, ~K2=0x70E44324×20
  (bit-inverted form of standard K2=0x8F1BBCDC), ~K3=0x359D3E2A×20
  (bit-inverted K3=0xCA62C1D6)
- **Callees**: fn 584 (XOR-deobfuscated load) ×many, fn 376 (store) ×5
- **n-token invocations**: 256× per token = Hashcash 8-bit PoW
- **Role**: SHA-1 compression function. Each call processes one 64-byte
  message block, producing a 20-byte digest.

### fn 276 — ChaCha20 core (DEAD path during n-token)
- **Body**: 10030 bytes, 5813 instructions
- **Signature**: `(i32) -> ()` — `(state_ptr)`
- **Profile**: 256 ROTL, 258 SHL, 128 XOR, 0 ROTR
- **Structure**: outer loop → 6-case br_table
- **Constants**: sigma "expand 32-byte k" = 0x61707865×16, 0x3320646e×16,
  0x79622d32×16, 0x6b206574×16
- **n-token invocations**: 0 (dead code path)
- **Role**: Standard ChaCha20 keystream generation, processing 64-byte
  state via quarter-round operations. NOT used during n-token production.

### fn 388 — AES-256 key schedule (encrypt/decrypt path)
- **Body**: 4125 bytes
- **Signature**: `(i32, i32) -> ()` — `(rk_out, master_key_in)`
- **Profile**: 249 XOR, 0 ROTL — fixslice32 KS
- **n-token invocations**: 0 (only called from fn 594 = vc dispatcher
  encrypt/decrypt path, never on the n-token path)
- **Role**: Expands a 32-byte master key into 15 round keys in fixslice32
  bitsliced storage form.

## Orchestrators

### fn 288 — Async Promise state machine for n-token
- **Body**: 205,466 bytes, 83,769 instructions (the largest function)
- **Structure**: 50+ nested blocks + outer loop + giant br_table dispatch
  to async-state cases
- **Callees**: 281 distinct functions, dominated by fn 584 (2398×),
  fn 376 (1851×), fn 428 (1248×)
- **Role**: wbg-bindgen Rust async `Future::poll` state machine. Drives
  the entire n-token computation: AES KS → PoW solve (256 SHA-1 calls)
  → encrypt N-byte plaintext → emit base64 wire blob.
- **NOT directly callable** (target of `call_indirect` only)

### fn 174 — Sub-orchestrator with UTF-8 + ChaCha20 paths
- **Body**: 564 bytes, 282 instructions
- **Signature**: `(i32, i32) -> ()` — `(ctx_ptr, state_buf)`
- **Structure**: outer loop → 18-state br_table
- **Callees**: fn 547×2, fn 376×7, fn 584×10, fn 276 (ChaCha20)×1, fn 336,
  fn 539, fn 550
- **n-token invocations**: 2× per token
- **Role**: Multi-purpose state machine handling UTF-8 byte sequencing
  AND a ChaCha20-keystream branch (dead during n-token). The
  classic Rust pattern of a Future enum with multiple variants.

### fn 330 — PoW finalization
- **Body**: 384 bytes, 194 instructions
- **Signature**: `(i32) -> ()` — `(pow_state_ptr)`
- **Structure**: outer loop → 18-state br_table
- **Callees**: fn 584×10, fn 264×4
- **n-token invocations**: 5× per token (only 1 captured under gated
  instrumentation, because gating closes too quickly for the others)
- **Role**: Walks the SHA-1 output buffer in 12-byte strides, finalizing
  the Hashcash stamp. Updates the in-place buffer state.

### fn 416 — Six-block AES sequence (likely KDF)
- **Body**: 3334 bytes, 1634 instructions
- **Signature**: `(i32, i32, i32) -> ()`
- **Structure**: outer loop → 49-state br_table
- **Callees**: fn 391 (AES round)×6, fn 451×55, fn 547×37, fn 376×37,
  fn 584×16, fn 428×10, fn 263×6
- **n-token invocations**: 2× per token
- **Constants**: 0x48d96ffc×10 (mystery constant, possibly a type-ID),
  0xff00×20 (byte swap mask), 0x80, 0x40, 0x28, 0x24
- **Role**: Encrypts 6 consecutive 16-byte blocks via fn 391. Net output
  = 96 bytes of derived material. Most likely a key derivation function
  that produces session-specific keys + IVs from a master.

## Memory helpers (the XOR-obfuscated load/store layer)

### fn 584 / fn 203 — Obfuscated i32.load
- **Body**: 426 bytes, 242 instructions
- **Signature**: `(i32 offset, i32 base) -> i32`
- **Algorithm**:
  1. `addr = base + offset`
  2. `page = addr / 320`
  3. `flag = load_byte(addr at low-nibble offset)` — page-control byte
  4. If `flag != 0`: return raw `load_i32(addr)`
  5. Else: return `load_i64(addr % 96 + 909) ^ load_i32(addr)` (XOR with
     rolling 8-byte key from a 96-byte table at WASM offset 909)
- **Role**: Anti-tamper memory load that conditionally XOR-decrypts
  protected pages using a hardcoded 96-byte XOR table.

### fn 376 / fn 193 — Obfuscated memory store
- **Body**: 244 bytes, 132 instructions
- **Signature**: `(i32 base, i32 value, i32 offset) -> ()`
- **Algorithm**: mirrors fn 584; XOR-encodes the value before storing it
  byte-by-byte to memory addresses computed from the same 320-byte-page
  + 96-byte-XOR-key scheme.
- **Role**: Anti-tamper memory store.

### fn 263 — Constant table lookup (leaf)
- **Body**: 624 bytes, 0 callees
- **Signature**: `(i32, i32, i32) -> i64`
- **Role**: Reads from a precomputed constant table (LEA-style addressing
  with no XOR-deobfuscation). Likely a wbg-bindgen helper for retrieving
  function-table indices or interned constants.

### fn 428 — Constant table lookup variant
- **Body**: 353 bytes
- **Signature**: `(i32 idx, i32 magic1, i64, i32 magic2, f32, i32 base) -> ()`
- **Callees**: fn 265×2
- **Role**: Similar to fn 263 but with magic-constant signatures. The
  magic constants (-550209296, 1222209532, -339673580) appear together
  in many places — likely a TypeId/TypeMagic pattern from Rust's
  wbg-bindgen exports.

## Rust runtime helpers (commonly called from fn 288)

These are standard wbg-bindgen-generated Rust helpers, not crypto. Their
roles are inferred from opcode profile + callee patterns:

- **fn 220** (7371B): Allocator — `Vec::with_capacity` / `Box::new`
- **fn 267** (7308B): Format/Display — likely `core::fmt` machinery
- **fn 360** (6851B): Hash-map or BTreeMap iteration
- **fn 510** (6777B): Float arithmetic (42 mul) — likely number formatting
- **fn 342** (5902B), **fn 241** (5857B): Vec/slice manipulation
- **fn 529** (5613B): Float arithmetic (43 mul) — Date/Time formatting
- **fn 517** (4501B): Allocator helper
- **fn 199** (4365B): String processing (50 AND) — UTF-8 boundary checks
- **fn 214** (4029B): wbg-bindgen interop layer
- **fn 468** (3791B): Vec push/extend
- **fn 438** (3638B): Box<dyn Trait> dispatcher
- **fn 367** (3144B on build 587df480): AES-256 ECB block encrypt (= old fn 548)
- **fn 546** (2986B): Allocator
- **fn 289** (2799B): String concat
- **fn 238** (2719B): Bytes serialize
- **fn 284** (2702B): UTF-8 encoder
- **fn 476** (2636B): Vec slice clone
- **fn 341** (2562B): Promise wrapper
- **fn 296** (2522B): Tuple/struct serialize
- **fn 586** (2517B): Json/msgpack encoder
- **fn 462** (2341B): Counter/state increment
- **fn 443** (2283B): Buffer copy
- **fn 492** (2275B): String to-bytes
- **fn 398** (2149B): Multi-byte arithmetic

## Key insights

1. **fn 548 (AES block) is called 256× per token** — but BOTH arg0 (round
   keys) AND arg1 (input buffer) are STATIC across calls. This means the
   per-block counter / input isn't passed through the function arguments
   we capture — it's mutated by deeper internal state.

2. **fn 434 (AES round) called 140×** — likely 10 blocks × 14 rounds
   internal decomposition (the 10 vs 14 mismatch + extra setup explains
   140 not 280). The variable input pattern at arg1 (4 unique sliding-
   window values) confirms it's doing per-block round operations.

3. **fn 268 (SHA-1) called 256×** — exact match for Hashcash 8-bit
   difficulty (2^8 = 256 average trials). Confirmed PoW, NOT n-token MAC.

4. **The cipher mode** uses these primitives but in a NON-STANDARD
   configuration. The actual mode of operation (how AES-256 block-encrypt
   combines with a MAC/integrity check to produce the n-token wire
   format) is implemented inside fn 288's giant state machine and cannot
   be identified from black-box testing.

## What we DEFINITIVELY know

- Cipher is **NOT**: AES-GCM, CCM, OCB, EAX, SIV, GCM-SIV, CFB, OFB, CBC,
  CTR (any reasonable counter format) with KEY_A or KEY_B
- Cipher is **NOT**: ChaCha20-Poly1305 (fn 276 dead-path + no
  R-clamp 0x0ffffffc)
- Cipher is **NOT**: AES-CTR + HMAC-SHA1/SHA256 (truncated to 16 bytes)
- Cipher is **NOT**: a key-derivation chain we can recover from external
  testing (20,000+ derived candidates tried, all fail)
- Wire format **IS** `ct(N) || tag(16) || iv(12) || ver(0x02)` (byte-exact
  math confirms)

The remaining mystery — the exact AEAD construction — is implemented in
the 83,769 instructions of fn 288. Symbolic execution at that scale is
not feasible from this side of the WASM boundary.
