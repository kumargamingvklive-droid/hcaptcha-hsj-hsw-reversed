"""WebAssembly 1.0 + post-MVP-common-extensions disassembler.

Parses a `.wasm` binary into per-section structures, decodes function
bodies (locals + instruction stream), and emits a readable summary
suitable for:

  * Finding the AES key-expansion function by opcode-profile heuristic.
  * Locating instruction sequences that load from rodata addresses
    (i32.load offsets that land in the data section range).
  * Identifying fixslice32 bit-permutation patterns (heavy XOR + ROL
    + AND with the canonical masks 0x55555555 / 0x33333333 / etc).

The disassembler is INTENTIONALLY non-validating — we don't care if
the module verifies, we just want to read it. We tolerate
post-MVP opcodes (saturating-conversion 0xfc-prefixed, SIMD 0xfd, atomic
0xfe) by skipping them instead of failing.

Usage:
    python wasm_disasm.py hsw                     # download + analyze
    python wasm_disasm.py path/to/file.wasm
    python wasm_disasm.py hsw --funcs 12,42,591   # only those funcs
    python wasm_disasm.py hsw --find-fixslice     # heuristic AES finder
    python wasm_disasm.py hsw --find-key-load     # find rodata 32-byte loads
"""
import argparse
import json
import os
import sys
from collections import Counter


# ---------------------------------------------------------------------------
# LEB128 decoder — varuint + varsint
# ---------------------------------------------------------------------------
def decode_uleb(buf, off):
    """Returns (value, bytes_consumed)."""
    val = 0
    shift = 0
    i = 0
    while True:
        b = buf[off + i]
        val |= (b & 0x7f) << shift
        i += 1
        if not (b & 0x80):
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("uleb128 overflow")


def decode_sleb(buf, off):
    val = 0
    shift = 0
    i = 0
    while True:
        b = buf[off + i]
        val |= (b & 0x7f) << shift
        i += 1
        if not (b & 0x80):
            if (b & 0x40):
                val -= (1 << (shift + 7))
            return val, i
        shift += 7
        if shift > 70:
            raise ValueError("sleb128 overflow")


# ---------------------------------------------------------------------------
# Type alphabet
# ---------------------------------------------------------------------------
VALTYPE = {
    0x7f: "i32", 0x7e: "i64", 0x7d: "f32", 0x7c: "f64",
    0x7b: "v128", 0x70: "funcref", 0x6f: "externref",
}


def decode_valtype(buf, off):
    return VALTYPE.get(buf[off], f"<vt:{buf[off]:#x}>"), 1


def decode_blocktype(buf, off):
    """Block types: empty (0x40), single valtype, or signed-LEB type index."""
    b = buf[off]
    if b == 0x40:
        return ("empty",), 1
    if b in VALTYPE:
        return (VALTYPE[b],), 1
    # signed LEB referring to a type index
    val, n = decode_sleb(buf, off)
    return ("type", val), n


# ---------------------------------------------------------------------------
# Opcode table — name, operand descriptor
#
# Descriptors:
#   "u32"      → single uleb operand
#   "s32"      → single sleb operand (i32.const, i64.const, etc.)
#   "memarg"   → align (uleb) + offset (uleb)
#   "block"    → blocktype (varied)
#   "table"    → vector of u32 then default u32 (br_table)
#   "select_t" → vector of valtypes
#   "two_u32"  → two ulebs (memory.init etc.)
#   None       → no operand
# ---------------------------------------------------------------------------
OPCODES = {
    # control flow
    0x00: ("unreachable",   None),
    0x01: ("nop",           None),
    0x02: ("block",         "block"),
    0x03: ("loop",          "block"),
    0x04: ("if",            "block"),
    0x05: ("else",          None),
    0x0b: ("end",           None),
    0x0c: ("br",            "u32"),
    0x0d: ("br_if",         "u32"),
    0x0e: ("br_table",      "table"),
    0x0f: ("return",        None),
    0x10: ("call",          "u32"),
    0x11: ("call_indirect", "two_u32"),
    0x12: ("return_call",   "u32"),
    0x13: ("return_call_indirect", "two_u32"),
    # parametric
    0x1a: ("drop",          None),
    0x1b: ("select",        None),
    0x1c: ("select_t",      "select_t"),
    # variable
    0x20: ("local.get",     "u32"),
    0x21: ("local.set",     "u32"),
    0x22: ("local.tee",     "u32"),
    0x23: ("global.get",    "u32"),
    0x24: ("global.set",    "u32"),
    # table
    0x25: ("table.get",     "u32"),
    0x26: ("table.set",     "u32"),
    # memory loads / stores
    0x28: ("i32.load",      "memarg"),
    0x29: ("i64.load",      "memarg"),
    0x2a: ("f32.load",      "memarg"),
    0x2b: ("f64.load",      "memarg"),
    0x2c: ("i32.load8_s",   "memarg"),
    0x2d: ("i32.load8_u",   "memarg"),
    0x2e: ("i32.load16_s",  "memarg"),
    0x2f: ("i32.load16_u",  "memarg"),
    0x30: ("i64.load8_s",   "memarg"),
    0x31: ("i64.load8_u",   "memarg"),
    0x32: ("i64.load16_s",  "memarg"),
    0x33: ("i64.load16_u",  "memarg"),
    0x34: ("i64.load32_s",  "memarg"),
    0x35: ("i64.load32_u",  "memarg"),
    0x36: ("i32.store",     "memarg"),
    0x37: ("i64.store",     "memarg"),
    0x38: ("f32.store",     "memarg"),
    0x39: ("f64.store",     "memarg"),
    0x3a: ("i32.store8",    "memarg"),
    0x3b: ("i32.store16",   "memarg"),
    0x3c: ("i64.store8",    "memarg"),
    0x3d: ("i64.store16",   "memarg"),
    0x3e: ("i64.store32",   "memarg"),
    0x3f: ("memory.size",   "u32"),
    0x40: ("memory.grow",   "u32"),
    # constants
    0x41: ("i32.const",     "s32"),
    0x42: ("i64.const",     "s64"),
    0x43: ("f32.const",     "f32"),
    0x44: ("f64.const",     "f64"),
    # i32 numeric
    0x45: ("i32.eqz",       None), 0x46: ("i32.eq",  None), 0x47: ("i32.ne",  None),
    0x48: ("i32.lt_s",      None), 0x49: ("i32.lt_u",None), 0x4a: ("i32.gt_s",None),
    0x4b: ("i32.gt_u",      None), 0x4c: ("i32.le_s",None), 0x4d: ("i32.le_u",None),
    0x4e: ("i32.ge_s",      None), 0x4f: ("i32.ge_u",None),
    # i64 numeric
    0x50: ("i64.eqz",       None), 0x51: ("i64.eq",  None), 0x52: ("i64.ne",  None),
    0x53: ("i64.lt_s",      None), 0x54: ("i64.lt_u",None), 0x55: ("i64.gt_s",None),
    0x56: ("i64.gt_u",      None), 0x57: ("i64.le_s",None), 0x58: ("i64.le_u",None),
    0x59: ("i64.ge_s",      None), 0x5a: ("i64.ge_u",None),
    # f32 / f64 compares
    0x5b: ("f32.eq",        None), 0x5c: ("f32.ne",  None), 0x5d: ("f32.lt",  None),
    0x5e: ("f32.gt",        None), 0x5f: ("f32.le",  None), 0x60: ("f32.ge",  None),
    0x61: ("f64.eq",        None), 0x62: ("f64.ne",  None), 0x63: ("f64.lt",  None),
    0x64: ("f64.gt",        None), 0x65: ("f64.le",  None), 0x66: ("f64.ge",  None),
    # i32 binops
    0x67: ("i32.clz",       None), 0x68: ("i32.ctz",  None), 0x69: ("i32.popcnt",None),
    0x6a: ("i32.add",       None), 0x6b: ("i32.sub",  None), 0x6c: ("i32.mul",   None),
    0x6d: ("i32.div_s",     None), 0x6e: ("i32.div_u",None), 0x6f: ("i32.rem_s", None),
    0x70: ("i32.rem_u",     None), 0x71: ("i32.and",  None), 0x72: ("i32.or",    None),
    0x73: ("i32.xor",       None), 0x74: ("i32.shl",  None), 0x75: ("i32.shr_s", None),
    0x76: ("i32.shr_u",     None), 0x77: ("i32.rotl", None), 0x78: ("i32.rotr",  None),
    # i64 binops
    0x79: ("i64.clz",       None), 0x7a: ("i64.ctz",  None), 0x7b: ("i64.popcnt",None),
    0x7c: ("i64.add",       None), 0x7d: ("i64.sub",  None), 0x7e: ("i64.mul",   None),
    0x7f: ("i64.div_s",     None), 0x80: ("i64.div_u",None), 0x81: ("i64.rem_s", None),
    0x82: ("i64.rem_u",     None), 0x83: ("i64.and",  None), 0x84: ("i64.or",    None),
    0x85: ("i64.xor",       None), 0x86: ("i64.shl",  None), 0x87: ("i64.shr_s", None),
    0x88: ("i64.shr_u",     None), 0x89: ("i64.rotl", None), 0x8a: ("i64.rotr",  None),
    # f32 / f64 numeric (we don't need details for AES finding, but list them)
    0x8b: ("f32.abs", None), 0x8c: ("f32.neg", None), 0x8d: ("f32.ceil", None),
    0x8e: ("f32.floor", None), 0x8f: ("f32.trunc", None), 0x90: ("f32.nearest", None),
    0x91: ("f32.sqrt", None), 0x92: ("f32.add", None), 0x93: ("f32.sub", None),
    0x94: ("f32.mul", None), 0x95: ("f32.div", None), 0x96: ("f32.min", None),
    0x97: ("f32.max", None), 0x98: ("f32.copysign", None),
    0x99: ("f64.abs", None), 0x9a: ("f64.neg", None), 0x9b: ("f64.ceil", None),
    0x9c: ("f64.floor", None), 0x9d: ("f64.trunc", None), 0x9e: ("f64.nearest", None),
    0x9f: ("f64.sqrt", None), 0xa0: ("f64.add", None), 0xa1: ("f64.sub", None),
    0xa2: ("f64.mul", None), 0xa3: ("f64.div", None), 0xa4: ("f64.min", None),
    0xa5: ("f64.max", None), 0xa6: ("f64.copysign", None),
    # type conversions
    0xa7: ("i32.wrap_i64", None), 0xa8: ("i32.trunc_f32_s", None),
    0xa9: ("i32.trunc_f32_u", None), 0xaa: ("i32.trunc_f64_s", None),
    0xab: ("i32.trunc_f64_u", None), 0xac: ("i64.extend_i32_s", None),
    0xad: ("i64.extend_i32_u", None), 0xae: ("i64.trunc_f32_s", None),
    0xaf: ("i64.trunc_f32_u", None), 0xb0: ("i64.trunc_f64_s", None),
    0xb1: ("i64.trunc_f64_u", None), 0xb2: ("f32.convert_i32_s", None),
    0xb3: ("f32.convert_i32_u", None), 0xb4: ("f32.convert_i64_s", None),
    0xb5: ("f32.convert_i64_u", None), 0xb6: ("f32.demote_f64", None),
    0xb7: ("f64.convert_i32_s", None), 0xb8: ("f64.convert_i32_u", None),
    0xb9: ("f64.convert_i64_s", None), 0xba: ("f64.convert_i64_u", None),
    0xbb: ("f64.promote_f32", None),
    0xbc: ("i32.reinterpret_f32", None), 0xbd: ("i64.reinterpret_f64", None),
    0xbe: ("f32.reinterpret_i32", None), 0xbf: ("f64.reinterpret_i64", None),
    # sign extensions
    0xc0: ("i32.extend8_s", None), 0xc1: ("i32.extend16_s", None),
    0xc2: ("i64.extend8_s", None), 0xc3: ("i64.extend16_s", None),
    0xc4: ("i64.extend32_s", None),
    # reference instructions
    0xd0: ("ref.null", "u32"), 0xd1: ("ref.is_null", None), 0xd2: ("ref.func", "u32"),
}


# ---------------------------------------------------------------------------
# Instruction parser
# ---------------------------------------------------------------------------
def parse_instruction(buf, off):
    """Decode one instruction. Returns (name, operands, bytes_consumed)."""
    opc = buf[off]
    if opc == 0xfc:  # saturating-conversion + bulk memory + table extension
        sub, n = decode_uleb(buf, off + 1)
        op_len = 1 + n
        name = f"0xfc.{sub}"
        operands = []
        # subset of common 0xfc instructions
        if sub == 8:  # memory.init
            mem_data, m1 = decode_uleb(buf, off + op_len); op_len += m1
            mem_idx,  m2 = decode_uleb(buf, off + op_len); op_len += m2
            name = "memory.init"; operands = [mem_data, mem_idx]
        elif sub == 9:
            data_idx, m1 = decode_uleb(buf, off + op_len); op_len += m1
            name = "data.drop"; operands = [data_idx]
        elif sub == 10:  # memory.copy
            d, m1 = decode_uleb(buf, off + op_len); op_len += m1
            s, m2 = decode_uleb(buf, off + op_len); op_len += m2
            name = "memory.copy"; operands = [d, s]
        elif sub == 11:  # memory.fill
            d, m1 = decode_uleb(buf, off + op_len); op_len += m1
            name = "memory.fill"; operands = [d]
        else:
            # Unknown 0xfc.NN — skip just the prefix bytes; we may
            # misalign but for AES finding it doesn't matter.
            pass
        return (name, operands, op_len)
    if opc in (0xfd, 0xfe):  # SIMD or threads — try to skip safely
        sub, n = decode_uleb(buf, off + 1)
        name = f"0x{opc:02x}.{sub}"
        # Most SIMD opcodes have NO immediates; memarg ones do.
        # Just emit the prefix and advance; will likely misalign if
        # there are immediates. Acceptable for opcode-counting.
        return (name, [sub], 1 + n)

    if opc not in OPCODES:
        return (f"<unk:0x{opc:02x}>", [], 1)
    name, desc = OPCODES[opc]
    if desc is None:
        return (name, [], 1)
    if desc == "u32":
        v, n = decode_uleb(buf, off + 1); return (name, [v], 1 + n)
    if desc == "s32":
        v, n = decode_sleb(buf, off + 1); return (name, [v], 1 + n)
    if desc == "s64":
        v, n = decode_sleb(buf, off + 1); return (name, [v], 1 + n)
    if desc == "f32":
        return (name, [int.from_bytes(buf[off+1:off+5], 'little')], 5)
    if desc == "f64":
        return (name, [int.from_bytes(buf[off+1:off+9], 'little')], 9)
    if desc == "memarg":
        a, na = decode_uleb(buf, off + 1)
        o, no = decode_uleb(buf, off + 1 + na)
        return (name, [a, o], 1 + na + no)
    if desc == "block":
        bt, n = decode_blocktype(buf, off + 1)
        return (name, [bt], 1 + n)
    if desc == "table":
        count, n = decode_uleb(buf, off + 1)
        cur = off + 1 + n
        labels = []
        for _ in range(count):
            v, m = decode_uleb(buf, cur); labels.append(v); cur += m
        default, m = decode_uleb(buf, cur); cur += m
        return (name, [labels, default], cur - off)
    if desc == "two_u32":
        a, na = decode_uleb(buf, off + 1)
        b, nb = decode_uleb(buf, off + 1 + na)
        return (name, [a, b], 1 + na + nb)
    if desc == "select_t":
        count, n = decode_uleb(buf, off + 1)
        cur = off + 1 + n
        types = []
        for _ in range(count):
            t, m = decode_valtype(buf, cur); types.append(t); cur += m
        return (name, [types], cur - off)
    return (name, [], 1)


# ---------------------------------------------------------------------------
# Section walker
# ---------------------------------------------------------------------------
SECTION_NAMES = {
    0: "custom", 1: "type", 2: "import", 3: "function", 4: "table",
    5: "memory", 6: "global", 7: "export", 8: "start", 9: "element",
    10: "code", 11: "data", 12: "datacount",
}


class WasmModule:
    def __init__(self, raw: bytes):
        if raw[:4] != b"\x00asm":
            raise ValueError("not a WebAssembly binary")
        self.raw = raw
        self.sections = []   # (id, name, start_off, payload_off, payload_len)
        self.types = []
        self.imports = []
        self.func_type_indices = []
        self.exports = []
        self.functions = []  # parsed func bodies
        self.data_segments = []
        self._parse_sections()
        self._parse_types()
        self._parse_imports()
        self._parse_function_section()
        self.globals = []
        self._parse_globals()
        self._parse_exports()
        self._parse_code()
        self._parse_data()

    def _parse_sections(self):
        off = 8
        while off < len(self.raw):
            sid = self.raw[off]; off += 1
            plen, n = decode_uleb(self.raw, off); off += n
            self.sections.append((sid, SECTION_NAMES.get(sid, f"sec{sid}"),
                                  off, plen))
            off += plen

    def _section(self, sid):
        for s in self.sections:
            if s[0] == sid: return s
        return None

    def _parse_types(self):
        s = self._section(1)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        for _ in range(count):
            kind = self.raw[off]; off += 1
            if kind != 0x60:
                self.types.append(("?", [], []))
                continue
            n_params, n2 = decode_uleb(self.raw, off); off += n2
            params = []
            for _ in range(n_params):
                t, m = decode_valtype(self.raw, off); params.append(t); off += m
            n_results, n3 = decode_uleb(self.raw, off); off += n3
            results = []
            for _ in range(n_results):
                t, m = decode_valtype(self.raw, off); results.append(t); off += m
            self.types.append((params, results))

    def _parse_imports(self):
        s = self._section(2)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        for _ in range(count):
            mlen, n1 = decode_uleb(self.raw, off); off += n1
            module = self.raw[off:off+mlen].decode("utf-8", errors="replace"); off += mlen
            nlen, n2 = decode_uleb(self.raw, off); off += n2
            name = self.raw[off:off+nlen].decode("utf-8", errors="replace"); off += nlen
            kind = self.raw[off]; off += 1
            if kind == 0x00:  # func
                ti, m = decode_uleb(self.raw, off); off += m
                self.imports.append({"kind": "func", "module": module, "name": name, "type": ti})
                self.func_type_indices.append(ti)
            elif kind == 0x01:  # table
                _, off = self._skip_table(off)
                self.imports.append({"kind": "table", "module": module, "name": name})
            elif kind == 0x02:  # memory
                _, off = self._skip_mem_limits(off)
                self.imports.append({"kind": "memory", "module": module, "name": name})
            elif kind == 0x03:  # global
                _, off = self._skip_global_type(off)
                self.imports.append({"kind": "global", "module": module, "name": name})

    def _skip_table(self, off):
        off += 1  # elem type
        return self._skip_mem_limits(off)

    def _skip_mem_limits(self, off):
        flag = self.raw[off]; off += 1
        _, n = decode_uleb(self.raw, off); off += n
        if flag & 1:
            _, n = decode_uleb(self.raw, off); off += n
        return None, off

    def _skip_global_type(self, off):
        off += 2  # valtype + mutability
        return None, off

    def _parse_function_section(self):
        s = self._section(3)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        for _ in range(count):
            ti, m = decode_uleb(self.raw, off); off += m
            self.func_type_indices.append(ti)

    def _parse_exports(self):
        s = self._section(7)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        for _ in range(count):
            nlen, n1 = decode_uleb(self.raw, off); off += n1
            name = self.raw[off:off+nlen].decode("utf-8", errors="replace"); off += nlen
            kind = self.raw[off]; off += 1
            idx, m = decode_uleb(self.raw, off); off += m
            self.exports.append({"name": name, "kind": ["func","table","memory","global"][kind], "idx": idx})

    def _parse_code(self):
        s = self._section(10)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        n_imports = sum(1 for i in self.imports if i["kind"] == "func")
        for i in range(count):
            body_len, m = decode_uleb(self.raw, off); off += m
            body_end = off + body_len
            # locals
            n_local_groups, m2 = decode_uleb(self.raw, off); off += m2
            locals_decl = []
            for _ in range(n_local_groups):
                gn, n1 = decode_uleb(self.raw, off); off += n1
                gt, n2 = decode_valtype(self.raw, off); off += n2
                locals_decl.append((gn, gt))
            code_start = off
            code_end = body_end
            type_idx = self.func_type_indices[n_imports + i]
            self.functions.append({
                "func_idx":   n_imports + i,
                "type_idx":   type_idx,
                "locals":     locals_decl,
                "code_start": code_start,
                "code_end":   code_end,
            })
            off = body_end

    def _parse_globals(self):
        s = self._section(6)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        self.globals = []
        for _ in range(count):
            vt, m = decode_valtype(self.raw, off); off += m
            mut = self.raw[off]; off += 1
            # init expr — i32.const or similar, end
            init_val = None
            if self.raw[off] == 0x41:
                init_val, m = decode_sleb(self.raw, off + 1); off += 1 + m
            elif self.raw[off] == 0x42:
                init_val, m = decode_sleb(self.raw, off + 1); off += 1 + m
            elif self.raw[off] == 0x23:  # global.get init
                init_val_n, m = decode_uleb(self.raw, off + 1); off += 1 + m
                init_val = ("global.get", init_val_n)
            while self.raw[off] != 0x0b:
                off += 1
            off += 1
            self.globals.append({"type": vt, "mut": bool(mut), "init": init_val})

    def _parse_data(self):
        s = self._section(11)
        if not s: return
        _, _, off, plen = s
        count, n = decode_uleb(self.raw, off); off += n
        for _ in range(count):
            mode, m = decode_uleb(self.raw, off); off += m
            init_off = None
            if mode == 0 or mode == 2:
                if mode == 2:
                    _, m2 = decode_uleb(self.raw, off); off += m2
                # parse initializer expression — i32.const N, end
                if self.raw[off] == 0x41:
                    init_off, m2 = decode_sleb(self.raw, off + 1); off += 1 + m2
                while self.raw[off] != 0x0b:
                    off += 1
                off += 1  # skip end
            dlen, m = decode_uleb(self.raw, off); off += m
            self.data_segments.append({
                "mode": mode, "vaddr": init_off,
                "data": self.raw[off:off+dlen],
            })
            off += dlen

    # -----------------------------------------------------------------
    # Higher-level helpers
    # -----------------------------------------------------------------
    def decode_function(self, func_idx):
        """Returns list of (name, operands, offset_from_code_start, length)."""
        f = next((fn for fn in self.functions if fn["func_idx"] == func_idx), None)
        if f is None: return None
        instrs = []
        off = f["code_start"]
        while off < f["code_end"]:
            name, ops, length = parse_instruction(self.raw, off)
            instrs.append((name, ops, off - f["code_start"], length))
            off += length
            if length == 0:
                break
        return instrs

    def opcode_histogram(self, func_idx):
        instrs = self.decode_function(func_idx) or []
        return Counter(name for name, _, _, _ in instrs)


# ---------------------------------------------------------------------------
# Heuristics
# ---------------------------------------------------------------------------
FIXSLICE_MASKS = {
    0x55555555,  # interleave bits 0,2,4,...
    0xaaaaaaaa,
    0x33333333,  # interleave pairs
    0xcccccccc,
    0x0f0f0f0f,  # interleave nibbles
    0xf0f0f0f0,
    0x00ff00ff,  # interleave bytes
    0xff00ff00,
    0x0000ffff,
    0xffff0000,
}


def find_fixslice_functions(mod: WasmModule, top_n: int = 20):
    """Score each function by how many fixslice32 mask constants it
    loads as i32.const operands. The functions with the highest count
    are the bitsliced AES transformation routines.
    """
    scores = []
    for f in mod.functions:
        score = 0
        details = Counter()
        instrs = mod.decode_function(f["func_idx"])
        for name, ops, _, _ in instrs:
            if name == "i32.const" and ops:
                v = ops[0] & 0xffffffff
                if v in FIXSLICE_MASKS:
                    score += 1
                    details[v] += 1
        if score > 0:
            scores.append((score, f["func_idx"], dict(details)))
    scores.sort(reverse=True)
    return scores[:top_n]


def find_static_loads(mod: WasmModule, func_idx: int):
    """Scan a function for the pattern `i32.const ADDR` immediately
    followed by `i32.load` / `i64.load` / `i32.load8_u` / etc. with
    memarg offset O. Returns list of (effective_address, load_op, length).
    """
    instrs = mod.decode_function(func_idx) or []
    out = []
    for i in range(len(instrs) - 1):
        n0, ops0, _, _ = instrs[i]
        n1, ops1, _, _ = instrs[i+1]
        if n0 == "i32.const" and n1.startswith(("i32.load", "i64.load", "f32.load", "f64.load")):
            base = ops0[0] & 0xffffffff
            memarg_off = ops1[1] if len(ops1) > 1 else 0
            eff = base + memarg_off
            size_map = {"i32.load": 4, "i64.load": 8, "f32.load": 4, "f64.load": 8,
                        "i32.load8_u": 1, "i32.load8_s": 1, "i32.load16_u": 2,
                        "i32.load16_s": 2, "i64.load8_u": 1, "i64.load8_s": 1,
                        "i64.load16_u": 2, "i64.load16_s": 2, "i64.load32_u": 4,
                        "i64.load32_s": 4}
            sz = size_map.get(n1, 4)
            out.append((eff, n1, sz))
    return out


def find_data_segment_at(mod: WasmModule, addr: int, size: int):
    """Return the bytes at a static memory address by reading the
    matching data segment."""
    for seg in mod.data_segments:
        if seg["vaddr"] is None: continue
        start = seg["vaddr"]
        end = start + len(seg["data"])
        if start <= addr and addr + size <= end:
            return seg["data"][addr - start : addr - start + size]
    return None


def find_callers(mod: WasmModule, target_func_indices: list[int]):
    """Find every function that contains a `call N` instruction where
    N is in the target set. Returns dict: target_idx -> list of caller_idx."""
    target_set = set(target_func_indices)
    out = {t: [] for t in target_set}
    for f in mod.functions:
        instrs = mod.decode_function(f["func_idx"])
        for name, ops, _, _ in instrs:
            if name == "call" and ops and ops[0] in target_set:
                out[ops[0]].append(f["func_idx"])
                break
    return out


def trace_function(mod: WasmModule, func_idx: int, head: int = 80):
    """Pretty-print the first N instructions of a function."""
    instrs = mod.decode_function(func_idx) or []
    out_lines = []
    indent = 0
    for i, (name, ops, off, length) in enumerate(instrs[:head]):
        if name == "end" or name == "else":
            indent = max(0, indent - 1)
        prefix = "  " * indent
        op_str = " ".join(repr(o) for o in ops) if ops else ""
        out_lines.append(f"  [{off:5d}]  {prefix}{name:18s} {op_str}")
        if name in ("block", "loop", "if", "else"):
            indent += 1
    return out_lines


def find_key_loads(mod: WasmModule, top_n: int = 20):
    """Find functions that issue 32-byte sequences of i32/i64 loads
    likely reading the master key from rodata. Heuristic: a function
    body containing 8+ consecutive `i32.load` (or 4 `i64.load`)
    instructions at sequential memarg offsets and the same base local.

    Returns (count, func_idx, sample_addresses) sorted by score.
    """
    scores = []
    for f in mod.functions:
        instrs = mod.decode_function(f["func_idx"])
        # count back-to-back i32.load / i64.load at memarg offsets that
        # are sequential 4 / 8 bytes apart.
        loads = [(i, name, ops) for i, (name, ops, _, _) in enumerate(instrs)
                 if name in ("i32.load", "i64.load")]
        # group by index proximity
        groups = []
        cur = []
        for entry in loads:
            i, name, ops = entry
            if not cur or i - cur[-1][0] <= 3:
                cur.append(entry)
            else:
                if len(cur) >= 4: groups.append(cur)
                cur = [entry]
        if len(cur) >= 4: groups.append(cur)
        if groups:
            best = max(groups, key=len)
            scores.append((len(best), f["func_idx"], [g[2][1] for g in best[:10]]))
    scores.sort(reverse=True)
    return scores[:top_n]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load_wasm(arg: str) -> bytes:
    if arg.endswith(".wasm") and os.path.exists(arg):
        with open(arg, "rb") as f:
            return f.read()
    if arg in ("hsw", "hsw.js"):
        from ..hsw_bridge import HSWAnalyzer
        info = HSWAnalyzer().analyze()
        return bytes.fromhex(info["wasm_bytes_hex"])
    raise ValueError(f"don't know how to load {arg!r}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser()
    p.add_argument("source", help="path to .wasm file OR 'hsw' to fetch hsw.js's WASM")
    p.add_argument("--funcs", type=str, default=None, help="comma-separated func indices to print")
    p.add_argument("--find-fixslice", action="store_true",
                   help="find functions using fixslice32 bit-permutation masks")
    p.add_argument("--find-key-load", action="store_true",
                   help="find functions with consecutive 32-byte loads (key reads)")
    p.add_argument("--summary", action="store_true",
                   help="overall module summary")
    p.add_argument("--callers", type=str, default=None,
                   help="comma-separated func indices; show all callers of each")
    p.add_argument("--static-loads", type=str, default=None,
                   help="comma-separated func indices; show static memory loads + segment bytes")
    p.add_argument("--head", type=int, default=80,
                   help="how many instructions to show with --funcs")
    p.add_argument("--save-json", type=str, default=None,
                   help="dump per-function summaries to JSON")
    args = p.parse_args()

    wasm = _load_wasm(args.source)
    mod = WasmModule(wasm)

    print(f"WASM: {len(wasm)} bytes")
    print(f"  sections:     {[s[1] for s in mod.sections]}")
    print(f"  types:        {len(mod.types)}")
    print(f"  imports:      {len(mod.imports)} ({sum(1 for i in mod.imports if i['kind']=='func')} funcs)")
    print(f"  functions:    {len(mod.functions)}")
    print(f"  exports:      {len(mod.exports)}")
    print(f"  data segments:{len(mod.data_segments)}")
    print(f"  globals:      {len(mod.globals)}")
    n_imports = sum(1 for i in mod.imports if i['kind']=='func')
    print(f"  total funcs:  {n_imports + len(mod.functions)} (imports + local)")

    if args.summary:
        print(f"\nExports:")
        for e in mod.exports:
            sig = ""
            if e["kind"] == "func" and e["idx"] >= n_imports:
                f = mod.functions[e["idx"] - n_imports]
                params, results = mod.types[f["type_idx"]]
                sig = f"  ({','.join(params)})->({','.join(results)})"
            print(f"  [{e['kind']:6s}] {e['name']:8s}  idx={e['idx']}{sig}")

    if args.find_fixslice:
        print(f"\n=== fixslice32 candidates (functions loading mask constants) ===")
        for score, fi, details in find_fixslice_functions(mod):
            params, results = mod.types[mod.functions[fi - n_imports]["type_idx"]]
            print(f"  score {score:3d}  func {fi}  sig=({','.join(params)})->({','.join(results)})  "
                  f"masks={ {hex(k): v for k,v in details.items()} }")

    if args.find_key_load:
        print(f"\n=== key-load candidates (sequential 32-byte loads) ===")
        for count, fi, mem_offs in find_key_loads(mod):
            params, results = mod.types[mod.functions[fi - n_imports]["type_idx"]]
            print(f"  loads {count:3d}  func {fi}  sig=({','.join(params)})->({','.join(results)})  "
                  f"sample_offs={mem_offs}")

    if args.funcs:
        for fi in args.funcs.split(","):
            fi = int(fi)
            f = mod.functions[fi - n_imports]
            params, results = mod.types[f["type_idx"]]
            print(f"\n=== func {fi}  ({','.join(params)}) -> ({','.join(results)})  "
                  f"locals={f['locals']}  body={f['code_end']-f['code_start']}B ===")
            hist = mod.opcode_histogram(fi)
            print(f"opcodes top: {dict(hist.most_common(15))}")
            head_n = int(getattr(args, 'head', 80) or 80)
            print(f"first {head_n} instructions:")
            for line in trace_function(mod, fi, head_n):
                print(line)

    if getattr(args, 'callers', None):
        targets = [int(x) for x in args.callers.split(",")]
        callers = find_callers(mod, targets)
        for t, cs in callers.items():
            print(f"\nfunc {t} is called by: {cs}")

    if getattr(args, 'static_loads', None):
        for fi in args.static_loads.split(","):
            fi = int(fi)
            loads = find_static_loads(mod, fi)
            print(f"\nfunc {fi} static loads ({len(loads)}):")
            for addr, op, size in loads:
                bs = find_data_segment_at(mod, addr, min(size, 32))
                bs_str = bs.hex() if bs else "(no segment)"
                print(f"  addr=0x{addr:06x}  {op}  size={size}  data={bs_str}")

    if args.save_json:
        out = []
        for f in mod.functions:
            params, results = mod.types[f["type_idx"]]
            hist = mod.opcode_histogram(f["func_idx"])
            out.append({
                "func_idx": f["func_idx"],
                "type":     {"params": params, "results": results},
                "locals":   [(c, t) for c, t in f["locals"]],
                "size":     f["code_end"] - f["code_start"],
                "opcode_hist": dict(hist),
            })
        with open(args.save_json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        print(f"\nwrote {args.save_json}")


if __name__ == "__main__":
    main()
