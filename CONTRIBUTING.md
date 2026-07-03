# Contributing to bean

Thanks for helping. bean is a local hybrid-search tool packaged as a Claude Code plugin, so most
changes are Python in `bean/` plus its offline test suite. By taking part you agree to the
[Code of Conduct](CODE_OF_CONDUCT.md).

## How to Contribute

The fastest wins:

- **Add a connector.** bean has no connector for your source? Write one — a single module dropped
  into `~/.bean/plugins/`, no core edits. [`docs/authoring-connectors.md`](docs/authoring-connectors.md)
  is the full guide (contract, helpers, a test recipe, and a template). The 10 built-in connectors in
  [`bean/connectors/`](bean/connectors/) are worked examples across every API shape.
- **Fix a bug or a doc.** Small, focused PRs are the easiest to review.
- **Improve retrieval or a connector's coverage.** Open an issue first if it changes behaviour.

## Development Setup

Everything runs offline and on CPU — no accounts, no network, no GPU.

```bash
git clone https://github.com/henneberger/bean.git
cd bean
make venv        # bootstrap .venv and install bean into it (first run only)
make test        # run the offline test suite (fake HTTP, fake embedder, real DuckDB + Lance)
```

Run the CLI the same way the plugin does:

```bash
python3 scripts/bean.py status
```

`make check` is the pre-PR gate — it verifies the version is in sync across `pyproject.toml` and
`.claude-plugin/plugin.json`, runs the tests, and byte-compiles the package.

## Pull Request Process

1. Fork and branch from `main`.
2. Make the change with tests. The suite is one file, `tests/test_bean.py`; add a check next to the
   ones for the area you touched. New behaviour without a test won't merge.
3. Run `make check` — it must pass (green tests, version in sync, clean compile).
4. Update the docs you affected: `README.md`, `docs/authoring-connectors.md`, or a
   `skills/connect-<source>/` setup skill.
5. Open a PR describing the what and the why, and link any related issue.

Keep credentials and machine-specific paths out of commits — bean stores those under `~/.bean/`,
never in the repo.

## Reporting Bugs

Open an issue with the steps to reproduce, what you expected versus what happened, and your
environment (OS, Python version, which connector). `python3 scripts/bean.py status` prints the
workspace and index state that's useful to paste in. Search existing issues first to avoid
duplicates. For anything security-related, follow [SECURITY.md](SECURITY.md) instead of opening a
public issue.
