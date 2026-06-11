"""Brute AES-CFB-8 and CFB-128 with master key + various IV paddings."""
import sys, json, base64
sys.stdout.reconfigure(encoding='utf-8')
from Crypto.Cipher import AES

master = bytes.fromhex('1bf04f88ca73b3486d0d4e0886336c35565f9907bb249ce2fab518fb296c8560')

d = json.load(open('current_capture.json'))
raw = base64.urlsafe_b64decode(d['token'] + '=' * (-len(d['token']) % 4))
L = len(raw)
N = L - 16 - 12 - 1
ct = raw[:N]
iv = raw[N+16:N+16+12]

def looks_meaningful(pt, max_check=200):
    if not pt: return 0
    sample = pt[:max_check]
    return sum(1 for b in sample if 32 <= b < 127)

iv_variants = [
    ('iv + 0000', iv + b'\x00' * 4),
    ('iv + 0001', iv + b'\x00\x00\x00\x01'),
    ('iv + 0002', iv + b'\x00\x00\x00\x02'),
    ('iv + 1000', iv + b'\x00\x00\x00\x10'),
    ('iv + 0010', iv + b'\x00\x00\x10\x00'),
    ('iv + 0x80*4', iv + b'\x80' * 4),
    ('iv + ff*4', iv + b'\xff' * 4),
    ('0000 + iv', b'\x00' * 4 + iv),
    ('0001 + iv', b'\x00\x00\x00\x01' + iv),
    ('iv[::-1] + 0000', iv[::-1] + b'\x00' * 4),
    ('iv[::-1] + 0001', iv[::-1] + b'\x00\x00\x00\x01'),
    ('iv[:8] + 0000 + iv[8:]', iv[:8] + b'\x00' * 4 + iv[8:]),
    ('iv[:4] + iv', iv[:4] + iv),
    ('iv + iv[:4]', iv + iv[:4]),
    ('ver*4 + iv', b'\x02' * 4 + iv),
    ('iv + ver*4', iv + b'\x02' * 4),
]

for seg_name, seg_size in [('CFB-8', 8), ('CFB-128', 128)]:
    print(f'=== AES-{seg_name} brute ===')
    best = []
    for iv_name, iv16 in iv_variants:
        try:
            c = AES.new(master, AES.MODE_CFB, iv=iv16, segment_size=seg_size)
            pt = c.decrypt(ct)
            score = looks_meaningful(pt, 100)
            best.append((score, iv_name, pt[:60]))
        except Exception as e:
            pass
    best.sort(reverse=True)
    print(f'  top 5 by printable score:')
    for s, name, pt in best[:5]:
        print(f'    score={s} iv={name}: {pt!r}')
    print()
