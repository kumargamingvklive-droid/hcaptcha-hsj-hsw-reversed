# `data/` — auto-refreshed key snapshots

Populated by [`.github/workflows/refresh-keys.yml`](../.github/workflows/refresh-keys.yml),
which runs every 12 hours and on manual dispatch.

## Layout

```
data/
├── keys.json                  ← the current build's keys (overwritten per run)
└── archive/
    ├── <build-hash-A>.json    ← per-build snapshots
    ├── <build-hash-B>.json
    └── ...
```

Each JSON file has the same schema returned by `python -m hcaptcha` —
**6 build-static master AES-256 keys** (3 HSJ + 3 HSW) plus a derived
7th fingerprint identifier:

```json
{
  "version": "<64-char build hash>",
  "hsj": {
    "n_key":                "<64 hex>",
    "response_decrypt_key": "<64 hex>",
    "payload_encrypt_key":  "<64 hex>"
  },
  "hsw": {
    "encrypt_key":          "<64 hex>",
    "decrypt_key":          "<64 hex>",
    "n_key":                "<64 hex>",
    "fingerprint_blob_key": "<64 hex>"
  },
  "cipher": "AES-256-GCM",
  "wire_format": {
    "hsj": "ct(N) || tag(16) || iv(12) || 0x00",
    "hsw": "iv(12) || ct(N) || tag(16)"
  },
  "aad": "",
  "verified": {
    "hsw_encrypt_key":           true,
    "hsw_decrypt_key":           true,
    "hsw_n_key":                 true,
    "hsw_fingerprint_blob_key":  true
  },
  "extraction_status": {
    "hsw_n_key": "captured-from-fXXX_a0-Nrecords-static",
    "hsw_n_key_meta": {
      "extraction_method":   "direct-aes-site-capture (fn XXX arg0 pattern)",
      "captured_rings":      ["fNNN_a0", "fNNN_a1", ...],
      "static_rings":        ["fNNN_a0", ...],
      "live_n_token_b64":    "<truncated in the committed snapshot>",
      "live_n_token_len_bytes": <int>,
      "wasm_sha256":         "<64-hex hash of the WASM blob>",
      "instrumented_fns":    [{ "fn": <int>, "n_args_i32": <int> }, ...]
    },
    "hsw_fingerprint_blob_key": {
      "construction": "sha256(hsw.n_key)",
      "source_ring":  "fNNN_a0"
    }
  },
  "pow": {
    "algorithm":           "Hashcash v1 + SHA-1",
    "library":             "rust-hashcash/0.3.3 (sha1 feature)",
    "stamp_format":        "1:bits:date:resource:ext:rand:counter",
    "date_format":         "YYYYMMDD (UTC)",
    "verified_in_wasm":    true,
    "wasm_evidence":       "SHA-1 K-constants 0x5A827999 and 0x6ED9EBA1 found as i32.const literals in code section"
  }
}
```

### Field reference

  - **`hsj.n_key` / `response_decrypt_key` / `payload_encrypt_key`** —
    HSJ's three master keys, extracted by AST-patching the bundle's
    AES key-schedule stack frame.
  - **`hsw.encrypt_key` / `decrypt_key`** — HSW's encrypt/decrypt keys,
    extracted by WASM-bytecode-patching the key schedule used by
    `encrypt_req_data` / `decrypt_resp_data`. Verified by AES-256-GCM
    round-trip against the bundle.
  - **`hsw.n_key`** — HSW's n-token AES master key, captured directly
    at the AES encrypt entry's `arg0` (the key-buffer pointer). On
    each build the entry function index rotates (`fn 330` on one
    build, `fn 437` on another, etc); `extraction_status.hsw_n_key`
    records which ring won. Build-static — constant across every
    capture record within the same build.
  - **`hsw.fingerprint_blob_key`** — `sha256(hsw.n_key)`. A
    deterministic build identifier suitable for de-duplicating
    sightings of the same build across deployments.
  - **`verified`** — all four HSW keys are verified on every
    extraction. HSJ keys are bundle-witnessed (the bundle's own AES
    output is the witness during AST-patch extraction) so they don't
    appear in this block.
  - **`extraction_status.hsw_n_key_meta.live_n_token_b64`** — the
    base64 n-token captured during this extraction run. **Truncated
    in the committed snapshot** so the JSON stays compact; re-run
    `python -m hcaptcha` for the full value.
  - **`pow`** — describes the Hashcash + SHA-1 algorithm used by the
    PoW side of HSW. See [`hsw_pow.py`](../src/hcaptcha/hsw_pow.py)
    for the pure-Python solver.

`data/keys.json` is updated only when the build hash rotates — no
churn-commits on no-op runs. Per-build archive snapshots are kept
forever in `data/archive/`.
