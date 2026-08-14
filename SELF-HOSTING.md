# Self-Hosting Moon Rider — Research Findings & Runbook

This repo is a snapshot of [supermedium/moonrider](https://github.com/supermedium/moonrider)
(MIT licensed, last upstream commit March 2024) plus the patches needed to
self-host it in 2026. This document explains what was broken, why, and what
was changed.

## TL;DR

1. **The build is not actually broken.** `npm ci` + webpack works on Node 22
   with one flag: `NODE_OPTIONS=--openssl-legacy-provider`. The included
   `Dockerfile` does this for you (`docker compose up -d --build`).
2. **Four places in the frontend hardcode BeatSaver/Supermedium URLs.** They
   are patched in this repo to use same-origin paths (`/api/…`,
   `/beatproxy?url=…`) that your reverse proxy fulfils.
3. **The "infinite loading" bug had two independent causes**, both fixed
   here — see below.

## Why the stock frontend can't work self-hosted

The stock code talks to four external endpoints straight from the browser:

| File | URL | What breaks |
|---|---|---|
| `src/components/search.js` | `https://beatsaver.com/api/search/text/…` and `https://api.beatsaver.com/playlists/…` | CORS / Cloudflare when called from your origin |
| `src/lib/convert-beatmap.js` | `https://beatproxy.b-cdn.net/<cover>.jpg` | This is **Supermedium's own private CORS proxy** (a bunny.net CDN). Your cover-art 404s come from here — it's not yours and not guaranteed to serve arbitrary origins |
| `src/lib/convert-beatmap.js` | `downloadURL` from the API response (`https://r2cdn.beatsaver.com/<hash>.zip`) | The **web worker** (`build/zip.js`) XHRs this URL directly. If CORS headers are missing/blocked, the XHR fails inside the worker — silently (see bug #1) |
| `src/components/song-preview.js` | `https://cdn.beatsaver.com/<hash>.mp3` | Same CORS story for preview audio |

BeatSaver has broken CORS on their side before and taken down even the
official site (see upstream issue
[#153](https://github.com/supermedium/moonrider/issues/153)), so pinning all
traffic through your own proxy is the robust move regardless.

**Patch applied:** all four now use relative URLs. The API goes to
`/api/<new-api-path>` (mapped to `https://api.beatsaver.com/…`), and every
CDN fetch (zip, cover, mp3) goes to `/beatproxy?url=<encoded-url>`, which the
Python proxy fulfils server-side with a browser User-Agent (host-allowlisted
to `*.beatsaver.com` so it isn't an open proxy).

## The infinite-loading bug — root causes

### Cause 1: the zip worker swallowed every error

`src/workers/zip.js` (compiled to `build/zip.js`) only ever posted a `load`
message back to the page. Every failure path — network error, corrupt zip,
JSON parse failure, unsupported map — did `console.error(err); return;`.
The page-side component (`src/components/zip-loader.js`) has a handler for an
`error` message, **but the worker never sent one**, so any failure = loading
screen forever. Additionally the worker only assembled the song once it had
seen both an audio file and `info.dat`; a zip missing either simply never
resolved.

**Patch applied:** the worker now posts `{message: 'error'}` on every failure
path (which makes the UI show the song-load-error state instead of spinning),
counts entries so "finished but incomplete" zips fail loudly, and
`zip-loader.js` treats an uncaught worker exception as a song load error too.

### Cause 2: `TypeError: this.destroy is not a function`

This is a real landmine in the dependency tree:

- The worker unzips with [`unzip-js`](https://github.com/Merzouqi/unzip-js),
  which reads entries through [`blob-slicer`](https://www.npmjs.com/package/blob-slicer).
- `blob-slicer` calls `this.destroy()` on its read stream when each entry
  finishes — but **forgets to declare `readable-stream` in its
  dependencies**. It gets whatever copy npm happens to hoist to the root of
  `node_modules`.
- `Readable.prototype.destroy()` only exists in `readable-stream` **≥ 2.3.0**.
  This dependency tree is full of ancient packages wanting readable-stream
  1.x, so a fresh/regenerated install can hoist a 1.x copy to the root.
- Result: reading any zip entry throws `this.destroy is not a function` **on
  the stream's `end` event — before the worker's own `end` handler runs** —
  so the song data is never assembled. Combined with cause 1, that's an
  infinite loading screen with exactly that TypeError in the console.

The committed `package-lock.json` happens to resolve root `readable-stream`
to 2.3.6 (fine), which is why builds from a clean `npm ci` work while builds
from a re-resolved tree can silently produce a broken `build/zip.js`.

**Patch applied:** `readable-stream: ^2.3.7` is now a *direct* dependency,
which forces the root hoist to a version that has `.destroy()` no matter how
the rest of the tree resolves.

### v4 beatmaps: supported via conversion

Moon Rider understands map format v2 natively and converts v3 → v2
(`src/components/beat-generator.js`). The **v4** format BeatSaver introduced
in 2024 originally crashed the zip worker mid-parse (another infinite load).
Both halves are now converted:

- v4 `info.dat` (flat `difficultyBeatmaps` list) is reshaped into v2
  `_difficultyBeatmapSets` in the zip worker (`convertInfo_4xx_to_2xx`).
- v4 beat data (notes/bombs/obstacles split into index + `*Data` attribute
  arrays) is dereferenced down to the v3 shape
  (`convertBeatData_4xx_to_320`), then flows through the existing v3 → v2
  converter.

Arcs, chains, and the separate v4 lightshow file are ignored — same as v3
maps, where Moon Rider already skips features the engine doesn't render. A
genuinely malformed map still fails with a visible song-load error rather
than a hang.

## Deploying

```
docker compose up -d --build
```

gives you:

- `moonrider` — nginx serving the compiled static site on port 8080
- `beatproxy` — Flask/gunicorn proxy on port 5000

Then route in your existing Caddy (see `Caddyfile.example`): `/api/*` and
`/beatproxy*` → port 5000, everything else → port 8080. HTTPS via Caddy as
you already have it — WebXR requires a secure context, so keep the https
frontage.

To build without Docker:

```
npm ci
NODE_ENV=production NODE_OPTIONS=--openssl-legacy-provider npx webpack
```

and serve `index.html`, `assets/`, `build/`, `vendor/` with any web server.

## Offline & fully-local operation

The deployment needs no external services except BeatSaver itself — and even
that is now cached:

- **No CDNs.** `index.html` used to load A-Frame from jsdelivr (pinned commit
  `2c4509aa`); that exact build is now vendored at
  `vendor/aframe-master-2c4509aa.min.js`. Google Analytics and the
  Supermedium newsletter form (which POSTed to supermedium.com) are removed.
  Firebase (leaderboards) only activates if an API key is configured, which
  it isn't.
- **Song cache.** The proxy caches everything on disk under `CACHE_DIR`
  (Docker: the `beatproxy-cache` volume; bare install: `proxy/cache/`).
  CDN files (zips, covers, preview mp3s) are content-addressed upstream, so
  they're served cache-first forever — any song played once replays with no
  internet at all.
- **Offline library.** Every map document that passes through the API proxy
  is harvested into a local index. When BeatSaver is unreachable, search and
  playlist requests are answered from that index instead, returning only
  maps whose zip is already downloaded — so everything the menu offers
  offline is actually playable. Searching by title/artist works against the
  local library.
- **To build a library deliberately**, just browse and play songs while
  online — or loop `curl` over the proxy: fetch
  `/api/search/text/<page>?sortOrder=Rating` and then `/beatproxy?url=<downloadURL>`
  for each result's `versions[0].downloadURL`.

Remaining caveat: the *default* menu playlists are a canned list bundled at
build time (`src/lib/search.json`); offline, entries you haven't cached will
show but fail with a visible song-load error. Search results, by contrast,
are filtered to downloaded songs only.

## Misc notes

- **Firebase / leaderboards:** `src/components/leaderboard.js` only
  initializes Firebase if an API key is configured; without one it degrades
  gracefully. No action needed for a home-lab install.
- **`previews.moonrider.xyz`** (`src/utils.js`) is Supermedium's S3 helper;
  not on the play-a-song critical path.
- The old advice of "Node < 12" in the upstream README predates the
  `--openssl-legacy-provider` workaround; Node 22 works.
