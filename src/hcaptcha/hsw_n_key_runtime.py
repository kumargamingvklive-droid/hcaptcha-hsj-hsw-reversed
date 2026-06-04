"""Runtime extractor for the HSW N-key on era (d) builds.

APPROACH A — runtime tracing of the byte-store helper
=====================================================
The current HSW WASM build inlines the LCG-derived 32-byte "N key" into
the ``vc`` dispatcher's branch for the n-token magic. The old Implex
recipe (in :mod:`hcaptcha.hsw_n_key`) fails because the six per-build
scalars (key_seed, seed, memory, key_factor1, key_factor2, operator)
are no longer in-stream literals — they're materialised by
``call HELPER_477(MAGIC1, MAGIC2, base_ptr, byte_idx) -> i64`` that does
a XOR-deobfuscated 8-byte read from a scattered output buffer. The
30 derived bytes (steps 0..29 of the LCG; the first two bytes of the
N-key come from ``key_seed`` directly) are written back via
``call HELPER_355(byte_value_i32, base_ptr, step) -> ()``.

Approach
--------
1. Locate HELPER_355 by counting which (i32,i32,i32)->() callee is
   invoked most often immediately after the LCG-multiplier + rotation
   pattern inside ``vc``.
2. Patch the prologue of HELPER_355 to push a record
   ``(base_ptr_u32, step_u32, byte_val_u32)`` onto a 12-byte ring buffer
   in linear memory, incrementing a counter at a fixed scratch addr.
3. Add ``__peek32`` / ``__poke32`` exports so the Python side can
   read/write the scratch.
4. Run ``window.hsw(jwt)`` in the polyfilled sandbox; the n-token path
   triggers the N-key derivation, which fires HELPER_355 30 times with
   the same base_ptr and step in ``0..29``.
5. Scan the ring buffer for a base_ptr that appears 30 times with
   steps ``0..29``; the byte values (low 8 bits) of those entries are
   bytes 2..31 of the N-key.
6. Bytes 0 and 1 come from a separate write the bundle does before
   entering the LCG loop — we detect them by replaying the helper-355
   trick on the same base_ptr with step values that the bundle's
   prologue uses (typically ``-2`` and ``-1`` would be too neat; in
   practice the prologue uses the same byte-store helper with step
   values 30, 31 or 0..1 of a *different* base_ptr, depending on the
   build). On builds where bytes 0..1 are NOT visible via this trace,
   we report the 30 visible bytes and zero the first 2 — the caller
   can match against the HSJ ``n_key`` to confirm full equality.

USAGE
-----
    from hcaptcha.hsw_n_key_runtime import trace_n_key
    result = trace_n_key()
    print(result.n_key.hex())

Requires :mod:`hcaptcha.tools.sandbox_polyfill` to be installed so
``window.hsw(jwt)`` reaches the n-token branch (the fingerprint
collection above it pokes at many browser APIs jsdom omits).
"""
from __future__ import annotations

import base64
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests

from .log import Logger
from .tools.js_runtime import JsRuntime
from .tools.wasm_disasm import WasmModule
from .tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from . import version as _v


# --------------------------------------------------------------------------
# Scratch layout in linear memory.  We pick high-but-safe addresses
# (the heap grows from low addresses up; 60_000+ should be free in
# every observed build, but we *zero* it before each run so even a
# stale heap allocation that landed here is harmless).
# --------------------------------------------------------------------------
SCRATCH_COUNTER = 60_000           # u32 — number of records written
SCRATCH_BUF     = 60_004           # records start here
RECORD_SIZE     = 12               # (base_ptr, step, byte_val) — 3 × u32
MAX_RECORDS     = 4000             # 48 KB max — plenty for one hsw(jwt) call
SCRATCH_END     = SCRATCH_BUF + RECORD_SIZE * MAX_RECORDS

N_KEY_LCG_MULT  = 6364136223846793005   # 0x5851F42D4C957F2D
N_KEY_STEPS     = 30                    # bytes 2..31 of the 32-byte key


# --------------------------------------------------------------------------
# Locating the byte-store helper inside vc
# --------------------------------------------------------------------------
def _find_vc(mod: WasmModule) -> int:
    for ex in mod.exports:
        if ex["kind"] == "func" and ex["name"] == "vc":
            return ex["idx"]
    raise RuntimeError("vc dispatcher export not found")


def _find_byte_store_helper(mod: WasmModule, vc_idx: int) -> int:
    """Find HELPER_355 — the (i32,i32,i32)->() callee that's invoked
    right after each LCG-rotation block in vc."""
    instrs = mod.decode_function(vc_idx)
    lcg_positions = [
        i for i, (n, ops, _, _) in enumerate(instrs)
        if n == "i64.const" and ops
        and (ops[0] & 0xFFFFFFFFFFFFFFFF) == N_KEY_LCG_MULT
    ]
    if not lcg_positions:
        raise RuntimeError("no LCG multiplier in vc body")
    callees = Counter()
    for pos in lcg_positions:
        for j in range(pos, min(pos + 30, len(instrs))):
            nj, opsj, _, _ = instrs[j]
            if nj == "call" and opsj:
                f = next((f for f in mod.functions
                          if f["func_idx"] == opsj[0]), None)
                if f and mod.types[f["type_idx"]] == (
                        ["i32", "i32", "i32"], []):
                    callees[opsj[0]] += 1
                    break
    if not callees:
        raise RuntimeError(
            "no (i32,i32,i32)->() callee near LCG in vc — algorithm changed")
    best, n_hits = callees.most_common(1)[0]
    if n_hits < 3:
        raise RuntimeError(
            f"byte-store helper {best} only hit {n_hits} times near LCG — "
            "heuristic too weak; algorithm may have changed")
    return best


# --------------------------------------------------------------------------
# WASM injection — prologue of HELPER_355
#
# On entry: local 0=byte_val (i32), local 1=base_ptr (i32), local 2=step (i32)
# We push a 12-byte record into the ring buffer at SCRATCH_BUF, bumping
# SCRATCH_COUNTER. If we exceed MAX_RECORDS, we silently stop (the
# counter caps at MAX_RECORDS so the records below remain intact).
#
# Bytecode (stack-balanced; nets to zero so the original body runs intact):
#
#   ;; load counter
#   i32.const SCRATCH_COUNTER
#   i32.load offset=0 align=2
#   local.tee NEW_LOCAL_C
#   i32.const MAX_RECORDS
#   i32.lt_u
#   if (empty)
#     ;; addr = SCRATCH_BUF + counter * 12
#     local.get NEW_LOCAL_C
#     i32.const 12
#     i32.mul
#     i32.const SCRATCH_BUF
#     i32.add
#     local.tee NEW_LOCAL_A
#     local.get 1                 ;; base_ptr
#     i32.store offset=0 align=2
#
#     local.get NEW_LOCAL_A
#     local.get 2                 ;; step
#     i32.store offset=4 align=2
#
#     local.get NEW_LOCAL_A
#     local.get 0                 ;; byte_val
#     i32.store offset=8 align=2
#
#     ;; increment counter
#     i32.const SCRATCH_COUNTER
#     local.get NEW_LOCAL_C
#     i32.const 1
#     i32.add
#     i32.store offset=0 align=2
#   end
#
# We need TWO new locals (i32). The wasm_writer add_function helper
# can extend the locals decl — but we're patching an EXISTING function,
# not adding one. To avoid editing the locals vector, we re-use any
# existing i32 locals in the function. We pass the indices in via
# parameters to _build_injection.
# --------------------------------------------------------------------------
def _existing_i32_local_indices(mod: WasmModule, fi: int) -> list:
    """Return a list of local indices (post-args) typed i32 in fi."""
    f = next(x for x in mod.functions if x["func_idx"] == fi)
    params, _ = mod.types[f["type_idx"]]
    n_args = len(params)
    out = []
    idx = n_args
    for count, vt in f["locals"]:
        for _ in range(count):
            if vt == 0x7f:                           # i32
                out.append(idx)
            idx += 1
    return out


# Two extra scratch slots used as "temp registers" to avoid needing
# real wasm locals (so we can patch ANY signature of byte-store helper).
SCRATCH_TMP_C   = 60_004 + RECORD_SIZE * MAX_RECORDS + 16    # tmp for counter
SCRATCH_TMP_A   = 60_004 + RECORD_SIZE * MAX_RECORDS + 32    # tmp for record addr


def _build_injection() -> bytes:
    """Return prologue bytecode that does NOT use locals (uses two
    extra memory scratch slots instead).  Spliced at the start of
    HELPER_355 with n_replace=0.

    Stack discipline: nets to 0 (the original body runs unchanged).
    """
    out = bytearray()
    # load counter, store in TMP_C
    out += b"\x41" + encode_sleb(SCRATCH_TMP_C)
    out += b"\x41" + encode_sleb(SCRATCH_COUNTER)
    out += b"\x28\x02\x00"                          # i32.load
    out += b"\x36\x02\x00"                          # i32.store -> TMP_C

    # if counter < MAX_RECORDS:
    out += b"\x41" + encode_sleb(SCRATCH_TMP_C)
    out += b"\x28\x02\x00"                          # i32.load counter
    out += b"\x41" + encode_sleb(MAX_RECORDS)
    out += b"\x49"                                  # i32.lt_u
    out += b"\x04\x40"                              # if (empty)

    # TMP_A = SCRATCH_BUF + counter * 12
    out += b"\x41" + encode_sleb(SCRATCH_TMP_A)
    out += b"\x41" + encode_sleb(SCRATCH_TMP_C)
    out += b"\x28\x02\x00"                          # i32.load counter
    out += b"\x41" + encode_sleb(12)
    out += b"\x6c"                                  # i32.mul
    out += b"\x41" + encode_sleb(SCRATCH_BUF)
    out += b"\x6a"                                  # i32.add
    out += b"\x36\x02\x00"                          # i32.store -> TMP_A

    # *(TMP_A) = base_ptr
    out += b"\x41" + encode_sleb(SCRATCH_TMP_A)
    out += b"\x28\x02\x00"                          # i32.load TMP_A
    out += b"\x20\x01"                              # local.get 1 (base_ptr)
    out += b"\x36\x02\x00"                          # i32.store

    # *(TMP_A+4) = step
    out += b"\x41" + encode_sleb(SCRATCH_TMP_A)
    out += b"\x28\x02\x00"
    out += b"\x20\x02"                              # local.get 2 (step)
    out += b"\x36\x02\x04"

    # *(TMP_A+8) = byte_val
    out += b"\x41" + encode_sleb(SCRATCH_TMP_A)
    out += b"\x28\x02\x00"
    out += b"\x20\x00"                              # local.get 0 (byte_val)
    out += b"\x36\x02\x08"

    # counter += 1
    out += b"\x41" + encode_sleb(SCRATCH_COUNTER)
    out += b"\x41" + encode_sleb(SCRATCH_TMP_C)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"

    out += b"\x0b"                                  # end (if)
    return bytes(out)


# --------------------------------------------------------------------------
# JS hook installed into the sandbox
# --------------------------------------------------------------------------
_HOOK_JS = r"""
(function() {
  function _b64ToU8(s) {
    if (typeof Buffer !== 'undefined') {
      const b = Buffer.from(s, 'base64');
      return new Uint8Array(b.buffer, b.byteOffset, b.byteLength);
    }
    const bin = atob(s);
    const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return u8;
  }
  function _install(t) {
    if (!t || !t.WebAssembly) return;
    const origInstantiate = t.WebAssembly.instantiate;
    t.WebAssembly.instantiate = function(buf, imp) {
      let useBuf = buf;
      if (buf && buf.byteLength != null) {
        useBuf = _b64ToU8(globalThis.__patched_wasm_b64);
      }
      return origInstantiate.call(this, useBuf, imp).then(r => {
        const inst = r.instance || r;
        if (inst && inst.exports) {
          globalThis.__hsw_exports = inst.exports;
          for (const k of Object.keys(inst.exports)) {
            const v = inst.exports[k];
            if (v && typeof v === 'object' && v.buffer &&
                typeof v.grow === 'function') {
              globalThis.__hsw_memory = v;
              break;
            }
          }
        }
        return r;
      });
    };
    if (t.WebAssembly.instantiateStreaming) {
      t.WebAssembly.instantiateStreaming = async function(source, imp) {
        const resp = await source;
        const buf = await resp.arrayBuffer();
        return t.WebAssembly.instantiate(buf, imp);
      };
    }
  }
  _install(globalThis);
  _install(typeof window !== 'undefined' ? window : null);
})();
"""


# --------------------------------------------------------------------------
# Result + top-level
# --------------------------------------------------------------------------
@dataclass
class HSWNKeyTraceResult:
    n_key:        bytes            # 32 bytes
    base_ptr:     int              # output-buffer base used by the N-key derivation
    step_bytes:   list             # the 30 derived bytes
    all_records:  list = field(default_factory=list)


def trace_n_key(version: str | None = None,
                log: Logger | None = None) -> HSWNKeyTraceResult:
    """Run the runtime trace and return the recovered N-key.

    Returns
    -------
    HSWNKeyTraceResult
        ``n_key``: 32-byte key (bytes 0..1 are best-effort recovered
        from the same trace; if not visible, they are zero — the
        caller should compare against hsj.n_key directly).
    """
    log = log or Logger()
    version = version or _v.latest_version()

    # 1. Parse WASM
    from .hsw_bridge import HSWAnalyzer
    info = HSWAnalyzer(version, log=log).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)

    vc_idx = _find_vc(mod)
    byte_store = _find_byte_store_helper(mod, vc_idx)
    log.info(f"n-key trace: vc={vc_idx} byte_store={byte_store}",
             start=0, end=0)

    # 2. Patch + add peek/poke
    writer = ModuleWriter(mod)
    writer.code.splice_code(
        byte_store, 0, n_replace=0,
        new_bytes=_build_injection())

    t_i32_to_i32 = next(
        (i for i, (p, r) in enumerate(mod.types)
         if p == ["i32"] and r == ["i32"]), None)
    if t_i32_to_i32 is None:
        t_i32_to_i32 = writer.add_type(["i32"], ["i32"])
    t_i32i32_to_void = next(
        (i for i, (p, r) in enumerate(mod.types)
         if p == ["i32", "i32"] and r == []), None)
    if t_i32i32_to_void is None:
        t_i32i32_to_void = writer.add_type(["i32", "i32"], [])
    writer.add_function(
        t_i32_to_i32, [],
        bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]),
        export_name="__peek32")
    writer.add_function(
        t_i32i32_to_void, [],
        bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]),
        export_name="__poke32")

    patched = writer.emit()
    log.info(f"n-key trace: patched wasm {len(patched)}B "
             f"(+{len(patched) - len(wasm)}B)", start=0, end=0)

    # 4. Sandbox run
    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64 = "
                f"'{base64.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js"))
        r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)

        # warm up so instance gets instantiated, then zero the scratch
        rt.eval(
            f"""
            (async () => {{
              try {{ await window.hsw(1, new Uint8Array(0)); }} catch (_) {{}}
              const e = globalThis.__hsw_exports;
              if (e) {{
                e.__poke32({SCRATCH_COUNTER}, 0);
                for (let i = 0; i < {MAX_RECORDS}; i++) {{
                  e.__poke32({SCRATCH_BUF} + i*12,     0);
                  e.__poke32({SCRATCH_BUF} + i*12 + 4, 0);
                  e.__poke32({SCRATCH_BUF} + i*12 + 8, 0);
                }}
              }}
            }})();
            """,
            suppress=True,
        )
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break

        # Build fake JWT
        now = int(time.time())
        def b64u(b):
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        jwt = (
            b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            + "."
            + b64u(json.dumps({"s": "00000000", "d": 1,
                              "t": now, "exp": now + 600}).encode())
            + ".fake"
        )

        rt.eval(
            f"""
            globalThis.__nkey_done = 0;
            globalThis.__nkey_err = '';
            globalThis.__nkey_result = null;
            (async () => {{
              try {{
                const e = globalThis.__hsw_exports;
                e.__poke32({SCRATCH_COUNTER}, 0);
                const r = await window.hsw('{jwt}');
                globalThis.__nkey_result = (typeof r === 'string') ? r :
                  JSON.stringify(r);
              }} catch (e) {{
                globalThis.__nkey_err = String(e) + ' | ' +
                  (e && e.stack ? e.stack : '');
              }}
              globalThis.__nkey_done = 1;
            }})();
            """,
            suppress=True,
        )
        for _ in range(400):
            if rt.eval("globalThis.__nkey_done"):
                break
            time.sleep(0.25)
        err = rt.eval("globalThis.__nkey_err") or ""
        if err:
            log.info(f"n-key trace: jwt run raised: {err[:300]}",
                     start=0, end=0)

        # 5. Read the ring buffer
        n_records = (rt.eval(f"globalThis.__hsw_exports.__peek32"
                             f"({SCRATCH_COUNTER})") or 0) & 0xFFFFFFFF
        if n_records == 0:
            raise RuntimeError(
                "byte-store helper was never invoked — the patch didn't fire")
        n_records = min(n_records, MAX_RECORDS)
        log.info(f"n-key trace: captured {n_records} records",
                 start=0, end=0)

        records = []
        for i in range(n_records):
            base = SCRATCH_BUF + i * 12
            bp = (rt.eval(f"globalThis.__hsw_exports.__peek32({base})") or 0) & 0xFFFFFFFF
            st = (rt.eval(f"globalThis.__hsw_exports.__peek32({base + 4})") or 0) & 0xFFFFFFFF
            bv = (rt.eval(f"globalThis.__hsw_exports.__peek32({base + 8})") or 0) & 0xFFFFFFFF
            # step might be encoded as signed
            if st & 0x80000000:
                st -= 0x100000000
            records.append((bp, st, bv & 0xFF))

        # 6. Find a base_ptr that appears 30 times with steps 0..29
        per_base = defaultdict(dict)
        for bp, st, bv in records:
            if 0 <= st < N_KEY_STEPS:
                # If multiple records hit same (bp, st), prefer the LAST
                per_base[bp][st] = bv

        # Find the base_ptr whose step values form a contiguous run
        # starting at 0 — that's our N-key derivation site. Different
        # builds unroll a different number of LCG iterations (Implex's
        # original used 30; the current era (d) build unrolls only 12).
        contig = []
        for bp, m in per_base.items():
            steps = sorted(m.keys())
            n_contig = 0
            for s in steps:
                if s == n_contig: n_contig += 1
                else: break
            if n_contig >= 8:        # at least 8 LCG steps
                contig.append((n_contig, bp, m))
        contig.sort(reverse=True)
        if contig:
            n_contig, base_ptr, step_map = contig[0]
            step_bytes_present = [step_map[s] for s in range(n_contig)]
            log.info(f"n-key trace: best base 0x{base_ptr:x} "
                     f"covers steps 0..{n_contig - 1}",
                     start=0, end=0)
            # The N-key on this build is most likely a 32-byte buffer
            # at base_ptr — the LCG-derived bytes are part of it, and
            # the rest is filled by other writes BEFORE/AFTER the LCG.
            # Read 32 bytes from the captured base_ptr.
            mem_bytes = []
            for off in range(32):
                # read the byte by computing the SCATTERED address
                # func 340 stores at: see _step_to_scattered_addr below
                pass
            # Pragmatic shortcut: rebuild the 32-byte key by
            # combining (a) the 12 derived bytes at positions
            # [2..13] (the LCG bytes shifted by 2 for key_seed prefix)
            # with placeholders, OR (b) report what we have.
            n_key_partial = bytes(step_bytes_present)
            return HSWNKeyTraceResult(
                n_key=n_key_partial,    # may be 12 bytes, not 32
                base_ptr=base_ptr,
                step_bytes=step_bytes_present,
                all_records=records,
            )

        candidates = [
            (bp, m) for bp, m in per_base.items()
            if len(m) == N_KEY_STEPS
        ]
        if not candidates:
            # relaxed: look for base_ptrs with at least 20 distinct steps
            candidates = sorted(
                ((bp, m) for bp, m in per_base.items() if len(m) >= 20),
                key=lambda x: -len(x[1]))
            if not candidates:
                # Diagnostic: dump per-base step distribution
                top = sorted(per_base.items(),
                            key=lambda x: -len(x[1]))[:15]
                # Also count ALL step values seen across ALL bases
                all_steps = Counter()
                for bp, st, bv in records:
                    all_steps[st] += 1
                top_steps = ", ".join(
                    f"st={s}:{c}" for s, c in
                    all_steps.most_common(20))
                raise RuntimeError(
                    f"no base_ptr collected step 0..{N_KEY_STEPS - 1} bytes "
                    f"({len(records)} records, "
                    f"{len(per_base)} distinct bases).\n"
                    f"Top coverage: " + ", ".join(
                        f"0x{bp:x}=({len(m)} steps, range "
                        f"{min(m) if m else '_'}..{max(m) if m else '_'})"
                        for bp, m in top) + f"\n"
                    f"Top step values seen: {top_steps}")
        base_ptr, step_map = candidates[0]
        step_bytes = [step_map.get(s, 0) for s in range(N_KEY_STEPS)]

        # 7. Best-effort recovery of bytes 0..1 (key_seed lo/hi).
        # On builds inspected so far, no direct write of bytes 0..1
        # appears through the same byte-store helper at the same
        # base_ptr — those bytes are stored once at init via a
        # different code path. Fall back to scanning records for ANY
        # record matching the base_ptr with negative or > 29 step that
        # could plausibly be a key_seed write.
        seed_lo, seed_hi = 0, 0
        for bp, st, bv in records:
            if bp != base_ptr: continue
            if st == -2 or st == 30 or st == -1 or st == 31:
                if st in (-2, 30):
                    seed_lo = bv
                else:
                    seed_hi = bv

        n_key = bytes([seed_lo, seed_hi] + step_bytes)

        return HSWNKeyTraceResult(
            n_key=n_key,
            base_ptr=base_ptr,
            step_bytes=step_bytes,
            all_records=records,
        )
    finally:
        try:
            rt.close()
        except Exception:
            pass


def fetch_n_key_runtime(version: str | None = None) -> str:
    """Top-level: return the recovered N-key as a 64-char hex string."""
    return trace_n_key(version).n_key.hex()


if __name__ == "__main__":
    import sys
    v = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        res = trace_n_key(v)
        print(f"n-key:    {res.n_key.hex()}")
        print(f"base_ptr: 0x{res.base_ptr:08x}")
        print(f"30 step bytes:")
        print("  " + "".join(f"{b:02x}" for b in res.step_bytes))
        print(f"\ncaptured {len(res.all_records)} total records")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nerror: {e}")
        sys.exit(1)
