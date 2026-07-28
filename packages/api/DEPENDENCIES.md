# API runtime dependency notes

Task 7 adds the following production dependencies, all pinned in
`requirements.lock`:

| Dependency | Purpose | License |
| --- | --- | --- |
| `pypdf` 6.14.2 | PDF validation, page limits and text-layer extraction | BSD-3-Clause |
| `python-docx` 1.2.0 | DOCX paragraph and table extraction | MIT |
| `reportlab` 5.0.0 | Deterministic server-side PDF rendering | BSD |
| `cos-python-sdk-v5` 1.9.44 | Tencent COS signed upload/download and object access | MIT |

Their transitive runtime packages are also fully pinned. `fonttools` is not a
runtime dependency; it was used only to create the static Noto Sans SC Regular
asset. The reproducible command and hashes are recorded beside the font.
