#!/usr/bin/env python3
import json
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

NASA_API_KEY = os.environ.get('NASA_API_KEY', 'DEMO_KEY')
NEO_URL = 'https://api.nasa.gov/neo/rest/v1/feed'
DATA_DIR = 'data'
OUTPUT_FILE = os.path.join(DATA_DIR, 'asteroids.json')
USER_AGENT = 'SpaceSentinel/1.0 (personal project; contact via GitHub repo)'


def date_str(d):
    return d.strftime('%Y-%m-%d')


def fetch_json(url, params):
    qs = urllib.parse.urlencode(params)
    full_url = f'{url}?{qs}'
    req = urllib.request.Request(full_url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def process_neo(data):
    """Flatten NeoWs response to a list of close-approach records."""
    out = []
    for date_str, neos in data.get('near_earth_objects', {}).items():
        for neo in neos:
            app = (neo.get('close_approach_data') or [{}])[0]
            dm = neo.get('estimated_diameter', {}).get('meters', {})
            diam_min = dm.get('estimated_diameter_min', 0)
            diam_max = dm.get('estimated_diameter_max', 0)
            rv = app.get('relative_velocity', {})
            md = app.get('miss_distance', {})
            out.append({
                'id':        neo['id'],
                'name':      neo['name'].replace('(', '').replace(')', '').strip(),
                'date':      app.get('close_approach_date', date_str),
                'vel_kms':   float(rv.get('kilometers_per_second', 0)),
                'miss_ld':   float(md.get('lunar', 0)),
                'miss_km':   float(md.get('kilometers', 0)),
                'diam_min':  diam_min,
                'diam_max':  diam_max,
                'diam_avg':  (diam_min + diam_max) / 2,

                'hazardous': neo.get('is_potentially_hazardous_asteroid', False),
                'magnitude': neo.get('absolute_magnitude_h'),
                'url':       neo.get('nasa_jpl_url', ''),
            })
    return sorted(out, key=lambda r: r['date'])


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    today = datetime.now(timezone.utc)

    all_records = {}
    for chunk in range(4):
        c_start = today + timedelta(days=chunk * 7)
        c_end   = c_start + timedelta(days=7)
        start, end = date_str(c_start), date_str(c_end)
        print(f'Fetching NEOs {start} → {end}...')
        params = {
            'start_date': start,
            'end_date':   end,
            'api_key':    NASA_API_KEY,
        }
        data = fetch_json(NEO_URL, params)
        if 'error' in data:
            raise RuntimeError(f"NASA API error: {data['error']}")
        for rec in process_neo(data):

            all_records[(rec['id'], rec['date'])] = rec
        time.sleep(1)  

    records = sorted(all_records.values(), key=lambda r: r['date'])
    print(f'  {len(records)} records total across 28-day window')

    window_end_expected = date_str(today + timedelta(days=27))
    latest = records[-1]['date'] if records else ''
    if len(records) < 20 or latest < window_end_expected:
        print(f'ERROR: Incomplete window ({len(records)} records, latest {latest}). '
              f'Aborting to preserve existing data.')
        raise SystemExit(1)

    window_start = date_str(today)
    window_end   = date_str(today + timedelta(days=28))
    output = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'window':    {'start': window_start, 'end': window_end},
        'count':     len(records),
        'asteroids': records,
    }
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(output, f, separators=(',', ':'))
    print(f'Wrote {OUTPUT_FILE}')


if __name__ == '__main__':
    main()
