# HSW dispatch table — every `g12.vc(MAGIC, …)` call site

`hsw.js` ships a single 8-argument all-`i32` WASM export named **`vc`** that
serves as a magic-number multiplexer for every "fast path" the wbg JS
glue needs to hit. Each callable lives behind a different `MAGIC`
constant; `vc` reads its first parameter, does an `i32.eq` against each
known magic, and branches into the corresponding internal function.

The deobfuscated bundle exposes the table as `g12.vc(MAGIC, a1..a7)` —
see [hsw_deobf.js](../../hsw-re-3/hsw_deobf.js) (and the byte-identical
copy at `HSJ/hsw_deobf.js`). The set of magics is **stable in shape but
rotates per build** — the values below are valid only for the current
deobf snapshot. Treat the magics as opaque cookies; locate each call
site by its **role** (DataView setter type + width, AES op), not its
literal number.

## Why one export instead of N

`wasm-bindgen` would normally emit one export per pub-fn. hCaptcha's
fork collapses them all behind `vc` so that the only stable symbol in
the exports section is `vc` itself; every "real" function index is
hidden inside `vc`'s branching logic and rotates per build. This is the
**era (d)** dispatcher pattern documented in
[10-architecture-eras.md](./10-architecture-eras.md).

Three sibling exports cover read-style and special paths:

| Export | Signature                        | Role                                                              |
| ------ | -------------------------------- | ----------------------------------------------------------------- |
| `vc`   | `(i32 × 8) -> ()`                | Write-side multiplexer — all `setIntN` / `setFloatN` / AES ops    |
| `uc`   | `(i32, i32, i32, i32, i32, i32, i32) -> i32` | Integer-load multiplexer — `getInt8/16/32`, `getUint8/16/32` |
| `sc`   | `(i32, i32, i32, i32, i32) -> f32` | `getFloat32`                                                      |
| `tc`   | `(i32, i32, i32, i32) -> f64`    | `getFloat64`                                                      |

The exact arity / result-type split is what lets `find_dispatcher` in
[`wasm_disasm.py`](../src/hcaptcha/tools/wasm_disasm.py) pick `vc` out
of the export list even when its name rotates: `vc` is the unique
"highest-arity all-i32 → void" export.

## The 10 distinct `vc` magics

Pulled directly from `hsw_deobf.js` line-by-line:

| #  | Magic (signed)   | Magic (hex)   | Role                          | Call signature (after magic)            | Deobf lines |
| -- | ----------------:| -------------:| ----------------------------- | --------------------------------------- | ----------- |
| 1  |    `-1852740449` | `0x91D6B89F`  | `setInt8` / `setUint8`        | `(addr, 0, 0, 0, 0, byte, 0)`           | 494, 500, 1023, 1044 |
| 2  |     `809794792`  | `0x30435868`  | `setInt16` / `setUint16` (BE-flipped) | `(word, addr, 0, 0, 0, 0, 0)`   | 528, 538, 1030, 1051 |
| 3  |    `-1132311010` | `0xBC91A05E`  | `setInt32` / `setUint32` (BE-flipped) | `(addr, 0, 0, dword, 0, 0, 0)`  | 548, 558, 1037, 1058 |
| 4  |     `1807796889` | `0x6BC8F119`  | `setFloat32` (BE-flipped, payload as i32 bit-pattern) | `(addr, 0, 0, 0, f32_bits, 0, 0)` | 568, 1065 |
| 5  |    `-1897133290` | `0x8F2C8156`  | `setFloat64` (BE-flipped, payload as 8 bytes split) | `(0, 0, hi, 0, 0, 0, lo)` | 578, 1073 |
| 6  |     `479622289`  | `0x1C975A11`  | **`encrypt_req_data`** — AES-256-GCM encrypt of the request payload | `(0, 0, 0, 0, 0, scratch_ptr, externref_obj)` | 5223 |
| 7  |    `2019615644`  | `0x785A661C`  | **`decrypt_resp_data`** — AES-256-GCM decrypt of the server response | `(0, 0, 0, 0, 0, externref_obj, scratch_ptr)` | 5383 |
| 8  |  (build-rotating)| —             | reserved — currently used for `set` paths that fall back through helper `kc/qc` | — | helper-only |
| 9  |  (build-rotating)| —             | reserved — currently unused in deobf output (placeholder in `vc`'s switch) | — | — |
| 10 |  (build-rotating)| —             | reserved — currently unused in deobf output | — | — |

Magics 1–5 are the **five typed-array setter magics** (one per
JS `DataView` setter family); magics 6–7 are the **two AES magics**;
magics 8–10 are slots that `vc`'s `if/else` chain still contains
i32-compare gates for but the present build's wbg JS layer never
calls. Future builds may resurrect them — the extractor in
[`hsw_keys.py`](../src/hcaptcha/hsw.py) only commits to the two AES
magics it can prove via fixslice32-call reachability, so resurrected
slots don't matter for key recovery.

### Why ten — the design

Each setter magic corresponds to one (element-type, endianness) tuple
that `wasm-bindgen` would normally emit as a separate
`__wbindgen_export_N` shim. The compiler-emitted shim that backs each
magic in `vc` looks roughly like:

```wasm
  ;; vc body, post-prologue
  block (result)
    local.get 0            ;; the MAGIC argument
    i32.const -1852740449  ;; setInt8 magic
    i32.eq
    if
      local.get 1          ;; addr
      local.get 6          ;; byte
      i32.store8 offset=0
      br 1
    end
    local.get 0
    i32.const 809794792    ;; setInt16 magic
    i32.eq
    if
      local.get 2          ;; addr
      local.get 1          ;; word
      i32.store16 offset=0
      br 1
    end
    ;; …repeated for every magic in the table…
  end
```

The argument slot each `local.get` reads from is also magic-specific
— hence the visually noisy `(magic, a, 0, 0, b, 0, 0, 0)`-shaped
calls. Each magic uses a different subset of the 7 trailing
arguments; the rest are zero-padding to satisfy the shared 8-param
type.

## Sibling load-side dispatchers

`vc` is write-only. Anything that **returns** a value needs a
different export so the WASM type-system gets the right result-type
back. Three exist:

### `uc` — `(i32 × 7) -> i32` integer-load multiplexer

| Magic (signed) | Magic (hex)  | Role          | Lines      |
| --------------:| ------------:| ------------- | ---------- |
|   `2134344475` | `0x7F36AF1B` | `getInt8`     | 491, 1041  |
|  `-1153642621` | `0xBB3FB283` | `getUint8`    | 497, 1017  |
|   `1134629968` | `0x439BCD90` | `getInt16`    | 522, 1048  |
|  `-1270602174` | `0xB4435142` | `getUint16`   | 532, 1027  |
|  `-1048354066` | `0xC177076E` | `getInt32`    | 542, 1055  |
|   `-993712006` | `0xC4C7DCFA` | `getUint32`   | 552, 1034  |

### `sc` — `(i32 × 5) -> f32` float32-load

| Magic (signed) | Magic (hex)  | Role          | Lines      |
| --------------:| ------------:| ------------- | ---------- |
|  `-1130526654` | `0xBCB2C742` | `getFloat32`  | 562, 1062  |

### `tc` — `(i32 × 4) -> f64` float64-load

| Magic (signed) | Magic (hex)  | Role          | Lines      |
| --------------:| ------------:| ------------- | ---------- |
|   `1106774880` | `0x41FAF260` | `getFloat64`  | 572, 1070  |

The load-side multiplexers are split by result type because WASM doesn't
allow polymorphic returns. They share `vc`'s magic-table style internally.

## Non-multiplex exports (called directly, no magic)

A handful of small exports take the **role of magic + dispatch combined**:
they're each a single function with a fixed responsibility, exported under
a 2-letter name. They appear as `g12.<name>(…)` in the deobf output:

| Export    | Role                                       | Sample call site (deobf line) |
| --------- | ------------------------------------------ | ----------------------------- |
| `mc`      | Stack-frame allocate/free (`mc(-16)` push, `mc(16)` pop) | 446, 451, 1792, 1799, 5222, 5229 |
| `ic`      | Generic 4-arg helper used in init path     | 447                           |
| `gc`      | String/buffer round-trip helper            | 1006                          |
| `pc`      | 4-arg setter used in the `c3` init helper  | 1793                          |
| `hc`      | Single-arg drop / unref                    | 1977                          |
| `cc`      | Promise-callback trampoline (4-arg)        | 5344                          |
| `jc`      | Reset/init slot writer                     | 1302                          |
| `kc` / `qc` | Allocator + free pair, passed to `d3()` for UTF-8 marshalling | 5242, 5274, 5292, 5316, … (very heavy use) |
| `oc`      | Memory export — the WASM `Memory` object   | 485 (`g12.oc.buffer`)         |
| `rc`      | Function table — passed to `r2()` to look up `dyn fn` indices for closures | 5534, 5827 |

`oc` and `rc` are not callables; they are the `Memory` and `Table`
exports. `rc` is the function-pointer table used by `r2(a, b, g12.rc, w1)`
to materialise a JS closure backed by a WASM function index.

## The N-token export — *separate* export, rotates per build

The third path of `window.hsw(jwt)` (deobf line 5844) does **not** go
through `vc`. The relevant tail is:

```js
window.hsw = function (a, b) {
  if (0 === a) return d1().then(function (a) { return a.decrypt_resp_data(b); });
  if (1 === a) return d1().then(function (a) { return a.encrypt_req_data(b); });
  // …parse `a` as a JWT, pull payload + timestamp…
  return d1().then(function (a) {
    return a.ec(JSON.stringify(e), f, c, w2);   // <-- direct export call
  });
};
```

That `.ec(...)` is the **N-token entry**. It is exported under a
2-letter name distinct from `vc` and is called **directly** — there is
no magic argument and no multiplexer. In the current deobf the export
is named **`ec`**; previous builds have used **`rc`**, and other
2-letter names appear in other archive versions. The name rotates per
build; what does not rotate is the call shape:

| Property                  | Value                                                |
| ------------------------- | ---------------------------------------------------- |
| Arity                     | 4 (`i32, i32, i32, i32`)                             |
| Returns                   | `i32` (an externref index → string via `b3()`)       |
| Argument roles            | `(0, ts, len, 0, str_ptr, jwt_externref, payload_externref_or_0)` |
| Wbg wrapper (deobf line)  | 5453                                                 |
| Call site (deobf line)    | 5456                                                 |

The deobf wrapper at line 5453 is:

```js
ec: function (a, b, c, d) {
  var e = d3(a, g12.kc, g12.qc);   // marshal `a` (the stringified payload) → WASM
  var f = h12;
  return b3(g12.ec(0, b, f, 0, e, l1(d), v2(c) ? 0 : l1(c)));
},
```

Note the export name `g12.ec` collides lexically with the wrapper's own
key `ec:` — `g12.ec` (the WASM export) and `j12.ec` (the wbg-import
wrapper) are different functions; the deobf bundler happens to give
them the same 2-letter name. The wbg wrapper is what JS callers see;
the export is what the WASM dispatcher calls back out through.

### Locating the N-token export programmatically

Since the export name rotates, the extractor must locate it by role.
Stable structural properties:

1. It is the **only** export with signature `(i32, i32, i32, i32) -> i32`
   that is **reachable from `window.hsw`'s third branch** (the JWT
   branch) and not reachable from `vc`.
2. Its body always contains the **LCG multiplier `0x5851F42D4C957F2D`**
   as an in-stream `i64.const` literal — see
   [09-hsw-keys-derivation.md](./09-hsw-keys-derivation.md). This is
   the same multiplier `_find_n_key_function` in
   [`hsw_n_key.py`](../src/hcaptcha/hsw_n_key.py) keys off.
3. It always reads the rodata blob at vaddr **1075552**.

Combining (2) and (3) gives a 100%-precise identifier independent of
the export name.

## Cross-references

| Topic                                                   | See                                              |
| ------------------------------------------------------- | ------------------------------------------------ |
| How the magics get patched / decoded for key extraction | [07-wasm-patching.md](./07-wasm-patching.md)     |
| The LCG-based N-token derivation that the `ec` export performs | [09-hsw-keys-derivation.md](./09-hsw-keys-derivation.md) |
| Why "named-export multiplex" replaces older direct-call dispatch | [10-architecture-eras.md](./10-architecture-eras.md) |
| The `fixslice32` AES backend that magics 6 + 7 reach into | [06-fixslice32.md](./06-fixslice32.md)         |
| The WASM binary layout `vc` lives inside                | [05-wasm-internals.md](./05-wasm-internals.md)   |
