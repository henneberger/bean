"""Obsidian vault source. Like localfiles, but vault-aware: it crawls a vault directory for `.md`
notes and augments each note's body with its resolved `[[wikilinks]]` and a computed Backlinks
section (which other notes link TO this one), so link structure is searchable. No auth — it reads
your disk. Change detection is per-file mtime as the revision id (content hash is the final
authority); doc_id is the absolute path; deleted notes are pruned.

Only the `obsidian:` prefix routes here — plain filesystem paths fall through to localfiles."""

from __future__ import annotations

import re
from pathlib import Path

from .. import localfiles

# [[Target]], [[Target|alias]], [[Target#heading]] — capture the note target only.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")
_HEADING = re.compile(r"^#\s+(.+)$", re.M)


def parse_add(item: str):
    """`obsidian:/abs/path/to/vault` → vaults. Anything else → None (plain paths go to localfiles)."""
    s = item.strip()
    if s.startswith("obsidian:"):
        p = Path(s[len("obsidian:"):]).expanduser()
        return ("vaults", str(p.resolve() if p.exists() else p))
    return None


def _md_files(vaults) -> list[Path]:
    out: list[Path] = []
    for raw in vaults:
        p = Path(raw).expanduser()
        if p.is_dir():
            for f in sorted(p.rglob("*.md")):
                if f.is_file() and not any(part.startswith(".") for part in f.relative_to(p).parts):
                    out.append(f)
        elif p.is_file() and p.suffix.lower() == ".md":
            out.append(p)
    return out


def _links(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK.finditer(text)]


def _title(path: Path, text: str) -> str:
    m = _HEADING.search(text or "")
    return m.group(1).strip() if m else path.stem


def sync(store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    files = _md_files(config.get("vaults", []))

    # One pass to read every note, then build the backlink map (stem -> notes that link to it)
    # before upserting, so each note can embed who points at it.
    texts: dict[Path, str] = {}
    backlinks: dict[str, list[str]] = {}
    for f in files:
        text = localfiles._read(f, settings.get("ocr", {}), log)
        if text is None:
            continue
        texts[f] = text
        for target in _links(text):
            backlinks.setdefault(target.lower(), []).append(f.stem)

    seen, changed = [], []
    for f in files:
        doc_id = str(f)
        text = texts.get(f)
        if text is None:
            continue
        seen.append(doc_id)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        rev = str(mtime)
        existing = store.get("obsidian", doc_id)
        if not full and existing and existing.revision_id == rev:
            continue  # untouched since last sync

        outgoing = sorted(dict.fromkeys(_links(text)))
        incoming = sorted(dict.fromkeys(n for n in backlinks.get(f.stem.lower(), []) if n != f.stem))
        title = _title(f, text)
        parts = [text]
        if outgoing:
            parts.append("Links: " + ", ".join(f"[[{t}]]" for t in outgoing))
        if incoming:
            parts.append("Backlinks:\n" + "\n".join(f"- [[{n}]]" for n in incoming))
        body = "\n\n".join(parts)
        if store.upsert("obsidian", doc_id, title=title, url=f"file://{f}",
                        revision_id=rev, body=body):
            changed.append(doc_id)
            log(f"obsidian: updated {f.name}")
    removed = [d for d in store.doc_ids("obsidian") if d not in seen]
    for doc_id in removed:
        store.delete("obsidian", doc_id)
    return {"changed": changed, "removed": removed}
