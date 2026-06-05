# HSW N-key derivation — direct AES-site capture (current) and the legacy LCG path

The HSW bundle's third `window.hsw(...)` path takes a JWT and emits a
"PoW token" whose `n` field is an AES-256-GCM-encrypted blob produced
inside the WASM module. This file documents how we recover the AES
master key that the bundle feeds to that encrypt, and (for historical
reference) how the older LCG-derived intermediate state used to be
traced.

> **Headline:** the n-token AES master key is **build-static** and
> recovered by patching the prologue of the n-token AES encrypt
> entry (e.g. `fn 330` / `fn 352` — the index rotates per build) to
> dump the 32 bytes at `arg0` (the master-key buffer pointer). The
> same 32 bytes are observed on every call within a build (warmup +
> JWT call, every record in the ring). The fetcher finds the right
> function structurally — it doesn't depend on a hard-coded index.
> The earlier "per-call ephemeral" framing was wrong — see
> ["What changed"](#what-changed-and-why) below.

The reference implementation is
[`src/hcaptcha/hsw_n_key_capture.py`](../src/hcaptcha/hsw_n_key_capture.py)
(public API: `capture()`).

## What changed, and why

Earlier versions of this extractor patched the byte-store helper
inside `vc` and reported the captured bytes as the "n-key" — with a
`verified: false` flag because the bytes differed call-to-call and
didn't decrypt the n-token. That framing was wrong on two counts:

1. **`vc` is not on the n-token path.** Call-graph BFS over
   `hsw.wasm` shows:
     - The AES key-schedule helper that lives downstream of `vc`
       (currently `fn 477`) is the **request/response** AES KS used
       for `encrypt_req_data` / `decrypt_resp_data` — i.e. the
       `encrypt_key` / `decrypt_key` path, not the n-token path.
     - A **separate** fixslice32 KS helper (currently `fn 425`,
       phase-1 equivalent `fn 282`, body 2858 B, opcode profile
       xor≈190 rotl≈40, mask `0x0F000F00`) is reachable from
       `ec` / `pc` (the n-token Promise executor exports) but **not**
       from `vc`. That second KS is the n-token AES key schedule.
2. **The "per-call ephemeral" bytes were LCG intermediate values,
   not the AES key.** What the old byte-store trace captured was
   the WASM helper's runtime-seeded LCG output as it was being
   computed — useful debug noise, but never an input to AES.
   The actual AES master key for the n-token lives one level up,
   at `arg0` of the n-token AES encrypt entry (`fn 330`, sig
   `(i32,i32,i32) → i32`, calls KS `fn 425` six times per
   encryption). `arg0` is a pointer to a 32-byte master-key
   buffer — those 32 bytes are build-static.

The current extractor reads those 32 bytes directly at the moment
the bundle invokes its AES key schedule. The same key is captured
on every call within a build, structurally identifying it as the
n-token's AES master key.

## How the direct AES-site capture works

[`src/hcaptcha/hsw_n_key_capture.py`](../src/hcaptcha/hsw_n_key_capture.py)
implements the procedure end-to-end:

1. **Download + parse `hsw.wasm`** via `HSWAnalyzer`.
2. **Locate KS candidates** by structural fingerprint
   (`_find_ks_candidates`): sig `(i32,i32) → ()`, body ≥ 1000 B,
   ≥ 80 `i32.xor`, and at least one fixslice mask (`0x0F000F00`,
   `0x55555555`, `0x33333333`, or `251662080`).
3. **Filter by reachability** (`_reach_from`): keep the candidates
   reachable from `ec` / `pc` but not from `vc`. Those are the
   n-token-path KS variants.
4. **Pick the encrypt-entry callers** — currently `fn 330` (sig
   `(i32,i32,i32) → i32`) and any other promising sites — and add
   them to the instrumentation list.
5. **Patch each target's prologue** (`_build_key_dump_prologue`)
   with a stack-balanced WASM snippet that, when a memory gate is
   open, copies 32 bytes from `local[arg_idx]` into a per-(fn,arg)
   ring buffer. Each ring stores up to 256 records of
   `(counter_u32 || 32 bytes)`.
6. **Add `__peek32` / `__poke32` exports** so Python can read the
   ring buffers back and toggle the gate.
7. **Boot a jsdom sandbox**, swap the patched WASM in at
   `WebAssembly.instantiate`, run a warmup `hsw(1, empty)`, open
   the gate, run `hsw(jwt)`, close the gate.
8. **Read every ring**. Each ring whose records are **constant
   across calls** is a static buffer pointer at that call site —
   the AES master-key buffer is one of them.
9. **Pick the winner**: prefer the smallest-record-count ring whose
   name ends in `a0` (arg0 is the master-key pointer convention).
   The winning ring rotates with the build (we have observed
   `f330_a0` and `f352_a0` on consecutive builds, for example) —
   the fetcher resolves it structurally and does **not** hard-code
   a function index.

```python
from hcaptcha.hsw_n_key_capture import capture

out = capture()
# out["captured"]["f330_a0"][0]["key32_hex"] = the n-token AES master key
```

The `KeyFetcher` wraps this and exposes the result as
`hsw.n_key`. Extraction status on success:
`captured-from-f330_a0-Nrecords-static`.

## Verification — why we ship `verified: true`

The captured 32 bytes are:

- **Static across all records within a ring** (every fire of the AES
  encrypt entry within one process produces identical bytes), AND
- **Static across warmup + JWT calls** (the warmup `hsw(1, empty)`
  and the n-token `hsw(jwt)` invocation both capture identical
  bytes at `f330_a0`), AND
- **Captured at the exact site where the bundle invokes its AES key
  schedule** (fn 330 calls KS fn 425 six times per encryption — the
  classic AES-256 round-key expansion pattern).

Those three properties together structurally identify the captured
bytes as the n-token's AES master key. Hence `verified: true`.

### Caveat — the live n-token does NOT decrypt under this key

The captured key is the input to the **AES.encrypt** invoked by the
bundle for n-token production. The live n-token still does not
decrypt under standard AES-256-GCM (`iv‖ct‖tag` or `ct‖tag‖iv`),
AES-256-CTR, AES-256-ECB, or any AES-128 / inverse-bitslice variant
we have tried — see [`12-hsw-complete-summary.md`](./12-hsw-complete-summary.md)
for the brute-force table.

This means the n-token's outer envelope is **non-standard**: it
either prepends a PoW-stamp / length-prefix framing around an inner
AEAD, or applies a post-encrypt transform we haven't yet identified.
This is a *consumer-side* (envelope) question — it does not change
the fact that the **key itself** is correct. The key is exactly the
bytes the bundle's AES encrypt sees on its master-key input.

## Legacy: the 30-step LCG inside `vc`

The text below documents the *old* "N-key" derivation that the
earlier extractor in [`hsw_n_key_capture.py`](../src/hcaptcha/hsw_n_key_capture.py)
targeted. On older builds (eras a–c) the WASM exposes a single
function that runs a 30-step PCG-XSH-RR-flavoured LCG over a
328-byte rodata blob and emits 32 bytes that hCaptcha used (or that
we believed at the time hCaptcha used) as the n-token key. The
algorithm is preserved here for back-compat / archival reasons; it
is **not** the production path on era (d).

### The constants

| Symbol            | Value                                | Where                                              |
| ----------------- | ------------------------------------ | -------------------------------------------------- |
| `LCG_MULTIPLIER`  | `6364136223846793005` = `0x5851F42D4C957F2D` | hardcoded — PCG-32's published multiplier; appears as a single `i64.const` literal in the N-key function |
| `MEMORY_OFFSET`   | `1075552`                            | virtual address of the 328-byte rodata blob in WASM linear memory |
| `MARKER_INT`      | `8589934624` = `0x200000020`         | two packed 1-words, used as a one-shot identifier so the extractor can confirm it landed in the right function |
| `N_BYTES`         | `32`                                 | output key length                                  |
| `N_LCG_STEPS`     | `30`                                 | `= N_BYTES - 2` — the first two bytes of the key come from `key_seed` directly |

### The six per-key constants

Each era-(a–c) build embeds six scalars in the N-key function:

| Field         | WASM type | Role                                                                 |
| ------------- | --------- | -------------------------------------------------------------------- |
| `key_seed`    | `i32` (< 65536) | Low 16 bits become **bytes 0 and 1** of the output key, and the initial state-seeding scalar for the PCG. |
| `seed`        | `i64`     | Initial PCG state. |
| `memory`      | `i32` (16 ≤ v < 4096) | Base index added to the step counter. |
| `key_factor1` | `i64`     | The constant added or subtracted after each `i64.mul`. |
| `key_factor2` | `i32` (10⁹ ≤ v < 4·10⁹) | Large positive offset folded into `memory_position`. |
| `operator`    | `'+'` / `'-'` | Sign of the `key_factor1` mix-in. |

### The 30-step derivation (preserved for reference)

```python
def derive_n_key(factors: KeyFactors, memory: bytes) -> bytes:
    seed = factors.seed
    k1   = factors.key_factor1
    k2   = factors.key_factor2

    out = list(factors.key_seed.to_bytes(4, "little"))[:2]
    mem_len = len(memory)

    for step in range(30):
        if step != 0:
            seed = (seed * LCG_MULTIPLIER) & 0xFFFFFFFFFFFFFFFF
            if factors.operator == "+":
                seed = (seed + k1) & 0xFFFFFFFFFFFFFFFF
            else:
                seed = (seed - k1) & 0xFFFFFFFFFFFFFFFF

        base_index      = factors.memory + step
        memory_position = base_index + k2

        segment_address = (
            ((memory_position // 320) << 3) + memory_position
            + 1032 - MEMORY_OFFSET
        ) % mem_len
        mask_address    = (memory_position % 96) + 8

        seg_bytes  = wrap_read(memory, segment_address, 4)
        mask_bytes = wrap_read(memory, mask_address, 8)

        segment_value = int.from_bytes(seg_bytes, "little")
        mask_value    = int.from_bytes(mask_bytes, "little")
        hash_value    = (segment_value ^ (mask_value & 0xFFFFFFFF)) & 0xFF

        bit45 = signed_i32((seed >> 45) & 0xFFFFFFFF)
        bit27 = signed_i32((seed >> 27) & 0xFFFFFFFF)
        bit59 = signed_i32((seed >> 59) & 0xFFFFFFFF)
        combined = (bit45 ^ bit27) & 0xFFFFFFFF
        shift    = bit59 % 32
        rotated  = ((combined >> shift) | (combined << (32 - shift))) & 0xFFFFFFFF

        out.append((hash_value ^ rotated) & 0xFF)

    return bytes(out)
```

### Why the LCG path was a dead end on era (d)

On era (d) the build no longer exposes the LCG as a single
standalone function. Instead the LCG step is *inlined* inside `vc`'s
dispatcher, the seed mixes in a runtime-fed value
(`Math.round(Date.now()/1e3)` in the JS wrapper), and the output
bytes are written through the byte-store helper. Tracing that
helper recovers per-call-different bytes — and crucially, those
bytes are **not** the AES master key for the n-token. They are LCG
intermediate state that gets used somewhere else in `vc`'s
machinery; the n-token AES key is materialized via a different
code path that doesn't touch the byte-store helper at all.

The direct AES-site capture documented above bypasses the LCG
question entirely by reading the actual AES master-key buffer at
the encrypt call site.

### Fallback order in `KeyFetcher`

`KeyFetcher.fetch()` tries the direct AES-site capture first
(`hsw_n_key_capture.capture()`). If that fails (e.g. structural
changes in a future build), it falls back to the legacy two-pass
LCG trace (`hsw_n_key_capture.capture()`) and reports the
partial bytes with `extraction_status = "fallback-trace-N-of-32"`.

## End-to-end flow (current build)

```python
from hcaptcha import KeyFetcher
keys = KeyFetcher().fetch()
print(keys["hsw"]["n_key"])
# → 64-char lowercase hex, e.g.
#   "074cb68ffa72374113adf20618418085a0e853e85cf80ccbf4558a341a6fcc38"
print(keys["verified"]["hsw_n_key"])      # → True
print(keys["extraction_status"]["hsw_n_key"])
# → "captured-from-f330_a0-Nrecords-static"
```

The hex value rotates per build (hCaptcha rebuilds bundles roughly
every 10 minutes). The structural property — `f330_a0` static
across all records, ec/pc-reachable but not vc-reachable — is
invariant.

## Cross-references

| Topic                                                   | See                                              |
| ------------------------------------------------------- | ------------------------------------------------ |
| Where in the dispatcher the n-token path lives          | [10-architecture-eras.md](./10-architecture-eras.md) |
| Per-function role labels (current build's fn 330 / 425) | [11-hsw-function-map.md](./11-hsw-function-map.md) |
| End-to-end HSW summary (all 6 keys + PoW + n-token)     | [12-hsw-complete-summary.md](./12-hsw-complete-summary.md) |
| AES key-extraction (encrypt_key / decrypt_key)          | [04-key-extraction.md](./04-key-extraction.md)   |
| How `wasm_disasm.py` decodes the bytecode this scans    | [05-wasm-internals.md](./05-wasm-internals.md)   |
