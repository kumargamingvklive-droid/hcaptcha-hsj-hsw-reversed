"""Static WASM-bytecode emulator for the HSW deobf helpers.

APPROACH B — pure-Python emulator for the small subset of WASM
needed to evaluate the XOR-deobfuscation helper functions called
during HSW N-key derivation.

Background
==========
On current era (d) builds, the 12 N-key LCG constants (initial seed,
key_factor1, etc.) are not in-stream ``iN.const`` literals.  Each
constant is materialised by a helper-call shape like::

    i32.const MAGIC_A           ;; e.g. -2096827833
    i32.const MAGIC_B           ;; e.g.  2004363313
    local.get BASE_PTR
    i32.const BYTE_IDX          ;; usually 0
    call HELPER                 ;; returns i64

The helper reads 8 bytes from a SCATTERED address space (built from a
rodata table at vaddr 1075552) with a XOR mask layered on top, and
returns the 64-bit deobfuscated value.  Implex's old recipe doesn't
match because the literal constants are not inline — they live behind
the helper call and depend on the helper-resident XOR table.

This module provides a minimal WASM emulator that can evaluate the
helper function statically: it implements only the opcodes the
helper uses (i32/i64 arithmetic + logic + memory loads of all
widths, control flow: block / loop / if / br / br_if / br_table /
end / return).  No floats, no SIMD, no globals (the helper doesn't
read any), no atomics, no GC.  Memory is backed by a sparse mapping
of (vaddr, byte) initialised from the WASM's data segments.

STATUS
======
This module is **a scaffold**.  It is not yet wired into
:mod:`hcaptcha.hsw_n_key`.  The reason: on the era (d) build inspected
(version ``2c5dc6f5ca56a7df...``), the LCG output bytes captured at
runtime via :mod:`hcaptcha.hsw_n_key_runtime` VARY between successive
calls within the same process — strongly suggesting the N-key on this
build is no longer a pure build-static derivation but mixes in a
runtime input (likely ``Math.round(Date.now()/1000)`` which the
``window.hsw(jwt)`` wrapper passes into the WASM ``rc`` export).

If that hypothesis is correct, the static emulator cannot reproduce
the runtime N-key for a particular call without also being given the
exact runtime inputs (timestamp + JWT bytes).  In that case the
runtime trace (Approach A) is the only reliable path.

This file is preserved as a starting point for any future build that
returns to a build-static N-key derivation, and as a reference
implementation of the WASM-emulator subset that other parts of the
project may need (e.g. evaluating individual deobf helpers for
constant-folding diagnostics).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List, Tuple

from .tools.wasm_disasm import WasmModule


MASK32 = (1 << 32) - 1
MASK64 = (1 << 64) - 1


def _to_signed(v: int, bits: int) -> int:
    mask = (1 << bits) - 1
    sign = 1 << (bits - 1)
    v &= mask
    return v - (1 << bits) if v & sign else v


@dataclass
class EmuFrame:
    """A single function frame during emulation."""
    locals_: List[int]
    instrs:  list
    pc:      int = 0
    stack:   List[int] = field(default_factory=list)


class WasmEmulator:
    """Minimal WebAssembly 1.0 emulator covering the opcodes used by
    the HSW deobf helpers (and the LCG-init helper)."""

    def __init__(self, mod: WasmModule):
        self.mod = mod
        # Initial memory image from data segments.  Stored as a dict
        # of (vaddr -> byte) for sparsity; the helper only reads ~328
        # bytes near vaddr 1075552 so this is cheap.
        self.mem = {}
        for seg in mod.data_segments:
            for i, b in enumerate(seg["data"]):
                self.mem[seg["vaddr"] + i] = b

    # ------------------------------------------------------------------
    # Memory accessors
    # ------------------------------------------------------------------
    def _load(self, addr: int, n: int, signed: bool = False) -> int:
        """Load n bytes little-endian. Out-of-bounds reads return 0."""
        v = 0
        for i in range(n):
            v |= self.mem.get(addr + i, 0) << (i * 8)
        if signed:
            v = _to_signed(v, n * 8)
        return v

    def _store(self, addr: int, n: int, value: int) -> None:
        value &= (1 << (n * 8)) - 1
        for i in range(n):
            self.mem[addr + i] = (value >> (i * 8)) & 0xFF

    def write_memory(self, addr: int, data: bytes) -> None:
        for i, b in enumerate(data):
            self.mem[addr + i] = b

    # ------------------------------------------------------------------
    # Call a function. args are integers in declaration order.
    # ------------------------------------------------------------------
    def call(self, func_idx: int, args: list) -> list:
        """Execute one function and return its result vector."""
        f = next(x for x in self.mod.functions
                 if x["func_idx"] == func_idx)
        params, results = self.mod.types[f["type_idx"]]
        if len(args) != len(params):
            raise ValueError(
                f"func {func_idx} expects {len(params)} args, got {len(args)}")

        instrs = self.mod.decode_function(func_idx) or []
        # initialise locals: args first, then declared locals (zeroed)
        locals_ = list(args)
        for count, vt in f["locals"]:
            for _ in range(count):
                locals_.append(0)

        frame = EmuFrame(locals_=locals_, instrs=instrs)
        try:
            self._exec(frame)
        except _Returning as r:
            return r.values
        # End of function with no explicit return — top of stack is return
        return [frame.stack.pop() for _ in results][::-1]

    # ------------------------------------------------------------------
    # Core interpreter
    # ------------------------------------------------------------------
    def _exec(self, frame: EmuFrame) -> None:
        # Build a label table: map each control-flow opcode position
        # to its matching 'end'.  Cheap O(n) pass per call.
        labels = {}
        stack = []
        for i, (name, ops, _, _) in enumerate(frame.instrs):
            if name in ("block", "loop", "if"):
                stack.append(i)
            elif name == "else":
                # mark the if's else position
                if stack:
                    labels[(stack[-1], "else")] = i
            elif name == "end":
                if stack:
                    open_i = stack.pop()
                    labels[(open_i, "end")] = i
                else:
                    # outermost end of function — terminator
                    labels[("fn_end",)] = i
        # Reverse map: for each instruction index, what's the enclosing
        # nesting; for br N, we need to know the target.
        # We'll just maintain a nesting stack during execution.

        # Block control stack: list of (kind, end_pc, loop_pc_if_loop,
        #                                start_stack_height, has_result)
        ctrl: List[Tuple[str, int, int, int, bool]] = []

        # Push a synthetic outermost block representing the function
        ctrl.append(("fn", len(frame.instrs), -1, 0, False))

        while frame.pc < len(frame.instrs):
            name, ops, _, _ = frame.instrs[frame.pc]

            if name == "block" or name == "if" or name == "loop":
                # ops[0] is the blocktype tuple
                bt = ops[0]
                has_res = (bt != ("empty",))
                if name == "if":
                    cond = frame.stack.pop() & 0xFFFFFFFF
                    end_pc = labels[(frame.pc, "end")]
                    else_pc = labels.get((frame.pc, "else"))
                    ctrl.append(("if", end_pc, -1,
                                 len(frame.stack), has_res))
                    if cond == 0:
                        # jump to else (if any) or end
                        frame.pc = else_pc if else_pc is not None else end_pc
                        continue
                else:
                    end_pc = labels[(frame.pc, "end")]
                    loop_pc = frame.pc if name == "loop" else -1
                    ctrl.append((name, end_pc, loop_pc,
                                 len(frame.stack), has_res))

            elif name == "else":
                # Reached normally only after taking the 'if' branch;
                # jump to matching end.
                # Find owning if
                # The 'else' is at a known label position whose end is
                # mapped from the same open-index. Simpler: pop ctrl
                # and skip to end_pc.
                if ctrl and ctrl[-1][0] == "if":
                    _, end_pc, _, _, _ = ctrl[-1]
                    frame.pc = end_pc
                    continue

            elif name == "end":
                # Pop control frame
                if ctrl:
                    kind, end_pc, _, _, _ = ctrl.pop()
                    if kind == "fn":
                        return

            elif name == "br":
                depth = ops[0]
                if depth >= len(ctrl):
                    raise RuntimeError(f"br depth {depth} exceeds nesting")
                # Pop 'depth' frames; target is the (depth+1)-th from top
                # For a loop, br jumps to the loop start; for others, to end.
                target = ctrl[-(depth + 1)]
                kind, end_pc, loop_pc, _, _ = target
                # Pop 'depth' control frames
                for _ in range(depth):
                    ctrl.pop()
                if kind == "loop":
                    frame.pc = loop_pc + 1
                    continue
                else:
                    # leave outer block; pop it
                    ctrl.pop()
                    frame.pc = end_pc + 1
                    continue

            elif name == "br_if":
                depth = ops[0]
                cond = frame.stack.pop() & 0xFFFFFFFF
                if cond != 0:
                    target = ctrl[-(depth + 1)]
                    kind, end_pc, loop_pc, _, _ = target
                    for _ in range(depth):
                        ctrl.pop()
                    if kind == "loop":
                        frame.pc = loop_pc + 1
                    else:
                        ctrl.pop()
                        frame.pc = end_pc + 1
                    continue

            elif name == "br_table":
                table, default = ops
                idx = frame.stack.pop() & 0xFFFFFFFF
                depth = table[idx] if idx < len(table) else default
                target = ctrl[-(depth + 1)]
                kind, end_pc, loop_pc, _, _ = target
                for _ in range(depth):
                    ctrl.pop()
                if kind == "loop":
                    frame.pc = loop_pc + 1
                else:
                    ctrl.pop()
                    frame.pc = end_pc + 1
                continue

            elif name == "return":
                # Pop function frame; results stay on stack.
                raise _Returning(list(frame.stack))

            elif name == "call":
                callee = ops[0]
                f = next((x for x in self.mod.functions
                          if x["func_idx"] == callee), None)
                if f is None:
                    raise RuntimeError(
                        f"call {callee}: function not local (import?)")
                params, results = self.mod.types[f["type_idx"]]
                n_args = len(params)
                args = [frame.stack.pop() for _ in range(n_args)][::-1]
                res = self.call(callee, args)
                frame.stack.extend(res)

            # ----- locals / globals -----
            elif name == "local.get":
                frame.stack.append(frame.locals_[ops[0]])
            elif name == "local.set":
                frame.locals_[ops[0]] = frame.stack.pop()
            elif name == "local.tee":
                frame.locals_[ops[0]] = frame.stack[-1]
            elif name == "drop":
                frame.stack.pop()
            elif name == "select":
                cond = frame.stack.pop() & 0xFFFFFFFF
                b = frame.stack.pop()
                a = frame.stack.pop()
                frame.stack.append(a if cond != 0 else b)

            # ----- constants -----
            elif name == "i32.const":
                frame.stack.append(ops[0] & MASK32)
            elif name == "i64.const":
                frame.stack.append(ops[0] & MASK64)

            # ----- loads -----
            elif name.startswith("i32.load") or name.startswith("i64.load"):
                bits, signed = self._load_attrs(name)
                _, off = ops
                addr = (frame.stack.pop() & MASK32) + off
                v = self._load(addr, bits // 8, signed)
                frame.stack.append(v & (MASK32 if name.startswith("i32") else MASK64))

            # ----- stores -----
            elif name.startswith("i32.store") or name.startswith("i64.store"):
                bits = self._store_bits(name)
                _, off = ops
                value = frame.stack.pop()
                addr = (frame.stack.pop() & MASK32) + off
                self._store(addr, bits // 8, value)

            # ----- i32 binops -----
            elif name == "i32.add":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a + b) & MASK32)
            elif name == "i32.sub":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a - b) & MASK32)
            elif name == "i32.mul":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a * b) & MASK32)
            elif name == "i32.and":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a & b) & MASK32)
            elif name == "i32.or":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a | b) & MASK32)
            elif name == "i32.xor":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a ^ b) & MASK32)
            elif name == "i32.shl":
                b = frame.stack.pop() & 0x1F; a = frame.stack.pop()
                frame.stack.append((a << b) & MASK32)
            elif name == "i32.shr_u":
                b = frame.stack.pop() & 0x1F; a = frame.stack.pop()
                frame.stack.append((a >> b) & MASK32)
            elif name == "i32.shr_s":
                b = frame.stack.pop() & 0x1F; a = _to_signed(frame.stack.pop(), 32)
                frame.stack.append((a >> b) & MASK32)
            elif name == "i32.div_u":
                b = frame.stack.pop(); a = frame.stack.pop()
                if b == 0: raise RuntimeError("i32.div_u by zero")
                frame.stack.append((a // b) & MASK32)
            elif name == "i32.rem_u":
                b = frame.stack.pop(); a = frame.stack.pop()
                if b == 0: raise RuntimeError("i32.rem_u by zero")
                frame.stack.append((a % b) & MASK32)
            elif name == "i32.eq":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append(1 if a == b else 0)
            elif name == "i32.ne":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append(0 if a == b else 1)
            elif name == "i32.eqz":
                a = frame.stack.pop()
                frame.stack.append(1 if a == 0 else 0)
            elif name == "i32.gt_s":
                b = _to_signed(frame.stack.pop(), 32); a = _to_signed(frame.stack.pop(), 32)
                frame.stack.append(1 if a > b else 0)
            elif name == "i32.lt_u":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append(1 if a < b else 0)
            elif name == "i32.lt_s":
                b = _to_signed(frame.stack.pop(), 32); a = _to_signed(frame.stack.pop(), 32)
                frame.stack.append(1 if a < b else 0)
            elif name == "i32.gt_u":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append(1 if a > b else 0)

            # ----- i64 binops (limited subset) -----
            elif name == "i64.add":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a + b) & MASK64)
            elif name == "i64.sub":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a - b) & MASK64)
            elif name == "i64.mul":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a * b) & MASK64)
            elif name == "i64.and":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a & b) & MASK64)
            elif name == "i64.or":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a | b) & MASK64)
            elif name == "i64.xor":
                b = frame.stack.pop(); a = frame.stack.pop()
                frame.stack.append((a ^ b) & MASK64)
            elif name == "i64.shl":
                b = frame.stack.pop() & 0x3F; a = frame.stack.pop()
                frame.stack.append((a << b) & MASK64)
            elif name == "i64.shr_u":
                b = frame.stack.pop() & 0x3F; a = frame.stack.pop()
                frame.stack.append((a >> b) & MASK64)

            # ----- conversions -----
            elif name == "i32.wrap_i64":
                a = frame.stack.pop()
                frame.stack.append(a & MASK32)
            elif name == "i64.extend_i32_u":
                a = frame.stack.pop() & MASK32
                frame.stack.append(a & MASK64)
            elif name == "i64.extend_i32_s":
                a = _to_signed(frame.stack.pop(), 32)
                frame.stack.append(a & MASK64)

            # ----- unsupported -----
            else:
                raise NotImplementedError(
                    f"emulator: opcode {name!r} (operands {ops}) not yet supported")

            frame.pc += 1

    # ------------------------------------------------------------------
    @staticmethod
    def _load_attrs(name: str) -> tuple:
        """Return (bits, signed) for load opcodes."""
        if name == "i32.load":       return (32, False)
        if name == "i64.load":       return (64, False)
        if name == "i32.load8_u":    return (8,  False)
        if name == "i32.load8_s":    return (8,  True)
        if name == "i32.load16_u":   return (16, False)
        if name == "i32.load16_s":   return (16, True)
        if name == "i64.load8_u":    return (8,  False)
        if name == "i64.load8_s":    return (8,  True)
        if name == "i64.load16_u":   return (16, False)
        if name == "i64.load16_s":   return (16, True)
        if name == "i64.load32_u":   return (32, False)
        if name == "i64.load32_s":   return (32, True)
        raise ValueError(name)

    @staticmethod
    def _store_bits(name: str) -> int:
        if name in ("i32.store", "i64.store32"):  return 32
        if name == "i64.store":                   return 64
        if name in ("i32.store8", "i64.store8"):  return 8
        if name in ("i32.store16", "i64.store16"):return 16
        raise ValueError(name)


class _Returning(Exception):
    """Carries return values up to the call boundary."""
    def __init__(self, values):
        self.values = values


# --------------------------------------------------------------------------
# Convenience: emulate a single helper call, returning its i64 result.
# --------------------------------------------------------------------------
def emulate_helper(mod: WasmModule, helper_idx: int, args: list) -> int:
    emu = WasmEmulator(mod)
    res = emu.call(helper_idx, args)
    return res[0] if res else 0
