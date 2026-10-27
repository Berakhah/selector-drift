# vendor/

## bok_core-0.0.1-py3-none-any.whl

A pre-built wheel of `bok-core` 0.0.1 (Apache-2.0), the shared boundary, provenance and
findings primitives from the backoffice-kit family. It is vendored here because bok-core is
not on PyPI yet (P-1), and a path to a sibling checkout cannot exist inside a container
image. `pyproject.toml` references it with a repo-relative path under `[tool.uv.sources]`,
so `uv sync --frozen` works from a clean checkout and inside the worker and bench-scorer
images (both Dockerfiles `COPY vendor ./vendor` before the first `uv sync`).

| | |
|---|---|
| Source repo | https://github.com/Berakhah/backoffice-kit (private) |
| Branch | `bok-core-minimal` |
| Commit | `3a4044d` |
| Package | `src/bok_core` (name `bok-core`, version `0.0.1`, hatchling) |
| sha256 | `f3fca74c90f92d1001a2249b4205e1237bb14740cf69bad2f3743043ef637ddf` |
| Licence | Apache-2.0 (`License-Expression` in the wheel's `METADATA`) |

The wheel's `bok_core-0.0.1.dist-info/licenses/LICENSE` travels inside it; keep the wheel
unmodified so the licence text stays with the binary.

### Build command

Always build from a `git archive` export of the commit, never from a working tree. The
sibling repo's committed blobs use CRLF line endings (9 of the 14 `.py` files in the wheel
are CRLF; that matches the commit and is not a defect), and a checkout under `autocrlf`
would normalise them and change the wheel hash.

```sh
mkdir bok-core-src
git -C <path-to-backoffice-kit> archive 3a4044d | tar -x -C bok-core-src
docker run --rm -v "$PWD/bok-core-src:/src" -w /src \
  ghcr.io/astral-sh/uv:python3.12-bookworm-slim \
  uv build --wheel --out-dir /src/dist
```

(The image tag `ghcr.io/astral-sh/uv:0.11.21-python3.12-bookworm-slim` does not exist; the
build used the floating `python3.12-bookworm-slim` tag, which shipped uv 0.9.30. A wheel
build is pure Python and independent of the uv version.)

Output: `dist/bok_core-0.0.1-py3-none-any.whl`.

Rebuild check: a rebuild from commit `3a4044d` should reproduce the sha256 above. If it
differs, unzip both wheels and compare file contents (a timestamp or tooling difference in
the archive is harmless; a content difference is not).

### How to rebuild or refresh

1. Pick the new bok-core commit and export it as above.
2. Build the wheel; if the version changed, the filename changes too.
3. Confirm the refreshed wheel is source-identical to the commit: unzip it and diff its
   `bok_core/**/*.py` against `src/bok_core` in the `git archive` export (byte-identical).
4. Replace the file in `vendor/`, update the path in `[tool.uv.sources]` in
   `pyproject.toml`, update the commit and sha256 in this file.
5. `uv lock` (do not pass `--upgrade`) and commit `vendor/`, `pyproject.toml`, `uv.lock`.

### How to switch back to PyPI

When bok-core is released: delete `vendor/`, change the dependency to
`bok-core==<version>`, remove the `bok-core` entry from `[tool.uv.sources]`, remove the two
`COPY vendor ./vendor` lines from the Dockerfiles, then `uv lock`.
