# HSW architectural eras — four generations of WASM dispatch

Across the archive of `hsw.js` builds we have observed (1.39.0 →
1.40.21 → 1.40.31 → 1.40.34 → current), the WASM module has gone
through **four distinct dispatch architectures**. Each generation
requires its own extractor strategy: pattern that works perfectly on
era (b) returns zero results on era (d). The current production
extractor in [`hcaptcha.hsw`](../src/hcaptcha/hsw.py) targets **era
(d)** exclusively; this document records the older shapes so the back-
test harnesses ([backtest_1_40_15.py](../backtest_1_40_15.py),
[backtest_1_40_31.py](../backtest_1_40_31.py),
[analyze_1_40_34*.py](../analyze_1_40_34.py)) make sense.

## Summary table

| Era | Range (observed)        | Dispatch shape                             | Magic                | Extractor strategy                            |
| --- | ----------------------- | ------------------------------------------ | -------------------- | --------------------------------------------- |
| (a) | ≤ 1.39.0, 1.40.15       | Direct named exports per pub-fn            | None (function index *is* the magic) | Locate each export by name (`encrypt_req_data`, `decrypt_resp_data`, etc.) |
| (b) | 1.40.21                 | `call_indirect` over a function table      | Table index = magic  | Walk the element segment; map JS-side enum to table slot |
| (c) | 1.40.31 – 1.40.34       | Named-export dispatcher (`cb` / `bb`), no magic compares | Argument-shape dispatch (no `i32.eq` gates) | Identify dispatcher by export name + arity; scan call-graph to fixslice |
| (d) | 1.40.34+ (current)      | Single multi-arg export (`vc`) + `i32.eq` magic chain | 32-bit hash per role | Pattern `i32.const C ; i32.eq ; if` inside `vc`, gate-to-fixslice reachability |

## Era (a) — pre-`vc` direct calls

**Observed in:** 1.39.0, 1.40.15.

The wasm-bindgen output is essentially stock. Every pub-fn the Rust
crate exports gets its own WASM export with its own type:

```
exports:
  encrypt_req_data       (externref) -> i32
  decrypt_resp_data      (externref) -> i32
  __wbindgen_malloc      (i32, i32)  -> i32
  __wbindgen_realloc     (i32, i32, i32, i32) -> i32
  __wbindgen_free        (i32, i32, i32) -> ()
  __wbindgen_export_4    (i32, i32)  -> i32       ;; DataView setInt8 path
  __wbindgen_export_5    (i32, i32, i32) -> ()    ;; setInt16
  …
```

The JS-side wbg shim references each export by name; no
multiplexer. Backtest evidence in
[`backtest_1_40_15.py`](../backtest_1_40_15.py) — the script reads the
JS bundle, pulls the base64 WASM blob via the **simple `0,null,"…"`
regex** (which only works on era (a) — modern builds inline the WASM
as multiple `jn(…)` chunks in undefined order), and looks for the
named exports directly.

### How to identify era (a) modules

1. Export list contains literal strings like `encrypt_req_data` /
   `decrypt_resp_data` (not 2-letter aliases).
2. No single export with arity ≥ 8.
3. Largest function in the module is the AES key schedule, not a
   dispatcher.

### Extractor strategy

The era (a) recipe is:

```python
for ex in mod.exports:
    if ex["name"] == "encrypt_req_data":
        encrypt_fi = ex["idx"]
    if ex["name"] == "decrypt_resp_data":
        decrypt_fi = ex["idx"]
# … then patch each at offset 0 with the same scratch-write injection
# documented in 07-wasm-patching.md.
```

No magic numbers to discover; the call sites are statically resolvable
by name. Era (a) is the easiest target — but it disappeared from
production by 1.40.21.

## Era (b) — `call_indirect` dispatch

**Observed in:** 1.40.21 (`1_40_21.wasm`, `1_40_21_hsw_bind.js` in
the project root).

hCaptcha collapses all the per-role wbg exports into a **single
dispatcher** that does `call_indirect` over the function table. The
"magic" is now a small integer that selects a table slot:

```wasm
;; dispatcher body (sketch)
local.get 0         ;; the magic (now a small uint, 0..N_TABLE_ENTRIES-1)
call_indirect (type $sig_for_role)
```

The element segment populates the table with the per-role functions
in a build-determined order. The JS-side glue still has
`encrypt_req_data` / `decrypt_resp_data` shims, but each shim calls
the dispatcher with a different small integer rather than a different
export.

### How to identify era (b) modules

1. Far fewer named exports than era (a) — most pub-fn shims have
   collapsed into one or two.
2. Element section is large and populated.
3. The (now single) dispatcher's body uses `call_indirect` heavily and
   has very few direct `call` instructions.
4. JS bundle's wbg shim contains literal small-int magics (e.g. `1`,
   `2`, `7`) being passed to a single exported function name.

### Extractor strategy

```python
# Find the table:
table_init = mod.element_segments[0]  # usually only one
# table_init["funcs"] is the slot -> func_idx mapping
# Map JS-side magic (small int) to slot.
# Locate the fixslice key schedule among table_init["funcs"]
# by mask-density (FIXSLICE_MASKS in wasm_disasm.py).
key_schedule_fi = find_fixslice_in(table_init["funcs"])
# Patch at the schedule's offset 0 as usual.
```

The element-segment walk is the key difference. Patching is identical
to era (a)/(d).

## Era (c) — named-export non-magic dispatcher

**Observed in:** 1.40.31, 1.40.34 (`backtest_1_40_31.py`,
[analyze_1_40_34.py](../analyze_1_40_34.py)).

The dispatcher comes back as a direct `call`-based switch — but
**there are no magic numbers and no `i32.eq` gates**. Instead the
dispatcher branches purely on argument *shape* (which arguments are
zero vs. non-zero) or on a small enum read from a struct field. The
2-letter export name is **`cb`** (in 1.40.34) or **`bb`** (in 1.40.31)
— the name rotates and is structurally unrelated to era (d)'s `vc`.

The diagnostic from `analyze_1_40_34_v3.py` is explicit:

```
Scanning 2 hosts (bb, cb and depth-1 callees)
Magic-gated branches that reach fixslice: 0
Unique magic constants gating fixslice: 0
```

i.e. the `i32.const C ; i32.eq ; if` pattern that works on era (d)
returns **zero** matches on era (c). The dispatcher is using
non-equality branches (`br_table`, `i32.lt_s`, struct-field reads).

### How to identify era (c) modules

1. There IS a high-arity export, but its name is `cb` or `bb` (not
   `vc`).
2. Its body contains many `call` instructions but few `i32.eq` /
   `i32.ne` gates.
3. The dispatcher reaches fixslice32 key-schedule functions via
   chains of 2-3 `call`s.
4. Magic-pattern scan returns 0 hits.

### Extractor strategy

The era (c) recipe requires a different fingerprint:

```python
# Locate the dispatcher by export-name + arity (5+ i32 params).
disp_idx = find_exported_dispatcher(mod)
# Build the call graph from disp_idx outward.
# For each callee, check fixslice32 mask density; the highest-scoring
# reachable function is the key schedule.
# Disambiguate encrypt vs decrypt by walking JS-side wbg shims to see
# which dispatcher arguments each path passes.
```

`analyze_1_40_34_v3.py` implements this fall-back: scan dispatcher
hosts `bb` and `cb` AND their depth-1 callees, looking for any
`i32.const C` near an `i32.eq` near an `if`, then check whether
calls in the taken branch reach a fixslice function. On era (c) this
yields zero matches — confirming the dispatcher does not use magic
gates at all.

The extractor for era (c) is currently unimplemented in the project;
back-testing the era (d) extractor against 1.40.31/1.40.34 confirms
it fails cleanly (no false positives) rather than returning wrong
keys.

## Era (d) — modern `vc` + magic multiplex

**Observed in:** 1.40.34+ and current production builds.

The dispatcher is the 8-arg all-`i32` export **`vc`**, and inside it
is a long chain of `i32.const MAGIC ; i32.eq ; if` gates — one per
role. The 10 magics documented in
[08-hsw-dispatch-table.md](./08-hsw-dispatch-table.md) make up the
table. The N-token export (`ec` in the current build) is **separate**
from `vc` and reachable only from `window.hsw`'s third path.

### Sub-architecture (d.1) — the n-token AES site is its own dispatcher path

`vc` is not a single monolithic dispatcher for all crypto. Call-graph
BFS over `hsw.wasm` shows that the n-token AES encryption is reached
via a **distinct sub-path** that does not touch `vc` at all:

```
window.hsw(jwt)
  └─ JS-side Promise wrapper
       └─ ec / pc (the n-token Promise executor exports)
            └─ ... → fn 548 (wbg-bindgen Promise state machine, ~192 KB body)
                 └─ AES encrypt entry  (fn 330 / fn 352 — rotates per build)
                      └─ AES KS  (fn 425 on current build, called 6× per encrypt)
```

The KS reached from this path (currently `fn 425`, structurally
matchable by sig `(i32,i32) → ()`, body ≥ 1000 B, ≥ 80 `i32.xor`,
mask `0x0F000F00`) is **only reachable from `ec`/`pc`**, never from
`vc`. Conversely the KS reached from `vc` (currently `fn 477`) is the
`encrypt_req_data` / `decrypt_resp_data` schedule that the
`encrypt_key` / `decrypt_key` patch targets. So era (d) effectively
has **two parallel AES key schedules**, sharing the fixslice32
implementation but routed by independent dispatcher paths.

The n-token AES master key (the `hsw.n_key` we report) is the 32
bytes at `arg0` of the AES encrypt entry on the second path. The
extractor identifies that entry structurally — by reachability filter
(ec-reachable AND NOT vc-reachable) plus the smallest-record-count
"a0" ring being constant across calls — rather than by hard-coded
function index. See [`09-hsw-keys-derivation.md`](./09-hsw-keys-derivation.md)
for the procedure.

### Identifying era (d) modules

1. There is a single export named `vc` with arity **8**, all
   parameters `i32`, no return.
2. Sibling exports `uc` / `sc` / `tc` exist for read-side typed-array
   loads (i32 / f32 / f64 result types respectively).
3. `vc`'s body contains a **long chain of `i32.const C ; i32.eq ; if`
   triples** (≥ 7).
4. The `if`-blocks for the AES magics call into a function whose body
   has fixslice32 mask density ≥ 3 (the `0x55555555`, `0x33333333`,
   `0x0F0F0F0F` family).

### Extractor strategy

This is the current production strategy, fully implemented:

```python
# 1. find dispatcher
vc = next(e for e in mod.exports if e["name"] == "vc")
# 2. walk vc body for i32.const ; i32.eq ; if triples
magics = []
for i in range(len(vc_instrs) - 2):
    if vc_instrs[i][0]   == "i32.const" and \
       vc_instrs[i+1][0] == "i32.eq"    and \
       vc_instrs[i+2][0] == "if":
        magic = vc_instrs[i][1][0]
        # 3. inside the if-block, find call to fixslice function
        ...
# 4. patch the key-schedule's offset 0 with scratch-write injection
# 5. drive window.hsw(0, ...) and window.hsw(1, ...) to fire encrypt/decrypt
```

Documented end-to-end in
[07-wasm-patching.md](./07-wasm-patching.md). Successful on every
production build since 1.40.34.

## Detection-time decision tree

```
┌─────────────────────────────────────────────────────┐
│ Does the export list contain literal                 │
│ "encrypt_req_data" / "decrypt_resp_data"?            │
└────────────────────┬────────────────────────────────┘
                     │ YES                NO
                     ▼                    │
                 ERA (a)                  │
                                          ▼
                     ┌─────────────────────────────────────┐
                     │ Does the dispatcher use call_indirect │
                     │ as its primary branching mechanism?   │
                     └───────────┬─────────────────────────┘
                                 │ YES                NO
                                 ▼                    │
                              ERA (b)                 │
                                                      ▼
                                      ┌─────────────────────────────────────┐
                                      │ Is there an export named "vc"?       │
                                      └──────────────┬──────────────────────┘
                                                     │ YES         NO
                                                     ▼             │
                                                  ERA (d)          │
                                                                   ▼
                                                                ERA (c)
                                                   (dispatcher = "bb"/"cb"
                                                    with no magic gates)
```

The decision is structural — no version string needed. The detector
runs in <100 ms on a parsed `WasmModule` and gives the right answer
even for builds the project has never seen, as long as they fit one
of these four shapes.

## Extractor coverage matrix

|                                 | Era (a) | Era (b) | Era (c) | Era (d) |
| ------------------------------- | :-----: | :-----: | :-----: | :-----: |
| `backtest_1_40_15.py`           |   ✅    |   —     |   —     |   —     |
| `backtest_1_40_31.py`           |   —     |   —     | partial |   —     |
| `analyze_1_40_34*.py` (v1–v4)   |   —     |   —     | confirms 0-magic shape | — |
| `inspect_dispatcher.py`         |   ✅    |   ✅    |   ✅    |   ✅    |
| `hcaptcha.hsw.HSWKeyFetcher`    |   —     |   —     |   —     |   ✅    |

The production extractor only commits to era (d). Eras (a)–(c) are
documented historically and required for back-testing claims about
key-rotation patterns across the archive but are NOT in the live
production path.

## Why hCaptcha keeps changing the dispatch shape

Each transition (a → b → c → d) makes static analysis harder by one
notch:

* **(a → b):** removed the per-role export names; you can no longer
  `grep` the exports for the role you want.
* **(b → c):** removed `call_indirect`; you can no longer read the
  element segment to enumerate roles.
* **(c → d):** added magic-number gates so the dispatcher's switch
  becomes deeper and the magics rotate per build.

Each transition required rewriting the production extractor. The
back-test harnesses in the repo root capture the structural
fingerprint of each era so a future (e) transition is detectable
quickly — when the era (d) extractor stops finding magics, look at
what the new dispatch shape is and add a new era row to the table
above.

## Cross-references

| Topic                                                  | See                                              |
| ------------------------------------------------------ | ------------------------------------------------ |
| Era (d) magic-table details                            | [08-hsw-dispatch-table.md](./08-hsw-dispatch-table.md) |
| Era (d) AES key extraction (current production)        | [07-wasm-patching.md](./07-wasm-patching.md)     |
| N-token AES master-key capture (sub-path d.1)          | [09-hsw-keys-derivation.md](./09-hsw-keys-derivation.md) |
| End-to-end HSW summary (6 keys + PoW + n-token)        | [12-hsw-complete-summary.md](./12-hsw-complete-summary.md) |
| WASM binary parsing primitives used by all eras        | [05-wasm-internals.md](./05-wasm-internals.md)   |
| Fixslice32 mask constants the era-detector keys off    | [06-fixslice32.md](./06-fixslice32.md)           |
