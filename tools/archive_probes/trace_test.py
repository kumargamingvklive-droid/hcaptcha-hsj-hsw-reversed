"""Test extended n-key tracer that captures both LCG byte stores AND
func 203 i64 stores. Aims to recover all 32 bytes."""

import base64, json, time, requests, sys
from collections import defaultdict, Counter
from hcaptcha.tools.wasm_disasm import WasmModule, find_data_segment_at
from hcaptcha.tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
from hcaptcha.tools.js_runtime import JsRuntime
from hcaptcha.hsw_bridge import HSWAnalyzer
from hcaptcha import version as _v
from hcaptcha.hsw_n_key_runtime import _HOOK_JS

# Old LCG patch slots
SCRATCH_COUNTER = 60_000
SCRATCH_BUF = 60_004
RECORD_SIZE = 12
MAX_RECORDS = 4000

# 203 patch slots (separate region)
SCRATCH_COUNTER_203 = 200_000
SCRATCH_BUF_203 = 200_004
RECORD_SIZE_203 = 24
MAX_203 = 400

# Temp scratch
TMP_C_LCG = 220_000
TMP_A_LCG = 220_004
TMP_C_203 = 220_016
TMP_A_203 = 220_020


def _build_lcg_prologue():
    """Inject at func 340 (byte-store helper) entry.
    Locals: 0=byte_val(i32), 1=base(i32), 2=step(i32)
    Records 12B (base, step, byte) in LCG ring buffer."""
    out = bytearray()
    out += b"\x41" + encode_sleb(TMP_C_LCG)
    out += b"\x41" + encode_sleb(SCRATCH_COUNTER)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    out += b"\x41" + encode_sleb(TMP_C_LCG)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_RECORDS)
    out += b"\x49"
    out += b"\x04\x40"
    out += b"\x41" + encode_sleb(TMP_A_LCG)
    out += b"\x41" + encode_sleb(TMP_C_LCG)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(12)
    out += b"\x6c"
    out += b"\x41" + encode_sleb(SCRATCH_BUF)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    # base
    out += b"\x41" + encode_sleb(TMP_A_LCG)
    out += b"\x28\x02\x00"
    out += b"\x20\x01"
    out += b"\x36\x02\x00"
    # step
    out += b"\x41" + encode_sleb(TMP_A_LCG)
    out += b"\x28\x02\x00"
    out += b"\x20\x02"
    out += b"\x36\x02\x04"
    # byte_val
    out += b"\x41" + encode_sleb(TMP_A_LCG)
    out += b"\x28\x02\x00"
    out += b"\x20\x00"
    out += b"\x36\x02\x08"
    # counter++
    out += b"\x41" + encode_sleb(SCRATCH_COUNTER)
    out += b"\x41" + encode_sleb(TMP_C_LCG)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    out += b"\x0b"
    return bytes(out)


def _build_203_prologue():
    """Inject at func 203 entry.
    Locals: 0=val(i64), 1=?, 2=base(i32), 3=f32, 4=f64, 5=off(i32)
    Records 24B (base, off, val_i64) in 203 ring buffer."""
    out = bytearray()
    out += b"\x41" + encode_sleb(TMP_C_203)
    out += b"\x41" + encode_sleb(SCRATCH_COUNTER_203)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    out += b"\x41" + encode_sleb(TMP_C_203)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(MAX_203)
    out += b"\x49"
    out += b"\x04\x40"
    out += b"\x41" + encode_sleb(TMP_A_203)
    out += b"\x41" + encode_sleb(TMP_C_203)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(RECORD_SIZE_203)
    out += b"\x6c"
    out += b"\x41" + encode_sleb(SCRATCH_BUF_203)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    # base
    out += b"\x41" + encode_sleb(TMP_A_203)
    out += b"\x28\x02\x00"
    out += b"\x20\x02"
    out += b"\x36\x02\x00"
    # off
    out += b"\x41" + encode_sleb(TMP_A_203)
    out += b"\x28\x02\x00"
    out += b"\x20\x05"
    out += b"\x36\x02\x04"
    # val i64
    out += b"\x41" + encode_sleb(TMP_A_203)
    out += b"\x28\x02\x00"
    out += b"\x20\x00"
    out += b"\x37\x03\x08"
    # counter++
    out += b"\x41" + encode_sleb(SCRATCH_COUNTER_203)
    out += b"\x41" + encode_sleb(TMP_C_203)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    out += b"\x0b"
    return bytes(out)


def main(timestamp_offset=0):
    version = _v.latest_version()
    info = HSWAnalyzer(version).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)
    print(f'wasm loaded: {len(wasm)}B  funcs: {len(mod.functions)}')

    # Read the rodata at vaddr 626 (mask table for func 203)
    mask_table = find_data_segment_at(mod, 626, 256)
    if mask_table is None:
        print("WARN: no rodata at vaddr 626")
        return None
    print(f'mask table @626: {len(mask_table)}B  first16={mask_table[:16].hex()}')

    writer = ModuleWriter(mod)
    writer.code.splice_code(340, 0, n_replace=0, new_bytes=_build_lcg_prologue())
    writer.code.splice_code(203, 0, n_replace=0, new_bytes=_build_203_prologue())

    t_i32_to_i32 = next((i for i, (p, r) in enumerate(mod.types)
                         if p == ['i32'] and r == ['i32']), None)
    if t_i32_to_i32 is None:
        t_i32_to_i32 = writer.add_type(['i32'], ['i32'])
    t_i32i32_to_void = next((i for i, (p, r) in enumerate(mod.types)
                             if p == ['i32', 'i32'] and r == []), None)
    if t_i32i32_to_void is None:
        t_i32i32_to_void = writer.add_type(['i32', 'i32'], [])
    writer.add_function(t_i32_to_i32, [],
                        bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]),
                        export_name='__peek32')
    writer.add_function(t_i32i32_to_void, [],
                        bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]),
                        export_name='__poke32')

    patched = writer.emit()
    print(f'patched wasm: {len(patched)}B (+{len(patched)-len(wasm)}B)')

    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64 = '{base64.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, 'hsw.js'))
        r.encoding = 'utf-8'
        rt.eval(r.text, suppress=True)
        rt.eval(f"""(async () => {{
            try {{ await window.hsw(1, new Uint8Array(0)); }} catch(_) {{}}
        }})();""", suppress=True)
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break

        now = int(time.time()) + timestamp_offset
        def b64u(b):
            return base64.urlsafe_b64encode(b).rstrip(b'=').decode()
        jwt = (b64u(json.dumps({'alg': 'HS256', 'typ': 'JWT'}).encode()) + '.'
               + b64u(json.dumps({'s': f'{timestamp_offset:08x}', 'd': 1,
                                  't': now, 'exp': now + 600}).encode()) + '.fake')

        rt.eval(f"""
            globalThis.__nkey_done = 0;
            (async () => {{
                const e = globalThis.__hsw_exports;
                e.__poke32({SCRATCH_COUNTER}, 0);
                e.__poke32({SCRATCH_COUNTER_203}, 0);
                try {{
                    const r = await window.hsw('{jwt}');
                    globalThis.__nkey_result = String(r);
                }} catch (ex) {{ globalThis.__nkey_err = String(ex); }}
                globalThis.__nkey_done = 1;
            }})();
        """, suppress=True)
        for _ in range(400):
            if rt.eval("globalThis.__nkey_done"):
                break
            time.sleep(0.25)

        n_lcg = (rt.eval(f"globalThis.__hsw_exports.__peek32({SCRATCH_COUNTER})") or 0) & 0xFFFFFFFF
        n_203 = (rt.eval(f"globalThis.__hsw_exports.__peek32({SCRATCH_COUNTER_203})") or 0) & 0xFFFFFFFF
        print(f'records: LCG={n_lcg} 203={n_203}')

        lcg_records = []
        for i in range(min(n_lcg, MAX_RECORDS)):
            a = SCRATCH_BUF + i * 12
            bp = (rt.eval(f"globalThis.__hsw_exports.__peek32({a})") or 0) & 0xFFFFFFFF
            st = (rt.eval(f"globalThis.__hsw_exports.__peek32({a+4})") or 0) & 0xFFFFFFFF
            bv = (rt.eval(f"globalThis.__hsw_exports.__peek32({a+8})") or 0) & 0xFFFFFFFF
            if st & 0x80000000:
                st -= 0x100000000
            lcg_records.append((bp, st, bv & 0xFF))

        rec_203 = []
        for i in range(min(n_203, MAX_203)):
            a = SCRATCH_BUF_203 + i * 24
            bp = (rt.eval(f"globalThis.__hsw_exports.__peek32({a})") or 0) & 0xFFFFFFFF
            off = (rt.eval(f"globalThis.__hsw_exports.__peek32({a+4})") or 0) & 0xFFFFFFFF
            lo = (rt.eval(f"globalThis.__hsw_exports.__peek32({a+8})") or 0) & 0xFFFFFFFF
            hi = (rt.eval(f"globalThis.__hsw_exports.__peek32({a+12})") or 0) & 0xFFFFFFFF
            rec_203.append((bp, off, (hi << 32) | lo))

        # The LCG records' base appears in the first 12 records
        # Figure out which base+step pattern is the n_key.
        # Each LCG record has base = local 10 (the LCG buffer base).
        per_base = defaultdict(dict)
        for bp, st, bv in lcg_records:
            if 0 <= st < 30:
                per_base[bp][st] = bv

        # Find the base with most contiguous steps
        best = max(per_base.items(), key=lambda x: len(x[1]))
        lcg_base = best[0]
        print(f'LCG base = 0x{lcg_base:x} covers steps {sorted(best[1].keys())}')

        # Now use 203 records to find writes near lcg_base
        # Compute: for each (bp, off, val) of 203, what virtual addr is bp+off?
        # And derive the 8 bytes written.
        # XOR mask is mask_table at offset ((bp+off) % 96)
        # NB: i64.load reads 8 bytes little-endian
        writes = []
        for bp, off, val in rec_203:
            virt = bp + off
            mask_off = virt % 96
            mask = int.from_bytes(mask_table[mask_off:mask_off + 8], 'little')
            xored = val ^ mask
            # 8 bytes little-endian written to virtual addr virt..virt+7
            for i in range(8):
                b = (xored >> (8 * i)) & 0xFF
                writes.append((virt + i, b, bp, off, i))

        # Pivot writes by base. We want a base that has writes covering offsets 0..31.
        # Group by base (bp) and use off as the virtual offset within buffer.
        buf_writes = defaultdict(dict)
        for vaddr, byte, bp, off, _ in writes:
            buf_writes[bp][off + (vaddr - (bp + off))] = byte  # vaddr - (bp+off) gives byte-index within the 8-byte i64

        # Simpler: group by bp, then map off..off+7 to bytes
        buffers = defaultdict(dict)
        for bp, off, val in rec_203:
            virt = bp + off
            mask_off = virt % 96
            mask = int.from_bytes(mask_table[mask_off:mask_off + 8], 'little')
            xored = val ^ mask
            for i in range(8):
                b = (xored >> (8 * i)) & 0xFF
                buffers[bp][off + i] = b

        # Print all bases with their write coverage
        print(f'\n203 buffers (top by coverage):')
        for bp, m in sorted(buffers.items(), key=lambda x: -len(x[1]))[:10]:
            offs = sorted(m.keys())
            print(f'  bp=0x{bp:x}  {len(m)} bytes, offset range {min(offs)}..{max(offs)}')

        # Look for a buffer that contains the LCG-base address range
        for bp, m in buffers.items():
            offs = sorted(m.keys())
            # Check if writing the LCG base address?
            # actually we want to overlay LCG-write bytes (at virtual addr lcg_base + step)
            # with 203-write bytes (at virtual addr bp + off)
            # to see if any combined view forms a 32-byte buffer covering offsets 0..31
            for_test_base = lcg_base - 2  # LCG writes bytes 2..13, so n_key base is 2 before
            covers = sum(1 for st in range(2, 14) if (st + lcg_base - 2 - bp) in m)
            if covers >= 0:
                pass

        # Also: maybe lcg_base itself is the start of the n_key buffer and LCG writes 0..11 in it.
        # Combine LCG bytes (at lcg_base + step) with 203 bytes (at bp + off)
        # for various candidate bases
        # Most likely candidate: bp == lcg_base or bp + min_off == lcg_base
        # Try bp == lcg_base directly
        print(f'\nCombined view (assuming n_key starts at lcg_base 0x{lcg_base:x}):')
        full = {}
        for st in range(12):
            if st in best[1]:
                full[st] = best[1][st]
        for bp, m in buffers.items():
            for off, b in m.items():
                virt = bp + off
                rel = virt - lcg_base
                if 0 <= rel < 32:
                    full[rel] = b
        for i in range(32):
            print(f'  byte[{i:2d}] = {full.get(i, "??")}')

        return {
            'lcg_base': lcg_base,
            'lcg_bytes': best[1],
            'buffers_203': dict(buffers),
            'n_lcg': n_lcg,
            'n_203': n_203,
            'rec_203': rec_203,
        }

    finally:
        rt.close()


if __name__ == '__main__':
    r = main()
