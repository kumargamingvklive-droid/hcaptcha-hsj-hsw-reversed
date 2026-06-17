# tools/experiments/ — n-token solve

The n-token cipher is **solved**: AES-256-GCM, master key recovered and verified
against the live keystream. Full write-up in
[`docs/19-ntoken-cipher-solved.md`](../../docs/19-ntoken-cipher-solved.md);
production recovery is `hcaptcha.hsw_n_token_decrypt.recover_ntoken_master_live()`.

The long dead-end investigation trail (~85 one-shot scripts) was pruned. What's
kept are the two reproducible end-to-end drivers:

| Script | Purpose |
| --- | --- |
| `solve_ntoken.py` | One live `window.hsw(jwt)` run: capture the n-token AES fn's bitsliced round-key array, invert the fixslice schedule to the master, verify (120/120 schedule match + AES(M,ctr)==live keystream per block), and decrypt the token. |
| `confirm_thirdparty.py` | Recover the master from one token's round keys, then verify it reproduces a **second, independently generated** token's keystream (different IV) — proves the key decrypts third-party n-tokens of the same build. |

`ntoken_master_SOLVED.json` / `thirdparty_proof.json` are the captured proof
artifacts. The key rotates per asset build; re-run against the current build.
