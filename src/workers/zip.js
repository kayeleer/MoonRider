var window = self;

var unzip = require('unzip-js')

// Fetch and unzip.
// The original worker only ever posted a 'load' message: any failure (bad
// download, corrupt zip, unsupported map format) was console.error'd and the
// UI hung on the loading screen forever. Every failure path now posts an
// 'error' message so zip-loader can emit songloaderror.
addEventListener('message', function (evt) {
  const version = evt.data.version;
  const hash = evt.data.hash;

  if (evt.data.abort) { return; }

  let posted = false;

  function fail (err) {
    console.error('[zip-worker]', err);
    if (posted) { return; }
    posted = true;
    postMessage({ message: 'error', version: version, hash: hash });
  }

  unzip(evt.data.directDownload, function (err, zipFile) {
    if (err) { return fail(err); }

    zipFile.readEntries(function (err, entries) {
      if (err) { return fail(err); }
      if (!entries || !entries.length) { return fail(new Error('empty zip')); }

      const data = {
        audio: undefined,
        beats: {}
      };

      const beatFiles = {};

      let pending = entries.length;

      function entryDone () {
        pending--;
        if (pending > 0 || posted) { return; }

        if (data.audio === undefined) {
          return fail(new Error('no .ogg/.egg audio file found in zip'));
        }
        if (data.info === undefined) {
          return fail(new Error('no info.dat found in zip'));
        }
        // v4 info.dat (2024+) drops _difficultyBeatmapSets; not supported here.
        if (!data.info._difficultyBeatmapSets) {
          return fail(new Error('unsupported info.dat format (no _difficultyBeatmapSets — v4 map?)'));
        }

        for (const difficultyBeatmapSet of data.info._difficultyBeatmapSets) {
          const beatmapCharacteristicName = difficultyBeatmapSet._beatmapCharacteristicName;

          for (const difficultyBeatmap of difficultyBeatmapSet._difficultyBeatmaps) {
            const difficulty = difficultyBeatmap._difficulty;
            const beatmapFilename = difficultyBeatmap._beatmapFilename;
            if (beatFiles[beatmapFilename] === undefined) { continue; }

            const id = beatmapCharacteristicName + '-' + difficulty;
            if (data.beats[id] === undefined) {
              data.beats[id] = beatFiles[beatmapFilename];
            }
          }
        }

        posted = true;
        postMessage({ message: 'load', data: data, version: version, hash: hash });
      }

      entries.forEach(function (entry) {
        const chunks = [];

        zipFile.readEntryData(entry, false, function (err, readStream) {
          if (err) { fail(err); return entryDone(); }

          readStream.on('error', function (err) { fail(err); entryDone(); });

          readStream.on('data', function (chunk) { chunks.push(chunk) })

          readStream.on('end', function () {
            try {
              if (entry.name.endsWith('.egg') || entry.name.endsWith('.ogg')) {
                var blob = new Blob(chunks, /* { type: 'application/octet-binary' } */);
                var url = URL.createObjectURL(blob);

                data.audio = url;
              } else {
                var filename = entry.name;
                if (filename.toLowerCase().endsWith('.dat')) {
                  var string = Buffer.concat(chunks).toString('utf8')
                  var value = JSON.parse(string);

                  if (filename.toLowerCase() === 'info.dat') {
                    data.info = value;
                  } else {
                    value._beatsPerMinute = evt.data.bpm;
                    beatFiles[filename] = value;
                  }
                }
              }
            } catch (e) {
              fail(e);
            }
            entryDone();
          })
        })
      })
    })
  })
});

// data: {audio url, beats { difficulty JSONs },
