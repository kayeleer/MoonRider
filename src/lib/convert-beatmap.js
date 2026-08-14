module.exports = function convertBeatmap (src) {
  if (src.converted) return src;

  if (src['map']) { src = src['map']; }

  src['version'] = src['versions'][0]['hash'];

  // Route CDN fetches through the same-origin proxy (see proxy/beatsaver_proxy.py)
  // so the browser never talks to *.beatsaver.com directly (CORS / Cloudflare).
  src['directDownload'] = '/beatproxy?url=' + encodeURIComponent(src['versions'][0]['downloadURL']);

  src['coverURL'] = '/beatproxy?url=' + encodeURIComponent(src['versions'][0]['coverURL']);

  let diffs = src['versions'][0]['diffs'];

  src.metadata.characteristics = {};

  for (const item of diffs) {

    if (src.metadata.characteristics[item['characteristic']] === undefined) {
      src.metadata.characteristics[item['characteristic']] = {};
    }

    src.metadata.characteristics[item['characteristic']][item['difficulty']] = item;
  }
  src.metadata.characteristics = JSON.stringify(src.metadata.characteristics);

  src.converted = true;

  return src;
};
