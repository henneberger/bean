"""Seed a throwaway bean workspace with known content, offline and deterministic.

Called in-process by run_eval.py *after* it has pointed bean_home at a temp dir. Uses the same
`Store.upsert` + `run_sync(full=True, refetch=False)` path the real test harness uses, and the
deterministic eval_embed plugin so no model is ever downloaded. Returns nothing — the expected
facts live next to each scenario in scenarios.py.
"""

from __future__ import annotations

import os
from pathlib import Path

from bean import config as cfgmod
from bean.store import Store
from bean.sync import run_sync
from bean.workspace import Workspace

import eval_embed

EMBED_PLUGIN = str(Path(__file__).resolve().parent / "eval_embed.py")

# (source, doc_id, title, url, body, modified_at) — a small cross-source knowledge base that the
# scenarios interrogate. Content is generic sample data; nothing here is anyone's real workspace.
DOCS = [
    ("slack", "slack:C_INC:1712000000",
     "#incidents: checkout 5xx spiking",
     "https://acme.slack.com/archives/C_INC/p1712000000",
     "@pager (14:22): checkout 5xx spiking — error rate 18%\n"
     "@sam (14:24): started right after the retry change merged\n"
     "@maya (14:26): retries are hammering the payments API, no backoff. tracking as ZQ-9001",
     "2026-07-04T14:26:00Z"),
    ("gdocs", "gdocs:launch",
     "Launch Plan",
     "https://docs.google.com/document/d/launch",
     "Go-to-market plan for the Q3 launch. The goal of the docs site is to book a sales demo. "
     "The sign-up flow should match the new design.",
     "2026-07-01T09:00:00Z"),
    ("gdocs", "gdocs:onboarding",
     "Onboarding Guide",
     "https://docs.google.com/document/d/onboarding",
     "New engineer onboarding. During first-run setup, connect Google and Slack so the index has "
     "something to search. Then run a sync.",
     "2026-06-28T09:00:00Z"),
    ("localfiles", "localfiles:src/checkout/retry.py",
     "src/checkout/retry.py",
     None,
     "def retry_loop(req):\n"
     "    # retries on a fixed delay, no exponential backoff or jitter\n"
     "    for attempt in range(RETRY_BUDGET):\n"
     "        resp = payments_client.charge(req)\n"
     "        if resp.ok:\n"
     "            return resp\n",
     "2026-07-03T12:00:00Z"),
    ("localfiles", "localfiles:config/timeouts.yaml",
     "config/timeouts.yaml",
     None,
     "retry_budget: 6\nrequest_timeout_ms: 800\n",
     "2026-07-03T12:00:00Z"),
    ("github", "github:issue-42",
     "issue #42: checkout error budget breached",
     "https://github.com/acme/store/issues/42",
     "Checkout 5xx breached the error budget after the retry change landed. We should add a "
     "circuit breaker in payments_client and back off on retries.",
     "2026-07-04T15:00:00Z"),
]


def build() -> Workspace:
    """Seed the fixture into the current bean_home and return its workspace."""
    # Point the live CLI's query-embedder at the same deterministic plugin.
    cfg = cfgmod.load_global()
    cfgmod.set_in(cfg, "embedding.plugin", EMBED_PLUGIN)
    cfgmod.save_global(cfg)

    ws = Workspace(Path(os.getcwd()))
    with Store(ws) as store:
        for source, doc_id, title, url, body, modified_at in DOCS:
            store.upsert(source, doc_id, title=title, url=url, revision_id="1",
                         body=body, meta={"modified_at": modified_at})
    run_sync(ws, full=True, refetch=False, embed_fn=lambda texts: eval_embed.embed(texts),
             log=lambda m: None)
    return ws
