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
format). The inner plaintext is a msgpack-encoded map whose keys mirror
the JWT payload (sitekey, host, time, fingerprint, PoW proof, …).

Cipher: AES-256-GCM. AAD: empty. The 12-byte IV is a fresh per-token
nonce; the 16-byte tag is the GMAC output.

State of the world
==================
The AES master key used to produce the n-token lives only inside WASM
linear memory and never appears as 32 contiguous plaintext bytes. The
key bytes are XOR-deobfuscated word-by-word inside the fixslice32
key-schedule routine and immediately bit-sliced into 8 u32 words.

This module implements both extraction paths discovered so far:

  1. **Static decrypt** — try AES-256-GCM with a list of candidate keys
     across every reasonable wire format. Use this when the master key
     has already been recovered for the current build (e.g. via the
     keystream / memdiff workflows in ``tools/``) and supplied via the
     ``candidate_keys`` argument.

  2. **Live decrypt** — boot a Node+jsdom sandbox, load hsw.js, patch
     every fixslice32 key-schedule function in the WASM module to
     dump both (a) the raw bytes at its second argument's pointer and
     (b) the XOR-deobfuscated 32-byte master key (mirroring the
     ``hsw.HSWKeyFetcher`` pattern, calling each KS function's most-used
     ``(i32,i32)->i32`` helper 8 times). Try every captured candidate
     against the supplied token. The encrypt/decrypt master keys for
     the bundle's ``encrypt_req_data`` / ``decrypt_resp_data`` paths are
     reliably recovered this way; the **n-token specific** AES key
     extraction depends on whether the n-token uses one of the same
     fixslice KS functions or a separate code path that the structural
     heuristic doesn't pick up. On every build observed to date the
     deobf helper for fn 434 (the vc-dispatched encrypt/decrypt KS) is
     reliably recovered (encrypt_key + decrypt_key); the n-token's
     specific KS may differ per build.

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
WARNING — current state (2026-06):
==================================
This module is NOT YET FUNCTIONAL on live n-tokens. The public API
(decrypt_n_token, NTokenError) exists and the infrastructure is in
place (key candidates from KeyFetcher + live capture + every wire
format we know of), but on the current build NO (key, wire-format)
combination successfully decrypts a live n-token. Calling
decrypt_n_token() on a real token raises NTokenError.

We have CONFIRMED via instrumentation:
  - fn 226 is the encrypt entry: sig (key_ptr, data_ptr, length) -> result
  - Two calls per window.hsw(jwt): one with length=3036 (n-token body),
    one with length=16 (GMAC tag finalization)
  - arg0 (key_ptr) is static across calls (same struct address each time)
  - The first 32 bytes at *arg0 are high-entropy, build-static, and
    look like an AES master key — but do NOT decrypt the token

Three remaining hypotheses:
  - The 32 bytes at *arg0 are a STRUCT HEADER (e.g. GCM context: H ||
    nonce || ...), not the AES key directly. The real key may be at
    a different offset in the struct.
  - The cipher is NOT standard AES-256-GCM (could be a custom AEAD).
  - There is a key derivation between *arg0 bytes and the AES key.

NEXT CONCRETE ATTACK: take memory snapshots BEFORE and AFTER each
fn 226 call. The plaintext-to-ciphertext transformation happens
in-place (or to a known output buffer); the diff reveals BOTH the
plaintext and the ciphertext bytes, sidestepping the key+wire-format
problem entirely.

See tools/{recover_keystream,instrument_encrypt_entry,memdiff_ntoken}.py
for the three attempted attacks. The first two are infrastructure-only
(no plaintext recovered); the third is the most promising direction
for the next iteration.
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


_HOOK_JS = r"""
(function () {
  function _b64ToU8(s) {
    if (typeof Buffer !== "undefined") {
      const b = Buffer.from(s, "base64");
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
    t.WebAssembly.instantiate = function (buf, imp) {
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
            if (v && typeof v === "object" && v.buffer &&
                typeof v.grow === "function") {
              globalThis.__hsw_memory = v;
              break;
            }
          }
        }
        return r;
      });
    };
    if (t.WebAssembly.instantiateStreaming) {
      t.WebAssembly.instantiateStreaming = async function (source, imp) {
        const resp = await source;
        const buf = await resp.arrayBuffer();
        return t.WebAssembly.instantiate(buf, imp);
      };
    }
  }
  _install(globalThis);
  _install(typeof window !== "undefined" ? window : null);
})();
"""


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


def _live_decrypt(raw: bytes,
                  version: str | None,
                  timeout: float = 90.0) -> DecryptResult | None:
    keys = _live_capture_keys(version, timeout=timeout)
    if not keys:
        return None
    return _static_decrypt(raw, keys)


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
