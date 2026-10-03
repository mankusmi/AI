"""File sources: a local / OneDrive-synced folder, or a SharePoint folder via Microsoft Graph.

Both yield ``FileEntry`` objects and know how to hand out a local copy of the
file (``materialize``) so the inspector never cares where the bytes came from.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, Optional
from urllib.parse import quote, urlparse


@dataclass
class FileEntry:
    rel_path: str                     # path relative to the profiled root, '/' separated
    name: str
    size_bytes: int
    modified: Optional[str] = None    # ISO 8601 UTC
    created: Optional[str] = None
    modified_by: Optional[str] = None
    location: str = ""                # absolute path or SharePoint web URL
    meta: dict = field(default_factory=dict)   # source-specific SharePoint metadata
    _materialize: Callable[[], contextlib.AbstractContextManager] = field(default=None, repr=False)

    @property
    def extension(self) -> str:
        return PurePosixPath(self.name).suffix.lower()

    @property
    def folder(self) -> str:
        parent = str(PurePosixPath(self.rel_path).parent)
        return "" if parent == "." else parent

    @property
    def depth(self) -> int:
        return len(PurePosixPath(self.rel_path).parts) - 1

    @contextlib.contextmanager
    def materialize(self) -> Iterator[Path]:
        with self._materialize() as p:
            yield p


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- local
class LocalSource:
    """Walk a local folder (e.g. a synced SharePoint library or a downloaded copy)."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Not a directory: {self.root}")

    def iter_files(self) -> Iterator[FileEntry]:
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames.sort()
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                try:
                    st = full.stat()
                except OSError:
                    continue
                yield FileEntry(
                    rel_path=full.relative_to(self.root).as_posix(),
                    name=fn,
                    size_bytes=st.st_size,
                    modified=_iso(st.st_mtime),
                    created=_iso(st.st_ctime),
                    location=str(full),
                    _materialize=lambda p=full: contextlib.nullcontext(p),
                )


class FileListSource:
    """Explicit list of local files (e.g. ticked in the browser UI)."""

    def __init__(self, paths: list[str | Path]):
        self.paths = [Path(p).expanduser().resolve() for p in paths]
        if not self.paths:
            raise ValueError("No files given")
        parents = {str(p.parent) for p in self.paths}
        self.root = Path(os.path.commonpath(parents))

    def iter_files(self) -> Iterator[FileEntry]:
        for full in sorted(self.paths):
            st = full.stat()
            yield FileEntry(
                rel_path=full.relative_to(self.root).as_posix(), name=full.name, size_bytes=st.st_size,
                modified=_iso(st.st_mtime), created=_iso(st.st_ctime), location=str(full),
                _materialize=lambda p=full: contextlib.nullcontext(p))


# --------------------------------------------------------------------------- graph
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.Read.All", "Sites.Read.All"]
# Microsoft's own public client ("Microsoft Graph Command Line Tools"): lets you sign in through the
# browser without registering an app. Your tenant may require admin consent or block it; pass
# --client-id for your own public-client app registration (redirect URI http://localhost) if so.
DEFAULT_CLIENT_ID = "14d82eec-204b-4c2c-b7e8-06de6ef8d2e6"
CACHE_PATH = Path.home() / ".sp_profiler" / "token_cache.json"


class BrowserAuth:
    """Delegated sign-in through the system browser, with a persistent token cache (silent refresh)."""

    def __init__(self, tenant_id: str = "organizations", client_id: str = DEFAULT_CLIENT_ID,
                 cache_path: Path = CACHE_PATH):
        import msal
        self.cache_path = Path(cache_path)
        self.cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self.cache.deserialize(self.cache_path.read_text())
        self.app = msal.PublicClientApplication(
            client_id, authority=f"https://login.microsoftonline.com/{tenant_id or 'organizations'}",
            token_cache=self.cache)

    def token(self) -> str:
        res = None
        accounts = self.app.get_accounts()
        if accounts:
            res = self.app.acquire_token_silent(SCOPES, account=accounts[0])
        if not res:
            res = self.app.acquire_token_interactive(SCOPES)   # opens the browser
        if "access_token" not in res:
            raise RuntimeError(f"Sign-in failed: {res.get('error_description', res)}")
        if self.cache.has_state_changed:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(self.cache.serialize())
            try:
                self.cache_path.chmod(0o600)
            except OSError:
                pass
        return res["access_token"]


class GraphSource:
    """List and download files from a SharePoint document library via Microsoft Graph."""

    def __init__(self, site_url: str, folder: str = "", library: str = "Documents",
                 auth=None, session=None):
        import requests  # local import: core package stays dependency-free
        self.site_url, self.folder, self.library = site_url, folder.strip("/"), library
        self.auth = auth or BrowserAuth()
        self.session = session or requests.Session()
        self.drive_id = self._resolve_drive()

    # graph helpers ---------------------------------------------------------
    def _get(self, url: str) -> dict:
        for attempt in range(5):
            r = self.session.get(url, headers={"Authorization": f"Bearer {self.auth.token()}"}, timeout=60)
            if r.status_code in (429, 503):               # throttled: honour Retry-After
                time.sleep(int(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def _resolve_drive(self) -> str:
        u = urlparse(self.site_url)
        site = self._get(f"{GRAPH}/sites/{u.netloc}:{u.path.rstrip('/')}")
        self.site_id = site["id"]
        drives = self._get(f"{GRAPH}/sites/{site['id']}/drives")["value"]
        for d in drives:
            if d["name"].lower() == self.library.lower():
                return d["id"]
        raise LookupError(f"Library {self.library!r} not found; available: {[d['name'] for d in drives]}")

    def _children_url(self, folder: str) -> str:
        if not folder:
            return f"{GRAPH}/drives/{self.drive_id}/root/children?$top=200"
        return f"{GRAPH}/drives/{self.drive_id}/root:/{quote(folder)}:/children?$top=200"

    def _walk(self, folder: str) -> Iterator[tuple[str, dict]]:
        url = self._children_url(folder)
        while url:
            page = self._get(url)
            for item in page.get("value", []):
                if "folder" in item:
                    yield from self._walk(f"{folder}/{item['name']}".strip("/"))
                elif "file" in item:
                    yield folder, item
            url = page.get("@odata.nextLink")

    def _download(self, item: dict):
        @contextlib.contextmanager
        def cm() -> Iterator[Path]:
            tmpdir = tempfile.mkdtemp(prefix="sp_profiler_")
            path = Path(tmpdir) / item["name"]
            try:
                url = item.get("@microsoft.graph.downloadUrl")
                headers = {}
                if not url:   # pre-authenticated URL must NOT get a bearer header; the /content one needs it
                    url = f"{GRAPH}/drives/{self.drive_id}/items/{item['id']}/content"
                    headers = {"Authorization": f"Bearer {self.auth.token()}"}
                with self.session.get(url, headers=headers, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    r.raw.decode_content = True
                    with open(path, "wb") as fh:
                        shutil.copyfileobj(r.raw, fh)
                yield path
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return cm

    def iter_files(self) -> Iterator[FileEntry]:
        for folder, item in self._walk(self.folder):
            rel_folder = folder[len(self.folder):].strip("/") if self.folder else folder
            by = lambda k: (item.get(k, {}).get("user", {}) or {})
            hashes = item.get("file", {}).get("hashes", {}) or {}
            yield FileEntry(
                rel_path=f"{rel_folder}/{item['name']}".strip("/"),
                name=item["name"],
                size_bytes=int(item.get("size", 0)),
                modified=item.get("lastModifiedDateTime"),
                created=item.get("createdDateTime"),
                modified_by=by("lastModifiedBy").get("displayName"),
                location=item.get("webUrl", ""),
                meta={
                    "site_url": self.site_url, "library": self.library, "drive_id": self.drive_id,
                    "item_id": item.get("id"), "sp_path": folder, "mime_type": item.get("file", {}).get("mimeType"),
                    "created_by": by("createdBy").get("displayName"),
                    "modified_by_email": by("lastModifiedBy").get("email"),
                    "etag": item.get("eTag"), "ctag": item.get("cTag"),
                    "quickxor_hash": hashes.get("quickXorHash"), "sha1_hash": hashes.get("sha1Hash"),
                },
                _materialize=self._download(item),
            )
