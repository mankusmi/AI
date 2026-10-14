# Databricks notebook source
# MAGIC %md
# MAGIC # SharePoint → Unity Catalog Volume ingestion
# MAGIC
# MAGIC Copies files from a SharePoint document-library folder into a UC Volume using the Microsoft Graph API
# MAGIC (app-only / client-credentials auth).
# MAGIC
# MAGIC **Prerequisites**
# MAGIC 1. Entra ID app registration with Graph **application** permission `Sites.Read.All` (or `Sites.Selected` + a site grant), admin-consented.
# MAGIC 2. Store credentials in a Databricks secret scope (default scope `sharepoint`, keys `tenant-id`, `client-id`, `client-secret`):
# MAGIC    `databricks secrets put-secret sharepoint client-secret`
# MAGIC 3. An existing UC Volume you can write to, e.g. `/Volumes/main/raw/sharepoint`.
# MAGIC 4. Cluster/serverless with outbound access to `login.microsoftonline.com` and `graph.microsoft.com`.

# COMMAND ----------

dbutils.widgets.text("secret_scope", "sharepoint", "Secret scope")
dbutils.widgets.text("site_hostname", "contoso.sharepoint.com", "SharePoint hostname")
dbutils.widgets.text("site_path", "sites/MySite", "Site path (e.g. sites/MySite)")
dbutils.widgets.text("library_name", "Documents", "Document library (drive) name")
dbutils.widgets.text("folder_path", "", "Folder inside library ('' = root)")
dbutils.widgets.text("volume_path", "/Volumes/main/raw/sharepoint", "Target volume path")
dbutils.widgets.dropdown("recursive", "true", ["true", "false"], "Recurse into subfolders")
dbutils.widgets.dropdown("overwrite", "false", ["true", "false"], "Overwrite existing files (else skip if same size)")
dbutils.widgets.text("extensions", "", "Only these extensions, comma-separated (blank = all)")

SECRET_SCOPE = dbutils.widgets.get("secret_scope")
SITE_HOSTNAME = dbutils.widgets.get("site_hostname").strip()
SITE_PATH = dbutils.widgets.get("site_path").strip("/ ")
LIBRARY_NAME = dbutils.widgets.get("library_name").strip()
FOLDER_PATH = dbutils.widgets.get("folder_path").strip("/ ")
VOLUME_PATH = dbutils.widgets.get("volume_path").rstrip("/")
RECURSIVE = dbutils.widgets.get("recursive") == "true"
OVERWRITE = dbutils.widgets.get("overwrite") == "true"
EXTENSIONS = tuple(
    "." + e.strip().lower().lstrip(".")
    for e in dbutils.widgets.get("extensions").split(",")
    if e.strip()
)

# COMMAND ----------

import os
import time
import requests
from urllib.parse import quote

GRAPH = "https://graph.microsoft.com/v1.0"


def get_token() -> str:
    tenant = dbutils.secrets.get(SECRET_SCOPE, "tenant-id")
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": dbutils.secrets.get(SECRET_SCOPE, "client-id"),
            "client_secret": dbutils.secrets.get(SECRET_SCOPE, "client-secret"),
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


_token = {"value": get_token(), "ts": time.time()}


def auth_headers() -> dict:
    # Tokens last ~60-90 min; refresh well before expiry for long runs.
    if time.time() - _token["ts"] > 45 * 60:
        _token.update(value=get_token(), ts=time.time())
    return {"Authorization": f"Bearer {_token['value']}"}


def graph_get(url: str, **kwargs) -> requests.Response:
    """GET with retry/backoff for throttling (429) and transient 5xx."""
    for attempt in range(6):
        resp = requests.get(url, headers=auth_headers(), timeout=60, **kwargs)
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(resp.headers.get("Retry-After", 2 ** attempt)))
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def graph_paged(url: str):
    while url:
        data = graph_get(url).json()
        yield from data.get("value", [])
        url = data.get("@odata.nextLink")

# COMMAND ----------

# Resolve site -> drive (document library)
site = graph_get(f"{GRAPH}/sites/{SITE_HOSTNAME}:/{SITE_PATH}").json()
drives = list(graph_paged(f"{GRAPH}/sites/{site['id']}/drives"))
drive = next((d for d in drives if d["name"].lower() == LIBRARY_NAME.lower()), None)
if drive is None:
    raise ValueError(f"Library '{LIBRARY_NAME}' not found. Available: {[d['name'] for d in drives]}")

print(f"Site:    {site['displayName']} ({site['id']})")
print(f"Library: {drive['name']} ({drive['id']})")

# COMMAND ----------

def children_url(path: str) -> str:
    base = f"{GRAPH}/drives/{drive['id']}"
    if path:
        return f"{base}/root:/{quote(path)}:/children?$top=200"
    return f"{base}/root/children?$top=200"


def walk(path: str):
    """Yield (relative_path, item) for every file under `path`."""
    for item in graph_paged(children_url(path)):
        rel = f"{path}/{item['name']}" if path else item["name"]
        if "folder" in item:
            if RECURSIVE:
                yield from walk(rel)
        elif "file" in item:
            yield rel, item


def download(rel_path: str, item: dict) -> str:
    # Relative to the ingested folder so the volume mirrors the source structure.
    sub = rel_path[len(FOLDER_PATH):].lstrip("/") if FOLDER_PATH else rel_path
    dest = f"{VOLUME_PATH}/{sub}"

    if not OVERWRITE and os.path.exists(dest) and os.path.getsize(dest) == item["size"]:
        return "skipped"

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    # Pre-authenticated URL: fetch it fresh and do NOT send the bearer token.
    url = item.get("@microsoft.graph.downloadUrl")
    if not url:
        url = graph_get(f"{GRAPH}/drives/{drive['id']}/items/{item['id']}").json()["@microsoft.graph.downloadUrl"]

    tmp = dest + ".part"
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
    os.replace(tmp, dest)
    return "downloaded"

# COMMAND ----------

stats = {"downloaded": 0, "skipped": 0, "failed": 0, "filtered": 0}
failures = []

for rel_path, item in walk(FOLDER_PATH):
    if EXTENSIONS and not item["name"].lower().endswith(EXTENSIONS):
        stats["filtered"] += 1
        continue
    try:
        stats[download(rel_path, item)] += 1
        print(f"{rel_path}  ({item['size']:,} bytes)")
    except Exception as e:  # keep going; report at the end
        stats["failed"] += 1
        failures.append((rel_path, str(e)))
        print(f"FAILED {rel_path}: {e}")

print("\nSummary:", stats)
if failures:
    raise RuntimeError(f"{len(failures)} file(s) failed, e.g. {failures[:3]}")

# COMMAND ----------

display(dbutils.fs.ls(VOLUME_PATH))
