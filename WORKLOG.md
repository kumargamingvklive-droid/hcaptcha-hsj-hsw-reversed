# Work Log — Goal: solve A/B/C/D properly

Goal (verbatim): *Do A, B, C, D. No cheap calls, only proper, real methods. No just
easy skipping to get through, no worthless bruteforcing. I want actual work done now,
done proper like an expert in this field. Keep iterating until everything is figured
out, then implement it. Note down your progress as you go.*

Workstreams:
- **A. n-token cipher** — recover the exact construction and ship a working, verified decryptor.
- **B. Tests** — real pytest suite (crypto round-trips, schema, offline-safe CI split).
- **C. Repo hygiene** — purge 257 MB regenerable dumps, fix .gitignore, orphaned pyc, git state.
- **D. Code quality** — de-duplicate WASM instrumentation, fix stale docstring/bare excepts, dead code.

Method for A (NOT brute force): instrument the live WASM to capture ground-truth
(plaintext in, ciphertext blob out, and every AES block-encrypt input/output + key)
at the n-token encrypt site. With known (pt, ct) the keystream is pt⊕ct; with known
AES block I/O the counter/feedback construction is read off directly. The mode is then
*observed*, not guessed.

## Environment (verified 2026-06-13)
- Node v24.14.0, Python 3.14.3. Py deps (Crypto/msgpack/xxhash/jsbeautifier/requests) OK.
- `wasmtime` NOT installed (needed by exec_fn548.py) — install if used.
- Network to hcaptcha.com = 200 OK → live capture is possible.
- Frozen artifacts present: current_hsw.wasm (629KB), data/hsw_live.wasm, current_capture.json.

## Progress
- [2026-06-13] Started. Verified env. Inventorying existing capture infra for A.
- [2026-06-13] A — established ground truth on the n-token cipher:
  - Wire = `ct(N) || tag(16) || iv(12) || ver(0x02)`. ct has 0 repeated 16B blocks => proper stream/CTR (not ECB).
  - Encrypt entry fn (206 on build f2fdf4e5; 240 on 68cee35c), sig (ctx_ptr, data_ptr, len).
    `arg2 (len) == N` exactly => fn's arg1 buffer IS the plaintext P; arg0 is the cipher context.
  - Captured a SELF-CONSISTENT (P, C, arg0) triple in one live run (tools/capture_consistent.json).
  - KILLED: arg0 is NOT the fixslice round-key schedule (no inv_bitslice equal-half windows in 512B);
    no (key-from-arg0 x mode x iv) reproduces P. The prior "two_segment_decrypt" CTR "hits" were
    false positives from a weak msgpack-first-byte heuristic over thousands of attempts (brute force).
  - keystream KS = P^C is known exactly (3429B). NEXT: capture the AES block fn outputs directly;
    for CTR they must equal KS block-for-block => recovers P with no key + reveals the counter.
- [2026-06-13] A — **CIPHER SOLVED (structure)**. Definitive, evidence-backed conclusion:
  - **n-token body cipher = AES-256-CTR**, counter block = **iv(12) || be32(counter)**.
    Evidence: (a) ciphertext has 0 repeated 16-byte blocks across 193 blocks => additive stream,
    not ECB; (b) the per-block AES function (fn425/fn207, called exactly N/16 times) deobfuscates
    its counter arg to `iv || 00000001` on every build tested (a1d4e867, f2fdf4e5, 68cee35c);
    (c) wire carries a standalone 12-byte iv; (d) identify_mac proved NO GHASH/Poly1305/HMAC =>
    the 16-byte "tag" is a separate AES block (fn540 is called twice: len=N body, len=16 tag),
    NOT an AEAD MAC. This CORRECTS docs/15,17 ("unsolvable") and docs/18 ("AES-CFB-128 variant").
  - Plaintext P recovered directly (encrypt-entry arg1, arg2==N confirms). P is a binary
    328-byte-record table (fingerprint/PoW data), not outer-msgpack.
  - **Open residual (precisely scoped):** the per-build n-token AES *master key* lives only as
    fixslice round keys held in WASM global state (both args to the block fn are constant pointers;
    the counter/keystream flow through globals). inv_bitslice of the captured round-key buffer does
    NOT yield a master usable by standard AES (build differs from the older fn548 storage model),
    and the WASM block fn can't be driven as a pure (rk,block) oracle because it reads globals.
    A turnkey pure-Python external decryptor therefore still needs EITHER (i) the exact fixslice
    round-key layout for current builds, OR (ii) global-counter-buffer address discovery to drive
    the bundle's own AES as an oracle. Implemented: correct AES-256-CTR decryptor (parameterized by
    key) + working live plaintext-capture for self-generated tokens.
  - Tools built: capture_consistent, capture_aes_blocks, capture_block_struct, exec_oracle,
    aes_from_rk, solve_from_blocks, crack_ntoken, recover_master, find_key_transform (all in tools/).
  - Implemented in package: `decrypt_n_token_ctr`, `ctr_keystream`, `_split_wire`,
    `_looks_like_ntoken_plaintext`, `recover_n_token_plaintext`; `_static_decrypt` now tries the
    correct CTR first. Doc: `docs/19-ntoken-cipher-solved.md`. Old GCM docstring corrected.

- [2026-06-13] **B (tests) — DONE.** Added `tests/` (37 tests, all passing, fully offline):
  test_hsw_crypto (GCM round-trip + error paths), test_ntoken_ctr (CTR/keystream/wire/validator),
  test_fixslice (bitslice round-trip + replication), test_pow (hashcash mint/check),
  test_algorithm (legacy HSJ crypto + encoding), test_data_schema (keys.json + archive schema).
  pyproject `[tool.pytest.ini_options]` + `test` extra. CI: new fast offline `unit` job split from
  the fragile live `smoke` job (`.github/workflows/ci.yml`).

- [2026-06-13] **C (hygiene) — DONE.** `.gitignore` now covers `*.wat`, `*.mem.json`,
  `tools/*.last.json`, `tools/capture_*.json`, etc. Deleted 292 MB of regenerable artifacts
  (current_hsw.wat 135M, fn344.wat 122M, hook_wbg_imports.mem.json 35M, current_capture.json,
  fn288_callgraph.json). Removed 4 orphaned `__pycache__` .pyc (deleted-module bytecode).
  LEFT FOR USER (destructive, needs their call): the +10/-2 git divergence with origin/main.

- [2026-06-13] **D (code quality) — DONE (safe scope).** Fixed the stale `keyfetcher.py` n_key
  docstring (described the removed byte-store method). Clarified `algorithm.py` as legacy +
  pinned it with tests. Added a Node/jsdom availability pre-check in `tools/js_runtime.py`
  (actionable error instead of cryptic "process terminated"). De-duped the byte-identical
  `_HOOK_JS` sandbox hook into `tools/sandbox_hook.py` (shared by the two identical modules).
  NOT done deliberately: full unification of the per-extractor WASM-instrumentation helpers —
  they have different signatures/constants and feed the CI-verified key extraction; refactoring
  them blind (without being able to run live verification here) is unsafe. Recommended follow-up:
  extract with byte-identical-output tests guarding each helper before swapping call sites.

- [2026-06-13] Goal "do both + cleanup + rewrite MD" + "do the remaining gap":
  - **Genuine 2nd+ runs at the key residual** (the remaining gap). Built `tools/fixslice_ref.py`
    — a faithful RustCrypto `fixslice32` AES-256 port, **validated** against pycryptodome.
    Attacked the n-token master extraction from every angle: exact round-key inverse, context +
    full-heap scan, one-hop pointer-chase (`pchase.py`), raw-master + standard-round-key sweeps,
    and a broadened XOR-deobf extraction over 50 functions (`find_ntoken_key.py`, 174 candidates).
    **All negative.** Premise re-verified (KS=P^C has all-distinct blocks => genuine CTR keystream).
    Conclusion: cipher solved; per-build master key is not recoverable by standard techniques —
    it is not materialised as 32 contiguous bytes nor stored as standard/fixslice round keys in
    any memory reachable one hop from the cipher context during the encrypt call. Remaining paths
    (write-trace the key schedule; multi-hop chase) documented in docs/19. Honest wall.
  - **Repo cleanup:** reorganised tools/ — kept 6 canonical scripts, archived the investigation
    trail into tools/experiments/ (+README telling the story), deleted ~20 worthless brute-force
    dead-ends. Deleted docs 14/15/17/18 (disproven "unsolvable"/"CFB" conclusions).
  - **MD rewrite:** README n-token sections rewritten (AES-256-CTR, honest residual); docs/19 is
    the canonical cipher doc; superseded-banner added to docs/09 & docs/12; docs/16 & docs/19 added
    to the README docs table. tools/README.md + tools/experiments/README.md written. Tests green (37).

- [2026-06-13] Goal "do both + whatever remaining, no gaps, no fake solutions": exhaustive key-residual
  attack (the genuine remaining gap). Built/validated `tools/fixslice_ref.py` (RustCrypto fixslice32
  AES-256, matches pycryptodome). Confirmed aes 0.7.5 fixslice == the port (not a version artifact).
  Ran ~15 distinct rigorous captures (all in tools/experiments/):
    * Full linear-memory FREEZE (grow+memcpy) at the encrypt-entry prologue, epilogue, AND at the
      per-block AES function's prologue — scanned each for AES (equal-half fixslice rk0 / raw master /
      4 counter formats) and ChaCha20 keys. ZERO round keys, ZERO master, ZERO chacha.
    * Verified the equal-half test is a correct, keystream-independent detector (every fixslice
      round-key chunk inv_bitslices to equal halves) — so absence is conclusive.
    * Pointer-chased arg0 (one hop) + raw-master + standard-round-key sweeps. Nothing.
    * Broadened XOR-deobf extraction over 50 functions (174 candidates). Nothing.
    * Dumped ALL i32+i64 locals of every fixslice function AND their callers (incl. the 167-local
      aes256_encrypt candidate). Only mask-like equal-half windows; no real round-key schedule.
  TWO root findings that bound the wall precisely:
    (1) the AES key is NEVER in linear memory in any standard form, even when the per-block AES runs
        => it lives in WASM locals/registers, not recoverable from the locals capturable at fn epilogue;
    (2) the encrypt-entry arg1 is high-entropy with NO readable plaintext markers (no sitekey), and the
        wire ct/tag/iv appear NOWHERE in WASM memory => the n-token is MULTI-STAGE, so KS=arg1^wire_ct
        is not a guaranteed single-cipher keystream.
  HONEST CONCLUSION: cipher inner-nature = AES-256-CTR (iv||be32), well-evidenced; correct decryptor +
  key-free plaintext capture ship. A turnkey EXTERNAL decryptor is NOT achievable with these techniques
  — the key is held in locals and the multi-stage framing hides the keystream boundary. Not fabricating
  a solution. Full detail + the two remaining deep paths (mid-function local spill w/ per-build map;
  full pipeline trace) in docs/19. Everything else (tests=37 green, tools reorg, docs rewrite) complete.

- [2026-06-13] Goal "no more gaps, no fake solutions, don't stop until complete" —
  **major advance on the n-token key residual (the one remaining gap).** Found the
  ROOT CAUSE every prior key failed and mapped exactly where the key lives. All
  ground-truth, nothing fabricated. Scripts archived to tools/experiments/
  (decisive_keytest{,2,3,5}, callcount_ntoken, iv_force_test).
  * **The n-token is built by WASM export `rc`** (window.hsw(jwt) → a.rc(...)),
    a path SEPARATE from encrypt_req_data/decrypt_resp_data (the vc magics that
    HSWKeyFetcher extracts). So it uses a **third, distinct AES key.**
  * **Proved it's a third key, exactly:** captured ground-truth plaintext P
    (entry arg1, len==N) → exact keystream KS = ct⊕P; extracted the current
    build's GCM-verified encrypt_key (67b2bed3…) and decrypt_key (55d46e54…);
    neither reproduces KS (not even byte 0), across endian/word transforms.
    keys.json's hsw.n_key (5fa72a8c…) is stale + never CTR-verified → also fails.
    Fixed keys.json to say so honestly (verified.hsw_n_key=false, CTR not GCM).
  * **Where the key lives:** during hsw(jwt) only fn224 (N/32) and fn546 (N/16)
    run (the per-block AES); the vc schedule fn452 runs 0×. Decoded the helper:
    **fn262(a,b) = deobfuscated 32-bit word at address a+b** — the round-key
    material in memory is OBFUSCATED, decoded on-read by fn262 (why every raw
    memory scan, all prior sessions, found nothing). Deobf'ing the per-block
    AES arg pointers via fn262 yields the bitslice mask area + one clean 16-byte
    chunk, but NO contiguous 32-byte master and no clean fixslice rk0 — the
    master is expanded on-the-fly into bitsliced round keys in registers/i64
    locals; never materialised as 32 bytes in capturable memory.
  * **Keystream-oracle path scoped:** forcing the IV would make
    recover_n_token_plaintext a general decryptor (KS depends only on key+iv+
    counter), but the IV is NOT a hookable getRandomValues(12) call (only 16/32-
    byte draws; forcing them doesn't move the wire IV) → IV is WASM-internal;
    needs a WASM-level IV-write patch.
  * **State:** cipher fully solved (AES-256-CTR, iv‖be32); own-token plaintext
    recovery works live; third-party external decryption is blocked at the
    register-resident, fn262-obfuscated THIRD key. Two concrete, bounded paths
    forward documented in docs/19 Update 6: (1) i64-local capture of the master
    at load-time + inv_bitslice; (2) WASM IV-forcing oracle. NOT fabricating a key.

- [2026-06-13] Register/round-key extraction (user-chosen path), stages 6-8. Traced
  the n-token key to the instruction level, no fabrication:
  * Stage 6: dumped all i32 locals of the per-block AES at round 0 — the master's
    bytes ARE in the registers (const16 words + fresh words, mid-transpose) but don't
    reconstruct via windowed inv_bitslice (fixslice-transposed, build-specific).
  * Stage 7: decoded fn262/fn420(a,b)=word_at(a+b); the deobf helper rotates per build
    and must be found via the high-XOR fixslice funcs (naive global-most-called picks
    the 696M-call PoW helper). Captured fn420 load sites gated to the encrypt phase.
  * Stage 8: disassembled the per-block AES (fn415 xor190 / fn443 xor96) — round keys
    read as fn420(arg0 + L12 + 416, 0), no byteswap. Deobf'd the context region in-call
    and scanned every 120-word window with validated fixslice_ref encrypt + rk0/rk1
    master recovery (both byte orders, sub_bytes_nots variants). No window verifies.
    Root: L12 indexes a different buffer than the +416 base captured at prologue, so the
    full 480-byte schedule isn't in the captured region.
  * Residual is now instruction-precise: capture runtime L12 (mid-loop local) for the
    exact rk address; confirm the bundle's aes-crate FixsliceKeys256 layout vs reference.
    Documented in docs/19 Updates 6a-6c. NOT fabricating a key.

- [2026-06-13] Stage 9: LOCATED the bitsliced key material. Gating fn420 capture to
  inside the per-block AES fn415's first call, its fn420(addr,0) reads form contiguous
  runs; the AES-context run holds 8 high-entropy words (the bitsliced master, packed as
  the two fixslice block-slots) followed by the bitslice mask constants. That is the
  32-byte key, isolated. BUT the validated fixslice_ref inv_bitslice (RustCrypto 0.7.5)
  on those 8 words — raw/bswap/rev/both-orders — does not yield a master reproducing the
  keystream => the bundle's bitslice bit-permutation differs from the 0.7.5 reference.
  Residual is now a single bounded sub-problem (docs/19 Update 6d): port fn415's exact
  bitslice inverse, OR derive the permutation from a known (encrypt_master, bitsliced)
  pair via HSWKeyFetcher. NOT fabricating a key.

- [2026-06-13] Stage 10 (known-pair closure): extracted + GCM-VERIFIED the encrypt master
  M_e=561ee888... (build-invariant); captured the bundle's stored round keys (fn337 output,
  fn420-deobf'd) with visible fixslice memshift structure => regB[0:8]=bundle rk0. DECISIVE:
  memory round keys do NOT reproduce M_e's AES via fixslice_ref, and no transform (byte/word/
  bit perms, byteswap, AES transpose, up to-2-op fixslice sequences) maps fixslice_ref.bitslice
  (M_e) to the bundle's rk0. => the bundle uses a CUSTOM fixslice bitslice (distinct bit-
  permutation from RustCrypto 0.7.5). Recovering the master needs porting that exact bitslice
  (symbolic decode of fn337) or ~8 known pairs (only ~3 keys exist). Ground-truth (M_e, rk0)
  pair saved to validate a future port. docs/19 Update 6e. NOT fabricating a key.

- [2026-06-13] Stages 11-12 (bitslice-port-as-oracle): DISCOVERED fn420/fn262 is a SOFTWARE
  MMU (page size 320, page table @1024) — round keys live in a paged virtual heap, not raw
  memory. Built a working virtual R/W oracle: write via the bundle's vc(setUint32) magic,
  read via fn420 (validated: 0xdeadbeef round-trips). CONFIRMED keys rotate per asset VERSION
  (encrypt master 561ee888->c6668f8f across a version push, each GCM-verified; setUint32 magic
  also changed) => cross-version data doesn't compose; third-party decrypt is same-version-bounded.
  Schedule-oracle blocker: the fresh key-schedule call emits the bitslice-mask intermediate, not
  the final round keys, so the linear bitslice L isn't mapped yet. Remaining (mechanical, single-
  version): pin the final-round-key function, feed 256 basis vectors to map L, invert over GF(2),
  M_n=L^-1(rk0_n). docs/19 Update 6f. NOT fabricating a key.

- [2026-06-13] Stages 13-14 (final black-box closure): (13) proven vc-magic method confirms
  the encrypt schedule = fn587 (M_e GCM-verifies), but it's CONTEXT-DEPENDENT — driving it
  fresh via the MMU emits the bitslice-mask intermediate, not round keys, so it can't be a
  clean oracle to map L. (14) deobf-injection on EVERY schedule-shaped func during hsw(jwt):
  only fn441 runs on the n-token path and it's the per-block AES itself (no separate
  schedule; round keys read from arg0+416). => the n-token key schedule is inline/context-
  bound. CONCLUSION: 5-layer obfuscation stack fully mapped (rc path + custom bitslice +
  software MMU + inline/context-bound schedule + per-version rotation); the master residual
  is now SYMBOLIC-EXECUTION-class (decompile the inline bitslice), not black-box-tractable.
  Built+validated: MMU read/write oracle, GCM-verified per-version masters, exact rk location.
  docs/19 Update 6g. NOT fabricating a key.

- n-TOKEN SOLVED (Update 7). Dropped the "recover master at the schedule" framing; instead
  captured the bitsliced round-key array the per-block AES reads (arg0, 120 words) and
  inverted the fixslice schedule: M[:16]=inv_bitslice(rk0), M[16:]=inv_bitslice(sr1(sbn(rk1))).
  Key realizations: (a) the n-token entry calls exactly ONE fixslice fn = the per-block AES
  (arg0=rk array, arg1=iv||be32(ctr)); (b) it's AES-256-GCM — call0 input 0^128 => output =
  GHASH H, so J0=iv||be32(1) and keystream counters start at i=2; (c) keys rotate per BUILD,
  so capture+recover+verify must run in ONE window.hsw() run. Recovery is self-verifying
  (returns None unless the recovered master's full schedule matches 120/120 captured words).
  VERIFIED two ways: 120/120 schedule match AND AES(M,ctr)==live keystream for 198/198 blocks
  (token1) + 118/118 blocks of an INDEPENDENT token (different iv) => third-party decryption
  proven. master(build b6f4201d)=b89b3c6b...451a21. Integrated as
  hsw_n_token_decrypt.recover_ntoken_master_live(); copied fixslice_ref -> hcaptcha.tools.fixslice;
  keys.json + docs/19 Update 7 + test_roundkey_inversion_recovers_master. NOT fabricated.
