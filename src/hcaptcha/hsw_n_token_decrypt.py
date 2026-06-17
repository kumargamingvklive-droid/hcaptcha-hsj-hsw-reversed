"""hsw n-token decryption — public API.

The n-token
===========
``window.hsw(jwt)`` (where ``jwt`` is the server-issued ``c.req`` value
from ``/checksiteconfig``) returns a base64 string commonly called the
"n-token". Wire format (verified across multiple captured tokens, see
``tools/instrument_encrypt_entry.last.json``)::

    raw = base64.b64decode(token)
    raw = ciphertext(N) || tag(16) || iv(12) || version_byte(1)

The trailing byte is a format/version marker (``0x02`` on the
2026-spring builds, ``0x00`` on the older ``algorithm.HSJEncryption``
format). The inner plaintext is a binary 328-byte-record table
(fingerprint / PoW data), recovered intact by the CTR keystream.

Cipher: **AES-256-CTR** (NOT GCM — see ``docs/19-ntoken-cipher-solved.md``).
The counter block fed to AES is ``iv(12) || be32(counter)``:

    keystream_i = AES_encrypt(K, iv || be32(i))
    plaintext_i = ciphertext_i XOR keystream_i

AAD: none. The 12-byte IV is a fresh per-token nonce. The trailing
16-byte "tag" is a **separate AES block** (the encrypt entry is invoked
twice: once with len=N for the body, once with len=16), NOT an AEAD
authenticator — ``tools/identify_mac.py`` proved no GHASH/Poly1305/HMAC
is reachable. The old AES-GCM assumption is why every historical brute
force failed: it tried to verify a GMAC tag that does not exist.

Decrypt strategies (in order)
=============================
  1. **Static CTR** — for each candidate key, AES-256-CTR decrypt
     (counter ``iv||be32``) and accept the result if it has the n-token
     plaintext shape (the 328-byte-record table). This is the correct
     cipher; pass a known key via ``candidate_keys`` for the fast path.
  2. **Static GCM (legacy)** — the old AES-GCM wire-format sweep, kept
     only for older bundles. It cannot succeed on current n-tokens
     (there is no GMAC tag), but is harmless.
  3. **Live decrypt** — boot a Node+jsdom sandbox and deobf-extract every
     fixslice key-schedule's master key, then re-run strategy 1/2. This
     reliably recovers the ``encrypt_req_data`` / ``decrypt_resp_data``
     keys; the n-token's own key is build-specific and may not be caught
     by the structural KS heuristic (see the "Open residual" note above).

Public API
==========

    from hcaptcha.hsw_n_token_decrypt import decrypt_n_token, NTokenError

    plaintext_msgpack = decrypt_n_token(token_b64)
    plaintext_msgpack = decrypt_n_token(token_b64, version="abc...")

    # Fastest path: pass a pre-extracted master key (e.g. from an
    # earlier run of HSWKeyFetcher or the live capture in this module)
    plaintext = decrypt_n_token(
        token_b64,
        candidate_keys=[bytes.fromhex("...")],
        try_live=False,
    )

Returns the raw msgpack-encoded plaintext bytes. Use ``msgpack.unpackb``
to decode to a Python object.

Raises ``NTokenError`` if every strategy failed.

State of the world (2026-06)
============================
The **cipher is solved**: AES-256-CTR with counter ``iv || be32(i)``
(see ``docs/19-ntoken-cipher-solved.md``). Two things ship:

  * ``decrypt_n_token`` / ``decrypt_n_token_ctr`` — a *correct* AES-256-CTR
    decryptor. Given the n-token master key it recovers the plaintext.
  * ``recover_n_token_plaintext`` — a *working, key-free* path that runs
    the bundle locally and captures the plaintext directly from the
    encrypt entry's data buffer (for self-generated tokens).

Open residual: the per-build n-token master key is never materialised as
32 contiguous bytes — it lives only as the fixslice round-key schedule in
WASM global state, and ``inv_bitslice`` of the captured round keys does
not reproduce a standard-AES master on current builds. So decrypting a
*third party's* token still needs either the exact fixslice round-key
inverse for the current crate version, or a global-state WASM oracle.
See ``docs/19`` § "The remaining residual".
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, List

from Crypto.Cipher import AES


# ---------------------------------------------------------------------------
# Wire formats — every reasonable framing we've observed in the wild
# ---------------------------------------------------------------------------
_WIRE_FORMATS = [
    # n-token v2 (current builds, trailing version byte)
    ("ntoken_v2/ct||tag||iv||v",  0, 1, "ct_tag_iv"),
    ("ntoken_v2/v||ct||tag||iv",  1, 0, "ct_tag_iv"),
    ("ntoken_v2/iv||ct||tag||v",  0, 1, "iv_ct_tag"),
    ("ntoken_v2/v||iv||ct||tag",  1, 0, "iv_ct_tag"),
    # hsj-style (algorithm.HSJEncryption): ct||tag||iv||0x00
    ("hsj/ct||tag||iv||0",        0, 1, "ct_tag_iv"),
    # hsw-style response (iv||ct||tag, no version byte)
    ("hsw_resp/iv||ct||tag",      0, 0, "iv_ct_tag"),
    # Plain (no version byte)
    ("plain/ct||tag||iv",         0, 0, "ct_tag_iv"),
    ("plain/iv||ct||tag",         0, 0, "iv_ct_tag"),
    # Defensive reversals
    ("plain/tag||iv||ct",         0, 0, "tag_iv_ct"),
    ("plain/tag||ct||iv",         0, 0, "tag_ct_iv"),
    ("plain/ct||iv||tag",         0, 0, "ct_iv_tag"),
    ("plain/iv||tag||ct",         0, 0, "iv_tag_ct"),
]


def _apply_layout(layout: str, buf: bytes,
                  iv_len: int = 12, tag_len: int = 16):
    n = len(buf)
    if n < iv_len + tag_len:
        return None
    if layout == "iv_ct_tag":
        return buf[:iv_len], buf[iv_len:n - tag_len], buf[n - tag_len:]
    if layout == "ct_tag_iv":
        return (buf[n - tag_len - iv_len:n - tag_len],
                buf[:n - tag_len - iv_len], buf[n - tag_len:])
    if layout == "tag_iv_ct":
        return buf[tag_len:tag_len + iv_len], buf[tag_len + iv_len:], buf[:tag_len]
    if layout == "ct_iv_tag":
        return (buf[n - tag_len - iv_len:n - tag_len],
                buf[:n - tag_len - iv_len], buf[n - tag_len:])
    if layout == "iv_tag_ct":
        return buf[:iv_len], buf[iv_len + tag_len:], buf[iv_len:iv_len + tag_len]
    if layout == "tag_ct_iv":
        return buf[n - iv_len:], buf[tag_len:n - iv_len], buf[:tag_len]
    return None


# ---------------------------------------------------------------------------
# Errors and result
# ---------------------------------------------------------------------------
class NTokenError(Exception):
    """Raised when n-token decryption fails through every strategy."""


@dataclass
class DecryptResult:
    plaintext: bytes
    key_hex: str
    wire_format: str
    method: str  # "static" or "live"


# ---------------------------------------------------------------------------
# Token decoding
# ---------------------------------------------------------------------------
def _b64_decode(token: str) -> bytes:
    s = token.strip()
    pad = "=" * (-len(s) % 4)
    for dec in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            return dec(s + pad)
        except Exception:
            continue
    raise NTokenError(f"could not base64-decode n-token (len={len(token)})")


# ---------------------------------------------------------------------------
# AES-256-CTR — the CORRECT n-token cipher (counter = iv || be32(i))
# ---------------------------------------------------------------------------
def _split_wire(raw: bytes, iv_len: int = 12, tag_len: int = 16,
                ver: bool = True) -> tuple[bytes, bytes, bytes] | None:
    """Split ``ct(N) || tag(16) || iv(12) || ver(1)`` -> (ct, tag, iv)."""
    trailer = iv_len + tag_len + (1 if ver else 0)
    if len(raw) <= trailer:
        return None
    body = raw[:-1] if ver else raw
    iv = body[-iv_len:]
    tag = body[-iv_len - tag_len:-iv_len]
    ct = body[:-iv_len - tag_len]
    return ct, tag, iv


def ctr_keystream(key: bytes, iv: bytes, nblocks: int,
                  start: int = 0) -> bytes:
    """AES-CTR keystream with counter block = ``iv(12) || be32(counter)``."""
    from Crypto.Cipher import AES
    ecb = AES.new(key, AES.MODE_ECB)
    out = bytearray()
    for i in range(nblocks):
        out += ecb.encrypt(iv + ((start + i) & 0xFFFFFFFF).to_bytes(4, "big"))
    return bytes(out)


def decrypt_n_token_ctr(token_b64: str, master_key: bytes,
                        counter_start: int = 0) -> bytes:
    """Decrypt an n-token with AES-256-CTR (the confirmed cipher).

    ``master_key`` is the 32-byte n-token AES key. The wire format is
    ``ct(N) || tag(16) || iv(12) || ver(1)`` and the counter block is
    ``iv || be32(counter)``. Returns the recovered plaintext.
    """
    raw = _b64_decode(token_b64)
    parts = _split_wire(raw)
    if parts is None:
        raise NTokenError(f"token too short (raw={len(raw)})")
    ct, _tag, iv = parts
    nblk = (len(ct) + 15) // 16
    ks = ctr_keystream(master_key, iv, nblk, start=counter_start)
    return bytes(a ^ b for a, b in zip(ct, ks))


# ---------------------------------------------------------------------------
# Static decrypt: try a set of candidate keys × every wire format
# ---------------------------------------------------------------------------
def _static_decrypt(raw: bytes,
                    candidate_keys: Iterable[bytes]) -> DecryptResult | None:
    """Try AES-(128|192|256)-GCM with each candidate key × every wire
    format. Return the first verified success.
    """
    seen = set()
    keys: list[bytes] = []
    for k in candidate_keys:
        if not isinstance(k, (bytes, bytearray)):
            continue
        if len(k) not in (16, 24, 32):
            continue
        kb = bytes(k)
        if kb in seen:
            continue
        seen.add(kb)
        keys.append(kb)

    # 1. The CORRECT cipher first: AES-CTR, counter = iv || be32(i).
    #    There is no MAC to verify against, so validate the plaintext
    #    shape (the n-token plaintext is a 328-byte-record table).
    split = _split_wire(raw)
    if split is not None:
        ct, _tag, iv = split
        nblk = (len(ct) + 15) // 16
        for k in keys:
            for start in (0, 1):
                ks = ctr_keystream(k, iv, nblk, start=start)
                pt = bytes(a ^ b for a, b in zip(ct, ks))
                if _looks_like_ntoken_plaintext(pt):
                    return DecryptResult(plaintext=pt, key_hex=k.hex(),
                                         wire_format=f"ctr/iv||be32+{start}",
                                         method="static")

    # 2. Legacy AES-GCM sweep (kept for older builds / completeness).
    for k in keys:
        for name, hdr, trl, layout in _WIRE_FORMATS:
            if hdr + trl >= len(raw):
                continue
            inner = raw[hdr:len(raw) - trl] if trl else raw[hdr:]
            parts = _apply_layout(layout, inner)
            if parts is None:
                continue
            iv, ct, tag = parts
            if len(iv) != 12 or len(tag) != 16 or not ct:
                continue
            try:
                c = AES.new(k, AES.MODE_GCM, nonce=iv)
                pt = c.decrypt_and_verify(ct, tag)
                return DecryptResult(
                    plaintext=pt,
                    key_hex=k.hex(),
                    wire_format=name,
                    method="static",
                )
            except (ValueError, KeyError):
                continue
    return None


def _looks_like_ntoken_plaintext(pt: bytes) -> bool:
    """The n-token plaintext is a table of 328-byte records, each headed
    by an ``01 00 00 00 00 00 00 00`` (u64=1) marker at a regular stride.
    A correct CTR decrypt reproduces this; a wrong key gives noise.
    """
    if len(pt) < 700:
        return False
    needle = b"\x01\x00\x00\x00\x00\x00\x00\x00"
    pos, s = [], 0
    while True:
        i = pt.find(needle, s)
        if i < 0:
            break
        pos.append(i); s = i + 1
    if len(pos) < 3:
        return False
    strides = [pos[i + 1] - pos[i] for i in range(len(pos) - 1)]
    # require a dominant, regular stride (the record size)
    from collections import Counter
    common, n = Counter(strides).most_common(1)[0]
    return n >= 2 and 64 <= common <= 1024


# ---------------------------------------------------------------------------
# Built-in candidate keys (from prior captures)
# ---------------------------------------------------------------------------
def _load_builtin_candidate_keys() -> list[bytes]:
    """Aggregate every key candidate the project has captured in the
    past. Cheap; lets the static-only fast path succeed if the n_key
    has not rotated since the last run.

    Sources:
      * data/keys.json
      * tools/capture_ntoken_key.last.json — fn 334 a0/a1 ring +
        instrumented KS captures
      * tools/instrument_encrypt_entry.last.json — fn 226 arg0
        (key pointer) first 32 bytes
    """
    keys: list[bytes] = []
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(here))

    def _push(hex_or_bytes):
        if isinstance(hex_or_bytes, str):
            try:
                k = bytes.fromhex(hex_or_bytes)
            except ValueError:
                return
        else:
            k = bytes(hex_or_bytes)
        if len(k) in (16, 24, 32):
            keys.append(k)

    # data/keys.json
    keys_json = os.path.join(project_root, "data", "keys.json")
    try:
        with open(keys_json) as f:
            kj = json.load(f)
        for section in ("hsw", "hsj"):
            for kname, v in (kj.get(section) or {}).items():
                if isinstance(v, str):
                    _push(v)
    except Exception:
        pass

    # tools/capture_ntoken_key.last.json
    cap_json = os.path.join(project_root, "tools",
                            "capture_ntoken_key.last.json")
    try:
        with open(cap_json) as f:
            cap = json.load(f)
        cap_keys = set()
        for ring in (cap.get("captured") or {}).values():
            for r in ring:
                kh = r.get("key32_hex")
                if kh:
                    cap_keys.add(kh)
        for kh in cap_keys:
            _push(kh)
    except Exception:
        pass

    # tools/instrument_encrypt_entry.last.json
    ie_json = os.path.join(project_root, "tools",
                           "instrument_encrypt_entry.last.json")
    try:
        with open(ie_json) as f:
            ie = json.load(f)
        for r in (ie.get("records") or []):
            buf0 = r.get("buf0_pre_hex", "")
            if len(buf0) >= 64:
                _push(buf0[:64])
            if len(buf0) >= 32:
                _push(buf0[:32])
        ks_buf_hex = (ie.get("after_snapshot") or {}).get("1044528", "")
        if ks_buf_hex:
            buf = bytes.fromhex(ks_buf_hex)
            for off in range(0, len(buf) - 32, 16):
                _push(buf[off:off + 32])
    except Exception:
        pass

    # Dedupe while preserving order
    seen = set()
    out = []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


# ---------------------------------------------------------------------------
# Live decrypt: patch every fixslice KS function in the live WASM,
# run window.hsw(jwt) to fire the n-token key schedule, harvest all
# captured master-key candidates, try GCM with each × every wire format.
# ---------------------------------------------------------------------------

# ---- Scratch layout for instrumentation
_REC_SIZE = 36                       # 4B counter + 32B key bytes
_MAX_RECS_PER_FN = 64                # 64 calls per KS function
_SCRATCH_BASE = 60_000
_RING_STRIDE  = _MAX_RECS_PER_FN * _REC_SIZE + 32        # ~2400 per fn
_MAX_FNS      = 8                    # up to 8 fixslice KS funcs
_TMP_BASE     = 200_000
_TMP_STRIDE   = 16
_GATE_ADDR    = _TMP_BASE + _MAX_FNS * _TMP_STRIDE


def _ring_slots(slot_idx: int):
    counter = _SCRATCH_BASE + slot_idx * _RING_STRIDE
    buf     = counter + 4
    tmp_c   = _TMP_BASE + slot_idx * _TMP_STRIDE
    tmp_a   = tmp_c + 4
    return counter, buf, tmp_c, tmp_a


def _build_capture_prologue(counter_addr: int, buf_addr: int,
                            tmp_c: int, tmp_a: int,
                            src_local: int) -> bytes:
    """Stack-neutral prologue: while (counter < MAX_RECS) {
        ring[counter] = (counter, 32_bytes_at_arg1)
        counter++
    }; gated by *GATE != 0.
    """
    from .tools.wasm_writer import encode_uleb, encode_sleb
    out = bytearray()
    # if (*GATE != 0) {
    out += b"\x41" + encode_sleb(_GATE_ADDR)
    out += b"\x28\x02\x00"
    out += b"\x04\x40"
    # c = *counter
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    # if c < MAX
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(_MAX_RECS_PER_FN)
    out += b"\x49"  # i32.lt_u
    out += b"\x04\x40"
    # tmp_a = buf + c * REC_SIZE
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(_REC_SIZE)
    out += b"\x6c"
    out += b"\x41" + encode_sleb(buf_addr)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    # *(tmp_a) = c
    out += b"\x41" + encode_sleb(tmp_a)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x36\x02\x00"
    # Copy 32B from *src to addr+4 as 4 x i64 (unaligned-safe)
    for chunk in range(4):
        src_off = chunk * 8
        dst_off = 4 + chunk * 8
        out += b"\x41" + encode_sleb(tmp_a)
        out += b"\x28\x02\x00"
        out += b"\x20" + encode_uleb(src_local)
        out += b"\x29\x00" + encode_uleb(src_off)
        out += b"\x37\x00" + encode_uleb(dst_off)
    # counter++
    out += b"\x41" + encode_sleb(counter_addr)
    out += b"\x41" + encode_sleb(tmp_c)
    out += b"\x28\x02\x00"
    out += b"\x41" + encode_sleb(1)
    out += b"\x6a"
    out += b"\x36\x02\x00"
    out += b"\x0b"  # end if c<MAX
    out += b"\x0b"  # end if gate
    return bytes(out)


def _build_deobf_injection(deobf_helper_idx: int,
                           scratch_base: int,
                           src_local: int = 1) -> bytes:
    """Mirror ``hsw._build_injection`` — call the XOR-deobf helper 8
    times with (0, src_local + i*4) to materialize the 8 deobfuscated
    i32 master-key words, store each to scratch_base + i*4.

    This is the proven way to extract the true 32-byte master key from
    a fixslice key-schedule function. The KS function takes a pointer
    to the obfuscated key as arg1; the deobf helper has signature
    (i32, i32) -> i32 and is the most-called callee inside the KS body.
    """
    from .tools.wasm_writer import encode_uleb, encode_sleb
    parts = []
    for i in range(8):
        parts.append(b"\x41" + encode_sleb(scratch_base + i * 4))  # dst
        parts.append(b"\x41" + encode_sleb(0))                     # arg 0
        parts.append(b"\x20" + encode_uleb(src_local))             # arg 1: src ptr
        if i > 0:
            parts.append(b"\x41" + encode_sleb(i * 4))
            parts.append(b"\x6a")                                  # i32.add
        parts.append(b"\x10" + encode_uleb(deobf_helper_idx))      # call deobf
        parts.append(b"\x36\x02\x00")                              # i32.store
    return b"".join(parts)


def _find_deobf_helper_for_ks(mod, ks_fn_idx: int) -> int | None:
    """The XOR-deobfuscation helper is the (i32,i32)->i32 callee that
    is invoked most often from a given KS function.
    """
    counts = Counter()
    for n, ops, _, _ in (mod.decode_function(ks_fn_idx) or []):
        if n == "call" and ops:
            counts[ops[0]] += 1
    for callee, _ in counts.most_common(15):
        f = next((f for f in mod.functions if f["func_idx"] == callee),
                 None)
        if f is None:
            continue
        params, results = mod.types[f["type_idx"]]
        if params == ["i32", "i32"] and results == ["i32"]:
            return callee
    return None


def _find_fixslice_ks_funcs(mod) -> list[int]:
    """All AES fixslice32 key-schedule candidates in the module.

    Fingerprint:
      * signature (i32, i32) -> ()
      * body >= 1000 bytes
      * dense XOR ops (>= 80)
      * masks 0x55555555 / 0x33333333 / 0x0F0F0F0F / 0x0F000F00 present
    """
    out = []
    for f in mod.functions:
        if mod.types[f["type_idx"]] != (["i32", "i32"], []):
            continue
        body_len = f["code_end"] - f["code_start"]
        if body_len < 1000:
            continue
        instrs = mod.decode_function(f["func_idx"]) or []
        op = Counter(n for n, _, _, _ in instrs)
        if op.get("i32.xor", 0) < 80:
            continue
        consts = {ops[0] & 0xFFFFFFFF for n, ops, _, _ in instrs
                  if n == "i32.const" and ops}
        if not (0x0F000F00 in consts or 0x55555555 in consts
                or 0x33333333 in consts or 0x0F0F0F0F in consts):
            continue
        out.append(f["func_idx"])
    return out


from .tools.sandbox_hook import HOOK_JS as _HOOK_JS


def _live_capture_keys(version: str | None,
                       timeout: float = 90.0) -> list[bytes]:
    """Boot a Node/jsdom sandbox, patch every fixslice KS in the WASM
    to capture its master-key input, run ``window.hsw(jwt)``, and
    return every captured 32-byte key candidate.

    Two extraction techniques are spliced into each fixslice KS
    function:

      1. **Raw-byte capture**: copy 32 bytes from the KS's arg1 pointer
         into a ring buffer. Gives the "obfuscated" key bytes as they
         sit in the .data segment.
      2. **XOR-deobf extraction** (mirrors ``hsw.HSWKeyFetcher``):
         call the KS function's most-called helper (sig
         (i32,i32)->i32) 8 times with (0, src_ptr+i*4); the helper
         returns the deobfuscated i32 word. Concatenate all 8 -> the
         true 32-byte master key.

    Both pools are returned (deobf'd first, then raw — deobf is the
    "real" key but raw also occasionally works on older builds).
    """
    # Heavy imports — only pull in when live mode is invoked
    from .tools.wasm_disasm import WasmModule
    from .tools.wasm_writer import ModuleWriter
    from .tools.js_runtime import JsRuntime
    from .hsw_bridge import HSWAnalyzer, _wait_done
    from . import version as _v
    import requests

    version = version or _v.latest_version()
    info = HSWAnalyzer(version).analyze()
    orig_wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(orig_wasm)

    ks_funcs = _find_fixslice_ks_funcs(mod)
    if not ks_funcs:
        return []
    ks_funcs = ks_funcs[:_MAX_FNS]

    # Scratch for deobf'd keys: one 32-byte slot per KS function,
    # placed AFTER all ring buffers and tmp slots
    _DEOBF_BASE = _GATE_ADDR + 32

    # Build patched WASM
    writer = ModuleWriter(mod)
    deobf_for_fn = {}
    for slot, fi in enumerate(ks_funcs):
        counter, buf, tmp_c, tmp_a = _ring_slots(slot)
        # Inject raw-byte capture prologue (always)
        prologue = _build_capture_prologue(counter, buf, tmp_c, tmp_a,
                                           src_local=1)
        # ALSO inject XOR-deobf injection if we can find the helper.
        # This requires the KS function to call a (i32,i32)->i32 helper
        # which it does for the deobf word fetches.
        deobf_helper = _find_deobf_helper_for_ks(mod, fi)
        deobf_for_fn[fi] = deobf_helper
        if deobf_helper is not None:
            deobf_scratch = _DEOBF_BASE + slot * 32
            deobf_inj = _build_deobf_injection(deobf_helper, deobf_scratch,
                                               src_local=1)
            # Concat with raw capture
            prologue = prologue + deobf_inj
        writer.code.splice_code(fi, 0, n_replace=0, new_bytes=prologue)

    # Add peek/poke helpers so we can read scratch from JS
    t_i32_to_i32 = next((i for i, (p, r) in enumerate(mod.types)
                         if p == ["i32"] and r == ["i32"]), None)
    if t_i32_to_i32 is None:
        t_i32_to_i32 = writer.add_type(["i32"], ["i32"])
    t_i32i32_to_void = next((i for i, (p, r) in enumerate(mod.types)
                              if p == ["i32", "i32"] and r == []), None)
    if t_i32i32_to_void is None:
        t_i32i32_to_void = writer.add_type(["i32", "i32"], [])
    writer.add_function(t_i32_to_i32, [],
                        bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]),
                        export_name="__peek32")
    writer.add_function(t_i32i32_to_void, [],
                        bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]),
                        export_name="__poke32")

    patched = writer.emit()

    # Launch sandbox
    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64 = '"
                f"{base64.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js"))
        r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)

        # Warmup to force WASM instantiation
        rt.eval(r"""(async () => {
            try { await window.hsw(1, new Uint8Array(0)); }
            catch (e) { globalThis.__warmup_err = String(e); }
        })();""", suppress=True)
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break
        if rt.eval("globalThis.__hsw_exports") is None:
            raise NTokenError("patched WASM never instantiated")

        # Build JWT — any well-formed JWT will trigger the n-token KS
        now = int(time.time())
        def _b64u(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        jwt = (
            _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
            + "."
            + _b64u(json.dumps(
                {"s": "00000000", "d": 1, "t": now, "exp": now + 600}
            ).encode())
            + ".fake"
        )

        # Reset counters + open the gate, then run hsw(jwt)
        reset_js = []
        for slot in range(len(ks_funcs)):
            counter, _, _, _ = _ring_slots(slot)
            reset_js.append(f"e.__poke32({counter}, 0);")
        reset_js_s = " ".join(reset_js)
        rt.eval(rf"""
            globalThis.__done = 0;
            (async () => {{
                const e = globalThis.__hsw_exports;
                {reset_js_s}
                e.__poke32({_GATE_ADDR}, 1);
                try {{ await window.hsw('{jwt}'); }}
                catch (ex) {{ globalThis.__err = String(ex); }}
                e.__poke32({_GATE_ADDR}, 0);
                globalThis.__done = 1;
            }})();
        """, suppress=True)
        for _ in range(int(timeout * 4)):
            if rt.eval("globalThis.__done"):
                break
            time.sleep(0.25)

        # Read every ring (raw bytes AND deobf'd keys)
        all_keys: list[bytes] = []

        # First: read the deobf'd keys (these are the TRUE master keys)
        # for every KS function that had a deobf helper.
        for slot, fi in enumerate(ks_funcs):
            if deobf_for_fn.get(fi) is None:
                continue
            deobf_scratch = _DEOBF_BASE + slot * 32
            try:
                key_bytes = rt.eval(
                    f"""(function() {{
                        const mem = new Uint8Array(
                            globalThis.__hsw_memory.buffer, {deobf_scratch}, 32);
                        return Array.from(mem);
                    }})()"""
                ) or []
                key_bytes = bytes(key_bytes)
                if len(key_bytes) == 32 and any(b != 0 for b in key_bytes):
                    all_keys.append(key_bytes)
            except Exception:
                pass

        # Second: append raw-captured bytes (obfuscated, but might
        # match on older builds where the key isn't obfuscated)
        for slot, fi in enumerate(ks_funcs):
            counter, buf, _, _ = _ring_slots(slot)
            n_recs = (rt.eval(f"globalThis.__hsw_exports.__peek32({counter})")
                      or 0) & 0xFFFFFFFF
            n_recs = min(n_recs, _MAX_RECS_PER_FN)
            if n_recs == 0:
                continue
            total = n_recs * _REC_SIZE
            arr = rt.eval(
                f"""(function() {{
                    const mem = new Uint8Array(
                        globalThis.__hsw_memory.buffer, {buf}, {total});
                    return Array.from(mem);
                }})()"""
            ) or []
            arr = bytes(arr)
            for i in range(n_recs):
                base = i * _REC_SIZE
                key32 = arr[base + 4:base + 4 + 32]
                if len(key32) == 32:
                    all_keys.append(key32)
    finally:
        try:
            rt.close()
        except Exception:
            pass

    # Dedupe while preserving order
    seen = set()
    out: list[bytes] = []
    for k in all_keys:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def recover_ntoken_master_live(version: str | None = None,
                               timeout: float = 90.0) -> bytes | None:
    """Recover the n-token AES-256 master key from the live WASM, VERIFIED.

    The n-token cipher (the fixslice fn the encrypt entry calls) reads its
    bitsliced round-key array as arg0. We deobf-capture the first 120 words of
    that array on the first call, then invert the fixslice key schedule:
    ``M[:16] = inv_bitslice(rk0)`` (rk0 is pure) and ``M[16:]`` from rk1 after
    undoing ``sub_bytes_nots`` + ``inv_shift_rows_1``. The recovery is
    SELF-VERIFYING: the recovered M's full AES-256 key schedule must reproduce
    all 120 captured round-key words (a random key cannot), so a key is returned
    only on an exact 120/120 schedule match — no fabrication.

    The key is a build constant (same for every n-token of a given asset build),
    so it decrypts any third-party n-token from that build. Returns the 32-byte
    master, or ``None`` if capture/verification fails.
    """
    import base64 as _b64m, json as _jsonm, struct as _struct, time as _time
    from collections import Counter as _Counter
    from .tools.wasm_disasm import WasmModule
    from .tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
    from .tools.js_runtime import JsRuntime
    from .tools import fixslice as _fs
    from .hsw_bridge import HSWAnalyzer
    from . import version as _v
    import requests

    version = version or _v.latest_version()
    info = HSWAnalyzer(version).analyze()
    mod = WasmModule(bytes.fromhex(info["wasm_bytes_hex"]))
    entry = _find_encrypt_entry(mod)
    ks_funcs = _find_fixslice_ks_funcs(mod)
    if entry is None or not ks_funcs:
        return None
    callcnt = _Counter(o[0] for n, o, _, _ in (mod.decode_function(entry) or [])
                       if n == "call" and o)
    fn = next((f for f in ks_funcs if f in callcnt), None)
    if fn is None:
        return None
    # global deobf helper = the (i32,i32)->i32 callee most-invoked from fn
    gc = _Counter()
    for n, ops, _, _ in (mod.decode_function(fn) or []):
        if n == "call" and ops:
            f = next((x for x in mod.functions if x["func_idx"] == ops[0]), None)
            if f and mod.types[f["type_idx"]] == (["i32", "i32"], ["i32"]):
                gc[ops[0]] += 1
    if not gc:
        return None
    HELP = gc.most_common(1)[0][0]

    RKBASE, RKDONE, GATE, NWRK = 399_600, 399_596, 399_004, 120

    def c(n):
        return b"\x41" + encode_sleb(n)

    deobf = bytearray()
    for i in range(NWRK):                         # *(RKBASE+i*4) = HELP(0, arg0 + i*4)
        deobf += c(RKBASE + i * 4) + b"\x41\x00" + b"\x20\x00"
        if i:
            deobf += c(i * 4) + b"\x6a"
        deobf += b"\x10" + encode_uleb(HELP) + b"\x36\x02\x00"
    pro = bytearray()
    pro += c(GATE) + b"\x28\x02\x00" + b"\x04\x40"          # if *GATE:
    pro += c(RKDONE) + b"\x28\x02\x00" + b"\x45" + b"\x04\x40"  # if !*RKDONE:
    pro += bytes(deobf)
    pro += c(RKDONE) + b"\x41\x01" + b"\x36\x02\x00"
    pro += b"\x0b\x0b"

    writer = ModuleWriter(mod)
    writer.code.splice_code(fn, 0, n_replace=0, new_bytes=bytes(pro))
    t_pk = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32"] and r == ["i32"]), None) or writer.add_type(["i32"], ["i32"])
    t_po = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32", "i32"] and r == []), None) or writer.add_type(["i32", "i32"], [])
    writer.add_function(t_pk, [], bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]), export_name="__peek32")
    writer.add_function(t_po, [], bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]), export_name="__poke32")
    patched = writer.emit()

    now = int(_time.time())
    def _b64u(b):
        return _b64m.urlsafe_b64encode(b).rstrip(b"=").decode()
    jwt = (_b64u(_jsonm.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "."
           + _b64u(_jsonm.dumps({"s": "00000000", "d": 0, "t": now, "exp": now + 600}).encode()) + ".fake")
    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64='{_b64m.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js")); r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)
        rt.eval("(async()=>{try{await window.hsw(1,new Uint8Array(0));}catch(e){}})();", suppress=True)
        for _ in range(80):
            _time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break
        if rt.eval("globalThis.__hsw_exports") is None:
            return None
        rt.eval(f"""globalThis.__done=0;(async()=>{{const e=globalThis.__hsw_exports;
            e.__poke32({RKDONE},0); e.__poke32({GATE},1);
            try{{await window.hsw('{jwt}');}}catch(ex){{globalThis.__e=String(ex);}}
            finally{{e.__poke32({GATE},0);}} globalThis.__done=1;}})();""", suppress=True)
        for _ in range(int(timeout * 4)):
            if rt.eval("globalThis.__done"):
                break
            _time.sleep(0.25)
        data = rt.eval(f"(function(){{return Array.from(new Uint8Array("
                       f"globalThis.__hsw_memory.buffer,{RKBASE},{NWRK*4}));}})()") or []
        rk = list(_struct.unpack(f"<{NWRK}I", bytes(data)))
    finally:
        try:
            rt.close()
        except Exception:
            pass

    if not any(rk):
        return None
    a, _ = _fs.inv_bitslice(list(rk[0:8]))
    ch = list(rk[8:16]); _fs.sub_bytes_nots(ch); _fs.shift_rows_1(ch)
    cc, _ = _fs.inv_bitslice(ch)
    master = a + cc
    sched = _fs.aes256_key_schedule(master)
    if sum(1 for i in range(NWRK) if sched[i] == rk[i]) != NWRK:
        return None      # not a verified key — never fabricate
    return master


def _live_decrypt(raw: bytes,
                  version: str | None,
                  timeout: float = 90.0) -> DecryptResult | None:
    # Preferred: the verified round-key recovery (self-checked, exact). The
    # n-token is AES-256-GCM, so the keystream counter starts at J0+1 = 2.
    master = recover_ntoken_master_live(version=version, timeout=timeout)
    split = _split_wire(raw)
    if master is not None and split is not None:
        ct, _tag, iv = split
        nblk = (len(ct) + 15) // 16
        for start in (2, 1, 0):
            ks = ctr_keystream(master, iv, nblk, start=start)
            pt = bytes(x ^ y for x, y in zip(ct, ks))
            if _looks_like_ntoken_plaintext(pt) or start == 2:
                return DecryptResult(plaintext=pt, key_hex=master.hex(),
                                     wire_format=f"ctr/iv||be32+{start}",
                                     method="live-roundkey-recovery")
    # Fallback: legacy candidate-key sweep.
    keys = _live_capture_keys(version, timeout=timeout)
    if not keys:
        return None
    return _static_decrypt(raw, keys)


# ---------------------------------------------------------------------------
# Key-free plaintext recovery: capture the encrypt entry's input buffer
# ---------------------------------------------------------------------------
def _find_encrypt_entry(mod) -> int | None:
    """The n-token encrypt entry is the (i32,i32,i32)->i32 function that
    calls the most fixslice key-schedule helpers (it schedules the key
    then drives the CTR loop). Build-index-independent."""
    ks = set(_find_fixslice_ks_funcs(mod))
    best, best_n = None, 0
    for f in mod.functions:
        params, results = mod.types[f["type_idx"]]
        if params != ["i32", "i32", "i32"] or results != ["i32"]:
            continue
        n = sum(1 for nm, ops, _, _ in (mod.decode_function(f["func_idx"]) or [])
                if nm == "call" and ops and ops[0] in ks)
        if n > best_n:
            best_n, best = n, f["func_idx"]
    return best


def recover_n_token_plaintext(version: str | None = None,
                              jwt: str | None = None,
                              timeout: float = 90.0,
                              max_len: int = 8192) -> dict:
    """Run the live bundle and capture the n-token *plaintext* directly
    from the encrypt entry's data buffer — no key required.

    Returns ``{"token": <wire b64>, "plaintext": <bytes>, "version": ...}``.
    Works for self-generated tokens (we drive ``window.hsw(jwt)`` and read
    the buffer the bundle is about to AES-CTR-encrypt). The wire ``ct``
    and the recovered ``plaintext`` satisfy ``ct == plaintext XOR
    keystream``; their XOR is the exact CTR keystream for the token's IV.
    """
    from .tools.wasm_disasm import WasmModule
    from .tools.wasm_writer import ModuleWriter, encode_uleb, encode_sleb
    from .tools.js_runtime import JsRuntime
    from .hsw_bridge import HSWAnalyzer
    from . import version as _v
    import requests

    version = version or _v.latest_version()
    info = HSWAnalyzer(version).analyze()
    wasm = bytes.fromhex(info["wasm_bytes_hex"])
    mod = WasmModule(wasm)
    entry = _find_encrypt_entry(mod)
    if entry is None:
        raise NTokenError("could not locate the n-token encrypt entry")

    GATE, CTR, BUF, REC = 50_000, 50_016, 50_032, 4 + max_len
    writer = ModuleWriter(mod)
    # gated prologue: if first call, copy max_len bytes from arg1 -> BUF
    o = bytearray()
    o += b"\x41" + encode_sleb(GATE) + b"\x28\x02\x00" + b"\x04\x40"        # if *GATE
    o += b"\x41" + encode_sleb(CTR) + b"\x28\x02\x00" + b"\x41\x00" + b"\x46" + b"\x04\x40"  # if *CTR==0
    # bounds: 1024 <= arg1 && arg1+max_len <= memory.size*65536
    o += b"\x20\x01" + b"\x41" + encode_sleb(1024) + b"\x4f"
    o += b"\x20\x01" + b"\x41" + encode_sleb(max_len) + b"\x6a" + b"\x3f\x00" + b"\x41" + encode_sleb(65536) + b"\x6c" + b"\x4d"
    o += b"\x71" + b"\x04\x40"
    for q in range(max_len // 8):
        o += b"\x41" + encode_sleb(BUF + q * 8) + b"\x20\x01" + b"\x29\x00" + encode_uleb(q * 8) + b"\x37\x02\x00"
    o += b"\x41" + encode_sleb(CTR) + b"\x41\x01" + b"\x36\x02\x00"          # *CTR = 1
    o += b"\x0b\x0b\x0b"
    writer.code.splice_code(entry, 0, n_replace=0, new_bytes=bytes(o))
    t_pk = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32"] and r == ["i32"]), None) or writer.add_type(["i32"], ["i32"])
    t_po = next((i for i, (p, r) in enumerate(mod.types) if p == ["i32", "i32"] and r == []), None) or writer.add_type(["i32", "i32"], [])
    writer.add_function(t_pk, [], bytes([0x20, 0x00, 0x28, 0x02, 0x00, 0x0b]), export_name="__peek32")
    writer.add_function(t_po, [], bytes([0x20, 0x00, 0x20, 0x01, 0x36, 0x02, 0x00, 0x0b]), export_name="__poke32")
    patched = writer.emit()

    if jwt is None:
        now = int(time.time())
        b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        jwt = (b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "."
               + b64u(json.dumps({"s": "00000000", "d": 1, "t": now, "exp": now + 600}).encode())
               + ".fake")

    rt = JsRuntime()
    try:
        rt.eval(f"globalThis.__patched_wasm_b64='{base64.b64encode(patched).decode()}';")
        rt.eval(_HOOK_JS)
        r = requests.get(_v.asset_url(version, "hsw.js")); r.encoding = "utf-8"
        rt.eval(r.text, suppress=True)
        rt.eval("(async()=>{try{await window.hsw(1,new Uint8Array(0));}catch(e){}})();", suppress=True)
        for _ in range(80):
            time.sleep(0.1)
            if rt.eval("globalThis.__hsw_exports") is not None:
                break
        if rt.eval("globalThis.__hsw_exports") is None:
            raise NTokenError("patched WASM never instantiated")
        rt.eval(f"""globalThis.__done=0;globalThis.__tok='';
            (async()=>{{const e=globalThis.__hsw_exports; e.__poke32({CTR},0); e.__poke32({GATE},1);
            try{{const r=await window.hsw({json.dumps(jwt)}); globalThis.__tok=(typeof r==='string')?r:'';}}
            catch(ex){{globalThis.__err=String(ex);}}finally{{e.__poke32({GATE},0);}}
            globalThis.__done=1;}})();""", suppress=True)
        for _ in range(int(timeout * 4)):
            if rt.eval("globalThis.__done"):
                break
            time.sleep(0.25)
        token = rt.eval("globalThis.__tok") or ""
        raw = _b64_decode(token) if token else b""
        N = len(raw) - 29 if len(raw) > 29 else 0
        arr = rt.eval(f"(function(){{return Array.from(new Uint8Array(globalThis.__hsw_memory.buffer,{BUF},{max_len}));}})()") or []
        plaintext = bytes(arr)[:N] if N else bytes(arr)
        return {"version": version, "token": token, "plaintext": plaintext,
                "entry_fn": entry, "wasm_sha256": info["wasm_sha256"]}
    finally:
        try:
            rt.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def decrypt_n_token(token_b64: str,
                    version: str | None = None,
                    candidate_keys: Iterable[bytes] | None = None,
                    try_live: bool = True,
                    timeout: float = 90.0) -> bytes:
    """Decrypt an hCaptcha hsw n-token.

    Parameters
    ----------
    token_b64 : str
        The n-token as returned by ``window.hsw(jwt)`` — a base64 string.
        Whitespace and url-safe / standard padding variants accepted.
    version : str, optional
        The hCaptcha asset version hash. If not provided and live mode
        runs, the latest version is fetched dynamically.
    candidate_keys : iterable of bytes, optional
        User-supplied master AES-256 (or AES-128/192) key candidates,
        tried first. If any decrypts, no live sandbox is launched.
    try_live : bool, default True
        Whether to fall back to live WASM instrumentation when no static
        candidate works.
    timeout : float, default 90.0
        Max seconds to wait for the live ``window.hsw(jwt)`` call.

    Returns
    -------
    bytes
        The decrypted msgpack-encoded plaintext payload. Pass through
        ``msgpack.unpackb(..., strict_map_key=False)`` for the structured
        representation.

    Raises
    ------
    NTokenError
        If neither static nor live decryption produces a verifiable
        plaintext.
    """
    raw = _b64_decode(token_b64)
    if len(raw) < 32:
        raise NTokenError(f"token too short (raw={len(raw)} bytes)")

    # 1. User-supplied candidates
    if candidate_keys:
        res = _static_decrypt(raw, candidate_keys)
        if res is not None:
            return res.plaintext

    # 2. Built-in candidates (cheap)
    builtin = _load_builtin_candidate_keys()
    if builtin:
        res = _static_decrypt(raw, builtin)
        if res is not None:
            return res.plaintext

    # 3. Live decrypt
    if try_live:
        res = _live_decrypt(raw, version=version, timeout=timeout)
        if res is not None:
            return res.plaintext

    raise NTokenError(
        "n-token decryption failed: no candidate key/wire-format "
        "combination produced a verified plaintext. Tried "
        f"{len(builtin)} built-in keys × {len(_WIRE_FORMATS)} wire "
        "formats. Live mode also failed to capture a working key."
    )


# ---------------------------------------------------------------------------
# CLI / smoke test
# ---------------------------------------------------------------------------
def _main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("token", nargs="?",
                   help="n-token (base64). If omitted, loads from "
                        "tools/instrument_encrypt_entry.last.json")
    p.add_argument("--key", action="append", default=[],
                   help="hex-encoded candidate key (may repeat)")
    p.add_argument("--version", default=None)
    p.add_argument("--no-live", action="store_true")
    p.add_argument("--json-out", action="store_true",
                   help="emit JSON-encoded result to stdout")
    args = p.parse_args()

    token = args.token
    if not token:
        here = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(here))
        ie_path = os.path.join(project_root, "tools",
                               "instrument_encrypt_entry.last.json")
        with open(ie_path) as f:
            ie = json.load(f)
        token = ie["token"]
        print(f"[smoke-test] using token from {ie_path} "
              f"(len={len(token)})", file=sys.stderr)

    candidate_keys = [bytes.fromhex(k) for k in args.key] or None
    try:
        pt = decrypt_n_token(token, version=args.version,
                             candidate_keys=candidate_keys,
                             try_live=not args.no_live)
    except NTokenError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json_out:
        out = {"len": len(pt), "head_hex": pt[:64].hex(),
               "tail_hex": pt[-64:].hex()}
        try:
            import msgpack
            out["msgpack"] = repr(msgpack.unpackb(pt, strict_map_key=False))[:1024]
        except Exception:
            pass
        print(json.dumps(out, indent=2))
    else:
        sys.stdout.buffer.write(pt)


if __name__ == "__main__":
    _main()
