# Vendored Unicode UTS #39 data

Pinned drop: **Unicode Security Mechanisms 17.0.0**
(`https://www.unicode.org/Public/17.0.0/security/`).

| File | Role |
|---|---|
| `17.0.0/confusables.txt` | UTS #39 visually-confusable mappings (skeleton prototypes) |
| `17.0.0/ReadMe.txt` | Unicode's directory readme for this drop |
| `17.0.0/SHA256SUMS` | SHA-256 of `confusables.txt` |
| `LICENSE` | Unicode License V3 |

Runtime never reads these files. `scripts/generate_confusables.py` compiles
them into `src/nodary/feature_extraction/_confusables_data.py`. See
`docs/DESIGN.md` (Normalization) for the contributor regeneration steps.
