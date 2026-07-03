# Releasing bean

bean ships two things from one repo, versioned together:

- the **`bean` Python package** (`pyproject.toml`, hatchling) — the CLI/engine, and
- the **Claude Code plugin** (`.claude-plugin/plugin.json`, `SKILL.md`, `skills/`, `scripts/bean.py`)
  — distributed as the repo itself; the marketplace entry points at `./`.

A release = a single version bumped across both manifests, a green offline test suite, built
artifacts, and a git tag `vX.Y.Z`. It is driven by `dev/release.py` (pure stdlib) or `make`.

## Versioning

- **Semver.** `MAJOR.MINOR.PATCH`; pre-release suffixes like `0.2.0-rc1` are allowed.
- **Single source of truth: `pyproject.toml`.** `.claude-plugin/plugin.json` mirrors it. The tools
  keep them in lockstep and `check` fails on drift — never hand-edit one without the other.

## Cut a release

Prereqs: the plugin venv (`make venv`, or any `bean.py` run bootstraps it) and the `build` package
(`make build` installs it, or `pip install -e '.[dev]'`).

```
make check                          # gate: versions in sync + tests + byte-compile
make release VERSION=0.2.0          # DRY RUN — prints the plan, changes nothing
make release VERSION=0.2.0 YES=1    # bump both manifests, check, build, commit "release: v0.2.0", tag v0.2.0
git push && git push origin v0.2.0
```

`make release … YES=1` runs, in order: set version → `check` (tests must pass) → `build` (wheel +
sdist into `dist/`) → one commit → one tag. It refuses to run on a dirty tree or an existing tag.

Equivalent without make:

```
python3 dev/release.py check
python3 dev/release.py cut 0.2.0 --yes
```

### After tagging

- **Plugin users** get the release by pointing at the repo and installing:
  `/plugin marketplace add <repo>` then `/plugin install bean@bean`. A tag pins a known-good version;
  `/plugin` can update to it.
- **Python package (optional).** The built `dist/*.whl` + `*.tar.gz` can go to PyPI with
  `twine upload dist/*` (not automated here — bean is primarily consumed as a plugin, not `pip install`).

## Individual commands

```
python3 dev/release.py version            # print current version
python3 dev/release.py version 0.2.0       # set it in pyproject + plugin.json only
python3 dev/release.py check               # version-sync + tests + compile
python3 dev/release.py build               # wheel + sdist into dist/
make clean                                     # remove dist/ build/ *.egg-info + caches
```

## Pre-release checklist

- `make check` is green (tests are fully offline — fake HTTP, fake embedder, real DuckDB + Lance).
- README / SKILL.md reflect any new commands or config.
- The Changelog below has an entry for the version.
- Working tree is clean.

## Changelog

Newest first. Dates are the tag date.

### Unreleased
- **Connectors + retrieval batch** — the 11 built-in connectors moved to `bean/connectors/` and
  Notion was removed (**10 core** now); Google Drive indexes each comment as its own
  author-attributed, timestamped document (`gdocs.comments`); the embedder is pluggable under
  `embedding` (default fast `model2vec`/`minishlab/potion-retrieval-32M`, plus `fastembed` and a
  `plugin` escape hatch); new `bean sql` runs read-only SELECT/WITH over the DuckDB store (no query
  prints the schema, `--global` for the shared store); sync checkpoints the embed phase per-document
  so an interrupted run resumes cleanly; local files gained `.pptx`/`.xlsx`/`.html`; each connector
  ships a guided `skills/connect-<name>/` setup skill; Lance scalar + vector indexes; `release.py`
  moved to `dev/`.
- **CLI simplification + per-source chunking** — removed `bean add`/`remove`/`reembed`; `sync --full`
  is now `sync --rebuild` (re-fetches AND re-embeds every doc, absorbing reembed); all `--json` output
  and the `--no-hybrid` flag removed (commands are human-readable; `bean init` prints setup detail as
  text); chunking is now per-source configurable (global `chunking` defaults + `<source>.chunking`
  overrides, Slack smaller by default).
- **Smart lookback windows** — `lookback_days` is now the one-time *initial backfill*, chosen at
  setup, not a per-sync window. Every windowed source (`slack`/`discord`/`gdocs`) reaches back
  `lookback_days` on its first sync, then tracks a cursor and pulls only what's new; `--rebuild` still
  reaches back `--since`. Slack/Discord no longer re-fetch a rolling 14-day window each sync, and
  Google Drive gained the same cursor. Lookback is a per-connector setting (resolved connector-first
  then from settings), and `bean init` surfaces each source's first-sync lookback.
- **README** — install command up top (`/plugin marketplace add` + `/plugin install`); dropped the
  Development section; Notion no longer listed in the connector table.
- **Google Drive PDFs** — the Drive connector now indexes native PDFs (owned files + tracked
  folders), downloaded and run through the shared PDF extractor (`bean/pdf.py`, honoring
  `ocr.backend`) — the same path local-file PDFs use. Adds a `content` bytes carrier to the HTTP
  `Response` so binary downloads survive the injectable fetch seam.
- **CI/release workflows** — `.github/workflows/ci.yml` (build + offline tests on push/PR across
  Python 3.10–3.12) and `release.yml` (rebuilds artifacts and publishes a GitHub Release on a
  `vX.Y.Z` tag). Both reuse `dev/release.py check`/`build`.
- **GitHub connector: issues + PRs only** — dropped repo Markdown indexing; the connector now tracks
  issues and pull requests (body + comments). Narrowing `include` prunes previously-indexed docs on
  the next sync.
- **One copy of chunk data** — dropped the DuckDB chunk mirror; chunks live only in Lance and the
  keyword/neighbour/merge queries run as DuckDB SQL directly over the Lance dataset (adds `pylance`).
  No reembed needed (chunk `ord` is derived).
- **Connector scope** — global (shared `~/.bean/_global/` index, every repo) vs local (per-repo);
  `bean scope`, `add --global/--local`, search unions both.
- **Retrieval upgrades** — weighted multi-query RRF (`--variant`), query-type routing, recency bias,
  section merge, chunk title-prefix + large chunks, optional local cross-encoder reranker, and a
  metadata-derived graph (`bean related`, `--author/--since/--before`).
- **Stale-index warning** — read commands warn when the index is older than `sync.stale_days` (7);
  bean never auto-syncs.
- **Plugin system** — core connectors plus drop-in plugins from `~/.bean/plugins/`, with
  `docs/authoring-connectors.md` (the authoring guide).
- **Release tooling** — `dev/release.py`, `Makefile`, this document.

### 0.1.0
- Initial: Slack, Google Docs/Drive, Notion, GitHub, local files (Markdown/office/PDF+OCR); hybrid
  vector + keyword search over DuckDB + Lance; per-repo workspaces; per-user credentials.
