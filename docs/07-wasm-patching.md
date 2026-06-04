# WASM bytecode patching — how the HSW key is actually pulled

The HSW master key sits in WASM linear memory only for the microseconds
it takes the AES key-schedule function to read it, bit-slice it, and
overwrite the words. None of the observation techniques in
[`04-key-extraction.md`](./04-key-extraction.md) can catch it — JS-side
hooks fire on import callbacks, not on plain memory reads.

The fix is to modify the binary itself: insert eight instructions at
the key-schedule's entry point that copy the master key bytes to a
fixed scratch region we can read back from JS. Two purpose-built tools
make this practical:

* **`wasm_disasm.py`** — full WASM 1.0 parser with structural
  heuristics for locating the dispatcher, key schedule, and XOR-deobf
  helper.
* **`wasm_writer.py`** — byte-perfect re-emitter. Re-emitting an
  unchanged module produces the same SHA-256 as the original;
  modifications are LEB128-correct so the patched binary instantiates
  without validation errors.

## End-to-end (what `HSWKeyFetcher` does)

1. **Run `deobf.py`** on the live `hsw.js` to expose readable source.
   From that source the encrypt and decrypt magic numbers are
   regex-extracted from the wbg wrappers.

2. **Parse the WASM** via `WasmModule(orig_bytes)`.

3. **Locate the dispatcher** — it's the WASM export named `vc`. Stable
   across builds.

4. **Locate the key schedule** by walking `vc`'s instruction stream:
   find the `i32.const MAGIC ; i32.eq ; if` triple for the encrypt
   magic, then within that `if` block find the first `call N` where
   `N` is a `(i32,i32)→()` function whose body contains the canonical
   fixslice32 mask constants (`0x55555555`, `0x33333333`, `0x0F0F0F0F`).
   This `N` is the AES key-schedule function.

5. **Locate the XOR-deobf helper** — the small `(i32,i32)→i32`
   function that the key schedule calls most often. Returns deobfuscated
   4-byte words when called with `(0, addr)`.

6. **Build the injection** — 8 sequences of:

   ```wasm
   i32.const SCRATCH+4i      ;; store address
   i32.const 0               ;; deobf arg0
   local.get 1               ;; key_ptr (the key-schedule's 2nd parameter)
   i32.const i*4             ;; (skipped when i=0)
   i32.add
   call DEOBF_HELPER         ;; returns the deobfuscated i32
   i32.store offset=0
   ```

   Plus a sentinel write at the end so JS can confirm the patch fired:

   ```wasm
   i32.const SENTINEL_ADDR
   i32.const 0xCAFEBABE
   i32.store offset=0
   ```

7. **Splice** the injection at code offset 0 of the key-schedule
   function (before its first opcode) via
   `CodeEditor.splice_code(func_idx, 0, n_replace=0, new_bytes=...)`.
   The code editor re-encodes the function's body-length LEB128 and
   the surrounding code-section length prefix.

8. **Add `__peek32` and `__poke32` exports** — trivial new functions
   that pass through to `i32.load` / `i32.store` so JS can read/write
   arbitrary linear-memory addresses without going through wbg
   marshaling.

9. **Substitute the patched binary** at runtime by hooking
   `WebAssembly.instantiate` inside the loaded bundle. The hook
   inspects the buffer argument; if it's the original `hsw.js` WASM
   blob (~592 KB), the hook returns our patched bytes instead. wbg
   reads `instance.exports` normally and never knows.

10. **Drive an encrypt** through `window.hsw(1, …)` — fires the
    encrypt branch of `vc`, which calls the patched key schedule,
    which executes our injection before any other instruction.

11. **Read 32 bytes from `SCRATCH_ENC`** via `__peek32` — that's the
    raw AES-256 master key.

12. **Verify** by AES-256-GCM-decrypting the same encrypt's output
    blob (`iv ‖ ct ‖ tag`). The GCM authentication tag's 2⁻¹²⁸
    false-positive rate means a successful verify = mathematical
    proof the key is correct.

## Decrypt-key in the same session

Both encrypt and decrypt go through the same compiled key-schedule
function (the compiler dedupes), passing different master-key pointers
in `vc`'s stack frame. So:

1. After encrypt finishes, copy the captured 32 bytes from
   `SCRATCH_ENC` to a safe slot (`SCRATCH_DEC`).
2. Drive `window.hsw(0, dummyBytes)` — the decrypt path. Authentication
   fails on our random bytes, but the key schedule runs FIRST and our
   injection overwrites `SCRATCH_ENC` with the decrypt key.
3. Read `SCRATCH_ENC` again — that's the decrypt master key.

Two keys per build, one sandbox session, ~8 seconds total.

## Why this is reliable across builds

Every function index in hsw.js rotates per build. Every magic number
rotates. Local indices, stack offsets, function order, segment layout
— all randomised. What does NOT rotate:

* The WASM export `vc` is always the dispatcher.
* Each encrypt / decrypt wbg wrapper always calls `vc(MAGIC, …)` with
  a literal int magic — discoverable by regex.
* The encrypt-magic `if`-block in `vc` always contains a call to
  the key schedule, which always uses ≥3 of the canonical fixslice32
  masks.
* The key schedule always reads its master key through 8 calls to a
  small `(i32,i32)→i32` XOR-deobf helper, with `local 1` as the key
  pointer base.
* The key schedule's `local 1` is always the second parameter — the
  caller passes the master-key pointer there.

These are structural properties of the compiled Rust + wasm-bindgen
output, not coincidences. The fetcher locates each piece by role, not
by number, and works on every build we've tested.

## Build verification

`wasm_writer.py` includes a self-test: re-emitting the unmodified
hsw.js WASM produces a binary with the same SHA-256 as the input.
This proves LEB128 round-trip correctness and that the
section-replacement logic doesn't introduce drift.

Adding a no-op exported function (`get4242: () → i32 { return 4242 }`)
and instantiating in Node also passes — proves the type / function /
export / code section updates wire up correctly.
