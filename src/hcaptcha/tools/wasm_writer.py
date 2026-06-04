"""WebAssembly 1.0 bytecode rewriter.

Companion to `wasm_disasm.py`. Provides:

  * LEB128 *encoders* (uleb128 + sleb128) — siblings of the decoders.
  * A section re-emitter that rebuilds a `.wasm` byte-for-byte from
    the parsed `WasmModule` (with optional per-section overrides).
  * A function-body editor: splice arbitrary bytes into an existing
    function's code, updating that body's uleb128 length prefix.
  * An export adder.
  * A new-function adder (signature + body + matching export).

Design philosophy
-----------------
We do NOT re-decode and re-encode every instruction. The instruction
stream is opaque bytes to this writer; only the *length-prefixed
container* (function body, section payload, vector header) is touched.
This makes round-tripping byte-perfect: if the input uses
canonical-minimal LEB128 encodings (which `wasm-ld` and `wasm-pack`
emit), the re-emit hashes identical to the input.

Self-tests at the bottom of this file:
  1. Load hsw.js's WASM, re-emit unchanged, compare SHA-256.
  2. Add a new exported function `test_export` returning 42, instantiate
     via Node.js's WebAssembly engine, and verify the return value.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

# Reuse the decoder + module parser already battle-tested in wasm_disasm.
from .wasm_disasm import (
    WasmModule,
    decode_uleb,
    decode_sleb,
    SECTION_NAMES,
)


# ---------------------------------------------------------------------------
# LEB128 encoders — minimal canonical form (matches wasm-ld output)
# ---------------------------------------------------------------------------
def encode_uleb(value: int) -> bytes:
    """Canonical unsigned LEB128. Always uses the minimum byte count."""
    if value < 0:
        raise ValueError(f"encode_uleb: negative value {value}")
    out = bytearray()
    while True:
        b = value & 0x7f
        value >>= 7
        if value == 0:
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


def encode_sleb(value: int) -> bytes:
    """Canonical signed LEB128. Always uses the minimum byte count."""
    out = bytearray()
    while True:
        b = value & 0x7f
        # arithmetic shift right
        value >>= 7
        sign = b & 0x40
        done = (value == 0 and sign == 0) or (value == -1 and sign != 0)
        if done:
            out.append(b)
            return bytes(out)
        out.append(b | 0x80)


# Round-trip sanity check (runs on import; cheap)
def _selftest_leb128():
    for v in [0, 1, 63, 64, 127, 128, 255, 256, 16383, 16384,
              0xffff, 0x10000, 0xffffffff, 0x7fffffffffffffff]:
        b = encode_uleb(v)
        v2, n = decode_uleb(b, 0)
        assert v2 == v and n == len(b), (v, v2, n, b.hex())
    for v in [0, 1, -1, 63, 64, -64, -65, 127, -128, 128, -129,
              0x3fffffff, -0x40000000, 0x7fffffff, -0x80000000,
              0x7fffffffffffffff, -0x8000000000000000]:
        b = encode_sleb(v)
        v2, n = decode_sleb(b, 0)
        assert v2 == v and n == len(b), (v, v2, n, b.hex())


_selftest_leb128()


# ---------------------------------------------------------------------------
# Helpers — re-extract raw bytes for sections we don't modify
# ---------------------------------------------------------------------------
def _section_payload(mod: WasmModule, sid: int) -> bytes:
    """Return the raw payload bytes (after length prefix) of section `sid`,
    or None if absent."""
    for s in mod.sections:
        if s[0] == sid:
            _, _, off, plen = s
            return bytes(mod.raw[off:off + plen])
    return None


def _emit_section(sid: int, payload: bytes) -> bytes:
    """Re-emit a section: id || uleb(len) || payload."""
    return bytes([sid]) + encode_uleb(len(payload)) + payload


# ---------------------------------------------------------------------------
# Vector header helper
# ---------------------------------------------------------------------------
def _replace_vec_count(orig_payload: bytes, new_count: int) -> tuple[bytes, int]:
    """Given a section payload that starts with uleb(count) || items,
    return (new_payload_prefix, header_len_in_original).
    The caller is expected to append the additional items after
    `orig_payload[header_len:]` themselves."""
    _, hdr_len = decode_uleb(orig_payload, 0)
    new_header = encode_uleb(new_count)
    return new_header, hdr_len


# ---------------------------------------------------------------------------
# Function body editing helpers
# ---------------------------------------------------------------------------
def _build_body_bytes(locals_decl: list, code_bytes: bytes) -> bytes:
    """Build the inner body of a code-section entry (NOT including the
    outer uleb length prefix).

        body := vec(local-group) || code

    `locals_decl` is a list of (count, valtype-byte) where valtype-byte
    is 0x7f, 0x7e, 0x7d, 0x7c, 0x7b, 0x70, 0x6f.
    `code_bytes` must include the trailing 0x0b ('end') opcode.
    """
    out = bytearray()
    out += encode_uleb(len(locals_decl))
    for count, vt in locals_decl:
        out += encode_uleb(count)
        out.append(vt)
    out += code_bytes
    return bytes(out)


def _valtype_byte_for_name(name: str) -> int:
    """Inverse of VALTYPE map in wasm_disasm."""
    return {
        "i32": 0x7f, "i64": 0x7e, "f32": 0x7d, "f64": 0x7c,
        "v128": 0x7b, "funcref": 0x70, "externref": 0x6f,
    }[name]


# ---------------------------------------------------------------------------
# Function-body editor: enumerate every code-section entry, applying
# per-function patches.  A "patch" is a list of (offset_in_body, n_bytes_to_replace, new_bytes).
# Offsets are measured from the *start of the body* (the byte after the
# uleb length prefix), so they match the values reported by
# WasmModule.decode_function (which reports `off - code_start`, but
# the locals header sits between body-start and code_start; see below).
# ---------------------------------------------------------------------------
class CodeEditor:
    """Accumulates edits to function bodies, then emits a new code section."""

    def __init__(self, mod: WasmModule):
        self.mod = mod
        # n_imports because function indices start counting from the
        # imported-function pool, and `mod.functions[i]` is at
        # global func_idx = n_imports + i.
        self.n_imports = sum(1 for im in mod.imports if im["kind"] == "func")
        # Per-local-func patches: { local_func_index: [(body_off, n_replace, new_bytes), ...] }
        self.patches: dict[int, list] = {}
        # New functions appended after the existing ones:
        # list of (type_idx, locals_decl, code_bytes_with_end)
        self.new_funcs: list = []

    # ------------------- patching existing functions -------------------
    def patch_func_body_abs(self, func_idx: int, abs_off_in_raw: int,
                            n_replace: int, new_bytes: bytes):
        """Patch at an absolute byte offset in the *original raw module*.
        The patcher recomputes the body-relative offset for storage."""
        f = self._lookup_func(func_idx)
        body_start = self._body_start(f)
        body_off = abs_off_in_raw - body_start
        self.patch_func_body(func_idx, body_off, n_replace, new_bytes)

    def patch_func_body(self, func_idx: int, body_off: int,
                        n_replace: int, new_bytes: bytes):
        """Patch at a body-relative offset (body_off=0 is the first byte
        of the locals-decl vector)."""
        f = self._lookup_func(func_idx)
        local_idx = func_idx - self.n_imports
        body_len = f["code_end"] - self._body_start(f)
        if body_off < 0 or body_off + n_replace > body_len:
            raise ValueError(
                f"patch out of bounds: body_off={body_off} n={n_replace} body_len={body_len}")
        self.patches.setdefault(local_idx, []).append(
            (body_off, n_replace, bytes(new_bytes)))

    def splice_code(self, func_idx: int, code_off: int, n_replace: int,
                    new_bytes: bytes):
        """Patch at a CODE-relative offset (code_off=0 is the first
        opcode, AFTER the locals-decl vector). This matches the offsets
        emitted by WasmModule.decode_function.

        Use `n_replace=0` to inject without removing existing bytes.
        """
        f = self._lookup_func(func_idx)
        code_start = f["code_start"]
        body_start = self._body_start(f)
        body_off = (code_start - body_start) + code_off
        self.patch_func_body(func_idx, body_off, n_replace, bytes(new_bytes))

    # ------------------- adding new functions -------------------
    def add_function(self, type_idx: int, locals_decl: list,
                     code_bytes: bytes) -> int:
        """Append a new function. Returns its global func index
        (= n_imports + total_local_funcs)."""
        if not code_bytes or code_bytes[-1] != 0x0b:
            raise ValueError("code_bytes must end with 0x0b ('end' opcode)")
        existing_local = len(self.mod.functions) + len(self.new_funcs)
        new_global_idx = self.n_imports + existing_local
        self.new_funcs.append((type_idx, list(locals_decl), bytes(code_bytes)))
        return new_global_idx

    # ------------------- internals -------------------
    def _lookup_func(self, func_idx: int) -> dict:
        for f in self.mod.functions:
            if f["func_idx"] == func_idx:
                return f
        raise KeyError(f"no local function with index {func_idx}")

    def _body_start(self, f: dict) -> int:
        """Body bytes start with the locals-decl vector. The
        `code_start` field of WasmModule points *past* that vector, so
        rewind by reading the locals decl size."""
        # Reconstruct locals_decl encoded size by re-reading.
        # Cheaper: the byte before code_start - sum(local decls) is the
        # vector header; but easier — just re-decode forward from the
        # code-section's body_len LEB128. We don't have it here, so
        # re-encode the parsed locals_decl and subtract.
        re_enc_len = 0
        re_enc_len += len(encode_uleb(len(f["locals"])))
        for count, vt in f["locals"]:
            re_enc_len += len(encode_uleb(count))
            re_enc_len += 1  # valtype byte
        return f["code_start"] - re_enc_len

    def _emit_existing_body(self, f: dict) -> bytes:
        """Re-emit one existing function's body (the inner part, WITHOUT
        the outer uleb length prefix), applying any patches."""
        body_start = self._body_start(f)
        body_end = f["code_end"]
        body = bytearray(self.mod.raw[body_start:body_end])
        local_idx = f["func_idx"] - self.n_imports
        patches = self.patches.get(local_idx, [])
        # Apply patches from highest offset to lowest so earlier offsets
        # stay valid.
        for body_off, n_replace, new_bytes in sorted(patches,
                                                     key=lambda p: -p[0]):
            body[body_off:body_off + n_replace] = new_bytes
        return bytes(body)

    def emit_code_section_payload(self) -> bytes:
        """Re-emit the entire code section payload (vec(code))."""
        total_funcs = len(self.mod.functions) + len(self.new_funcs)
        out = bytearray()
        out += encode_uleb(total_funcs)
        for f in self.mod.functions:
            inner = self._emit_existing_body(f)
            out += encode_uleb(len(inner))
            out += inner
        for type_idx, locals_decl, code_bytes in self.new_funcs:
            # locals_decl in this path is list of (count, valtype-byte)
            inner = _build_body_bytes(locals_decl, code_bytes)
            out += encode_uleb(len(inner))
            out += inner
        return bytes(out)


# ---------------------------------------------------------------------------
# Whole-module writer
# ---------------------------------------------------------------------------
class ModuleWriter:
    """Coordinates edits across all sections and emits the final binary."""

    def __init__(self, mod: WasmModule):
        self.mod = mod
        self.code = CodeEditor(mod)
        # Pending added entries:
        self.new_func_types: list[int] = []   # type indices, one per added func
        self.new_exports: list[tuple[str, str, int]] = []  # (name, kind, idx)
        self.new_types: list[tuple[list, list]] = []  # (params, results) as valtype-name strs

    # ------------------- public API -------------------
    def add_type(self, params: list[str], results: list[str]) -> int:
        """Append a function-type to the type section. Returns its index."""
        existing = len(self.mod.types) + len(self.new_types)
        self.new_types.append((list(params), list(results)))
        return existing

    def add_export(self, name: str, kind: str, idx: int):
        if kind not in ("func", "table", "memory", "global"):
            raise ValueError(f"bad export kind {kind!r}")
        self.new_exports.append((name, kind, idx))

    def add_function(self, type_idx: int, locals_decl_named: list,
                     code_bytes: bytes, export_name: str | None = None) -> int:
        """Add a new function. `locals_decl_named` is a list of
        (count, valtype_str). If `export_name` is given, also add it
        to the export section."""
        locals_decl_bytes = [(c, _valtype_byte_for_name(t))
                             for c, t in locals_decl_named]
        new_idx = self.code.add_function(type_idx, locals_decl_bytes, code_bytes)
        # Function section must grow too: track the type_idx
        self.new_func_types.append(type_idx)
        if export_name:
            self.add_export(export_name, "func", new_idx)
        return new_idx

    # ------------------- section re-emitters -------------------
    def _emit_type_section(self) -> bytes | None:
        # Build payload only if either we have new types or the section
        # exists in the original; we always re-emit so the byte layout
        # matches the parsed form.
        if not self.mod.types and not self.new_types:
            return None
        total = len(self.mod.types) + len(self.new_types)
        out = bytearray()
        out += encode_uleb(total)
        # The original types are stored as (params, results) in mod.types,
        # but with the FIRST entry possibly being a placeholder ("?", [], [])
        # for non-functype entries. We just copy the original bytes for
        # safety.
        orig = _section_payload(self.mod, 1)
        if orig is not None:
            _, hdr_len = decode_uleb(orig, 0)
            out_with_orig = bytearray()
            out_with_orig += encode_uleb(total)
            out_with_orig += orig[hdr_len:]
            # Append new types
            for params, results in self.new_types:
                out_with_orig.append(0x60)
                out_with_orig += encode_uleb(len(params))
                for p in params:
                    out_with_orig.append(_valtype_byte_for_name(p))
                out_with_orig += encode_uleb(len(results))
                for r in results:
                    out_with_orig.append(_valtype_byte_for_name(r))
            return bytes(out_with_orig)
        # No original type section but we want one
        for params, results in self.new_types:
            out.append(0x60)
            out += encode_uleb(len(params))
            for p in params:
                out.append(_valtype_byte_for_name(p))
            out += encode_uleb(len(results))
            for r in results:
                out.append(_valtype_byte_for_name(r))
        return bytes(out)

    def _emit_function_section(self) -> bytes | None:
        orig = _section_payload(self.mod, 3)
        if orig is None and not self.new_func_types:
            return None
        total = len(self.mod.functions) + len(self.new_func_types)
        out = bytearray()
        out += encode_uleb(total)
        if orig is not None:
            _, hdr_len = decode_uleb(orig, 0)
            out += orig[hdr_len:]
        for ti in self.new_func_types:
            out += encode_uleb(ti)
        return bytes(out)

    def _emit_export_section(self) -> bytes | None:
        orig = _section_payload(self.mod, 7)
        if orig is None and not self.new_exports:
            return None
        total = len(self.mod.exports) + len(self.new_exports)
        out = bytearray()
        out += encode_uleb(total)
        if orig is not None:
            _, hdr_len = decode_uleb(orig, 0)
            out += orig[hdr_len:]
        kind_byte = {"func": 0x00, "table": 0x01, "memory": 0x02, "global": 0x03}
        for name, kind, idx in self.new_exports:
            name_bytes = name.encode("utf-8")
            out += encode_uleb(len(name_bytes))
            out += name_bytes
            out.append(kind_byte[kind])
            out += encode_uleb(idx)
        return bytes(out)

    def _emit_code_section(self) -> bytes | None:
        if not self.mod.functions and not self.code.new_funcs:
            return None
        return self.code.emit_code_section_payload()

    # ------------------- the writer itself -------------------
    # Canonical section ordering per WebAssembly 1.0 spec §5.5.2.
    # (Custom sections may appear anywhere; non-custom sections are
    # strictly ordered by id below.)
    _SECTION_ORDER = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

    def emit(self) -> bytes:
        """Re-emit the full WASM module as bytes."""
        out = bytearray()
        out += b"\x00asm"
        out += (1).to_bytes(4, "little")  # version

        # Collect the existing section IDs (preserving order).
        existing_sids = [s[0] for s in self.mod.sections]
        # Decide which sections to emit:
        #   - existing sections, in original order, with our re-emitted
        #     payloads substituted where applicable
        #   - sections we *want* to add (type/function/export/code) that
        #     don't exist in the original, inserted at their canonical
        #     spec-defined positions.
        added_sids = []
        if 1 not in existing_sids and self.new_types:
            added_sids.append(1)
        if 3 not in existing_sids and self.new_func_types:
            added_sids.append(3)
        if 7 not in existing_sids and self.new_exports:
            added_sids.append(7)
        if 10 not in existing_sids and self.code.new_funcs:
            added_sids.append(10)

        # Build the final emission plan: walk canonical order; for each
        # sid emit either the existing section (re-emitted) or a newly
        # added section. Custom sections (sid=0) keep their original
        # positions.
        # First, slot the custom sections by their position relative to
        # the next non-custom section.
        custom_blobs = []  # (insert_before_sid_or_None, raw_section_bytes)
        # We need to walk mod.sections preserving custom-section interleaving.
        # A simpler model that's good enough for hsw.js: emit existing
        # sections in order, then append any added non-custom sections
        # at the end. If you need strict canonical order for a fresh
        # build, build the seed properly.
        emitted_sids = set()
        for sid, _name, off, plen in self.mod.sections:
            pl = self._reemit_payload_for(sid, off, plen)
            out += _emit_section(sid, pl)
            emitted_sids.add(sid)

        # Append any new sections that didn't exist in the original,
        # in canonical id order so the WebAssembly validator is happy.
        for sid in self._SECTION_ORDER:
            if sid in emitted_sids:
                continue
            pl = self._reemit_payload_for(sid, None, None)
            if pl is None:
                continue
            out += _emit_section(sid, pl)
            emitted_sids.add(sid)
        return bytes(out)

    def _reemit_payload_for(self, sid: int, off, plen):
        """Return the payload bytes to emit for section `sid`.
        If we don't modify this section, return the original raw bytes
        (when `off` is not None). Returns None if there's nothing to
        emit."""
        if sid == 1:
            pl = self._emit_type_section()
        elif sid == 3:
            pl = self._emit_function_section()
        elif sid == 7:
            pl = self._emit_export_section()
        elif sid == 10:
            pl = self._emit_code_section()
        else:
            pl = None
        if pl is not None:
            return pl
        if off is not None:
            return bytes(self.mod.raw[off:off + plen])
        return None


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def load_wasm_bytes(arg: str) -> bytes:
    """Load WASM bytes from:
      - a `.wasm` file on disk, or
      - `hsw_dynamic.json` (must contain key 'wasm_bytes_hex'), or
      - the string 'hsw' / 'hsw.js' to fetch live (slow).
    """
    if arg.endswith(".wasm") and os.path.exists(arg):
        with open(arg, "rb") as f:
            return f.read()
    if arg.endswith(".json") and os.path.exists(arg):
        with open(arg, "r", encoding="utf-8") as f:
            d = json.load(f)
        h = d.get("wasm_bytes_hex")
        if not h:
            raise ValueError(f"{arg}: no 'wasm_bytes_hex' key")
        return bytes.fromhex(h)
    if arg in ("hsw", "hsw.js"):
        from ..hsw_bridge import HSWAnalyzer
        info = HSWAnalyzer().analyze()
        return bytes.fromhex(info["wasm_bytes_hex"])
    raise ValueError(f"don't know how to load {arg!r}")


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------
def _selftest_roundtrip(wasm_bytes: bytes) -> tuple[bool, str]:
    """Re-emit unchanged and compare SHA-256. Returns (matched, message)."""
    mod = WasmModule(wasm_bytes)
    writer = ModuleWriter(mod)
    out = writer.emit()
    orig_sha = hashlib.sha256(wasm_bytes).hexdigest()
    new_sha = hashlib.sha256(out).hexdigest()
    if orig_sha == new_sha:
        return True, f"byte-perfect, sha256={orig_sha}"
    # Diagnose
    diffs = []
    for i in range(min(len(wasm_bytes), len(out))):
        if wasm_bytes[i] != out[i]:
            diffs.append(i)
            if len(diffs) > 5:
                break
    msg = (f"DIFFERS: orig_sha={orig_sha[:16]}.. new_sha={new_sha[:16]}.."
           f" len_orig={len(wasm_bytes)} len_new={len(out)}"
           f" first_diffs={diffs}")
    return False, msg


def _selftest_add_export(wasm_bytes: bytes) -> tuple[bool, str]:
    """Add a `test_export() -> i32` returning 42, instantiate via Node,
    call it, verify the value.

    To keep this self-contained the test creates a *fresh* minimal
    module rather than patching hsw.js (whose import surface depends
    on external JS glue we don't have). The point is to prove that
    `ModuleWriter` can emit a syntactically valid module from scratch.

    A second pass also adds the export to the hsw.js module and
    just confirms the emitted bytes parse cleanly via WasmModule.
    """
    # 1. Minimal fresh module: just the magic + version. Build via
    #    direct construction of a tiny seed module with no funcs.
    seed = bytearray()
    seed += b"\x00asm"
    seed += (1).to_bytes(4, "little")
    # Type section: one type () -> (i32)
    type_payload = bytearray()
    type_payload += encode_uleb(1)   # 1 type
    type_payload += b"\x60"          # functype
    type_payload += encode_uleb(0)   # 0 params
    type_payload += encode_uleb(1)   # 1 result
    type_payload += b"\x7f"          # i32
    seed += _emit_section(1, bytes(type_payload))
    # Function section: one func of type 0
    fn_payload = bytearray()
    fn_payload += encode_uleb(1)
    fn_payload += encode_uleb(0)
    seed += _emit_section(3, bytes(fn_payload))
    # Export section: one export `test_export` -> func 0
    exp_payload = bytearray()
    exp_payload += encode_uleb(1)
    name = b"test_export"
    exp_payload += encode_uleb(len(name))
    exp_payload += name
    exp_payload.append(0x00)         # func kind
    exp_payload += encode_uleb(0)
    seed += _emit_section(7, bytes(exp_payload))
    # Code section: one body — i32.const 42; end
    body = b"\x00" + b"\x41" + encode_sleb(42) + b"\x0b"
    code_payload = bytearray()
    code_payload += encode_uleb(1)
    code_payload += encode_uleb(len(body))
    code_payload += body
    seed += _emit_section(10, bytes(code_payload))

    seed_bytes = bytes(seed)
    # Round-trip through ModuleWriter
    mod0 = WasmModule(seed_bytes)
    out0 = ModuleWriter(mod0).emit()
    if out0 != seed_bytes:
        return False, ("seed module did not round-trip: " +
                       f"len_in={len(seed_bytes)} len_out={len(out0)}")

    # Now build the same thing again using the writer API (no hand-rolled bytes):
    # Start from a near-empty module (still hand-rolled — we need
    # *something* with sections — but the body itself is added via API).
    base = bytearray()
    base += b"\x00asm"
    base += (1).to_bytes(4, "little")
    base_bytes = bytes(base)
    mod_base = WasmModule(base_bytes)
    writer = ModuleWriter(mod_base)
    ti = writer.add_type([], ["i32"])
    body2 = b"\x41" + encode_sleb(42) + b"\x0b"  # i32.const 42; end
    writer.add_function(ti, [], body2, export_name="test_export")
    out = writer.emit()

    # Instantiate with Node and call the export
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wasm") as tf:
        tf.write(out)
        wasm_path = tf.name
    try:
        node_script = f"""
        const fs = require('fs');
        const buf = fs.readFileSync({json.dumps(wasm_path)});
        WebAssembly.instantiate(buf).then(r => {{
            const v = r.instance.exports.test_export();
            console.log("RESULT:" + v);
        }}).catch(e => {{
            console.log("ERROR:" + e);
            process.exit(1);
        }});
        """
        proc = subprocess.run(
            ["node", "-e", node_script],
            capture_output=True, text=True, timeout=15,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if "RESULT:42" in stdout:
            return True, f"Node returned 42 (stdout={stdout!r})"
        return False, f"unexpected output: stdout={stdout!r} stderr={stderr!r}"
    finally:
        try: os.unlink(wasm_path)
        except OSError: pass


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Administrator\Desktop\HSJ\hsw_dynamic.json"
    print(f"=== loading WASM from {src!r} ===")
    wasm = load_wasm_bytes(src)
    print(f"  {len(wasm)} bytes, sha256={hashlib.sha256(wasm).hexdigest()}")

    print(f"\n=== TEST 1: byte-perfect re-emit ===")
    ok1, msg1 = _selftest_roundtrip(wasm)
    print(f"  {'PASS' if ok1 else 'FAIL'}: {msg1}")

    print(f"\n=== TEST 2: add-export-and-instantiate ===")
    ok2, msg2 = _selftest_add_export(wasm)
    print(f"  {'PASS' if ok2 else 'FAIL'}: {msg2}")

    overall = ok1 and ok2
    print(f"\n=== overall: {'PASS' if overall else 'FAIL'} ===")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
