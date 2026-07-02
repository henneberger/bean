"""bean CLI.

  bean init                     Connection status + exact next steps (the /bean init flow)
  bean auth google              Browser sign-in through gcloud
  bean auth slack --token X     Validate + store a Slack user token
  bean add <doc-url|folder-url|#channel>
  bean remove <item>
  bean sync [google|slack] [--full] [--since N]
  bean search "question" [--k N] [--json]
  bean status [--json]

All state lives under ~/.bean/<repo-name>-<hash>/ — one workspace per repo, nothing written
inside the repo itself. Credentials are shared per user at ~/.bean/credentials/ (mode 0600).
"""

from __future__ import annotations

import argparse
import json
import sys

from . import gdocs, slack
from .index import search as lance_search
from .store import Store
from .workspace import Workspace


def _config_lists(config: dict) -> dict:
    google = config.setdefault("google", {})
    google.setdefault("docs", [])
    google.setdefault("folders", [])
    config.setdefault("slack", {}).setdefault("channels", [])
    return config


def cmd_init(ws: Workspace, args) -> int:
    config = _config_lists(ws.load_config())
    g, s = gdocs.connected(), slack.connected()
    tracked = len(config["google"]["docs"]) + len(config["google"]["folders"]) + len(config["slack"]["channels"])
    print(f"bean workspace: {ws.dir}  (repo: {ws.repo})")
    print()
    google_state = ("connected" + (f" as {g['account']}" if g.get("account") else "")) if g \
        else "→ run: bean auth google   (browser sign-in via gcloud; no Google Cloud setup)"
    print(f"[{'x' if g else ' '}] Google  {google_state}")
    print(f"[{'x' if s else ' '}] Slack   {'connected as ' + s.get('user', '?') + ' in ' + s.get('team', '?') if s else '→ run: bean auth slack --token xoxp-…   (user token from your workspace Slack app)'}")
    print(f"[{'x' if tracked else ' '}] Sources {str(tracked) + ' tracked' if tracked else '→ run: bean add <google-doc-url | drive-folder-url | #channel>'}")
    print()
    if g or s:
        print("Then: bean sync    (first sync downloads the embedding model once, ~100 MB)")
        print("Ask:  bean search \"how do refunds work?\"")
    return 0


def cmd_auth(ws: Workspace, args) -> int:
    try:
        if args.provider == "google":
            gdocs.connect()
        elif args.provider == "slack":
            if not args.token:
                print("Usage: bean auth slack --token xoxp-…  (paste your Slack user token)", file=sys.stderr)
                return 2
            slack.connect(args.token)
    except Exception as err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    return 0


def cmd_add(ws: Workspace, args) -> int:
    config = _config_lists(ws.load_config())
    item = args.item
    if item.startswith("#"):
        target = config["slack"]["channels"]
    else:
        ref = gdocs.parse_ref(item)
        if not ref:
            print("Not a Google Doc/folder URL or a #channel. Paste the doc URL from the browser.", file=sys.stderr)
            return 2
        kind, item = ref
        target = config["google"]["folders" if kind == "folder" else "docs"]
    if item not in target:
        target.append(item)
    ws.save_config(config)
    print(f"✓ tracking {args.item} — next: bean sync")
    return 0


def cmd_remove(ws: Workspace, args) -> int:
    config = _config_lists(ws.load_config())
    ref = gdocs.parse_ref(args.item)
    candidates = {args.item, f"#{args.item.lstrip('#')}", ref[1] if ref else args.item}
    removed = False
    for lst in (config["google"]["docs"], config["google"]["folders"], config["slack"]["channels"]):
        for value in list(lst):
            if value in candidates:
                lst.remove(value)
                removed = True
    if not removed:
        print(f'"{args.item}" is not tracked — see bean status.', file=sys.stderr)
        return 2
    ws.save_config(config)
    print(f"✓ untracked {args.item}")
    return 0


def cmd_sync(ws: Workspace, args) -> int:
    from .sync import run_sync
    result = run_sync(ws, only=args.source, full=args.full, since_days=args.since,
                      log=lambda m: print(f"  · {m}", file=sys.stderr))
    for err in result["errors"]:
        print(f"✗ {err}", file=sys.stderr)
    n_changed, n_removed = len(result["changed"]), len(result["removed"])
    if not n_changed and not n_removed:
        print("✓ knowledge base is up to date." if not result["errors"] else "nothing synced.")
    else:
        print(f"✓ {n_changed} document(s) updated, {n_removed} removed — {result['chunks']} chunk(s) embedded.")
    return 1 if result["errors"] else 0


def cmd_search(ws: Workspace, args) -> int:
    query = " ".join(args.query)
    if not query:
        print('Usage: bean search "your question"', file=sys.stderr)
        return 2
    from .embed import embed_query  # lazy: model load only on search
    hits = lance_search(ws, embed_query(query), k=args.k, source=args.source)
    if args.json:
        print(json.dumps(hits, indent=2))
        return 0
    if not hits:
        print("No matches. Have you run `bean sync`? (`bean status` shows what's indexed.)")
        return 1
    print(f'bean: "{query[:100]}"')
    for i, h in enumerate(hits, 1):
        where = f'{h["title"]}' + (f'  <{h["url"]}>' if h["url"] else "")
        print(f"\n{i:2}. {where}  (score {h['score']})")
        for line in [l.strip() for l in h["text"].splitlines() if l.strip()][:4]:
            print(f"      {line[:110]}")
    return 0


def cmd_status(ws: Workspace, args) -> int:
    config = _config_lists(ws.load_config())
    g, s = gdocs.connected(), slack.connected()
    with Store(ws) as store:
        counts = store.counts()
    payload = {
        "workspace": str(ws.dir), "repo": str(ws.repo),
        "google": {"connected": bool(g), "account": (g or {}).get("account"),
                   "docs": config["google"]["docs"], "folders": config["google"]["folders"],
                   "indexed": counts.get("gdocs", 0)},
        "slack": {"connected": bool(s), "user": (s or {}).get("user"), "team": (s or {}).get("team"),
                  "channels": config["slack"]["channels"], "indexed_weeks": counts.get("slack", 0)},
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"workspace: {ws.dir}")
    print(f"google : {'connected' if g else 'not connected — bean auth google'}"
          f"  docs={len(config['google']['docs'])} folders={len(config['google']['folders'])} indexed={counts.get('gdocs', 0)}")
    print(f"slack  : {'connected (' + (s or {}).get('user', '?') + ' @ ' + (s or {}).get('team', '?') + ')' if s else 'not connected — bean auth slack --token …'}"
          f"  channels={len(config['slack']['channels'])} weeks indexed={counts.get('slack', 0)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bean", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="connection status + next steps").set_defaults(fn=cmd_init)

    p = sub.add_parser("auth", help="connect Google or Slack")
    p.add_argument("provider", choices=["google", "slack"])
    p.add_argument("--token", help="Slack user token (xoxp-…)")
    p.set_defaults(fn=cmd_auth)

    p = sub.add_parser("add", help="track a Google Doc / Drive folder / #channel")
    p.add_argument("item")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("remove", help="stop tracking an item")
    p.add_argument("item")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("sync", help="fetch changes and re-embed them")
    p.add_argument("source", nargs="?", choices=["google", "slack"])
    p.add_argument("--full", action="store_true", help="ignore cursors and re-fetch everything")
    p.add_argument("--since", type=int, default=90, help="first-sync history bound in days (default 90)")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("search", help="ask the knowledge base a question")
    p.add_argument("query", nargs="+")
    p.add_argument("--k", type=int, default=8)
    p.add_argument("--source", choices=["gdocs", "slack"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("status", help="workspace, auth, and index state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    return args.fn(Workspace(), args)


if __name__ == "__main__":
    raise SystemExit(main())
