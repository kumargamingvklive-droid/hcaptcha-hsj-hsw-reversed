# HSW N-key derivation — the LCG/PCG byte-XOR recipe

The HSW bundle's third `window.hsw(...)` path takes a JWT and emits a
"PoW token" whose `n` field is a 32-byte challenge response. Those 32
bytes are **not random** — they are derived from a single 328-byte
rodata blob compiled into the WASM module, mixed through a 30-step
PCG-XSH-RR-flavoured LCG. The derivation is **deterministic per
build** (the rodata blob and the six "key factors" rotate with each
WASM rebuild, but the algorithm is fixed).

This file documents the math. The reference implementation is
[`src/hcaptcha/hsw_n_key.py`](../src/hcaptcha/hsw_n_key.py); credit to
Implex for the original reverse-engineering.

## The constants

| Symbol            | Value                                | Where                                              |
| ----------------- | ------------------------------------ | -------------------------------------------------- |
| `LCG_MULTIPLIER`  | `6364136223846793005` = `0x5851F42D4C957F2D` | hardcoded — PCG-32's published multiplier; appears as a single `i64.const` literal in the N-key function |
| `MEMORY_OFFSET`   | `1075552`                            | virtual address of the 328-byte rodata blob in WASM linear memory |
| `MARKER_INT`      | `8589934624` = `0x200000020`         | two packed 1-words, used as a one-shot identifier so the extractor can confirm it landed in the right function |
| `N_BYTES`         | `32`                                 | output key length                                  |
| `N_LCG_STEPS`     | `30`                                 | `= N_BYTES - 2` — the first two bytes of the key come from `key_seed` directly |

`0x5851F42D4C957F2D` is the published [PCG-32 LCG
multiplier](https://www.pcg-random.org/) — a Lehmer-style constant
chosen for full-period output on a 64-bit state. hCaptcha did not
invent it; they reused the canonical PCG number, which is why the
detection heuristic (find the function carrying the highest density of
`i64.const 0x5851F42D4C957F2D`) is so precise.

## The six per-key constants

Each build embeds six scalars in the N-key function. The extractor
recovers them by walking the bytecode of that function:

| Field         | WASM type | Role                                                                 |
| ------------- | --------- | -------------------------------------------------------------------- |
| `key_seed`    | `i32` (< 65536) | The low 16 bits become **bytes 0 and 1** of the output key, and the initial state-seeding scalar for the PCG. |
| `seed`        | `i64`     | Initial PCG state. First `i64.const` literal in the function that isn't the LCG multiplier or `key_factor1`. |
| `memory`      | `i32` (16 ≤ v < 4096) | Base index added to the step counter before computing the rodata segment address. |
| `key_factor1` | `i64`     | The constant added or subtracted right after each `i64.mul` with the LCG multiplier — Lemire-style PCG additive. |
| `key_factor2` | `i32` (10⁹ ≤ v < 4·10⁹) | Large positive offset folded into `memory_position`. Empirically always a 32-bit value with the top bit set or near it. |
| `operator`    | `'+'` / `'-'` | `+` if the post-mul op is `i64.add`, `-` if `i64.sub` — determines the sign of the `key_factor1` mix-in. |

In a build where the constants are inlined as `iN.const` literals,
[`extract_key_factors`](../src/hcaptcha/hsw_n_key.py) recovers them
directly. In builds that materialize them through helper calls
(`call HELPER` returning the value, no in-stream `const`), the
extractor returns `None` and the caller must fall back to a sandbox
drive (run `window.hsw(jwt)` and capture the result).

## The function-locator heuristic

```python
def _find_n_key_function(mod: WasmModule) -> Optional[int]:
    best_fi, best_count = None, 0
    for f in mod.functions:
        instrs = mod.decode_function(f["func_idx"]) or []
        c = sum(
            1 for name, ops, _, _ in instrs
            if name == "i64.const" and ops
            and (ops[0] & 0xFFFFFFFFFFFFFFFF) == LCG_MULTIPLIER
        )
        if c > best_count:
            best_fi, best_count = f["func_idx"], c
    return best_fi
```

The N-key function carries between 4 and 30 distinct occurrences of
the LCG multiplier (the inliner unrolls the LCG step ~15× in current
builds; older builds compile a single loop with one occurrence). Any
other function in the module carries at most one `i64.const
0x5851F42D4C957F2D` literal — they exist only for unrelated arithmetic
and are rare.

## The 30-step derivation

```python
def derive_n_key(factors: KeyFactors, memory: bytes) -> bytes:
    seed = factors.seed
    k1   = factors.key_factor1
    k2   = factors.key_factor2

    # Bytes 0,1 of the output = low/high byte of key_seed (LE).
    out = list(factors.key_seed.to_bytes(4, "little"))[:2]
    mem_len = len(memory)

    for step in range(30):                                # 30 iterations
        if step != 0:
            seed = (seed * LCG_MULTIPLIER) & 0xFFFFFFFFFFFFFFFF
            if factors.operator == "+":
                seed = (seed + k1) & 0xFFFFFFFFFFFFFFFF
            else:
                seed = (seed - k1) & 0xFFFFFFFFFFFFFFFF

        base_index      = factors.memory + step
        memory_position = base_index + k2

        # Two derived addresses, both reduced mod mem_len with wrap.
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

        # PCG-XSH-RR-style rotated-xor mix on the state.
        bit45 = signed_i32((seed >> 45) & 0xFFFFFFFF)
        bit27 = signed_i32((seed >> 27) & 0xFFFFFFFF)
        bit59 = signed_i32((seed >> 59) & 0xFFFFFFFF)
        combined = (bit45 ^ bit27) & 0xFFFFFFFF
        shift    = bit59 % 32
        rotated  = ((combined >> shift) | (combined << (32 - shift))) & 0xFFFFFFFF

        out.append((hash_value ^ rotated) & 0xFF)

    return bytes(out)
```

Each iteration:

1. **Advance the LCG.** Standard Lehmer step: `state ← state · M ± C` (mod 2⁶⁴).
   Step 0 is skipped — the initial `seed` is used as-is.
2. **Compute two rodata addresses** from `memory + step + key_factor2`:
   * `segment_address` — a "stretched" position that adds 8 bytes of
     gap every 320, then offsets by `1032 − MEMORY_OFFSET`. Reads 4
     bytes LE.
   * `mask_address` — `(pos mod 96) + 8`. Reads 8 bytes LE.
3. **Both reads wrap** around the end of the 328-byte blob if they'd
   overrun. The Python implementation uses `wrap_read` (inlined in
   `derive_n_key` proper); the WASM does the same via `memory.size`
   masking.
4. **XOR-fold** the segment's bottom byte against the mask's bottom 32
   bits: `hash_value = (segment_value ^ (mask_value & 0xFFFFFFFF)) & 0xFF`.
5. **Mix the LCG state** via the PCG-XSH-RR-style rotated-XOR:
   * Take three windows of the state (`>>45`, `>>27`, `>>59`),
     reinterpreting each as a signed i32 (the sign-extension matches
     the WASM's `i32.wrap_i64` followed by signed comparisons).
   * `combined = bit45 ^ bit27`; rotate right by `bit59 % 32`.
6. **Output byte** = `hash_value ⊕ (rotated & 0xFF)`.

After 30 iterations the output is 2 (seed bytes) + 30 (derived bytes)
= 32 bytes. Hex-encoded, that's the 64-character N-key string
returned by `fetch_n_key()`.

## Memory layout of the rodata blob

```
vaddr=1075552  +--------------------------------+   offset 0
               |  328 bytes of mask/segment data |
               |  read at indices derived from   |
               |  memory_position = memory + step + key_factor2
               +--------------------------------+   offset 328 (end)
```

The blob is laid out as **interleaved mask + segment quads** — every
8-byte chunk plays both roles in different iterations because the two
address derivations are offset relative to each other. The
`segment_address`'s `+1032 − MEMORY_OFFSET` term shifts reads into the
blob from a synthetic "virtual" address space; in practice this
biases reads toward the high end of the 328-byte window.

If the build no longer places a data segment exactly at vaddr 1075552
(it always has so far), `get_rodata_blob()` returns `None` and the
extractor must fall back to a runtime drive.

## End-to-end flow

```python
from hcaptcha.hsw_n_key import fetch_n_key
n_hex = fetch_n_key()                                    # uses latest_version()
# or:
n_hex = fetch_n_key("3441ba6850bebb5729a3e9698c8c5419272f07785b9fbb4178d928bd2bde44c9")
print(n_hex)
# → 64-char lowercase hex, e.g. "fe1ba43f33813dbac034ef12f34f3ee371b09057e2a25346a652c681edb2104b"
```

`fetch_n_key` does:

1. **Download** `hsw.js` for the version.
2. **Extract** the WASM bytes via the cheap `0,null,"…"` regex; falls
   back to the full sandbox extractor (`HSWAnalyzer`) for builds that
   split the WASM across multiple base64 chunks.
3. **Parse** with `WasmModule(wasm)` from
   [`wasm_disasm.py`](../src/hcaptcha/tools/wasm_disasm.py).
4. **Locate** the N-key function via LCG-multiplier density.
5. **Extract** the six per-build factors via in-stream `iN.const` scan.
6. **Read** the 328-byte rodata blob at vaddr `1075552`.
7. **Run** `derive_n_key(factors, blob)` for 30 iterations.
8. **Hex-encode** the resulting 32 bytes.

Total wall-clock: ~3 seconds (the WASM parse dominates).

## Validation against archived builds

| Version (truncated)        | n-key (truncated)            |
| -------------------------- | ---------------------------- |
| `8e8ed392ff7d339b77…`      | `fe1ba43f33813dba…b2104b`    |
| `a7e6714159bfb34b…`        | (per-build, see `data/archive/*.json`) |
| `eb32ed1d87031f73…`        | (per-build, see `data/archive/*.json`) |

These match the reference values stored in
[`data/archive/`](../data/archive/) and bit-exactly match what the
live `window.hsw(jwt)` returns when driven in a sandbox.

## Why this is reliable across builds

| What rotates per build               | What does not                                    |
| ------------------------------------ | ------------------------------------------------ |
| The six per-build constants          | The LCG multiplier `0x5851F42D4C957F2D`          |
| The 328-byte rodata blob             | Its virtual address `1075552`                    |
| The N-key function's index           | Its bytecode-shape (highest density of LCG-mul)  |
| Which subset of constants is inlined vs. helper-call-emitted | The 30-step PCG-XSH-RR-style mix |
| Number of LCG-mul occurrences (the inliner unrolls 1–30×) | The fact that no other function in the module references the LCG multiplier |

The combination of multiplier + vaddr + step-count fingerprints
the algorithm cleanly. If hCaptcha ever changes the multiplier (e.g.
to a different PCG variant), the extractor fails fast (`RuntimeError:
no function in WASM contains the LCG multiplier 6364136223846793005`)
rather than returning garbage.

## Cross-references

| Topic                                                   | See                                              |
| ------------------------------------------------------- | ------------------------------------------------ |
| Where in `vc`'s dispatch table the N-token export lives | [08-hsw-dispatch-table.md](./08-hsw-dispatch-table.md) |
| How `wasm_disasm.py` decodes the bytecode this scans    | [05-wasm-internals.md](./05-wasm-internals.md)   |
| Older architectural eras where this function had a different shape | [10-architecture-eras.md](./10-architecture-eras.md) |
| AES key-extraction (the other two HSW keys)             | [04-key-extraction.md](./04-key-extraction.md)   |
