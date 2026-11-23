# Vendored code

`tomli/` is **tomli 2.5.0**, copied unchanged from the source distribution `tomli-2.5.0.tar.gz`
(<https://pypi.org/project/tomli/>, SHA-256 of the archive `264507556cd8b8c8e7c6ee037cdf443a463f03f4c958e57195e3d369711b8ff6`).
It is the pure-Python TOML parser that became Python 3.11's `tomllib`; the licence is MIT (`tomli/LICENSE`).

`transcript_metrics.py` uses the standard library's `tomllib` and falls back to this copy on Python 3.9 and 3.10, which have no `tomllib`.

Do not edit these files. `tests/test_vendored_tomli.py` compares each file's SHA-256 with `tests/fixtures/vendored_tomli.sha256`, and on Python 3.11 and newer checks that this copy parses every built-in mapping, and every string in the tests, exactly as `tomllib` does. To update tomli, replace the files from a new release, update this note and the list of hashes, and run the tests.
