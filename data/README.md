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

Each JSON file has the same schema returned by `python -m hcaptcha`:

```json
{
  "version": "<64-char build hash>",
  "hsj": {
    "n_key":                "<64 hex>",
    "response_decrypt_key": "<64 hex>",
    "payload_encrypt_key":  "<64 hex>"
  },
  "hsw": {
    "encrypt_key": "<64 hex>",
    "decrypt_key": "<64 hex>"
  },
  "cipher":      "AES-256-GCM",
  "wire_format": {
    "hsj": "ct(N) || tag(16) || iv(12) || 0x00",
    "hsw": "iv(12) || ct(N) || tag(16)"
  },
  "verified":    { "hsw_encrypt_key": true }
}
```

`data/keys.json` is updated only when the build hash rotates — no churn-commits
on no-op runs.
