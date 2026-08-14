"""BeatSaver reverse proxy for self-hosted Moon Rider.

The frontend makes two kinds of same-origin requests that this app fulfils
by fetching from BeatSaver on the server side (no browser CORS, no
Cloudflare bot-blocking of XHR):

  /api/<path>           -> https://api.beatsaver.com/<path>   (JSON API)
  /beatproxy?url=<url>  -> the given URL, restricted to *.beatsaver.com
                           (map zips, cover art, preview mp3s)

Run behind Caddy/Nginx which routes /api/* and /beatproxy to this app and
everything else to the static site.

    pip install -r requirements.txt
    gunicorn -w 2 -b 0.0.0.0:5000 beatsaver_proxy:app
"""

from urllib.parse import urlparse

import requests
from flask import Flask, Response, abort, request

app = Flask(__name__)
session = requests.Session()

API_BASE = "https://api.beatsaver.com"

# A plain browser User-Agent; requests' default UA is what trips
# Cloudflare's bot protection.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Response headers copied through from upstream on binary fetches.
# Content-Length / Content-Encoding are deliberately NOT forwarded:
# requests transparently decompresses gzip, so forwarding the original
# headers would corrupt the body (a classic way to break zip downloads).
PASS_HEADERS = ("Content-Type", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified")


def allowed_host(url):
    host = urlparse(url).hostname or ""
    return host == "beatsaver.com" or host.endswith(".beatsaver.com")


def with_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Range, Content-Type"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Range, Accept-Ranges"
    return resp


@app.route("/api/<path:path>")
def api(path):
    upstream = f"{API_BASE}/{path}"
    try:
        r = session.get(
            upstream,
            params=request.args,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as exc:
        app.logger.error("api fetch failed: %s (%s)", upstream, exc)
        abort(502)
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

    headers = {"User-Agent": USER_AGENT}
    # Forward Range so <audio> seeking on preview mp3s works.
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        r = session.get(url, headers=headers, timeout=60, stream=True)
    except requests.RequestException as exc:
        app.logger.error("beatproxy fetch failed: %s (%s)", url, exc)
        abort(502)

    resp = Response(
        r.iter_content(chunk_size=64 * 1024),
        status=r.status_code,
        content_type=r.headers.get("Content-Type", "application/octet-stream"),
    )
    for name in PASS_HEADERS:
        if name in r.headers and name != "Content-Type":
            resp.headers[name] = r.headers[name]
    return with_cors(resp)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
