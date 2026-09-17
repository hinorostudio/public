#!/usr/bin/env python3
"""Builds the EasyRadio station snapshot: a trimmed copy of the Radio Browser
catalogue, hosted outside radio-browser.info so the app keeps working if that
project ever goes away.

Output (in --out):
  stations.json.gz  JSON array in the exact shape of the API's station objects,
                    restricted to the fields the app's StationDto reads, sorted
                    by clickcount descending.
  meta.json         date, counts, sha256 — small enough for the app to poll.

Exits non-zero WITHOUT touching --out when the result looks wrong (API down,
truncated answer, sudden shrink). The previous snapshot then stays published,
which is the whole point: a bad run must never replace a good backup.

Standard library only.
"""

import argparse
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Discovery order mirrors the app: round-robin name first, then the known host.
API_HOSTS = ["all.api.radio-browser.info", "de1.api.radio-browser.info"]
USER_AGENT = "EasyRadioSnapshot/1.0 (+https://github.com/hinorostudio/public)"

# The API's default page is 1000; ask for far more than the catalogue holds and
# verify against /json/stats that nothing was capped.
FETCH_LIMIT = 500_000

SCHEMA_VERSION = 1

# Exactly the @SerializedName fields of the app's StationDto.
STRING_FIELDS = [
    "stationuuid", "name", "url_resolved", "url", "homepage", "favicon", "tags",
    "countrycode", "state", "language", "languagecodes", "codec",
]
INT_FIELDS = ["votes", "bitrate", "hls", "lastcheckok", "clickcount"]

MIN_STATIONS = 10_000          # absolute floor, whatever the history says
MAX_SHRINK = 0.20              # vs. the previous snapshot
MAX_STATS_MISMATCH = 0.05      # fetched vs. what /json/stats announces


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def http_get_json(url, timeout, attempts=4):
    last = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = e
            log(f"  attempt {attempt}/{attempts} failed: {e}")
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise last


def fetch_from_api():
    """Returns (stations, announced_count, host). Tries each host in turn."""
    last = None
    for host in API_HOSTS:
        try:
            log(f"Fetching from {host} ...")
            stats = http_get_json(f"https://{host}/json/stats", timeout=30)
            stations = http_get_json(
                f"https://{host}/json/stations?hidebroken=true&limit={FETCH_LIMIT}",
                timeout=300,
            )
            return stations, int(stats["stations"]), host
        except Exception as e:  # noqa: BLE001 - any failure means "try the next host"
            last = e
            log(f"  {host} unusable: {e}")
    raise SystemExit(f"FAIL: no Radio Browser host answered ({last})")


def trim(station):
    out = {}
    for key in STRING_FIELDS:
        value = station.get(key)
        out[key] = value.strip() if isinstance(value, str) else ""
    for key in INT_FIELDS:
        try:
            out[key] = int(station.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    # The app does `urlResolved ?: url`: an empty string would win over url.
    if not out["url_resolved"]:
        out["url_resolved"] = out["url"]
    return out


def is_usable(s):
    if not s["stationuuid"] or not s["name"]:
        return False
    if s["lastcheckok"] != 1:
        return False
    stream = s["url_resolved"] or s["url"]
    return stream.startswith(("http://", "https://"))


def dedupe(stations):
    """Same two passes as the app's RadioRepositoryImpl.toDomainList():
    best entry per stationuuid, then best entry per resolved stream URL
    (favicon first, then votes, bitrate, clickcount)."""
    by_uuid = {}
    for s in stations:
        key = s["stationuuid"]
        rank = (s["votes"], s["bitrate"], s["clickcount"])
        if key not in by_uuid or rank > by_uuid[key][0]:
            by_uuid[key] = (rank, s)

    by_url = {}
    for _, s in by_uuid.values():
        key = s["url_resolved"] or s["url"]
        rank = (bool(s["favicon"]), s["votes"], s["bitrate"], s["clickcount"])
        if key not in by_url or rank > by_url[key][0]:
            by_url[key] = (rank, s)
    return [s for _, s in by_url.values()]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path, help="output directory")
    parser.add_argument("--previous-meta", type=Path,
                        help="meta.json of the last published snapshot (shrink guard)")
    parser.add_argument("--input", type=Path,
                        help="read a saved /json/stations response instead of calling the API (tests)")
    args = parser.parse_args()

    if args.input:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        announced, host = len(raw), f"file:{args.input.name}"
    else:
        raw, announced, host = fetch_from_api()
    if not isinstance(raw, list):
        raise SystemExit("FAIL: API answer is not a JSON array")
    log(f"Fetched {len(raw)} stations ({announced} announced by /json/stats)")

    if announced and len(raw) < announced * (1 - MAX_STATS_MISMATCH):
        raise SystemExit(
            f"FAIL: got {len(raw)} stations but the API announces {announced} - answer was capped or truncated"
        )

    trimmed = [trim(s) for s in raw if isinstance(s, dict)]
    usable = [s for s in trimmed if is_usable(s)]
    stations = dedupe(usable)
    # Most popular first; uuid as tie-break keeps the output deterministic.
    stations.sort(key=lambda s: (-s["clickcount"], -s["votes"], s["stationuuid"]))
    log(f"Usable: {len(usable)}  after dedupe: {len(stations)}")

    if len(stations) < MIN_STATIONS:
        raise SystemExit(f"FAIL: only {len(stations)} stations, floor is {MIN_STATIONS}")

    if args.previous_meta and args.previous_meta.exists():
        previous = json.loads(args.previous_meta.read_text(encoding="utf-8")).get("stations", 0)
        if previous and len(stations) < previous * (1 - MAX_SHRINK):
            raise SystemExit(
                f"FAIL: {len(stations)} stations vs {previous} last time "
                f"(-{1 - len(stations) / previous:.0%}, limit -{MAX_SHRINK:.0%}) - keeping the previous snapshot"
            )
        log(f"Previous snapshot had {previous} stations - shrink guard OK")

    payload = json.dumps(stations, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # mtime=0 -> identical input gives a byte-identical archive
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)

    countries = {s["countrycode"] for s in stations if s["countrycode"]}
    meta = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": host,
        "source_stations": len(raw),
        "stations": len(stations),
        "countries": len(countries),
        "file": "stations.json.gz",
        "bytes": len(compressed),
        "bytes_uncompressed": len(payload),
        "sha256": hashlib.sha256(compressed).hexdigest(),
    }

    # Everything validated: only now write to --out.
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "stations.json.gz").write_bytes(compressed)
    (args.out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    log(f"Wrote {len(compressed) / 1e6:.2f} MB ({len(payload) / 1e6:.1f} MB raw) to {args.out}")
    print(json.dumps(meta))


if __name__ == "__main__":
    main()
