"""PDF text extraction with a pluggable OCR backend.

Config `ocr.backend` picks the strategy:

  "text"          — embedded text only (pymupdf). Fast; nothing for scans.
  "auto"          — embedded text per page; pages that come back (nearly) empty are OCR'd.
  "unlimited-ocr" — every page through baidu/Unlimited-OCR (a transformers VLM; best for
                    scanned/handwritten/complex layouts).

bean owns the OCR toolchain. pymupdf is a base dependency, and the heavier pieces (torch,
transformers) are provisioned automatically the first time OCR actually runs — same philosophy as
the embedding model, which downloads on first use rather than at setup — so you never install
anything by hand. The model weights then download from Hugging Face and are cached. The two
workers (`native_text`, `ocr_pages`) are injectable so tests exercise the routing without models.

In "auto" mode a page that already carries embedded text is used as-is: that is a property of the
page, not a dependency gap. Only if OCR itself errors at runtime (e.g. no usable compute device)
does "auto" keep the embedded text and log the real reason; "unlimited-ocr" surfaces the error,
because you asked for OCR explicitly."""

from __future__ import annotations

MIN_NATIVE_CHARS = 16  # a page with less than this is treated as "no real text" → OCR candidate


def extract_pdf(path, cfg: dict, *, native_text=None, ocr_pages=None, log=lambda m: None) -> str:
    backend = (cfg or {}).get("backend", "auto")
    native_text = native_text or _native_text
    ocr_pages = ocr_pages or _ocr_pages

    if backend == "unlimited-ocr":
        return "\n\n".join(ocr_pages(path, cfg)).strip()

    pages = native_text(path)
    if backend == "text":
        return "\n\n".join(pages).strip()

    # "auto": OCR only the pages that carry no embedded text.
    weak = [i for i, p in enumerate(pages) if len(p.strip()) < MIN_NATIVE_CHARS]
    if weak:
        try:
            fixed = ocr_pages(path, cfg, only=weak)
            for i, txt in zip(weak, fixed):
                pages[i] = txt
        except Exception as err:
            log(f"pdf: OCR failed for {len(weak)} image page(s) ({err}); keeping embedded text")
    return "\n\n".join(pages).strip()


def _native_text(path) -> list[str]:
    """Per-page embedded text via pymupdf (a base dependency)."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError("reading PDFs needs pymupdf (pip install pymupdf)")
    with fitz.open(str(path)) as doc:
        return [page.get_text("text") for page in doc]


# The Unlimited-OCR remote code imports these at load time (checked by transformers before the
# model even instantiates), so all of them are part of the toolchain bean owns.
_OCR_PACKAGES = ["torch>=2.1", "transformers>=4.44", "pillow>=10.0", "torchvision>=0.16",
                 "addict", "matplotlib"]
_OCR_MODULES = ["torch", "transformers", "torchvision", "addict", "matplotlib", "PIL"]


def _provision_ocr(log=lambda m: None, *, allow_install: bool = True) -> None:
    """Ensure the OCR toolchain is importable, installing it into the running venv on first use.
    bean owns this — the user never runs pip for it, exactly like the embedding model that downloads
    itself on first use. Set `ocr.auto_install=false` to forbid the runtime pip (e.g. a locked-down
    or offline venv); then a clear error asks you to pre-install `bean[ocr]` yourself."""
    import importlib.util
    missing = [m for m in _OCR_MODULES if importlib.util.find_spec(m) is None]
    if not missing:
        return
    if not allow_install:
        raise RuntimeError(
            f"OCR needs {', '.join(missing)} but ocr.auto_install is off — "
            "pre-install it with `pip install 'bean[ocr]'` (torch, transformers, pillow).")
    import subprocess
    import sys
    log(f"pdf: provisioning the OCR toolchain ({', '.join(missing)}) — one time, into this venv…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *_OCR_PACKAGES],
                       check=True)
    except (subprocess.CalledProcessError, OSError) as err:
        raise RuntimeError(
            f"OCR toolchain install failed ({err}). Install it yourself with "
            "`pip install 'bean[ocr]'`, or set ocr.backend='text' to skip OCR.") from err


def _ocr_device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"  # Apple Silicon
    return "cpu"


def _ocr_pages(path, cfg: dict, only: list[int] | None = None, log=lambda m: None) -> list[str]:
    """Render pages to images with pymupdf and parse each through the Unlimited-OCR VLM. The
    toolchain is provisioned on first use and the model weights (baidu/Unlimited-OCR) download from
    Hugging Face and are cached. Runs on CUDA, Apple MPS, or CPU — whichever the machine has."""
    _provision_ocr(log, allow_install=bool(cfg.get("auto_install", True)))
    import tempfile

    import fitz  # pymupdf (base dependency)
    import torch
    from transformers import AutoModel, AutoTokenizer

    model_name = cfg.get("model", "baidu/Unlimited-OCR")
    dpi = int(cfg.get("dpi", 200))
    if not hasattr(_ocr_pages, "_m"):
        device = _ocr_device()
        dtype = torch.float32 if device == "cpu" else torch.bfloat16
        tok = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
        model = AutoModel.from_pretrained(model_name, trust_remote_code=True,
                                          torch_dtype=dtype).eval().to(device)
        _ocr_pages._m = (tok, model)  # type: ignore[attr-defined]
    tok, model = _ocr_pages._m  # type: ignore[attr-defined]

    out: list[str] = []
    with fitz.open(str(path)) as doc, tempfile.TemporaryDirectory() as tmp:
        indices = only if only is not None else range(len(doc))
        for i in indices:
            pix = doc[i].get_pixmap(dpi=dpi)
            img = f"{tmp}/p{i}.png"
            pix.save(img)
            res = model.infer(tok, prompt="<image>document parsing.", image_file=img,
                              output_path=tmp)
            out.append(res if isinstance(res, str) else str(res or ""))
    return out
