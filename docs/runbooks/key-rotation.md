# Key rotation

| Key | Protects | Rotate |
|---|---|---|
| `UNIONDEB_FIELD_KEY` | Raw rows of ingest errors, case packs, extract pseudonym maps | Yearly, and at once if exposed |
| `UNIONDEB_AUDIT_ANCHOR_KEY` | The signature on audit anchors written outside the database | Yearly, and after any audit incident |
| `UNIONDEB_PSEUDONYM_KEY` | Stable pseudonyms on alerts and dashboards | Only if exposed (see below) |
| Database credentials | Everything | Per the platform's policy, and after any incident |
| OIDC signing keys | Sign-in | Managed by the identity provider; the API picks up new keys within an hour |

## Field key

```bash
uniondeb store keygen                       # prints a new key; put it in the secret store
UNIONDEB_FIELD_KEY=<current> UNIONDEB_FIELD_KEY_NEW=<new> uniondeb store rotate-key
```

`rotate-key` re-encrypts every encrypted property and prints how many of each it changed. It can be
re-run: values already under the new key are skipped. If it stops with "could not be decrypted", a
value is under neither key; nothing has been lost, so find the right old key before continuing.
Then set `UNIONDEB_FIELD_KEY` to the new key everywhere and restart `api` and `worker`. Backups
taken before the rotation still need the old key: keep it, sealed, until those backups expire.

## Audit anchor key

Write a final anchor with the old key, switch the key, write a new anchor at once, and keep both
files. `audit verify --anchor` must be run with the key that signed the anchor it is given.

## Pseudonym key

Changing it changes every pseudonym, so trends on dashboards break and open alerts no longer match
new scans. If it is exposed, rotate anyway: an exposed key lets anyone with the roster recompute who
a pseudonym is. Close or re-raise open alerts afterwards.
