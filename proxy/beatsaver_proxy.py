"""BeatSaver reverse proxy for self-hosted Moon Rider, with offline cache.

The frontend makes two kinds of same-origin requests that this app fulfils
by fetching from BeatSaver on the server side (no browser CORS, no
Cloudflare bot-blocking of XHR):

  /api/<path>           -> https://api.beatsaver.com/<path>   (JSON API)
  /beatproxy?url=<url>  -> the given URL, restricted to *.beatsaver.com
                           (map zips, cover art, preview mp3s)

Offline behaviour
-----------------
Everything that passes through is cached on disk under CACHE_DIR
(default ./cache; set the env var to persist elsewhere):

- CDN files (zips/covers/mp3s) are content-addressed upstream, so they are
  served cache-first and kept forever. Any song played once replays with no
  internet at all.
- API JSON responses are network-first with a cache fallback.
- Every map document seen in an API response is harvested into a local
  library index. When BeatSaver is unreachable, /api/search and playlist
  requests are answered from the library instead — returning only maps
  whose zip is already downloaded, so everything offered offline is
  actually playable.

Run behind Caddy/Nginx which routes /api/* and /beatproxy to this app and
everything else to the static site.

    pip install -r requirements.txt
    gunicorn -w 2 -b 0.0.0.0:5000 beatsaver_proxy:app
"""

import hashlib
import json
import os
from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, request

app = Flask(__name__)
session = requests.Session()

API_BASE = "https://api.beatsaver.com"

CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(os.path.dirname(__file__), "cache"))
BLOB_DIR = os.path.join(CACHE_DIR, "blobs")
API_CACHE_DIR = os.path.join(CACHE_DIR, "api")
LIBRARY_DIR = os.path.join(CACHE_DIR, "library")
for d in (BLOB_DIR, API_CACHE_DIR, LIBRARY_DIR):
    os.makedirs(d, exist_ok=True)

# A plain browser User-Agent; requests' default UA is what trips
# Cloudflare's bot protection.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SEARCH_PAGE_SIZE = 20


def allowed_host(url):
    host = urlparse(url).hostname or ""
    return host == "beatsaver.com" or host.endswith(".beatsaver.com")


def with_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Range, Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Range, Accept-Ranges"
    return resp


def cache_key(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Local library index (harvested from API responses passing through)
# ---------------------------------------------------------------------------

def extract_map_docs(payload):
    """Pull map documents out of any BeatSaver API JSON response shape."""
    docs = []
    if isinstance(payload, dict):
        for entry in payload.get("docs") or payload.get("maps") or []:
            # Playlist responses wrap each map as {"map": {...}}.
            doc = entry.get("map") if isinstance(entry, dict) and "map" in entry else entry
            if isinstance(doc, dict) and doc.get("id") and doc.get("versions"):
                docs.append(doc)
        # Single-map lookups (/maps/id/<id>) return the doc at the top level.
        if payload.get("id") and payload.get("versions"):
            docs.append(payload)
    return docs


def harvest_library(payload):
    for doc in extract_map_docs(payload):
        try:
            path = os.path.join(LIBRARY_DIR, f"{doc['id']}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(doc, f)
        except OSError as exc:
            app.logger.warning("library write failed for %s: %s", doc.get("id"), exc)


def zip_is_cached(doc):
    try:
        url = doc["versions"][0]["downloadURL"]
    except (KeyError, IndexError, TypeError):
        return False
    return os.path.exists(os.path.join(BLOB_DIR, cache_key(url)))


def library_docs(downloaded_only=True):
    docs = []
    for name in os.listdir(LIBRARY_DIR):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(LIBRARY_DIR, name), encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            continue
        if downloaded_only and not zip_is_cached(doc):
            continue
        docs.append(doc)
    return docs


def offline_api_response(path):
    """Answer an API request from the local library when BeatSaver is down."""
    query = (request.args.get("q") or "").strip().lower()
    page = 0
    parts = path.strip("/").split("/")
    if parts and parts[-1].isdigit():
        page = int(parts[-1])

    docs = library_docs(downloaded_only=True)

    if query:
        def matches(doc):
            meta = doc.get("metadata") or {}
            haystack = " ".join(
                str(v) for v in (
                    doc.get("name"),
                    meta.get("songName"),
                    meta.get("songSubName"),
                    meta.get("songAuthorName"),
                    meta.get("levelAuthorName"),
                ) if v
            ).lower()
            return all(word in haystack for word in query.split())
        docs = [d for d in docs if matches(d)]

    docs.sort(key=lambda d: (d.get("stats") or {}).get("score", 0), reverse=True)
    start = page * SEARCH_PAGE_SIZE
    page_docs = docs[start:start + SEARCH_PAGE_SIZE]

    app.logger.info("offline library answered /api/%s: %d of %d docs", path, len(page_docs), len(docs))
    resp = Response(json.dumps({"docs": page_docs}), status=200, content_type="application/json")
    return with_cors(resp)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/<path:path>")
def api(path):
    upstream = f"{API_BASE}/{path}"
    full_url = upstream + ("?" + request.query_string.decode("utf-8") if request.query_string else "")
    api_cache_path = os.path.join(API_CACHE_DIR, cache_key(full_url) + ".json")

    try:
        r = session.get(
            upstream,
            params=request.args,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
    except requests.RequestException as exc:
        app.logger.warning("api fetch failed: %s (%s) — trying cache/library", full_url, exc)
        if os.path.exists(api_cache_path):
            with open(api_cache_path, "rb") as f:
                resp = Response(f.read(), status=200, content_type="application/json")
            return with_cors(resp)
        return offline_api_response(path)

    try:
        payload = r.json()
        harvest_library(payload)
        with open(api_cache_path, "wb") as f:
            f.write(r.content)
    except (ValueError, OSError) as exc:
        app.logger.warning("api cache/harvest skipped: %s", exc)

    resp = Response(
        r.content,
        status=r.status_code,
        content_type=r.headers.get("Content-Type", "application/json"),
    )
    return with_cors(resp)


@app.route("/beatproxy")
def beatproxy():
    url = request.args.get("url", "")
    if not url.startswith("https://") or not allowed_host(url):
        abort(403)

    blob_path = os.path.join(BLOB_DIR, cache_key(url))
    meta_path = blob_path + ".meta"

    # Cache-first: CDN content is content-addressed and immutable.
    if os.path.exists(blob_path):
        content_type = "application/octet-stream"
        try:
            with open(meta_path, encoding="utf-8") as f:
                content_type = json.load(f).get("content_type", content_type)
        except (OSError, ValueError):
            pass
        with open(blob_path, "rb") as f:
            body = f.read()
        resp = Response(body, status=200, content_type=content_type)
        return with_cors(resp)

    headers = {"User-Agent": USER_AGENT}
    try:
        r = session.get(url, headers=headers, timeout=60)
        r.raise_for_status()
    except requests.RequestException as exc:
        app.logger.error("beatproxy fetch failed and not cached: %s (%s)", url, exc)
        abort(502)

    content_type = r.headers.get("Content-Type", "application/octet-stream")
    try:
        tmp = blob_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, blob_path)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"url": url, "content_type": content_type}, f)
    except OSError as exc:
        app.logger.warning("blob cache write failed: %s", exc)

    resp = Response(r.content, status=200, content_type=content_type)
    return with_cors(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
