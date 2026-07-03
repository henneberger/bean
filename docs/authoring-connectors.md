# Authoring a bean connector

> Reference doc for writing a connector bean doesn't ship. The main `bean` skill points here when a
> user asks to connect a source that isn't in `bean plugins list`. This is a guide, not a separate
> slash command.

A connector is **one Python module** exposing four callables and a `SOURCE`. Drop it in
`~/.bean/plugins/` and it's live — no core edits. bean ships 12 core connectors; ~45 more live in
`bean/prototypes/` as worked examples you can read or copy. Your job: write the module, test it
offline, install it.

**Before writing, read 1–2 prototypes whose shape matches:**

| Source shape | Read | Notes |
|---|---|---|
| REST list + item + comments | `bean/prototypes/{linear,asana,gitlab}.py` | issues/tasks/cards → one doc each |
| Wiki / KB (HTML bodies) | `bean/prototypes/{guru,bookstack}.py`, `bean/confluence.py` | `html_to_text` the body |
| Whole-collection (index everything) | `bean/prototypes/{servicenow,intercom}.py` | `always_when_connected`, never prune |
| Chat (per-week digests) | `bean/prototypes/zulip.py`, `bean/discord.py` | reuse `bean.slack.iso_week` / `week_start` |
| GraphQL / POST API | `bean/prototypes/{slab,fireflies,linear}.py` | `api_json_post` |
| Files (office/pdf) | `bean/prototypes/{dropbox,buckets}.py` | temp-file → `extract_office`/`extract_pdf` |
| Rows / tables | `bean/prototypes/{sqldb,airtable}.py` | render fields as `key: value` |
| CLI/OAuth-minted token | `bean/prototypes/gmail.py`, `bean/microsoft.py` | injectable `token_fn=` for offline tests |
| No-auth fetch | `bean/prototypes/{web,rss}.py` | `auth=None`, no `connect` |

## The contract

```python
def parse_add(item: str) -> tuple[str, object] | None: ...   # claim your refs, else None
def connect(*, token=None, url=None, email=None, subdomain=None, key=None,
            secret=None, method=None, fetch=None, log=print) -> dict: ...   # verify + save cred
def connected() -> dict | None: ...                          # load_credential(name)
def sync(store, config, *, settings, fetch=None, full=False,
         since_days=90, log=lambda m: None) -> dict: ...      # -> {"changed": [...], "removed": [...]}
SOURCE = Source("name", "name", "Label", ("lists",), sync, parse_add, auth="name", ...)
```

## Rules (non-negotiable — the review checklist)

- **All HTTP through the injectable seam.** `from bean.http import api_json, api_json_post, api_get,
  api_post, AuthError` and thread `fetch=fetch` through every call. Never `import requests`. This is
  what makes the module testable offline. Async polls take an injectable `sleep=`.
- **Change detection.** Give each doc a cheap `revision_id` (a version, `updated_at`, ETag, mtime).
  `store.upsert(...)` returns `True` only when the body actually changed → append that id to
  `changed`. Skip refetching a body whose `revision_id` matches `store.get(src, id).revision_id`.
- **Stable doc ids** that survive edits: `owner/repo#123`, a uuid, `PROJ-123`, a path, `base/tbl/rec`.
- **Prune by origin.** Item-tracked sources: `removed = [d for d in store.doc_ids(src) if d not in
  seen]` then `store.delete(src, d)`. Whole-collection and chat/mail sources return `"removed": []`.
- **Credentials, not config, hold secrets.** `connect()` verifies against a cheap identity/list
  endpoint, then `save_credential(name, {...})` (→ `~/.bean/credentials/<name>.json`, mode 0600).
  Store per-tenant context (base url, subdomain, method) in the cred. Raise `RuntimeError` with an
  actionable message ("pass --token …, get one at <url>") on missing/invalid input.
- **`parse_add` is specific.** Claim a `name:` prefix and/or your native URL host; return `None` for
  everything else (bare words, paths, other hosts) so routing falls through. Never claim a filesystem
  path — `localfiles` owns those.
- **Tolerate one bad item** (try/except + `log`) without aborting the whole sync.

## Helpers

```python
from bean.http import api_json, api_json_post, api_get, api_post, AuthError
from bean.workspace import load_credential, save_credential
from bean.html import html_to_text, extract_readable      # HTML bodies / full web pages
from bean.office import extract_office, OFFICE_EXT         # .docx/.odt/.rtf (needs a Path)
from bean.pdf import extract_pdf                           # PDF (needs a path)
from bean import slack                                     # slack.iso_week / week_start for chat
from bean.sources import Source
```

`store.upsert(source, doc_id, *, title, url, revision_id, body, meta=None)` — `meta` may carry
`created_at`, `modified_at` (must be ISO/parseable timestamps, not junk), `author`, `mime`.

## Write it — start from the template

Copy [`docs/connector-template.py`](connector-template.py) to `~/.bean/plugins/<name>.py` and fill it in. Or copy a
matching prototype. Keep it self-contained.

## Test it offline (do this before installing)

Write a throwaway check driving `sync` with a fake `fetch` — no network, no accounts:

```python
from bean.workspace import set_bean_home, Workspace, save_credential
import tempfile; set_bean_home(tempfile.mkdtemp())
from bean.http import Response
from bean.store import Store
import importlib.util, json, pathlib
spec = importlib.util.spec_from_file_location("myplug", "PATH/TO/name.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

save_credential("name", {"token": "t"})            # whatever the cred needs
def fake(u, h, method="GET", body=None):
    return Response(200, json.dumps({"items": [{"id": "1", "updated_at": "2026-01-01"}]}), {})
d = pathlib.Path(tempfile.mkdtemp()); (d/".git").mkdir()
with Store(Workspace(d)) as s:
    r = m.sync(s, {"lists": ["x"]}, settings={}, fetch=fake)
    assert r["changed"] == ["1"], r                # first sync ingests
    assert m.sync(s, {"lists": ["x"]}, settings={}, fetch=fake)["changed"] == []  # re-sync no-op
    assert m.parse_add("name:1") == ("lists", "1") and m.parse_add("/a/path") is None
print("ok")
```

A fake `fetch(url, headers, method="GET", body=None)` with `method`/`body` defaults serves both GET
and POST connectors.

## Install it

1. `mkdir -p ~/.bean/plugins && cp <name>.py ~/.bean/plugins/` — anything there with a `SOURCE` (or
   `SOURCES` list, or `register()`) loads automatically.
2. Verify: `bean plugins list` shows it under drop-in plugins; `bean init` lists it.
3. Set it up (`bean auth <name> …` or write the cred file — see the main bean skill), then
   `bean add <ref>` (or write config), `bean sync`, `bean search`.

To instead turn on a bundled prototype (Linear, GitLab, Zendesk-articles, …): `bean plugins enable
<name>` — no file needed. Promote a prototype to core by moving its module into `bean/` and adding a
row to `CORE_SOURCES` in `bean/sources.py`.

## Common mistakes

- Importing `requests` → untestable. Use the `fetch` seam.
- `meta["modified_at"] = <raw epoch or junk>` → DuckDB rejects it. Pass an ISO string (convert
  epochs first).
- A greedy `parse_add` that claims bare words/paths → it shadows other sources. Be strict.
- Pruning a whole-collection source → it deletes everything each run. Return `"removed": []`.
- Forgetting `revision_id` → every sync re-embeds every doc.
