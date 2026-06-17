"""Faithful Python port of RustCrypto `aes/src/soft/fixslice32.rs`
(non-compact backend). Ported VERBATIM from the upstream source so the
round-key storage + encrypt match the bundle's AES exactly.

Validated by a known-answer test against pycryptodome (see _selftest).
"""
from __future__ import annotations
import struct

M32 = 0xFFFFFFFF


def _u32(x): return x & M32
def ror(x, y): y &= 31; return ((x >> y) | (x << (32 - y))) & M32 if y else x & M32
def ror_distance(rows, cols): return (rows << 3) + (cols << 1)


def delta_swap_1(a, shift, mask):
    t = (a ^ (a >> shift)) & mask
    return _u32(a ^ t ^ (t << shift))


def delta_swap_2(a, b, shift, mask):
    t = (a ^ (b >> shift)) & mask
    return _u32(a ^ t), _u32(b ^ (t << shift))


def bitslice(in0: bytes, in1: bytes):
    """8 u32 from two 16-byte blocks (RustCrypto bitslice)."""
    t0 = struct.unpack("<I", in0[0:4])[0]
    t2 = struct.unpack("<I", in0[4:8])[0]
    t4 = struct.unpack("<I", in0[8:12])[0]
    t6 = struct.unpack("<I", in0[12:16])[0]
    t1 = struct.unpack("<I", in1[0:4])[0]
    t3 = struct.unpack("<I", in1[4:8])[0]
    t5 = struct.unpack("<I", in1[8:12])[0]
    t7 = struct.unpack("<I", in1[12:16])[0]
    t1, t0 = delta_swap_2(t1, t0, 1, 0x55555555)
    t3, t2 = delta_swap_2(t3, t2, 1, 0x55555555)
    t5, t4 = delta_swap_2(t5, t4, 1, 0x55555555)
    t7, t6 = delta_swap_2(t7, t6, 1, 0x55555555)
    t2, t0 = delta_swap_2(t2, t0, 2, 0x33333333)
    t3, t1 = delta_swap_2(t3, t1, 2, 0x33333333)
    t6, t4 = delta_swap_2(t6, t4, 2, 0x33333333)
    t7, t5 = delta_swap_2(t7, t5, 2, 0x33333333)
    t4, t0 = delta_swap_2(t4, t0, 4, 0x0F0F0F0F)
    t5, t1 = delta_swap_2(t5, t1, 4, 0x0F0F0F0F)
    t6, t2 = delta_swap_2(t6, t2, 4, 0x0F0F0F0F)
    t7, t3 = delta_swap_2(t7, t3, 4, 0x0F0F0F0F)
    return [t0, t1, t2, t3, t4, t5, t6, t7]


def inv_bitslice(st):
    t0, t1, t2, t3, t4, t5, t6, t7 = st
    t1, t0 = delta_swap_2(t1, t0, 1, 0x55555555)
    t3, t2 = delta_swap_2(t3, t2, 1, 0x55555555)
    t5, t4 = delta_swap_2(t5, t4, 1, 0x55555555)
    t7, t6 = delta_swap_2(t7, t6, 1, 0x55555555)
    t2, t0 = delta_swap_2(t2, t0, 2, 0x33333333)
    t3, t1 = delta_swap_2(t3, t1, 2, 0x33333333)
    t6, t4 = delta_swap_2(t6, t4, 2, 0x33333333)
    t7, t5 = delta_swap_2(t7, t5, 2, 0x33333333)
    t4, t0 = delta_swap_2(t4, t0, 4, 0x0F0F0F0F)
    t5, t1 = delta_swap_2(t5, t1, 4, 0x0F0F0F0F)
    t6, t2 = delta_swap_2(t6, t2, 4, 0x0F0F0F0F)
    t7, t3 = delta_swap_2(t7, t3, 4, 0x0F0F0F0F)
    a = struct.pack("<I", t0) + struct.pack("<I", t2) + struct.pack("<I", t4) + struct.pack("<I", t6)
    b = struct.pack("<I", t1) + struct.pack("<I", t3) + struct.pack("<I", t5) + struct.pack("<I", t7)
    return a, b


def sub_bytes(s):
    u7, u6, u5, u4, u3, u2, u1, u0 = s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7]
    y14 = u3 ^ u5; y13 = u0 ^ u6; y12 = y13 ^ y14; t1 = u4 ^ y12; y15 = t1 ^ u5
    t2 = y12 & y15; y6 = y15 ^ u7; y20 = t1 ^ u1; y9 = u0 ^ u3; y11 = y20 ^ y9
    t12 = y9 & y11; y7 = u7 ^ y11; y8 = u0 ^ u5; t0 = u1 ^ u2; y10 = y15 ^ t0
    y17 = y10 ^ y11; t13 = y14 & y17; t14 = t13 ^ t12; y19 = y10 ^ y8; t15 = y8 & y10
    t16 = t15 ^ t12; y16 = t0 ^ y11; y21 = y13 ^ y16; t7 = y13 & y16; y18 = u0 ^ y16
    y1 = t0 ^ u7; y4 = y1 ^ u3; t5 = y4 & u7; t6 = t5 ^ t2; t18 = t6 ^ t16
    t22 = t18 ^ y19; y2 = y1 ^ u0; t10 = y2 & y7; t11 = t10 ^ t7; t20 = t11 ^ t16
    t24 = t20 ^ y18; y5 = y1 ^ u6; t8 = y5 & y1; t9 = t8 ^ t7; t19 = t9 ^ t14
    t23 = t19 ^ y21; y3 = y5 ^ y8; t3 = y3 & y6; t4 = t3 ^ t2; t17 = t4 ^ y20
    t21 = t17 ^ t14; t26 = t21 & t23; t27 = t24 ^ t26; t31 = t22 ^ t26; t25 = t21 ^ t22
    t28 = t25 & t27; t29 = t28 ^ t22; z14 = t29 & y2; z5 = t29 & y7; t30 = t23 ^ t24
    t32 = t31 & t30; t33 = t32 ^ t24; t35 = t27 ^ t33; t36 = t24 & t35; t38 = t27 ^ t36
    t39 = t29 & t38; t40 = t25 ^ t39; t43 = t29 ^ t40; z3 = t43 & y16; tc12 = z3 ^ z5
    z12 = t43 & y13; z13 = t40 & y5; z4 = t40 & y1; tc6 = z3 ^ z4; t34 = t23 ^ t33
    t37 = t36 ^ t34; t41 = t40 ^ t37; z8 = t41 & y10; z17 = t41 & y8; t44 = t33 ^ t37
    z0 = t44 & y15; z9 = t44 & y12; z10 = t37 & y3; z1 = t37 & y6; tc5 = z1 ^ z0
    tc11 = tc6 ^ tc5; z11 = t33 & y4; t42 = t29 ^ t33; t45 = t42 ^ t41; z7 = t45 & y17
    tc8 = z7 ^ tc6; z16 = t45 & y14; z6 = t42 & y11; tc16 = z6 ^ tc8; z15 = t42 & y9
    tc20 = z15 ^ tc16; tc1 = z15 ^ z16; tc2 = z10 ^ tc1; tc21 = tc2 ^ z11; tc3 = z9 ^ tc2
    s0 = tc3 ^ tc16; s3 = tc3 ^ tc11; s1 = s3 ^ tc16; tc13 = z13 ^ tc1; z2 = t33 & u7
    tc4 = z0 ^ z2; tc7 = z12 ^ tc4; tc9 = z8 ^ tc7; tc10 = tc8 ^ tc9; tc17 = z14 ^ tc10
    s5 = tc21 ^ tc17; tc26 = tc17 ^ tc20; s2 = tc26 ^ z17; tc14 = tc4 ^ tc12
    tc18 = tc13 ^ tc14; s6 = tc10 ^ tc18; s7 = z12 ^ tc18; s4 = tc14 ^ s3
    s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7] = (_u32(s7), _u32(s6), _u32(s5),
                                                      _u32(s4), _u32(s3), _u32(s2),
                                                      _u32(s1), _u32(s0))


def sub_bytes_nots(s):
    s[0] ^= M32; s[1] ^= M32; s[5] ^= M32; s[6] ^= M32


def shift_rows_1(s):
    for i in range(8):
        x = delta_swap_1(s[i], 4, 0x0C0F0300)
        s[i] = delta_swap_1(x, 2, 0x33003300)


def shift_rows_2(s):
    for i in range(8):
        s[i] = delta_swap_1(s[i], 4, 0x0F000F00)


def shift_rows_3(s):
    for i in range(8):
        x = delta_swap_1(s[i], 4, 0x030F0C00)
        s[i] = delta_swap_1(x, 2, 0x33003300)


def inv_shift_rows_1(s): shift_rows_3(s)
def inv_shift_rows_2(s): shift_rows_2(s)
def inv_shift_rows_3(s): shift_rows_1(s)


def rotate_rows_1(x): return ror(x, ror_distance(1, 0))
def rotate_rows_2(x): return ror(x, ror_distance(2, 0))
def rrc_1_1(x): return (ror(x, ror_distance(1, 1)) & 0x3F3F3F3F) | (ror(x, ror_distance(0, 1)) & 0xC0C0C0C0)
def rrc_1_2(x): return (ror(x, ror_distance(1, 2)) & 0x0F0F0F0F) | (ror(x, ror_distance(0, 2)) & 0xF0F0F0F0)
def rrc_1_3(x): return (ror(x, ror_distance(1, 3)) & 0x03030303) | (ror(x, ror_distance(0, 3)) & 0xFCFCFCFC)
def rrc_2_2(x): return (ror(x, ror_distance(2, 2)) & 0x0F0F0F0F) | (ror(x, ror_distance(1, 2)) & 0xF0F0F0F0)


def _mix(s, first, second):
    a = list(s)
    b = [first(a[i]) for i in range(8)]
    c = [_u32(a[i] ^ b[i]) for i in range(8)]
    s[0] = _u32(b[0]        ^ c[7] ^ second(c[0]))
    s[1] = _u32(b[1] ^ c[0] ^ c[7] ^ second(c[1]))
    s[2] = _u32(b[2] ^ c[1]        ^ second(c[2]))
    s[3] = _u32(b[3] ^ c[2] ^ c[7] ^ second(c[3]))
    s[4] = _u32(b[4] ^ c[3] ^ c[7] ^ second(c[4]))
    s[5] = _u32(b[5] ^ c[4]        ^ second(c[5]))
    s[6] = _u32(b[6] ^ c[5]        ^ second(c[6]))
    s[7] = _u32(b[7] ^ c[6]        ^ second(c[7]))


def mix_columns_0(s): _mix(s, rotate_rows_1, rotate_rows_2)
def mix_columns_1(s): _mix(s, rrc_1_1, rrc_2_2)
def mix_columns_2(s): _mix(s, rrc_1_2, rotate_rows_2)
def mix_columns_3(s): _mix(s, rrc_1_3, rrc_2_2)


def add_round_key(s, rk):
    for i in range(8):
        s[i] = _u32(s[i] ^ rk[i])


def memshift32(rk, src):
    for i in range(8):
        rk[src + 8 + i] = rk[src + i]


def add_round_constant_bit(rk, off, bit):
    rk[off + bit] = _u32(rk[off + bit] ^ 0x0000C000)


def xor_columns(rk, offset, idx_xor, idx_ror):
    for i in range(8):
        oi = offset + i
        r = _u32(rk[oi - idx_xor] ^ (0x03030303 & ror(rk[oi], idx_ror)))
        rk[oi] = _u32(r ^ (0xFCFCFCFC & (r << 2)) ^ (0xF0F0F0F0 & (r << 4)) ^ (0xC0C0C0C0 & (r << 6)))


def aes256_key_schedule(key: bytes):
    assert len(key) == 32
    rk = [0] * 120
    rk[0:8] = bitslice(key[0:16], key[0:16])
    rk[8:16] = bitslice(key[16:32], key[16:32])
    rk_off = 8
    rcon = 0
    while True:
        memshift32(rk, rk_off); rk_off += 8
        ch = rk[rk_off:rk_off + 8]; sub_bytes(ch); sub_bytes_nots(ch); rk[rk_off:rk_off + 8] = ch
        add_round_constant_bit(rk, rk_off, rcon)
        xor_columns(rk, rk_off, 16, ror_distance(1, 3))
        rcon += 1
        if rcon == 7:
            break
        memshift32(rk, rk_off); rk_off += 8
        ch = rk[rk_off:rk_off + 8]; sub_bytes(ch); sub_bytes_nots(ch); rk[rk_off:rk_off + 8] = ch
        xor_columns(rk, rk_off, 16, ror_distance(0, 3))
    for i in range(8, 104, 32):
        c = rk[i:i + 8]; inv_shift_rows_1(c); rk[i:i + 8] = c
        c = rk[i + 8:i + 16]; inv_shift_rows_2(c); rk[i + 8:i + 16] = c
        c = rk[i + 16:i + 24]; inv_shift_rows_3(c); rk[i + 16:i + 24] = c
    c = rk[104:112]; inv_shift_rows_1(c); rk[104:112] = c
    for i in range(1, 15):
        c = rk[i * 8:i * 8 + 8]; sub_bytes_nots(c); rk[i * 8:i * 8 + 8] = c
    return rk


def aes256_encrypt(rk, blk0: bytes, blk1: bytes):
    """Encrypt two 16-byte blocks. rk is the 120-u32 FixsliceKeys256."""
    st = bitslice(blk0, blk1)
    add_round_key(st, rk[0:8])
    rk_off = 8
    while True:
        sub_bytes(st); mix_columns_1(st); add_round_key(st, rk[rk_off:rk_off + 8]); rk_off += 8
        if rk_off == 112:
            break
        sub_bytes(st); mix_columns_2(st); add_round_key(st, rk[rk_off:rk_off + 8]); rk_off += 8
        sub_bytes(st); mix_columns_3(st); add_round_key(st, rk[rk_off:rk_off + 8]); rk_off += 8
        sub_bytes(st); mix_columns_0(st); add_round_key(st, rk[rk_off:rk_off + 8]); rk_off += 8
    shift_rows_2(st)
    sub_bytes(st)
    add_round_key(st, rk[112:120])
    return inv_bitslice(st)


def _selftest():
    from Crypto.Cipher import AES
    import os
    for _ in range(20):
        key = os.urandom(32); a = os.urandom(16); b = os.urandom(16)
        rk = aes256_key_schedule(key)
        oa, ob = aes256_encrypt(rk, a, b)
        ref = AES.new(key, AES.MODE_ECB)
        assert oa == ref.encrypt(a), "block A mismatch"
        assert ob == ref.encrypt(b), "block B mismatch"
    print("fixslice_ref self-test PASS (matches pycryptodome AES-256-ECB)")


if __name__ == "__main__":
    _selftest()
