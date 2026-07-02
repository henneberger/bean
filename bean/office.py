"""Text extraction for office "docs" — Word (.docx/.docm) and OpenDocument (.odt/.fodt), plus a
best-effort RTF stripper. All stdlib: modern office files are just zipped XML, so bean reads them
directly with `zipfile` + `xml.etree` and owns no extra toolchain (unlike the PDF OCR path). Each
extractor returns plain text with paragraph breaks preserved, or raises so the caller can log-skip
a single unreadable file without aborting the whole sync.

Legacy binary formats (.doc, .ppt) are intentionally not handled here — they need an external
converter (antiword/LibreOffice), which would break the "no manual install" promise; the caller
skips them with a clear message."""

from __future__ import annotations

import zipfile
from pathlib import Path

DOCX_EXT = {".docx", ".docm"}
ODT_EXT = {".odt", ".fodt"}
RTF_EXT = {".rtf"}
OFFICE_EXT = DOCX_EXT | ODT_EXT | RTF_EXT

# OOXML / ODF namespaces we pull text out of.
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ODT_TEXT_TAGS = {"p", "h"}  # text:p (paragraph) and text:h (heading), namespace-stripped


def extract_office(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in DOCX_EXT:
        return _docx(path)
    if ext in ODT_EXT:
        return _odt(path)
    if ext in RTF_EXT:
        return _rtf(path.read_bytes().decode("latin-1", errors="replace"))
    raise ValueError(f"unsupported office extension: {ext}")


def _docx(path: Path) -> str:
    """Word .docx/.docm: text lives in word/document.xml as runs (<w:t>) grouped by paragraph
    (<w:p>). We join runs within a paragraph and paragraphs with newlines; tabs and breaks become
    whitespace."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    paras = []
    for p in root.iter(f"{_W}p"):
        buf = []
        for node in p.iter():
            tag = node.tag
            if tag == f"{_W}t" and node.text:
                buf.append(node.text)
            elif tag == f"{_W}tab":
                buf.append("\t")
            elif tag in (f"{_W}br", f"{_W}cr"):
                buf.append("\n")
        paras.append("".join(buf))
    return "\n".join(paras).strip()


def _odt(path: Path) -> str:
    """OpenDocument .odt: text lives in content.xml. We take each paragraph/heading element's full
    inner text (spans, links, etc. flatten via itertext)."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    paras = []
    for node in root.iter():
        if node.tag.split("}")[-1] in _ODT_TEXT_TAGS:
            paras.append("".join(node.itertext()))
    return "\n".join(paras).strip()


def _rtf(text: str) -> str:
    """Crude RTF → text: drop the font/color/stylesheet groups, decode \\'hh and unicode \\uN
    escapes, turn \\par into newlines, then strip remaining control words and braces. Not a full
    parser — good enough to make RTF searchable."""
    import re

    # Drop groups we never want as body text (font tables, color tables, stylesheets, info).
    text = re.sub(r"\{\\\*?\\(?:fonttbl|colortbl|stylesheet|info|generator)[^{}]*\}", " ", text)
    # Paragraph/line/tab breaks: a control word swallows one trailing delimiter space.
    text = re.sub(r"\\par\b ?", "\n", text)
    text = re.sub(r"\\line\b ?", "\n", text)
    text = re.sub(r"\\tab\b ?", "\t", text)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("latin-1"), text)
    text = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 0x10000), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)  # remaining control words
    text = text.replace("{", "").replace("}", "").replace("\\", "")
    return re.sub(r"[ \t]+\n", "\n", text).strip()
