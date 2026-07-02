# Connector backlog

bean ships with five connectors: **Slack**, **Google Docs/Drive**, **Notion**, **GitHub**, and
**Local files** (Markdown / text / PDF). Below is the backlog of connectors to add, roughly
ranked by how commonly teams keep knowledge there. Everything bean does is local — a new
connector only needs to authenticate with the user's own credentials and land documents in the
DuckDB catalog; the shared chunk → embed → hybrid-search pipeline does the rest.

Adding one is small: write a `sync()` that upserts documents (see `bean/notion.py` for the
shortest example), then append a `Source(...)` row to `bean/sources.py`. Keep to the injectable
`(url, headers)` GET-only fetch contract where you can, so it stays testable offline.

## Ranked backlog

| # | Connector | Auth | Notes |
|---|-----------|------|-------|
| 1 | **Confluence** (Atlassian) | API token | The other big enterprise wiki; CQL search + page bodies as storage-format HTML → text. |
| 2 | **Jira** | API token | Issues + comments; mirrors the GitHub-issues shape closely. |
| 3 | **Gmail / Google Mail** | gcloud / OAuth | Threads as documents; rides the same gcloud auth as Docs. |
| 4 | **Microsoft SharePoint / OneDrive** | MSAL device code | Office docs via Graph API; huge in enterprises. |
| 5 | **Outlook / Microsoft 365 Mail** | MSAL device code | Graph API messages + threads. |
| 6 | **Linear** | API key | Issues, projects, and comments; GraphQL. |
| 7 | **Google Sheets** | gcloud / OAuth | Export tabs as Markdown tables; reuses Drive auth. |
| 8 | **Dropbox Paper / Dropbox** | OAuth | Docs + files; PDF/office files flow through the OCR pipeline. |
| 9 | **Asana** | PAT | Tasks + descriptions + comments. |
| 10 | **Trello** | API key/token | Cards, checklists, comments. |
| 11 | **Zendesk** | API token | Tickets + help-center articles; strong for support knowledge. |
| 12 | **Intercom** | Access token | Conversations + articles. |
| 13 | **Discord** | Bot token | Same week-digest treatment as Slack. |
| 14 | **Microsoft Teams** | Graph API | Channel messages via Graph; week digests. |
| 15 | **Coda** | API token | Docs + tables. |
| 16 | **Obsidian / local vault** | none | Already covered by Local files; add wikilink/backlink awareness. |
| 17 | **Readwise / Reader** | API token | Highlights and saved articles. |
| 18 | **Web pages / sitemap** | none | Fetch + readability-extract a URL or crawl a sitemap. |
| 19 | **Figma** | PAT | Comments + doc text from design files. |
| 20 | **Salesforce Knowledge** | OAuth | Articles + cases for revenue teams. |
| 21 | **ServiceNow** | Basic/OAuth | KB articles + incidents. |
| 22 | **Airtable** | PAT | Bases as row documents. |
| 23 | **PostgreSQL / SQLite (arbitrary query)** | connection string | Index rows returned by a user-supplied query. |
| 24 | **S3 / GCS / Azure Blob buckets** | cloud creds | Bucket of Markdown/PDF → same pipeline as Local files. |
| 25 | **RSS / Atom feeds** | none | Newest entries as documents. |

## Design notes for future connectors

- **Change detection first.** Every source needs a cheap "did this change?" signal so `bean sync`
  re-embeds only deltas: a revision id, an `updated_at`, an ETag/blob sha, or a file mtime. Fall
  back to the content hash (the store does this automatically) as the final authority.
- **Stable doc ids.** Pick an id that survives edits (`owner/repo#123`, a page uuid, an absolute
  path). Chunk ids derive from it, so unchanged bodies re-embed nothing.
- **Threads/units that stay stable as history grows.** Slack cuts channels into per-week digests;
  chat-style sources should do the same rather than one ever-growing document.
- **POST-only APIs.** The shared fetch contract is GET-only for offline testability. If a source
  needs POST (Notion database queries, Slack search), extend `http.py` behind the same injectable
  seam rather than reaching for `requests` directly.
