"""Execute fn 548 (AES block encrypt) directly via wasmtime to verify it
matches standard AES-256. Provides stubs for the 164 JS imports and adds
an exported wrapper that calls fn 548 with controlled memory pointers."""
import sys, struct
sys.stdout.reconfigure(encoding='utf-8')

import wasmtime
from hcaptcha.tools.wasm_disasm import WasmModule
from hcaptcha.tools.wasm_writer import ModuleWriter
from hcaptcha.tools.fixslice_inverse import bitslice, inv_bitslice
from Crypto.Cipher import AES

# AES KS for verification
SBOX = [
0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16,
]
RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36,0x6c,0xd8,0xab,0x4d]
def sb(w): return ((SBOX[(w>>24)&0xFF]<<24)|(SBOX[(w>>16)&0xFF]<<16)|(SBOX[(w>>8)&0xFF]<<8)|SBOX[w&0xFF])
def rw(w): return ((w<<8)&0xFFFFFFFF)|(w>>24)
def expand(key):
    Nk=8; Nr=14
    W=list(struct.unpack(f">{Nk}I", key))
    for i in range(Nk, (Nr+1)*4):
        t=W[i-1]
        if i%Nk==0: t=sb(rw(t))^(RCON[i//Nk-1]<<24)
        elif i%Nk==4: t=sb(t)
        W.append(W[i-Nk]^t)
    return [struct.pack(">4I",*W[i:i+4]) for i in range(0,len(W),4)]

def encode_uleb(n):
    out = b''
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out

wasm = open('current_hsw.wasm', 'rb').read()
mod = WasmModule(wasm)
writer = ModuleWriter(mod)

t_void_2arg = next((i for i,(p,r) in enumerate(mod.types) if p==['i32','i32'] and r==[]), None)
if t_void_2arg is None:
    t_void_2arg = writer.add_type(['i32','i32'], [])

# Wrapper: local.get 0; local.get 1; call 548; end
body = b'\x20\x00\x20\x01\x10' + encode_uleb(548) + b'\x0b'
writer.add_function(t_void_2arg, [], body, export_name='__call_548')

patched = writer.emit()
print(f'patched: {len(patched)} bytes')

engine = wasmtime.Engine()
store = wasmtime.Store(engine)
module = wasmtime.Module(engine, patched)

def make_stub(ft):
    results = list(ft.results)
    def stub(*args):
        if not results: return None
        if len(results) == 1:
            r = results[0]
            if r == wasmtime.ValType.i32(): return 0
            if r == wasmtime.ValType.i64(): return 0
            if r == wasmtime.ValType.f32(): return 0.0
            if r == wasmtime.ValType.f64(): return 0.0
        return tuple(0 for _ in results)
    return stub

linker = wasmtime.Linker(engine)
for imp in module.imports:
    ft = imp.type
    stub = wasmtime.Func(store, ft, make_stub(ft))
    linker.define(store, imp.module, imp.name, stub)

print('Instantiating...')
instance = linker.instantiate(store, module)
exports_d = {}
for exp in module.exports:
    e = instance.exports(store).get(exp.name)
    exports_d[exp.name] = e

print(f'Got exports: {list(exports_d.keys())}')
test548 = exports_d.get('__call_548')
mem = exports_d.get('rc')  # memory exported as 'rc' (not 'mc'!)
print(f'__call_548: {test548}')
print(f'mc (memory): {mem}')

if not test548 or not mem:
    print('FATAL: missing required exports')
    sys.exit(1)

# Compute round keys for master = 1bf04f88...
master = bytes.fromhex('1bf04f88ca73b3486d0d4e0886336c35565f9907bb249ce2fab518fb296c8560')
rks = expand(master)
full_bs_rks = b''
for i in range(0, 14, 2):
    full_bs_rks += struct.pack('<8I', *bitslice(rks[i] + rks[i+1]))
# rk14 alone — duplicate it for fixslice
full_bs_rks += struct.pack('<8I', *bitslice(rks[14] + rks[14]))
print(f'full bitsliced round keys: {len(full_bs_rks)}B')

# Test input: known plaintext "AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH"
plaintext = b'AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHH'
plaintext_bs = struct.pack('<8I', *bitslice(plaintext))

# Write to memory
mem_data = mem.data_ptr(store)
mem_size = mem.data_len(store)
print(f'Memory: {mem_size} bytes available')

# Address layout: arg0 (round keys) at 1000, arg1 (input plaintext) at 2000
for i, b in enumerate(full_bs_rks):
    mem_data[1000 + i] = b
for i, b in enumerate(plaintext_bs):
    mem_data[2000 + i] = b

# Save expected outputs (standard AES-256-ECB)
c_std = AES.new(master, AES.MODE_ECB)
expected_A = c_std.encrypt(plaintext[:16])
expected_B = c_std.encrypt(plaintext[16:])
print(f'standard AES-256(blk_A): {expected_A.hex()}')
print(f'standard AES-256(blk_B): {expected_B.hex()}')

print('Calling __call_548(1000, 2000)...')
try:
    test548(store, 1000, 2000)
    print('call succeeded!')
except wasmtime.Trap as e:
    print(f'TRAP: {e}')
except Exception as e:
    print(f'failed: {type(e).__name__}: {e}')
    sys.exit(1)

# Read output from memory
# Possible locations: 1000 (overwritten round keys), 2000 (overwritten input),
# or somewhere else. Dump several windows.
for addr in [1000, 2000, 3000, 4000]:
    region = bytes(mem_data[addr:addr+64])
    print(f'mem[{addr}..{addr+64}]: {region.hex()}')

# Try interpreting mem[2000..2032] as bitsliced output
out_bs = bytes(mem_data[2000:2032])
try:
    out_raw = inv_bitslice(list(struct.unpack('<8I', out_bs)))
    print(f'inv_bitslice(mem[2000..2032]): {out_raw.hex()}')
    print(f'  matches expected_A||expected_B?: {out_raw == expected_A + expected_B}')
except Exception as e:
    print(f'inv_bitslice err: {e}')
