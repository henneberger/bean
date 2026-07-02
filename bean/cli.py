"""bean CLI — the mechanism the Claude Code plugin drives (it is not meant to be used directly
from a terminal; the plugin calls these subcommands to retrieve context).

  bean init                       Connection status + exact next steps
  bean auth <provider> [--token]  google | slack | notion | github
  bean add <ref>                  Doc/Drive URL, #channel, Notion page, owner/repo, or a path
  bean remove <ref>
  bean sync [source] [--full] [--since N]
  bean search "question" [--source S] [--doc SUBSTR] [--k N] [--expand N] [--no-hybrid] [--json]
  bean recent [--source S] [--doc SUBSTR] [--limit N] [--json]
  bean thread <ref> [--source S] [--json]     Whole thread / document as one block
  bean doc <ref> [--source S] [--json]        Full document body
  bean neighbors <chunk-id> [--radius N] [--json]
  bean config [get PATH | set PATH VALUE | list]
  bean reembed                    Re-chunk + re-embed everything with the current settings
  bean status [--json]

All state lives under ~/.bean/<repo-name>-<hash>/ — one workspace per repo. Credentials are
per user at ~/.bean/credentials/ (mode 0600). Configuration is files, never env vars.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as cfgmod
from .search import document, neighbors, recent, search, thread
from .sources import SOURCES, route_add
from .store import Store
from .workspace import Workspace

SOURCE_KEYS = [s.key for s in SOURCES]
AUTH = {s.auth: s for s in SOURCES if s.auth}


def _ensure_lists(config: dict) -> dict:
    for s in SOURCES:
        node = config.setdefault(s.config_key, {})
        for name in s.lists:
            node.setdefault(name, [])
    return config


def _tracked(config: dict, src) -> int:
    node = config.get(src.config_key) or {}
    return sum(len(node.get(name) or []) for name in src.lists)


# -- init / status ------------------------------------------------------------------------------
def cmd_init(ws: Workspace, args) -> int:
    config = _ensure_lists(ws.load_config())
    print(f"bean workspace: {ws.dir}  (repo: {ws.repo})\n")
    any_connected = False
    for s in SOURCES:
        conn = s.connected() if s.connected else {"local": True}
        tracked = _tracked(config, s)
        mark = "x" if (conn and (tracked or s.always_when_connected)) else " "
        if conn:
            any_connected = True
            if s.auth:
                who = f" ({conn.get('account') or conn.get('login') or conn.get('user') or conn.get('bot')})"
                head = "connected" + who
            else:
                head = "ready (local, no auth)"
            state = f"{head} — {tracked} tracked" if tracked else f"{head} — add sources: bean add {s.add_help}"
        else:
            verb = "bean auth " + s.auth
            state = f"→ {verb}" + ("" if s.interactive_auth else " --token …")
        print(f"[{mark}] {s.label:<13} {state}")
    if any_connected:
        print("\nThen: bean sync   (first sync downloads the embedding model once)")
        print('Ask:  bean search "how do refunds work?"')
    return 0


def cmd_status(ws: Workspace, args) -> int:
    config = _ensure_lists(ws.load_config())
    with Store(ws) as store:
        counts = store.counts()
        indexed_model = store.get_state("embedding.model")
    settings = cfgmod.resolve(ws)
    sources = {}
    for s in SOURCES:
        conn = s.connected() if s.connected else {"local": True}
        node = config.get(s.config_key) or {}
        sources[s.key] = {"connected": bool(conn), "tracked": _tracked(config, s),
                          "indexed": counts.get(s.key, 0),
                          "lists": {name: node.get(name) or [] for name in s.lists}}
    payload = {"workspace": str(ws.dir), "repo": str(ws.repo),
               "embedding": {"configured": settings["embedding"]["model"], "indexed_with": indexed_model},
               "sources": sources}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"workspace: {ws.dir}")
    em = settings["embedding"]["model"]
    warn = "" if (not indexed_model or indexed_model == em) else f"  ⚠ index built with {indexed_model} — run `bean reembed`"
    print(f"embedding: {em}{warn}")
    for s in SOURCES:
        info = sources[s.key]
        conn = "local" if not s.auth else ("connected" if info["connected"] else "not connected")
        print(f"{s.label:<13} {conn:<15} tracked={info['tracked']} indexed={info['indexed']}")
    return 0


# -- auth / add / remove ------------------------------------------------------------------------
def cmd_auth(ws: Workspace, args) -> int:
    src = AUTH.get(args.provider)
    if not src:
        print(f"Unknown provider. Choose from: {', '.join(AUTH)}", file=sys.stderr)
        return 2
    try:
        if src.interactive_auth:
            src.connect()
        else:
            if not args.token:
                print(f"Usage: bean auth {args.provider} --token <token>", file=sys.stderr)
                return 2
            src.connect(args.token)
    except Exception as err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    return 0


def cmd_add(ws: Workspace, args) -> int:
    routed = route_add(args.item)
    if not routed:
        print("Not a recognized ref. Expected a Google Doc/Drive URL, #channel, Notion page URL,\n"
              "owner/repo (or github.com URL), or a file/folder path.", file=sys.stderr)
        return 2
    src, list_key, value = routed
    config = _ensure_lists(ws.load_config())
    target = config[src.config_key][list_key]
    if value not in target:
        target.append(value)
    ws.save_config(config)
    print(f"✓ tracking {args.item} in {src.label} — next: bean sync")
    return 0


def cmd_remove(ws: Workspace, args) -> int:
    config = _ensure_lists(ws.load_config())
    routed = route_add(args.item)
    candidates = {args.item, args.item.lstrip("#")}
    if routed:
        candidates.add(routed[2])
    removed = False
    for s in SOURCES:
        for name in s.lists:
            lst = config[s.config_key][name]
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


# -- sync / reembed -----------------------------------------------------------------------------
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


def cmd_reembed(ws: Workspace, args) -> int:
    from .sync import reembed
    r = reembed(ws, log=lambda m: print(f"  · {m}", file=sys.stderr))
    print(f"✓ re-embedded {r['docs']} document(s) → {r['chunks']} chunk(s) with {r['model']}.")
    return 0


# -- retrieval ----------------------------------------------------------------------------------
def _print_hits(query: str | None, hits: list[dict], as_json: bool, empty: str) -> int:
    if as_json:
        print(json.dumps(hits, indent=2))
        return 0 if hits else 1
    if not hits:
        print(empty)
        return 1
    if query:
        print(f'bean: "{query[:100]}"')
    for i, h in enumerate(hits, 1):
        where = f'{h.get("title") or h["doc_id"]}' + (f'  <{h["url"]}>' if h.get("url") else "")
        score = f"  (score {h['score']})" if h.get("score") is not None else ""
        print(f"\n{i:2}. {where}{score}")
        text = h.get("context") or h.get("text") or ""
        for line in [l.strip() for l in text.splitlines() if l.strip()][:5]:
            print(f"      {line[:110]}")
    return 0


def cmd_search(ws: Workspace, args) -> int:
    query = " ".join(args.query)
    if not query:
        print('Usage: bean search "your question"', file=sys.stderr)
        return 2
    hits = search(ws, query, k=args.k, source=args.source, doc_like=args.doc,
                  expand=args.expand, hybrid=not args.no_hybrid)
    return _print_hits(query, hits, args.json,
                       "No matches. Have you run `bean sync`? (`bean status` shows what's indexed.)")


def cmd_recent(ws: Workspace, args) -> int:
    hits = recent(ws, source=args.source, doc_like=args.doc, limit=args.limit)
    return _print_hits(None, hits, args.json, "Nothing indexed yet — run `bean sync`.")


def cmd_thread(ws: Workspace, args) -> int:
    hits = thread(ws, args.ref, source=args.source)
    return _print_hits(None, hits, args.json, f'No thread/document matching "{args.ref}".')


def cmd_doc(ws: Workspace, args) -> int:
    hits = document(ws, args.ref, source=args.source)
    return _print_hits(None, hits, args.json, f'No document matching "{args.ref}".')


def cmd_neighbors(ws: Workspace, args) -> int:
    hits = neighbors(ws, args.chunk_id, radius=args.radius)
    return _print_hits(None, hits, args.json, f'No chunk "{args.chunk_id}".')


# -- config -------------------------------------------------------------------------------------
def cmd_config(ws: Workspace, args) -> int:
    if args.action in (None, "list", "get"):
        merged = cfgmod.resolve(ws)
        if args.action == "get" and args.path:
            print(json.dumps(cfgmod.get(merged, args.path)))
            return 0
        print(json.dumps(merged, indent=2))
        return 0
    if args.action == "set":
        if not args.path or args.value is None:
            print("Usage: bean config set <path> <value>", file=sys.stderr)
            return 2
        g = cfgmod.load_global()
        cfgmod.set_in(g, args.path, args.value)
        cfgmod.save_global(g)
        print(f"✓ {args.path} = {json.dumps(cfgmod.get(cfgmod.resolve(ws), args.path))}")
        if args.path.startswith(("embedding.", "chunking.")):
            print("  (changes the index shape — run `bean reembed` to apply it to existing docs.)")
        return 0
    print(f"Unknown config action. Known paths:\n  " + "\n  ".join(cfgmod.known_paths()), file=sys.stderr)
    return 2


# -- parser -------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bean", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="connection status + next steps").set_defaults(fn=cmd_init)

    p = sub.add_parser("auth", help="connect a provider")
    p.add_argument("provider", choices=sorted(AUTH))
    p.add_argument("--token")
    p.set_defaults(fn=cmd_auth)

    p = sub.add_parser("add", help="track a doc/channel/page/repo/path")
    p.add_argument("item")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("remove", help="stop tracking a ref")
    p.add_argument("item")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("sync", help="fetch changes and re-embed them")
    p.add_argument("source", nargs="?", choices=SOURCE_KEYS)
    p.add_argument("--full", action="store_true")
    p.add_argument("--since", type=int, default=90)
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("reembed", help="re-embed everything with current settings")
    p.set_defaults(fn=cmd_reembed)

    p = sub.add_parser("search", help="hybrid semantic + keyword search")
    p.add_argument("query", nargs="+")
    p.add_argument("--k", type=int, default=None)
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--doc", help="restrict to docs whose id/name contains this substring")
    p.add_argument("--expand", type=int, default=None, help="neighbouring chunks pulled in per hit")
    p.add_argument("--no-hybrid", action="store_true", help="vector only (skip keyword fusion)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("recent", help="most recently changed docs/messages")
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--doc", help="filter by doc id/name substring (e.g. a #channel)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_recent)

    p = sub.add_parser("thread", help="a whole thread/document as one block")
    p.add_argument("ref")
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_thread)

    p = sub.add_parser("doc", help="full document body")
    p.add_argument("ref")
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doc)

    p = sub.add_parser("neighbors", help="chunks surrounding a chunk id")
    p.add_argument("chunk_id")
    p.add_argument("--radius", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_neighbors)

    p = sub.add_parser("config", help="view or set configuration")
    p.add_argument("action", nargs="?", choices=["get", "set", "list"])
    p.add_argument("path", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("status", help="workspace, auth, and index state")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    return args.fn(Workspace(), args)


if __name__ == "__main__":
    raise SystemExit(main())
