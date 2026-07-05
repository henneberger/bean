"""bean CLI — the mechanism the Claude Code plugin drives (it is not meant to be used directly
from a terminal; the plugin calls these subcommands to retrieve context).

  bean init                       Connection status + exact next steps (paths, fields, lists)
  bean auth <provider> [--token]  google | slack | github | …
  bean sync [source] [--rebuild] [--since N]
  bean search "question" [--source S] [--doc SUBSTR] [--k N] [--expand N]
  bean recent [--source S] [--doc SUBSTR] [--limit N]
  bean thread <ref> [--source S]              Whole thread / document as one block
  bean doc <ref> [--source S]                 Full document body
  bean neighbors <chunk-id> [--radius N]
  bean config [get PATH | set PATH VALUE | list]
  bean cloud init --bucket NAME [--prefix P] [--region R]   Become a cloud writer (push local index to S3)
  bean cloud connect --bucket NAME [--prefix P] [--region R]   Become a cloud consumer (pull from S3)
  bean pull                       Fetch the latest cloud index into the local replica
  bean sql "SELECT …"             Read-only SQL over the store (no query = print the schema)
  bean status

Tracked refs (docs/channels/pages/repos/paths) are written straight into a source's config lists —
`bean init` prints each source's config file + list names. `sync --rebuild` does a full resweep:
it ignores cursors, re-fetches, and re-embeds every doc (so a chunking or embedding-model change
lands on the whole index).

All state lives under ~/.bean/<repo-name>-<hash>/ — one workspace per repo. Credentials are
per user at ~/.bean/credentials/ (mode 0600). Configuration is files, never env vars.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import config as cfgmod
from .search import (document_many, neighbors_many, recent_many, related_many, search_many,
                     thread_many)
from .sources import BY_KEY, SOURCES
from .store import Store
from .workspace import (Workspace, credential_context, credential_path, load_scopes,
                        set_source_scope, source_scope)

SOURCE_KEYS = [s.key for s in SOURCES]
AUTH = {s.auth: s for s in SOURCES if s.auth}

# Subcommands that read the index — these trigger a guarded auto-pull first when cloud-enabled
# (see `main`). Deliberately excludes non-read commands (init/auth/config/scope/cloud/pull/sync/
# plugins/status): `sync` already pulls internally, `pull` is explicit, and the rest don't read
# the catalog.
_READ_CMDS = {"search", "recent", "doc", "thread", "neighbors", "related", "sql"}


def _scope_split():
    """(global_keys, local_keys) over the registered sources, from ~/.bean/scopes.json."""
    scopes = load_scopes()
    glob = {k for k in SOURCE_KEYS if scopes.get(k, "local") == "global"}
    return glob, set(SOURCE_KEYS) - glob


def _retrieval_wss(ws: Workspace) -> list:
    """The workspaces a query should read: the repo workspace, plus the shared global one when any
    connector is global."""
    glob, _ = _scope_split()
    return [ws, Workspace.global_()] if glob else [ws]


def _ensure_lists(config: dict) -> dict:
    for s in SOURCES:
        node = config.setdefault(s.config_key, {})
        for name in s.lists:
            node.setdefault(name, [])
    return config


def _tracked(config: dict, src) -> int:
    node = config.get(src.config_key) or {}
    return sum(len(node.get(name) or []) for name in src.lists)


def _last_sync_age(ws: Workspace):
    """(last_sync_iso, age_in_days) or (None, None) if never synced / unreadable."""
    from datetime import datetime, timezone
    try:
        with Store(ws) as store:
            last = store.get_state("last_sync")
    except Exception:
        return None, None
    if not last:
        return None, None
    try:
        ts = datetime.fromisoformat(last)
    except ValueError:
        return last, None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return last, (datetime.now(timezone.utc) - ts).days


def _worst_age(ws: Workspace):
    """Most-stale last-sync age across the repo workspace and (if any connector is global) the
    shared global one — so a stale global index warns too."""
    glob, _ = _scope_split()
    wss = [ws] + ([Workspace.global_()] if glob else [])
    ages = [a for a in (_last_sync_age(w)[1] for w in wss) if a is not None]
    return max(ages) if ages else None


def _staleness_note(ws: Workspace) -> str | None:
    """A stderr warning when the index is older than `sync.stale_days` — so an assistant reading the
    output nudges the user to sync. bean NEVER auto-syncs; this only warns."""
    days = (cfgmod.resolve(ws).get("sync") or {}).get("stale_days", 7)
    if not days:
        return None
    age = _worst_age(ws)
    if age is not None and age >= days:
        return (f"⚠ bean: last synced {age} days ago (threshold {days}d) — the index may be stale. "
                f"Suggest the user run `/bean sync`; do not run sync yourself.")
    return None


# -- init / status ------------------------------------------------------------------------------
def _cred_path(name: str):
    from .workspace import bean_home
    return bean_home() / "credentials" / f"{name}.json"


def _scope_ctx(ws: Workspace):
    """(scopes, repo_config, global_config, global_ws) — everything init/status need to place each
    source in its scope."""
    gws = Workspace.global_()
    return (load_scopes(), _ensure_lists(ws.load_config()), _ensure_lists(gws.load_config()), gws)


def cmd_init(ws: Workspace, args) -> int:
    """Connection status + the exact next step for each source, in enough detail that the assistant
    can set one up by writing files directly: credential path + fields, config path + the lists that
    hold tracked refs, scope, and (where a source has one) the first-sync lookback prompt."""
    scopes, repo_cfg, glob_cfg, gws = _scope_ctx(ws)
    from .sources import LOOKBACK_DEFAULTS
    settings = cfgmod.resolve(ws)
    print(f"bean workspace: {ws.dir}  (repo: {ws.repo})\n")
    any_connected = False
    for s in SOURCES:
        scope = scopes.get(s.key, "local")
        cfg, cfgws = (glob_cfg, gws) if scope == "global" else (repo_cfg, ws)
        cred_ws = None if scope == "global" else ws
        with credential_context(cred_ws):
            conn = s.connected() if s.connected else {"local": True}
        tracked = _tracked(cfg, s)
        mark = "x" if (conn and (tracked or s.always_when_connected)) else " "
        if conn:
            any_connected = True
            if s.auth:
                ident = (conn.get('account') or conn.get('login') or conn.get('user')
                         or conn.get('bot') or conn.get('name') or conn.get('email')
                         or conn.get('url'))
                head = "connected" + (f" ({ident})" if ident else "")
            else:
                head = "ready (local, no auth)"
            state = f"{head} — {tracked} tracked" if tracked else head
        else:
            state = f"→ bean auth {s.auth}" + ("" if s.interactive_auth else f" {s.auth_help}".rstrip())
        print(f"[{mark}] {s.label:<20} {scope:<6} {state}")
        # Per-source setup detail — the paths + fields the assistant writes to.
        if s.auth:
            print(f"      credential: {credential_path(s.auth, cred_ws)}"
                  + (f"   fields: {s.auth_help}" if s.auth_help else ""))
        lists = "|".join(s.lists)
        print(f"      config:     {cfgws.config_path}  →  {s.config_key}.[{lists}]"
              + (f"   ({s.add_help})" if s.add_help else ""))
        if s.always_when_connected:
            print("      indexes everything once connected; tracked refs only narrow scope")
        if s.key in LOOKBACK_DEFAULTS:
            current = (cfg.get(s.config_key) or {}).get("lookback_days",
                       settings.get(s.key, {}).get("lookback_days", LOOKBACK_DEFAULTS[s.key]))
            print(f"      lookback:   {current}d on first sync (0=all) — "
                  f"set: bean config set {s.key}.lookback_days N")
    print("\nScope: `global` connectors index once and are searchable from every repo; `local` ones "
          "are scoped to this repo (e.g. a GitHub project). **Ask the user per connector**, then set "
          "it with `bean scope <source> global|local`.")
    print("\nSetup is assistant-guided. For each source, either paste the token and let the assistant "
          "run `bean auth …` / write the files, run the printed `bean auth …` yourself, or write the "
          "credential JSON to its `credential:` path and tracked refs into its `config:` file.")
    if any_connected:
        print("\nThen: bean sync ")
        print('Ask:  bean search "how do refunds work?"')
    return 0


def cmd_pull(ws: Workspace, args) -> int:
    """Fetch the latest cloud index into the local replica. No-op (and a loud non-zero exit) when
    `ws` isn't cloud-connected — there's nothing to pull."""
    if not ws.is_cloud:
        print("✗ not a cloud workspace — run `bean cloud init` or `bean cloud connect` first",
              file=sys.stderr)
        return 1
    from . import remote
    remote.pull(ws)
    print(f"✓ pulled {ws.remote_uri} → local replica")
    from datetime import datetime, timezone
    with Store(ws) as store:
        store.set_state("last_pull", datetime.now(timezone.utc).isoformat())
    return 0


def cmd_status(ws: Workspace, args) -> int:
    scopes, repo_cfg, glob_cfg, gws = _scope_ctx(ws)
    with Store(ws) as store:
        rc = store.counts()
        indexed_model = store.get_state("embedding.model")
        last_sync = store.get_state("last_sync")
        last_pull = store.get_state("last_pull")
    with Store(gws) as gstore:
        gc = gstore.counts()
    settings = cfgmod.resolve(ws)
    age = _worst_age(ws)
    stale_days = (settings.get("sync") or {}).get("stale_days", 7)
    stale = bool(stale_days and age is not None and age >= stale_days)
    sources = {}
    for s in SOURCES:
        scope = scopes.get(s.key, "local")
        cfg = glob_cfg if scope == "global" else repo_cfg
        counts = gc if scope == "global" else rc
        with credential_context(None if scope == "global" else ws):
            conn = s.connected() if s.connected else {"local": True}
        node = cfg.get(s.config_key) or {}
        sources[s.key] = {"connected": bool(conn), "scope": scope, "tracked": _tracked(cfg, s),
                          "indexed": counts.get(s.key, 0),
                          "lists": {name: node.get(name) or [] for name in s.lists}}
    print(f"workspace: {ws.dir}")
    if ws.is_cloud:
        role = ws.cloud.get("role", "?")
        last = last_pull or last_sync or "never"
        print(f"cloud:     role={role} remote={ws.remote_uri} last sync/pull={last}")
    from .embed import identity
    em = identity(settings["embedding"])
    warn = "" if (not indexed_model or indexed_model == em) else f"  ⚠ index built with {indexed_model} — run `bean sync --rebuild`"
    print(f"embedding: {em}{warn}")
    sync_line = "never synced" if age is None else f"{age}d ago" + ("  ⚠ stale — run `bean sync`" if stale else "")
    print(f"last sync:  {sync_line}")
    for s in SOURCES:
        info = sources[s.key]
        conn = "local" if not s.auth else ("connected" if info["connected"] else "not connected")
        print(f"{s.label:<13} {info['scope']:<7} {conn:<15} tracked={info['tracked']} indexed={info['indexed']}")
    return 0


# -- auth ---------------------------------------------------------------------------------------
def cmd_auth(ws: Workspace, args) -> int:
    src = AUTH.get(args.provider)
    if not src:
        print(f"Unknown provider. Choose from: {', '.join(AUTH)}", file=sys.stderr)
        return 2
    # Pass along only the credential fields the user actually supplied; each connect() validates
    # that it got what it needs and stores it (base url/email/etc.) in ~/.bean/credentials.
    fields = {k: getattr(args, k, None) for k in
              ("token", "url", "email", "subdomain", "key", "secret", "method")}
    kwargs = {k: v for k, v in fields.items() if v}
    # Save the credential to this source's scope: a local connector's cred lives in this repo's
    # workspace (so a different token per project works); a global connector's is shared.
    cred_ws = None if source_scope(src.key) == "global" else ws
    try:
        with credential_context(cred_ws):
            if src.interactive_auth and not kwargs:
                src.connect()  # browser / device-code flow, no secrets on the command line
            else:
                if not kwargs and not src.interactive_auth:
                    print(f"Usage: bean auth {args.provider} --token <token> {src.auth_help}".rstrip(),
                          file=sys.stderr)
                    return 2
                src.connect(**kwargs)
    except Exception as err:
        print(f"✗ {err}", file=sys.stderr)
        return 1
    return 0


def cmd_scope(ws: Workspace, args) -> int:
    """Show or set whether a connector is global (all repos) or local (this repo). Setting it moves
    the connector's tracked config to the right workspace and purges its old index so the next
    `bean sync` re-indexes it in the new scope."""
    scopes = load_scopes()
    if not args.source:
        for k in SOURCE_KEYS:
            print(f"{k:<14} {scopes.get(k, 'local')}")
        return 0
    key = args.source
    if key not in SOURCE_KEYS:
        print(f"Unknown source {key!r}. Choose from: {', '.join(SOURCE_KEYS)}", file=sys.stderr)
        return 2
    if not args.value:
        print(f"{key}: {scopes.get(key, 'local')}")
        return 0
    new, old = args.value, scopes.get(key, "local")
    if new == old:
        print(f"{key} is already {new}.")
        return 0
    old_ws = Workspace.global_() if old == "global" else ws
    new_ws = Workspace.global_() if new == "global" else ws
    src = BY_KEY[key]
    ocfg, ncfg = _ensure_lists(old_ws.load_config()), _ensure_lists(new_ws.load_config())
    for name in src.lists:  # move tracked items to the new workspace
        for v in ocfg[src.config_key][name]:
            if v not in ncfg[src.config_key][name]:
                ncfg[src.config_key][name].append(v)
        ocfg[src.config_key][name] = []
    old_ws.save_config(ocfg)
    new_ws.save_config(ncfg)
    # Move the credential to the new scope's location (global = shared dir; local = repo workspace).
    if src.auth:
        from .workspace import bean_home
        old_cdir = (bean_home() if old == "global" else ws.dir) / "credentials"
        new_cdir = (bean_home() if new == "global" else ws.dir) / "credentials"
        old_cred = old_cdir / f"{src.auth}.json"
        if old_cred.exists() and old_cdir != new_cdir:
            new_cdir.mkdir(parents=True, exist_ok=True); new_cdir.chmod(0o700)
            new_cred = new_cdir / f"{src.auth}.json"
            old_cred.replace(new_cred)
            new_cred.chmod(0o600)
    from .index import delete_doc  # purge old index (DuckDB + Lance) so a resync repopulates
    with Store(old_ws) as store:
        for d in store.doc_ids(key):
            store.delete(key, d)
            delete_doc(old_ws, key, d)
    set_source_scope(key, new)
    print(f"✓ {key}: {old} → {new}. Run `bean sync` to (re)index it in the {new} store.")
    return 0


# -- sync ---------------------------------------------------------------------------------------
def cmd_sync(ws: Workspace, args) -> int:
    from .sync import run_sync
    glob, loc = _scope_split()
    log = lambda m: print(f"  · {m}", file=sys.stderr)  # noqa: E731
    results = [run_sync(ws, only=args.source, keys=loc, full=args.rebuild, since_days=args.since, log=log)]
    if glob:  # global sources sync into the shared workspace
        results.append(run_sync(Workspace.global_(), only=args.source, keys=glob, full=args.rebuild,
                                since_days=args.since, log=log))
    errors = [e for r in results for e in r["errors"]]
    changed = sum(len(r["changed"]) for r in results)
    removed = sum(len(r["removed"]) for r in results)
    chunks = sum(r["chunks"] for r in results)
    for err in errors:
        print(f"✗ {err}", file=sys.stderr)
    if not changed and not removed:
        print("✓ knowledge base is up to date." if not errors else "nothing synced.")
    else:
        print(f"✓ {changed} document(s) updated, {removed} removed — {chunks} chunk(s) embedded.")
    return 1 if errors else 0


# -- retrieval ----------------------------------------------------------------------------------
#: default per-hit character budget when --full is given without a number.
FULL_DEFAULT_CHARS = 4000


def _print_hits(query: str | None, hits: list[dict], empty: str, full: int | None = None) -> int:
    """Render hits. By default each hit shows a short preview (5 lines × 110 chars). When `full`
    is set it prints the whole body up to `full` characters per hit (0 = no cap), so a snippet is
    never silently truncated."""
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
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if full is None:  # short preview
            for line in lines[:5]:
                print(f"      {line[:110]}")
            continue
        budget = full if full > 0 else None  # 0 = unlimited
        shown, cut = 0, False
        for line in lines:
            room = None if budget is None else budget - shown
            if room is not None and room <= 0:
                cut = True
                break
            out = line if room is None else line[:room]
            print(f"      {out}")
            shown += len(out)
        if cut:
            print(f"      … (truncated at {full} chars — pass a larger --full N, or --full 0 for all)")
    return 0


def cmd_search(ws: Workspace, args) -> int:
    query = " ".join(args.query)
    if not query:
        print('Usage: bean search "your question"', file=sys.stderr)
        return 2
    hits = search_many(_retrieval_wss(ws), query, queries=args.variant, k=args.k,
                       source=args.source, doc_like=args.doc, expand=args.expand,
                       author=args.author, since=args.since, before=args.before)
    return _print_hits(query, hits,
                       "No matches. Have you run `bean sync`? (`bean status` shows what's indexed.)",
                       full=args.full)


def cmd_recent(ws: Workspace, args) -> int:
    hits = recent_many(_retrieval_wss(ws), source=args.source, doc_like=args.doc,
                       author=args.author, since=args.since, before=args.before, limit=args.limit)
    return _print_hits(None, hits, "Nothing indexed yet — run `bean sync`.", full=args.full)


def cmd_related(ws: Workspace, args) -> int:
    hits = related_many(_retrieval_wss(ws), args.ref, source=args.source, limit=args.limit)
    return _print_hits(None, hits,
                       f'No documents related to "{args.ref}" (graph edges build on `bean sync`).',
                       full=args.full)


def cmd_thread(ws: Workspace, args) -> int:
    hits = thread_many(_retrieval_wss(ws), args.ref, source=args.source)
    return _print_hits(None, hits, f'No thread/document matching "{args.ref}".', full=args.full)


def cmd_doc(ws: Workspace, args) -> int:
    hits = document_many(_retrieval_wss(ws), args.ref, source=args.source)
    return _print_hits(None, hits, f'No document matching "{args.ref}".', full=args.full)


def cmd_neighbors(ws: Workspace, args) -> int:
    hits = neighbors_many(_retrieval_wss(ws), args.chunk_id, radius=args.radius)
    return _print_hits(None, hits, f'No chunk "{args.chunk_id}".', full=args.full)


# -- plugins ------------------------------------------------------------------------------------
def cmd_plugins(ws: Workspace, args) -> int:
    """List connectors beyond the core set — drop-in plugin files under the plugin dirs. Every
    `*.py` there is loaded automatically; there's nothing to enable."""
    from .sources import Source, CORE_SOURCES
    from .plugins import plugin_dirs, discover_sources
    g = cfgmod.load_global()

    core = {s.key for s in CORE_SOURCES}
    print("core connectors (always on):")
    print("  " + ", ".join(sorted(core)))
    drop = discover_sources(Source, global_config=g)
    print(f"\ndrop-in plugins ({', '.join(str(d) for d in plugin_dirs(g))}):")
    print("  " + (", ".join(f"{s.key}" for s in drop) or "(none)"))
    return 0


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
        if args.path.startswith(("embedding.", "chunking.")) or ".chunking." in args.path:
            print("  (changes the index shape — run `bean sync --rebuild` to apply it to existing docs.)")
        return 0
    print("Unknown config action. Known paths:\n  " + "\n  ".join(cfgmod.known_paths()), file=sys.stderr)
    return 2


# -- cloud ----------------------------------------------------------------------------------------
def cmd_cloud(ws: Workspace, args) -> int:
    """Turn this workspace's Lance catalog into a cloud-backed one. `init` writes the workspace as
    the writer of a new (or empty) bucket and pushes whatever's already indexed locally up to it;
    `connect` joins an existing bucket as a read-only consumer and pulls it down, no source
    credentials required."""
    from . import remote
    if args.action == "init":
        if not args.bucket:
            print("Usage: bean cloud init --bucket NAME [--prefix P] [--region R]", file=sys.stderr)
            return 2
        remote.cloud_init(ws, args.bucket, args.prefix or "", args.region or "")
        print(f"✓ cloud writer initialised: bucket={args.bucket} prefix={args.prefix or ''} role=writer")
        return 0
    if args.action == "connect":
        if not args.bucket:
            print("Usage: bean cloud connect --bucket NAME [--prefix P] [--region R]", file=sys.stderr)
            return 2
        remote.cloud_connect(ws, args.bucket, args.prefix or "", args.region or "")
        print(f"✓ cloud consumer connected: bucket={args.bucket} prefix={args.prefix or ''} role=consumer")
        return 0
    print(f"Unknown cloud action {args.action!r}. Known actions: init, connect", file=sys.stderr)
    return 2


# -- sql ----------------------------------------------------------------------------------------
_SQL_SCHEMA = """bean stores documents/revisions/edges/chunks in a Lance catalog + a private state table. Query READ-ONLY:
  bean sql "SELECT ..."        (only SELECT / WITH)      add --global for the shared cross-repo store

TABLES
  documents(source, doc_id, title, url, revision_id, hash, body,
            created_at, modified_at, author, mime, fetched_at)
      one row per indexed document. created_at/modified_at are the doc's OWN timestamps at the
      source; `author` is source-native. A Google Doc comment is its own row: doc_id
      '<fileId>#comment:<id>', author = the commenter, modified_at = last activity.
  edges(source, src_doc, rel, dst_kind, dst)
      derived links. rel ∈ {authored_by, in_repo, in_project, in_channel}; dst_kind ∈
      {person, container}.
  state(key, value)                internal sync cursors / checkpoints.
  _chunks(id, source, doc_id, title, url, start, "end", text, vector)
      embedded chunks (registered from Lance on demand; "end" is reserved — quote it; ids ending
      '-large' are coarse doc-level chunks).

EXAMPLES
  bean sql "SELECT author, count(*) n FROM documents GROUP BY author ORDER BY n DESC LIMIT 10"
  bean sql "SELECT title, modified_at FROM documents WHERE doc_id LIKE '%#comment:%'
            AND author ILIKE '%eric%' ORDER BY modified_at DESC LIMIT 5"
"""


def cmd_sql(ws: Workspace, args) -> int:
    query = " ".join(args.query).strip()
    if not query:
        print(_SQL_SCHEMA)
        return 0
    head = query.lower().lstrip("( \t")
    if not (head.startswith("select") or head.startswith("with")):
        print("bean sql runs read-only queries only (SELECT / WITH).", file=sys.stderr)
        return 2
    target = Workspace.global_() if args.global_ else ws
    if not target.catalog_dir.exists():
        print("Nothing indexed yet — run `bean sync`.", file=sys.stderr)
        return 1
    from .lancecat import Catalog
    con = Catalog(target.catalog_dir).duck()
    try:
        from .index import chunks_dataset
        ds = chunks_dataset(target)
        if ds is not None:
            con.register("_chunks", ds)
        # Attach the private state table if it exists so `SELECT ... FROM state` works.
        if target.db_path.exists():
            try:
                con.execute(f"ATTACH '{target.db_path}' AS _priv (READ_ONLY)")
                con.execute("CREATE VIEW state AS SELECT * FROM _priv.state")
            except Exception:
                # Guard: if the state table doesn't exist in the private db, skip the view.
                pass
        try:
            cur = con.execute(query)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        except Exception as err:  # a bad query is the user's, not a crash
            print(f"SQL error: {err}", file=sys.stderr)
            return 1
    finally:
        con.close()
    _print_table(cols, rows)
    return 0


def _print_table(cols: list[str], rows: list) -> int:
    if not rows:
        print("(no rows)")
        return 0

    def cell(v) -> str:
        s = "" if v is None else str(v).replace("\n", " ")
        return (s[:79] + "…") if len(s) > 80 else s

    data = [[cell(v) for v in r] for r in rows]
    widths = [max(len(cols[i]), *(len(r[i]) for r in data)) for i in range(len(cols))]
    fmt = lambda cells: "  ".join(s.ljust(widths[i]) for i, s in enumerate(cells))  # noqa: E731
    print(fmt(cols))
    print(fmt(["-" * w for w in widths]))
    for r in data:
        print(fmt(r))
    print(f"\n{len(rows)} row(s)")
    return 0


# -- parser -------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bean", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="connection status + next steps")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("auth", help="connect a provider")
    p.add_argument("provider", choices=sorted(AUTH))
    p.add_argument("--token", help="API token / PAT / access token")
    p.add_argument("--url", help="site/base URL for self-hosted or multi-tenant providers")
    p.add_argument("--email", help="account email (Atlassian Cloud, Zendesk, IMAP)")
    p.add_argument("--subdomain", help="tenant subdomain (Zendesk, ServiceNow)")
    p.add_argument("--key", help="API key (Trello, Salesforce consumer key)")
    p.add_argument("--secret", help="API secret / password")
    p.add_argument("--method", help="auth method when a provider supports several (e.g. device|az)")
    p.set_defaults(fn=cmd_auth)

    p = sub.add_parser("scope", help="show/set whether a connector is global (all repos) or local")
    p.add_argument("source", nargs="?", choices=SOURCE_KEYS)
    p.add_argument("value", nargs="?", choices=["global", "local"])
    p.set_defaults(fn=cmd_scope)

    p = sub.add_parser("sync", help="fetch changes and re-embed them")
    p.add_argument("source", nargs="?", choices=SOURCE_KEYS)
    p.add_argument("--rebuild", action="store_true",
                   help="full resweep: ignore cursors, re-fetch back --since, and re-embed every "
                        "doc (applies a chunking / embedding-model change to the whole index)")
    p.add_argument("--since", type=int, default=90)
    p.set_defaults(fn=cmd_sync)

    def add_full(parser, default=None):
        parser.add_argument("--full", type=int, nargs="?", const=FULL_DEFAULT_CHARS, default=default,
                            metavar="N",
                            help="print full bodies instead of a 5-line preview, capped at N chars "
                                 f"per hit (bare --full = {FULL_DEFAULT_CHARS}; --full 0 = no cap)")

    p = sub.add_parser("search", help="hybrid semantic + keyword search")
    p.add_argument("query", nargs="+")
    add_full(p)
    p.add_argument("--variant", action="append",
                   help="an extra query variant to fuse (repeatable) — e.g. a paraphrase or the "
                        "identifiers you spotted; weighted-RRF fuses them with the main query")
    p.add_argument("--k", type=int, default=None)
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--doc", help="restrict to docs whose id/name contains this substring")
    p.add_argument("--author", help="restrict to docs whose author matches this substring")
    p.add_argument("--since", help="only docs modified on/after this date (YYYY-MM-DD)")
    p.add_argument("--before", help="only docs modified before this date (YYYY-MM-DD)")
    p.add_argument("--expand", type=int, default=None,
                   help="neighbouring chunks pulled in per hit — only applies when section-merge is "
                        "off (search.merge_sections=false); merged sections already carry their "
                        "surrounding context")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("recent", help="most recently changed docs/messages")
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--doc", help="filter by doc id/name substring (e.g. a #channel)")
    p.add_argument("--author", help="filter by author substring")
    p.add_argument("--since", help="only docs modified on/after this date (YYYY-MM-DD)")
    p.add_argument("--before", help="only docs modified before this date (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, default=20)
    add_full(p)
    p.set_defaults(fn=cmd_recent)

    p = sub.add_parser("related", help="documents one hop away in the graph (same repo/project/channel/author)")
    p.add_argument("ref", help="a doc id/title substring to expand from")
    p.add_argument("--source", choices=SOURCE_KEYS)
    p.add_argument("--limit", type=int, default=20)
    add_full(p)
    p.set_defaults(fn=cmd_related)

    p = sub.add_parser("thread", help="a whole thread/document as one block")
    p.add_argument("ref")
    p.add_argument("--source", choices=SOURCE_KEYS)
    add_full(p, default=0)  # whole-body command: full by default, no cap
    p.set_defaults(fn=cmd_thread)

    p = sub.add_parser("doc", help="full document body")
    p.add_argument("ref")
    p.add_argument("--source", choices=SOURCE_KEYS)
    add_full(p, default=0)  # whole-body command: full by default, no cap
    p.set_defaults(fn=cmd_doc)

    p = sub.add_parser("neighbors", help="chunks surrounding a chunk id")
    p.add_argument("chunk_id")
    p.add_argument("--radius", type=int, default=3)
    add_full(p, default=0)  # whole-body command: full by default, no cap
    p.set_defaults(fn=cmd_neighbors)

    p = sub.add_parser("config", help="view or set configuration")
    p.add_argument("action", nargs="?", choices=["get", "set", "list"])
    p.add_argument("path", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("cloud", help="make this workspace's catalog cloud-backed (S3)")
    p.add_argument("action", choices=["init", "connect"])
    p.add_argument("--bucket", help="S3 bucket name")
    p.add_argument("--prefix", help="key prefix under the bucket (default: none/root)")
    p.add_argument("--region", help="AWS region")
    p.set_defaults(fn=cmd_cloud)

    p = sub.add_parser("pull", help="fetch the latest cloud index into the local replica")
    p.set_defaults(fn=cmd_pull)

    p = sub.add_parser("status", help="workspace, auth, and index state")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("sql", help="run a read-only SQL query over the store (no query = show schema)")
    p.add_argument("query", nargs="*")
    p.add_argument("--global", dest="global_", action="store_true", help="query the shared cross-repo store")
    p.set_defaults(fn=cmd_sql)

    p = sub.add_parser("plugins", help="list drop-in connectors loaded from the plugin dirs")
    p.set_defaults(fn=cmd_plugins)

    args = parser.parse_args(argv)
    ws = Workspace()
    # Guarded auto-pull before cloud reads: a consumer sees fresh data without a manual `bean
    # pull`, but back-to-back queries don't each re-pull (auto_pull's own min-interval guard).
    # Best-effort — a transient refresh failure (e.g. flaky S3) must never fail a read; it just
    # falls back to whatever's already in the local replica.
    if args.cmd in _READ_CMDS and ws.is_cloud:
        from . import remote
        try:
            remote.auto_pull(ws)
        except Exception as exc:
            print(f"⚠ bean: auto-pull before read failed ({exc}) — reading the current replica",
                  file=sys.stderr)
    # Warn (never auto-sync) when the index is stale, on the commands that read the index.
    if args.cmd in {"search", "recent", "thread", "doc", "related", "status", "init"}:
        note = _staleness_note(ws)
        if note:
            print(note, file=sys.stderr)
    return args.fn(ws, args)


if __name__ == "__main__":
    raise SystemExit(main())
