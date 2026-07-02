"""Cloud object storage source: S3, GCS, and Azure Blob. There is no `bean auth` step — each
provider uses the DEFAULT credential chain already on the machine (boto3's, google-cloud-storage's,
azure-storage-blob's), so `auth=None` and there is no connect/connected. Tracks bucket prefixes as
URIs (`s3://bucket/prefix`, `gs://bucket/prefix`, `az://account/container/prefix`). Every object
with a supported extension (Markdown, text, office, PDF) is downloaded and extracted. Change
detection is the object etag/generation/last-modified as the revision id. doc_id is the full
`scheme://bucket/key`; keys that vanish under a tracked prefix are pruned.

The object-store client is injectable (`client=`) so tests pass a fake that yields objects; the
default builds the real SDK client lazily and raises a clear error naming the missing pip package."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..office import OFFICE_EXT
from ..store import Store

TEXT_EXT = {".md", ".markdown", ".txt", ".text"}
PDF_EXT = {".pdf"}
SUPPORTED = TEXT_EXT | OFFICE_EXT | PDF_EXT

_SCHEMES = ("s3", "gs", "az")


# -- refs ---------------------------------------------------------------------------------------
def parse_add(item: str):
    """`s3://…`, `gs://…`, `az://…` → the 'buckets' list."""
    item = item.strip()
    m = re.match(r"(s3|gs|az)://", item, re.I)
    if m:
        return ("buckets", item)
    return None


@dataclass
class _Obj:
    key: str
    revision: str | None


def _parse_uri(uri: str) -> tuple[str, str, str]:
    """(scheme, bucket, prefix). For az, `bucket` is the container and the account is resolved
    separately by the backend."""
    m = re.match(r"(s3|gs|az)://([^/]+)(?:/(.*))?$", uri, re.I)
    if not m:
        raise ValueError(f"not an object-store URI: {uri!r}")
    scheme, first, rest = m.group(1).lower(), m.group(2), m.group(3) or ""
    if scheme == "az":
        # `az://account/container/prefix`, or `az://container/prefix` with account from env.
        if os.environ.get("AZURE_STORAGE_ACCOUNT"):
            return scheme, first, rest  # first = container
        container, _, prefix = rest.partition("/")
        return scheme, container, prefix  # first = account (backend reads it from the uri)
    return scheme, first, rest


def _ext(key: str) -> str:
    base = key.rsplit("/", 1)[-1]
    i = base.rfind(".")
    return base[i:].lower() if i >= 0 else ""


def _console_url(scheme: str, bucket: str, key: str, uri: str) -> str:
    if scheme == "s3":
        return f"https://s3.console.aws.amazon.com/s3/object/{bucket}?prefix={key}"
    if scheme == "gs":
        return f"https://console.cloud.google.com/storage/browser/_details/{bucket}/{key}"
    return uri  # azure needs the account; the uri is a fine fallback


# -- real backends (lazy SDK imports) -----------------------------------------------------------
class _Backend:
    """Duck-typed object-store client: .list(bucket, prefix) -> Iterable[_Obj];
    .download(bucket, key) -> bytes. Real subclasses import their SDK lazily."""


def _build_backend(scheme: str, uri: str) -> _Backend:
    if scheme == "s3":
        try:
            import boto3  # noqa: F401
        except ImportError:
            raise RuntimeError("reading s3:// needs boto3 (pip install boto3)")
        return _S3Backend()
    if scheme == "gs":
        try:
            from google.cloud import storage  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "reading gs:// needs google-cloud-storage (pip install google-cloud-storage)")
        return _GCSBackend()
    try:
        from azure.storage.blob import BlobServiceClient  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "reading az:// needs azure-storage-blob (pip install azure-storage-blob)")
    # Account: env override, else the first path segment of the uri.
    account = os.environ.get("AZURE_STORAGE_ACCOUNT")
    if not account:
        account = re.match(r"az://([^/]+)/", uri, re.I).group(1)
    return _AzureBackend(account)


class _S3Backend(_Backend):
    def __init__(self):
        import boto3
        self._c = boto3.client("s3")

    def list(self, bucket, prefix):
        for page in self._c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                rev = (o.get("ETag") or "").strip('"') or str(o.get("LastModified"))
                yield _Obj(key=o["Key"], revision=rev)

    def download(self, bucket, key) -> bytes:
        return self._c.get_object(Bucket=bucket, Key=key)["Body"].read()


class _GCSBackend(_Backend):
    def __init__(self):
        from google.cloud import storage
        self._c = storage.Client()

    def list(self, bucket, prefix):
        for b in self._c.list_blobs(bucket, prefix=prefix):
            rev = str(b.generation or b.etag or b.updated)
            yield _Obj(key=b.name, revision=rev)

    def download(self, bucket, key) -> bytes:
        return self._c.bucket(bucket).blob(key).download_as_bytes()


class _AzureBackend(_Backend):
    def __init__(self, account):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if conn:
            self._svc = BlobServiceClient.from_connection_string(conn)
        else:
            self._svc = BlobServiceClient(f"https://{account}.blob.core.windows.net",
                                          credential=DefaultAzureCredential())

    def list(self, container, prefix):
        cc = self._svc.get_container_client(container)
        for b in cc.list_blobs(name_starts_with=prefix):
            rev = (b.etag or "").strip('"') or str(b.last_modified)
            yield _Obj(key=b.name, revision=rev)

    def download(self, container, key) -> bytes:
        return self._svc.get_blob_client(container, key).download_blob().readall()


# -- body extraction ----------------------------------------------------------------------------
def _extract(data: bytes, ext: str, name: str, log) -> str | None:
    if ext in TEXT_EXT:
        return data.decode("utf-8", "replace")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        p = Path(tmp.name)
        try:
            if ext in PDF_EXT:
                from ..pdf import extract_pdf
                return extract_pdf(p, {}, log=log)
            from ..office import extract_office
            return extract_office(p)
        except Exception as err:
            log(f"buckets: {name} skipped ({err})")
            return None


# -- sync ---------------------------------------------------------------------------------------
def sync(store: Store, config: dict, *, settings: dict, fetch=None, full: bool = False,
         since_days: int = 90, log=lambda m: None, client=None) -> dict:
    uris = list(dict.fromkeys(config.get("buckets", [])))
    changed, seen, tracked_roots = [], [], []
    for uri in uris:
        try:
            scheme, bucket, prefix = _parse_uri(uri)
        except ValueError as err:
            log(f"buckets: {err}")
            continue
        backend = client or _build_backend(scheme, uri)
        try:
            objects = list(backend.list(bucket, prefix))
        except RuntimeError as err:  # missing SDK
            log(f"buckets: {uri} skipped ({err})")
            continue
        except Exception as err:
            log(f"buckets: {uri} listing failed ({err})")
            continue
        # Only prune under prefixes we successfully listed, so a transient failure can't wipe them.
        tracked_roots.append(uri.rstrip("/"))
        for obj in objects:
            ext = _ext(obj.key)
            if ext not in SUPPORTED:
                continue
            doc_id = f"{scheme}://{bucket}/{obj.key}"
            seen.append(doc_id)
            existing = store.get("buckets", doc_id)
            if not full and obj.revision and existing and existing.revision_id == obj.revision:
                continue
            try:
                data = backend.download(bucket, obj.key)
            except Exception as err:
                log(f"buckets: {doc_id} skipped ({err})")
                continue
            name = obj.key.rsplit("/", 1)[-1]
            body = _extract(data, ext, name, log)
            if body is None:
                continue
            if store.upsert("buckets", doc_id, title=name,
                            url=_console_url(scheme, bucket, obj.key, doc_id),
                            revision_id=obj.revision, body=body):
                changed.append(doc_id)
                log(f"buckets: updated {doc_id}")

    # Prune keys no longer present under a successfully-listed prefix. When nothing is configured
    # at all, drop everything; when configured prefixes all failed to list, prune nothing.
    roots = tuple(tracked_roots)
    prune_all = not uris
    removed = [d for d in store.doc_ids("buckets")
               if (prune_all or d.startswith(roots)) and d not in seen]
    for doc_id in removed:
        store.delete("buckets", doc_id)
    return {"changed": changed, "removed": removed}
