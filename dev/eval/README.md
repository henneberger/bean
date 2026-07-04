# Plugin eval harness

The offline suite (`tests/test_bean.py`) covers bean's **engine** — sync, hybrid ranking, workspace
hygiene — with a deterministic fake embedder. It can't cover the **plugin layer**: whether an LLM,
handed `skills/bean/SKILL.md`, actually picks the right retrieval commands and cites its sources.

This harness does. It stands in for Claude Code with a real tool-calling model — **DeepSeek**, whose
API is OpenAI-compatible — driving the same `bean` CLI over a seeded, fully offline fixture.

## How it works

- **`eval_embed.py`** — a deterministic, dependency-free embedder wired in through bean's own
  `embedding.plugin` code hook. The fixture *and* the live CLI both embed with it, so vector search
  is reproducible and nothing is downloaded. (It doubles as a worked example of the plugin hook.)
- **`fixture.py`** — seeds a throwaway workspace with a small cross-source knowledge base (a Slack
  `#incidents` thread, two Google Docs, two source files, a GitHub issue) via the same
  `Store.upsert` + `run_sync` path the unit tests use.
- **`scenarios.py`** — each scenario is a question plus *invariant* checks over the agent's
  transcript: which command it chose, whether the answer cites the fixture doc that holds the answer.
  Never exact-string matching — LLM output varies.
- **`run_eval.py`** — the agent loop: system prompt = `SKILL.md`, one tool (`run_bean`), loop the
  model's tool calls against the fixture, then score. Scenarios run in parallel.

State is isolated per run: `bean_home` and the CLI subprocesses share a temp `$HOME`, so your real
`~/.bean` is never touched.

## Run it

```bash
export DEEPSEEK_API_KEY=sk-…            # or DEEPSEEK_API
.venv/bin/python dev/eval/run_eval.py
```

With no key set it **skips cleanly** (exit 0), so forks and the offline gate stay green. In CI it
runs as its own job with the key supplied from the `DEEPSEEK_API_KEY` repo secret.
