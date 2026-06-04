# WASM 1.0 internals — a primer for `wasm_disasm.py`

This document is the reference for `wasm_disasm.py`, the WebAssembly
disassembler in this repo. It explains the binary format end-to-end
and how the disassembler navigates it.

## Module layout

```
+--------+--------+
| \0asm  | 01     |   magic (4) + version (4 = "1.0 little-endian")
+--------+--------+
| section 1       |   type section
| section 2       |   import section
| section 3       |   function section
| section 4       |   table section
| section 5       |   memory section
| section 6       |   global section
| section 7       |   export section
| section 8       |   start section
| section 9       |   element section
| section 10      |   code section
| section 11      |   data section
| section 12      |   datacount section (post-MVP)
| section 0...    |   custom sections (name info, source map, etc.)
+-----------------+
```

Every section begins with a 1-byte id, then a uleb128 payload length,
then the payload. Sections (except custom) must appear in id order.

### LEB128 — the bedrock of WASM encoding

WebAssembly uses LEB128 (Little-Endian Base 128) for variable-length
integers — both unsigned (`uleb128`) and signed (`sleb128`, two's
complement). Every continuation bit (the MSB of each byte) is set
until the final byte.

```
encode 624485 as uleb128:
  624485 = 0b 100110_0000111100 1100101
  groups of 7:  0100110 0000111 1001100 1100101
  reverse:      1100101 1001100 0000111 0100110
  set continuation on all but last:
                E5      CC      87      26
  bytes:        E5 CC 87 26
```

`decode_uleb` and `decode_sleb` in `wasm_disasm.py` are the
canonical implementations.

## Sections — the ones that matter for key extraction

### Type section (id 1)

A vector of function types, each `0x60` + uleb128 N + N valtypes
(params) + uleb128 M + M valtypes (results).

```python
mod.types = [
    (["i32", "i32"], ["i32"]),      # type 0 = (i32, i32) -> i32
    (["i32"],        []),            # type 1 = (i32) -> ()
    ...
]
```

### Import section (id 2)

Defines all the JS-side functions / tables / memories / globals the
WASM relies on. Each import has a `(module, name, kind, type)`
tuple. For function imports, `type` is an index into the type table.

For HSW, all 164 imports are functions in module `a` (the wbg shim
default). Imports occupy function indices 0..163.

### Function section (id 3)

Just a vector of type-indices — one per local (non-imported) function.
The actual code is in the **code section**. Together they define
function index `n_imports + i` for the i-th local function.

For HSW: 164 imports + 431 locals = 595 total functions.

### Export section (id 7)

A vector of `(name, kind, idx)`. For exports, `kind` is 0=func, 1=table,
2=memory, 3=global, and `idx` is into the corresponding index space.

HSW exports 19 functions + 1 memory under two-letter names that
rotate per build (`cc`, `dc`, `ec`, …, `vc`).

### Code section (id 10)

The actual instruction streams. A vector of "function bodies", one
per entry in the function section:

```
body_length    (uleb128)
locals_count   (uleb128)         # number of LOCAL-DECLARATION GROUPS
for each group:
  count        (uleb128)         # how many locals share the same type
  type         (1 byte valtype)
instructions...
0x0b           (end opcode)
```

Locals are stored in groups for compactness — e.g. `(3, i32), (2, i64)`
declares 5 locals: 3 i32 + 2 i64. Parameters occupy local indices
`0..n_params-1`, then declared locals come after.

### Data section (id 11)

Static memory contents copied into linear memory at instantiation.
Each segment has:

```
mode          (uleb128)
if mode in {0, 2}:
  if mode == 2:
    memory_index    (uleb128)
  offset_expr       (constant expression — i32.const N, end)
length             (uleb128)
data               (length bytes)
```

Mode 0 = "active, default memory 0", mode 1 = passive, mode 2 =
"active, specified memory". For HSW, all 186 segments are mode 0.

## The instruction set

Every WASM instruction is a 1-byte opcode followed by zero or more
immediate operands. The operand format depends on the opcode:

| Opcode group         | Immediates                                       |
| -------------------- | ------------------------------------------------ |
| Control (`block`, `loop`, `if`) | block type (valtype or type index)       |
| `br`, `br_if`        | label index (uleb128)                            |
| `br_table`           | vector of label indices + default                 |
| `call`               | function index (uleb128)                          |
| `call_indirect`      | type index + table index                          |
| `local.get/set/tee`  | local index (uleb128)                             |
| `global.get/set`     | global index (uleb128)                            |
| `*.const`            | i32 = sleb128, i64 = sleb128, f32 = 4 raw bytes, f64 = 8 raw bytes |
| Memory loads/stores  | alignment (uleb128) + offset (uleb128) — "memarg" |
| 0xFC-prefix          | extended opcodes (saturating conversion, bulk memory) |
| 0xFD-prefix          | SIMD                                              |
| 0xFE-prefix          | atomic                                            |

`wasm_disasm.py:OPCODES` is the lookup table covering every MVP +
common-extension opcode. The disassembler is intentionally tolerant —
unknown opcodes are stubbed rather than throwing, since we just want
to read function bodies for pattern matching.

## How `wasm_disasm.py` is structured

```python
class WasmModule:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.sections        = []   # all sections, in order
        self.types           = []   # parsed type table
        self.imports         = []   # parsed import table
        self.func_type_indices = [] # type idx per function (imports first)
        self.exports         = []   # parsed export table
        self.functions       = []   # parsed function bodies
        self.data_segments   = []   # parsed data segments
        self.globals         = []   # parsed globals
        self._parse_*()              # populated section by section

    def decode_function(self, func_idx):
        """Returns a list of (opcode_name, operands, offset_in_body, length)."""
        ...

    def opcode_histogram(self, func_idx):
        """Counter of opcode names for one function."""
        ...
```

The top-level CLI exposes a few research-oriented commands:

| Flag                    | Action                                                                  |
| ----------------------- | ----------------------------------------------------------------------- |
| `--summary`             | List all exports with their signatures.                                  |
| `--funcs 374`           | Print the first N instructions of a function (with `--head`).            |
| `--callers 374,300`     | For each given function, list every local function that `call`s it.       |
| `--find-fixslice`       | Heuristic — find functions loading the canonical fixslice32 bit-mask constants. |
| `--find-key-load`       | Find functions with 8+ consecutive `i32.load`/`i64.load` at sequential memarg offsets. |
| `--static-loads 313`    | Print every `i32.const ADDR; i32.load …` pair in a function, plus the bytes at that address from the data section. |
| `--save-json out.json`  | Dump per-function summaries to JSON for offline analysis.                |

## Heuristics — finding things in a stripped WASM

### Identifying fixslice32 routines

The canonical RustCrypto fixslice32 bit-permutation uses a few magic
masks:

```
0x55555555   ← interleave bits 0,2,4,…
0xAAAAAAAA   ← bits 1,3,5,…
0x33333333   ← interleave pairs
0xCCCCCCCC
0x0F0F0F0F   ← interleave nibbles
0xF0F0F0F0
0x00FF00FF   ← interleave bytes (sometimes)
0xFF00FF00
```

A function loading all four of `0x0F0F0F0F`, `0xF0F0F0F0`,
`0x55555555`, `0x33333333` is almost certainly a bit-permutation
step. Scoring by count of distinct masks finds them reliably even
in stripped binaries.

### Identifying the dispatcher

The dispatcher (`vc`) is a giant `if/else if` chain comparing its
first arg to magic numbers. After deobfuscating `hsw.js`, the magic
numbers for encrypt and decrypt are visible in the wbg wrappers
(`encrypt_req_data` calls `vc(ENC_MAGIC, …)`, `decrypt_resp_data`
calls `vc(DEC_MAGIC, …)`).

### Identifying static data loads

A function performing `i32.const ADDR; i32.load offset` is reading
from memory address `ADDR + offset`. `--static-loads` finds these
and prints the bytes that live at that address in the data section.

For HSW, the AES master key isn't loaded this way — it's inlined as
`i64.const` literals in the dispatcher itself, which a 4-million-
candidate brute force couldn't decode (see
[`04-key-extraction.md`](./04-key-extraction.md)).

## Limits — what the disassembler doesn't do

* **No validation.** We don't check that types match, that branches
  target valid labels, etc.
* **SIMD opcodes are skipped.** The 0xFD prefix has hundreds of
  sub-opcodes with varied operand formats. We decode the prefix and
  sub-opcode but don't recurse into immediates. This may cause
  misalignment in SIMD-heavy regions; HSW doesn't use SIMD so it's
  fine here.
* **No control-flow reconstruction.** Output is a linear instruction
  stream — no CFG, no basic blocks. For pattern matching this is
  enough.
* **No type recovery for locals.** All locals are typed (i32, i64,
  …) but we don't reconstruct *what* they represent (key bytes vs
  pointer vs counter).

Continue in [`06-fixslice32.md`](./06-fixslice32.md).
