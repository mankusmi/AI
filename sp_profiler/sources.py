"""File sources: a local / OneDrive-synced folder, or a SharePoint folder via Microsoft Graph.

Both yield ``FileEntry`` objects and know how to hand out a local copy of the
file (``materialize``) so the inspector never cares where the bytes came from.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
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


# --------------------------------------------------------------------------- graph
GRAPH = "https://graph.microsoft.com/v1.0"


class GraphSource:
    """List and download files from a SharePoint document library via Microsoft Graph.

    Needs ``requests`` and ``msal``. App registration needs Graph permission
    ``Sites.Read.All`` (application, with ``client_secret``) or ``Files.Read.All``
    (delegated, interactive device-code login when no secret is given).
    """

    def __init__(self, site_url: str, folder: str = "", library: str = "Documents",
                 tenant_id: str = "", client_id: str = "", client_secret: str = "",
                 token: str = "", session=None):
        import requests  # local import: core package stays dependency-free
        self.site_url, self.folder, self.library = site_url, folder.strip("/"), library
        self.session = session or requests.Session()
        self._token = token or self._acquire_token(tenant_id, client_id, client_secret)
        self.session.headers["Authorization"] = f"Bearer {self._token}"
        self.drive_id = self._resolve_drive()

    # auth ------------------------------------------------------------------
    @staticmethod
    def _acquire_token(tenant_id: str, client_id: str, client_secret: str) -> str:
        if not (tenant_id and client_id):
            raise ValueError("Provide --token, or --tenant-id and --client-id")
        import msal
        authority = f"https://login.microsoftonline.com/{tenant_id}"
        if client_secret:
            app = msal.ConfidentialClientApplication(client_id, client_secret, authority=authority)
            res = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        else:
            app = msal.PublicClientApplication(client_id, authority=authority)
            flow = app.initiate_device_flow(scopes=["Files.Read.All", "Sites.Read.All"])
            print(flow["message"], flush=True)
            res = app.acquire_token_by_device_flow(flow)
        if "access_token" not in res:
            raise RuntimeError(f"Token acquisition failed: {res.get('error_description', res)}")
        return res["access_token"]

    # graph helpers ---------------------------------------------------------
    def _get(self, url: str) -> dict:
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.json()

    def _resolve_drive(self) -> str:
        u = urlparse(self.site_url)
        site = self._get(f"{GRAPH}/sites/{u.netloc}:{u.path.rstrip('/')}")
        drives = self._get(f"{GRAPH}/sites/{site['id']}/drives")["value"]
        names = [d["name"] for d in drives]
        for d in drives:
            if d["name"].lower() == self.library.lower():
                return d["id"]
        raise LookupError(f"Library {self.library!r} not found on site; available: {names}")

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
                url = item.get("@microsoft.graph.downloadUrl") or \
                    f"{GRAPH}/drives/{self.drive_id}/items/{item['id']}/content"
                with self.session.get(url, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(path, "wb") as fh:
                        shutil.copyfileobj(r.raw, fh)
                yield path
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        return cm

    def iter_files(self) -> Iterator[FileEntry]:
        for folder, item in self._walk(self.folder):
            rel_folder = folder[len(self.folder):].strip("/") if self.folder else folder
            yield FileEntry(
                rel_path=f"{rel_folder}/{item['name']}".strip("/"),
                name=item["name"],
                size_bytes=int(item.get("size", 0)),
                modified=item.get("lastModifiedDateTime"),
                created=item.get("createdDateTime"),
                modified_by=(item.get("lastModifiedBy", {}).get("user", {}) or {}).get("displayName"),
                location=item.get("webUrl", ""),
                _materialize=self._download(item),
            )
