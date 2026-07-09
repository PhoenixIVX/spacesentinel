#!/usr/bin/env python3
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone

for key in ('SUPABASE_URL', 'SUPABASE_SERVICE_KEY', 'RESEND_API_KEY'):
    os.environ.setdefault(key, '')

from atproto import Client, client_utils

from fetch_pointing import derive_cad_des
from send_alerts import TIER_ORDER, apparent_mag, resolve_miss_km, tier_for_mag

SITE_URL = os.environ.get('SITE_URL', 'https://spacesentinel.xyz')
SOURCE_FILES = ['data/asteroids.json', 'data/close-calls-events.json']
POSTED_FILE = 'data/social-posted.json'
TARGET_DAYS = 7
MAX_POSTS_PER_RUN = 3
MAX_POST_CHARS = 300
TIER_PHRASES = {
    'naked_eye':  'bright enough to see with the naked eye',
    'binoculars': 'visible in binoculars',
    'telescope':  'bright enough for a backyard telescope',
}


def display_name(obj):
    return ' '.join(str(obj.get('name', '')).replace('(', ' ').replace(')', ' ').split())


def designation(obj):
    return obj.get('des') or derive_cad_des(obj.get('name', ''))


def load_candidates(now):
    records = []
    for fname in SOURCE_FILES:
        if not os.path.exists(fname):
            print(f'Warning: {fname} not found, skipping.')
            continue
        with open(fname) as f:
            data = json.load(f)
        records.extend(data.get('asteroids') or data.get('events') or [])

    seen = set()
    candidates = []
    for obj in records:
        if obj.get('type', 'asteroid') == 'comet':
            continue
        d = obj.get('date')
        if not d:
            continue
        try:
            obj_date = datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if abs((obj_date - now).days - TARGET_DAYS) > 1:
            continue
        key = (display_name(obj).lower(), d)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(obj)
    candidates.sort(key=lambda o: o['date'])
    return candidates


def qualifies(obj):
    h = obj.get('h') or obj.get('magnitude')
    if h is None:
        return None
    miss_km = resolve_miss_km(obj)
    if miss_km is None:
        return None
    mag = apparent_mag(float(h), miss_km, False)
    if mag is None:
        return None
    tier = tier_for_mag(mag)
    if TIER_ORDER.index(tier['id']) > TIER_ORDER.index('telescope'):
        return None
    return tier


def compose(obj, tier):
    des = designation(obj)
    d = datetime.strptime(obj['date'], '%Y-%m-%d')
    ld = resolve_miss_km(obj) / 384_400
    if ld < 1:
        passage = 'passes closer to Earth than the Moon'
    else:
        passage = f'passes about {ld:.1f}× the Moon’s distance from Earth'
    url = f'{SITE_URL}/#object={urllib.parse.quote(des)}'
    for name in (display_name(obj), des):
        prefix = (
            f'Asteroid {name} {passage} '
            f'{d:%A}, {d:%B} {d.day} — {TIER_PHRASES[tier["id"]]}. '
            f'See if it’s visible from your location: '
        )
        if len(prefix) + len(url) <= MAX_POST_CHARS:
            builder = client_utils.TextBuilder().text(prefix).link(url, url)
            return builder, prefix + url, des
    return None, None, des


def load_posted():
    if not os.path.exists(POSTED_FILE):
        return []
    with open(POSTED_FILE) as f:
        return json.load(f).get('posted', [])


def save_posted(posted):
    with open(POSTED_FILE, 'w') as f:
        json.dump({'posted': posted}, f, indent=2)
        f.write('\n')


def main():
    dry_run = os.environ.get('DRY_RUN', '1').strip().lower() not in ('0', 'false', 'no')
    now = datetime.now(timezone.utc)

    candidates = load_candidates(now)
    print(f'{len(candidates)} asteroids about {TARGET_DAYS} days out.')

    posted = load_posted()
    already = {(p['designation'], p['approach_date']) for p in posted}

    queue = []
    for obj in candidates:
        tier = qualifies(obj)
        if tier is None:
            continue
        if (designation(obj), obj['date']) in already:
            print(f'  Skipping {designation(obj)} ({obj["date"]}) — already posted.')
            continue
        queue.append((obj, tier))
    print(f'{len(queue)} qualifying and unposted.')

    queue = queue[:MAX_POSTS_PER_RUN]
    if not queue:
        print('Nothing to post.')
        return

    client = None
    if not dry_run:
        client = Client()
        client.login(os.environ['BLUESKY_HANDLE'], os.environ['BLUESKY_APP_PASSWORD'])

    for i, (obj, tier) in enumerate(queue):
        builder, text, des = compose(obj, tier)
        if builder is None:
            print(f'  Skipping {des} ({obj["date"]}) — post exceeds {MAX_POST_CHARS} characters.')
            continue
        label = 'DRY RUN' if dry_run else 'POSTING'
        print(f'[{label}] {des} ({obj["date"]}) tier={tier["id"]} chars={len(text)}')
        print(f'  {text}')
        if dry_run:
            continue
        client.send_post(builder)
        posted.append({
            'designation': des,
            'approach_date': obj['date'],
            'posted_at': datetime.now(timezone.utc).isoformat(),
        })
        save_posted(posted)
        if i < len(queue) - 1:
            time.sleep(2)


if __name__ == '__main__':
    main()
