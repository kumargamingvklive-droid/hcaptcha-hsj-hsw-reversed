# Deobfuscation pipeline

Both bundles ship with the same obfuscation toolchain (looks like
javascript-obfuscator-style output). The deobf pipeline runs as a
Python wrapper (`deobf.py`) over a Node-based AST processor
(`deobf.js`). It applies twelve passes in sequence.

## Why a two-process pipeline

* Python side handles the I/O (download, beautify with `jsbeautifier`,
  invoke Node, write output).
* Node side does the AST work, because the only viable JS parser with
  full ES support is `acorn` + the matching code generator `astring`.
  Running these from Python would mean bundling them anyway.

## Common obfuscation techniques in the bundles

Before walking the passes, here's what's actually obfuscated:

1. **String-array encoding.** Every property name and string literal
   is replaced with a numeric index into a lazy-initialised table.
   The table is decoded once on first access via a custom base64
   alphabet (lowercase-first: `abcdef...xyzABC...XYZ0123456789+/=`).
2. **Lazy-init decoder.** A function like `er(index)` that returns
   the decoded string. Detected by its self-referential init marker:
   `void 0 === er.<6charrandomsuffix>` on first call.
3. **Numeric constant aliases.** `var rk = 391; ... er(rk)` instead
   of inlining `er(391)`.
4. **Decoder aliases.** `var f = er; ... f(391)` — multiple names
   for the same decoder.
5. **String-table rotation IIFE.** A small startup IIFE that loops
   `try { parseInt math ... break } catch { table.push(table.shift()) }`
   solely to permute the string table. After string resolution it's
   dead code.
6. **`!`-prefix on top-level IIFEs.** A minifier idiom to avoid
   `function statement` parsing.
7. **Sequence-expression flattening.** `(a = 1, b = 2, c = f(b))`
   instead of three statements.
8. **For-init sequences.** `for ((a = 1, b = 2); cond; up) body`
   instead of declaring before the loop.
9. **Bracketed property access.** `obj["foo"]` instead of `obj.foo`.
10. **Embedded base64 blobs.** Binary WASM bytecode is split into
    base64 chunks passed through `atob`, scattered through the JS.

## The twelve passes

### 1. Decoder detection

We scan the source for every function definition that contains the
self-marker pattern `void 0 === NAME.<6chars>`. Each such name is a
decoder. In practice there are 2 per bundle (the HSJ deobf has 2
overlapping decoders, HSW has 2 distinct ones like `mp` and `pi`).

### 2. Sandbox capture

To learn what each decoder returns for what index, we need to *run*
the source. We do this in a Node `vm` sandbox with `window`,
`document`, `navigator`, `location`, etc. stubbed as `loose()` proxies
that return themselves for every property — enough to let the IIFE
init code complete without throwing.

The trick: the IIFE often `return`s early at top level (e.g., returns
the public API). We use an AST walk to find the first `ReturnStatement`
at the IIFE body level and **inject an exposure patch BEFORE it**:

```js
try {
    globalThis.__deobf_patch_reached = 1;
    er(0); er(1); ... sn(0); sn(1); ...   // touch each decoder so it inits
    globalThis.__deobf_decoders = { er, sn };
} catch (e) { globalThis.__deobf_err = String(e); }
```

After the sandbox runs, `globalThis.__deobf_decoders` holds live JS
references to each decoder function.

### 3. Table build

For each decoder, we probe a wide index range (`-1000 .. 4000`) and
record `index → string`. Typical tables: HSW's `mp` decodes 681
strings, `pi` decodes 140.

### 4. String-decoder resolution (scope-aware, with const-prop)

Walk the AST. For every `CallExpression` whose callee resolves to a
known decoder (directly or via aliases), and whose argument is a
constant (literal number, or a `var rk = 391`-style numeric alias)
**evaluable at compile time**, replace the call with the literal
string.

Constant propagation matters because the obfuscation systematically
hides indices behind aliases:

```js
var f = er;
var rk = 391;
f(rk);              // before: opaque
                    // after:  "encrypt_req_data"
```

This pass runs scope-aware: a `var f = er` in an inner function does
not affect outer-scope `f` uses, and vice versa. ~1400 calls
replaced per HSW pass.

### 5. Binary blob extraction

Any call argument that is a base64 string ≥ 200 chars is decoded and
written to `<output>.blobs.bin` with an index entry in
`<output>.blobs.json`. The JS argument is replaced with a short
`__BLOB_N__` placeholder so the source stays readable. ~600 KB of
WASM bytecode comes out this way for `hsw.js`.

### 6. Bracket → dot member access

`obj["foo"]` becomes `obj.foo` when `"foo"` is a valid identifier and
not a JS reserved word. Runs as a separate post-order pass because
during the resolve pass the bracket arg was still a CallExpression
(the decoder call), not yet a Literal.

### 7. Dead-variable removal (scope-aware)

After string resolution, many declarations are dead:
* `var rk = 391;` whose only use was as the decoder argument we just
  inlined.
* `var f = er;` aliases whose uses have all been substituted.

We track refcount per scope and drop declarators with zero remaining
uses. Typically ~500 dead decls per bundle.

### 8. Parameter + local renaming

A heuristic readability boost: functions whose *all* declared
identifier names are ≤ 3 characters get their params and `var`-decls
renamed to sequential `a, b, c, …`. Nested-function shadowing is
handled correctly via a per-scope `inherited` map: an inner `var b`
removes `b` from the inherited rename map before recursing.

This is by far the heaviest pass (~12,000 renames per HSW pass) and
produces the largest readability win.

### 9. Sequence-expression flattening

`(a = 1, b = 2, c = f(b))` in statement context becomes three
separate statements. Also handles `return (a, b, c)` →
`a; b; return c;`, `throw (a, b, c)` analogously, and `if (x) (a, b)`
→ `if (x) { a; b; }`.

asm.js-style compiled output is dominated by these — 209 flattened in
HSW per pass, 142 in HSJ.

### 10. For-init sequence flattening

`for ((a = 1, b = 2); cond; up) body` becomes
`{ a = 1; b = 2; for (; cond; up) body }`. The wrapping
`BlockStatement` preserves scope semantics. ~94 per HSW pass.

### 11. Rotation-IIFE removal (AST-precise)

The string-table rotation IIFE is small and has a very specific
structure:
* A `!`-prefixed `CallExpression`
* Whose callee is a `FunctionExpression`
* Whose body is small (we cap at 5 top-level statements)
* And contains a `TryStatement` whose `catch` body has the literal
  pattern `id.push(id.shift())`

Match precision is critical: the *outer module IIFE* also starts with
`!(function(){…})` (it's bang-prefixed too) but it has hundreds of
statements. The 5-statement cap distinguishes them. Without that
guard, dropping the rotation IIFE would accidentally drop the entire
module body — which is exactly the bug we hit and fixed during
development.

### 12. Useless `!`-prefix removal

After pass 11, leftover `!(function(){…})()` statements (now without
the rotation pattern) get their `!` stripped — astring already emits
the parens explicitly so the bang serves no purpose.

## End-to-end numbers (HSW, current build)

```
[deobf] decoders: mp,pi
[deobf] mp: decoded 681 strings
[deobf] pi: decoded 140 strings
[deobf] decoder-calls replaced: 1451
[deobf] bracket->dot: 1000
[deobf] blobs extracted: 16 (596159 bytes total)
[deobf] dead decls removed: 489
[deobf] renamed locals: 11664
[deobf] sequences flattened: 209
[deobf] for-inits flattened: 94
[deobf] rotation-IIFEs dropped: 1
[deobf] useless !-prefix removed: 1
[deobf] wrote .\hsw_deobf.js
```

Output size: ~6,000 lines. Direct comparison vs the obfuscated
input:

```js
// Before (obfuscated, after beautify):
encrypt_req_data: function(Ng) {
    var fa = 361;
    try {
        var jC = t.dc(-16);
        t.vc(1913951152, 0, iO(Ng), 0, 0, 0, 0, jC);
        var jB = iX()[nE(fa)](jC + 0, !0);
        var mT = iX()[nE(361)](jC + 4, !0);
        if (iX()[nE(fa)](jC + 8, !0)) throw yy(mT);
        return yy(jB)
    } finally { t.dc(16) }
},

// After (deobfuscated):
encrypt_req_data: function (a) {
    try {
        var b = j12.qc(-16);
        j12.vc(1579470607, 0, 0, b, 0, a3(a), 0, 0);
        var c = p3().getInt32(b + 0, !0);
        var d = p3().getInt32(b + 4, !0);
        if (p3().getInt32(b + 8, !0)) throw b4(d);
        return b4(c);
    } finally {
        j12.qc(16);
    }
},
```

The structural pattern — dispatcher magic + stack alloc + struct read
— is now obvious. That's what `keyfetcher_hsw_keys.py` uses to
auto-extract the magic and export names.

## Limits — the asm.js / WASM compiled section

The lower half of `hsj.js` and the wbg shim of `hsw.js` are *compiled
output* — emscripten / wasm-bindgen / Rust emitted patterns like
`f[(a |= 0) >> 2]` and tight bit-manipulation chains. Renaming + flattening
makes individual statements readable but no further structural cleanup
is possible without disassembling back to source-level Rust. That's
a separate reverse-engineering project (and the WASM disassembler in
`wasm_disasm.py` is the start of it — see
[`05-wasm-internals.md`](./05-wasm-internals.md)).

Continue in [`04-key-extraction.md`](./04-key-extraction.md).
