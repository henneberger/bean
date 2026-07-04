"""Local files source. Point bean at a folder (crawled recursively) or a single file and it indexes
documents: Markdown/plain text, Word (.docx/.docm), OpenDocument (.odt), RTF, PowerPoint (.pptx),
Excel (.xlsx), HTML, and PDF (via the OCR-capable extractor). No auth — it reads your disk. Change
detection is per-file mtime (skipped when unchanged) with the content hash as the final authority, so
a re-sync only re-embeds files you actually edited. doc_id is the absolute path; files that disappear
are pruned from the index."""

from __future__ import annotations

from pathlib import Path

from ..office import OFFICE_EXT

# Liberal on prose/markup: Markdown variants, plain text, and the common lightweight-markup
# formats. Deliberately excludes logs and source code — this source is for documents.
TEXT_EXT = {".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".mdx", ".txt", ".text",
            ".rst", ".org", ".adoc", ".asciidoc", ".asc", ".textile", ".tex", ".me"}
HTML_EXT = {".html", ".htm", ".xhtml"}
PDF_EXT = {".pdf"}
SUPPORTED = TEXT_EXT | OFFICE_EXT | PDF_EXT | HTML_EXT


def _iter_files(paths):
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in SUPPORTED and not any(
                        part.startswith(".") for part in f.relative_to(p).parts):
                    yield f
        elif p.is_file() and p.suffix.lower() in SUPPORTED:
            yield p


def _read(path: Path, ocr_cfg: dict, log) -> str | None:
    ext = path.suffix.lower()
    if ext in PDF_EXT:
        from ..pdf import extract_pdf
        try:
            return extract_pdf(path, ocr_cfg, log=log)
        except Exception as err:  # a single unreadable PDF must not abort the whole sync
            log(f"localfiles: {path.name} skipped ({err})")
            return None
    if ext in OFFICE_EXT:
        from ..office import extract_office
        try:
            return extract_office(path)
        except Exception as err:  # a malformed/encrypted doc must not abort the whole sync
            log(f"localfiles: {path.name} skipped ({err})")
            return None
    if ext in HTML_EXT:
        from ..html import html_to_text
        try:
            return html_to_text(path.read_text(errors="replace"))
        except Exception as err:
            log(f"localfiles: {path.name} skipped ({err})")
            return None
    try:
        return path.read_text(errors="replace")
    except OSError as err:
        log(f"localfiles: {path} unreadable ({err})")
        return None


def sync(store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None) -> dict:
    ocr_cfg = settings.get("ocr", {})
    seen, changed = [], []
    for f in _iter_files(config.get("paths", [])):
        doc_id = str(f)
        seen.append(doc_id)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        key = f"localfiles.mtime.{doc_id}"
        if not full and store.get("localfiles", doc_id) and store.get_state(key) == mtime:
            continue  # untouched since last sync — skip the read entirely
        body = _read(f, ocr_cfg, log)
        if body is None:
            continue
        store.set_state(key, mtime)
        if store.upsert("localfiles", doc_id, title=f.name, url=f"file://{f}",
                        revision_id=None, body=body):
            changed.append(doc_id)
            log(f"localfiles: updated {f.name}")
    removed = [d for d in store.doc_ids("localfiles") if d not in seen]
    for doc_id in removed:
        store.delete("localfiles", doc_id)
    return {"changed": changed, "removed": removed}
