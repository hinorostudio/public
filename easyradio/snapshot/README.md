# EasyRadio station snapshot

A weekly, trimmed copy of the [Radio Browser](https://www.radio-browser.info)
catalogue (public-domain data). The EasyRadio app uses it as a fallback station
source if the Radio Browser API is unreachable.

| | |
|---|---|
| Data | <https://github.com/hinorostudio/public/releases/download/easyradio-stations/stations.json.gz> |
| Metadata | <https://github.com/hinorostudio/public/releases/download/easyradio-stations/meta.json> |
| Last week's data | `.../easyradio-stations/stations-previous.json.gz` |
| Built by | `.github/workflows/easyradio-stations-snapshot.yml` (Mondays, or *Run workflow*) |

`stations.json.gz` is a JSON array using the same field names as the API's
`/json/stations` (only the 17 fields the app reads), working stations only,
de-duplicated by uuid then by stream URL, most-clicked first.

`meta.json` in this folder is the record of the last successful run. The build
refuses to publish if the API answer is truncated, holds fewer than 10 000
stations, or shrank more than 20% since the last run - the previous snapshot
then simply stays online. To accept a legitimate large drop, delete `meta.json`
and re-run.

Local run: `python3 build_snapshot.py --out /tmp/snapshot`
