#!/usr/bin/env python3
"""
Space Sentinel — close-call alert sender.
"""
import json
import math
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx   

SUPABASE_URL        = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_SERVICE_KEY = os.environ['SUPABASE_SERVICE_KEY']   
RESEND_API_KEY      = os.environ['RESEND_API_KEY']
FROM_EMAIL          = os.environ.get('FROM_EMAIL', 'Space Sentinel <alerts@spacesentinel.xyz>')
SITE_URL            = os.environ.get('SITE_URL', 'https://spacesentinel.xyz')
LOOK_AHEAD_DAYS     = 30
AU_KM               = 149_597_870.7


VIS_TIERS = [
    {'id': 'naked_eye',  'max_mag':  6,  'label': 'Naked eye'},
    {'id': 'binoculars', 'max_mag': 10,  'label': 'Binoculars'},
    {'id': 'telescope',  'max_mag': 14,  'label': 'Backyard telescope'},
    {'id': 'too_faint',  'max_mag': 999, 'label': 'Professional equipment only'},
]
TIER_ORDER = [t['id'] for t in VIS_TIERS]

def apparent_mag(h: float, miss_km: float, is_comet: bool) -> float:
    if not miss_km or miss_km <= 0:
        return None
    delta_au = miss_km / AU_KM
    r_au     = 1.0
    if is_comet:
        return h + 5 * math.log10(delta_au) + 10 * math.log10(r_au)
    else:
        phase_correction = 0.04 * 45   
        return h + 5 * math.log10(r_au * delta_au) + phase_correction

def tier_for_mag(mag: float) -> dict:
    for t in VIS_TIERS:
        if mag <= t['max_mag']:
            return t
    return VIS_TIERS[-1]

def subscriber_tier_index(threshold_tier: str) -> int:
    try:
        return TIER_ORDER.index(threshold_tier)
    except ValueError:
        return TIER_ORDER.index('telescope')   

def object_meets_threshold(obj: dict, thresholds: dict) -> bool:
    """True if this close-approach object meets the subscriber's thresholds."""
    obj_type  = obj.get('type', 'asteroid')
    is_comet  = obj_type == 'comet'

    if is_comet and not thresholds.get('include_comets', True):
        return False
    if not is_comet and not thresholds.get('include_asteroids', True):
        return False

    h       = obj.get('h') or obj.get('magnitude')
    miss_km = obj.get('miss_km') or (obj.get('miss_ld', 0) * 384_400)
    if obj_type == 'comet':
        miss_km = (obj.get('dist', 0) * AU_KM)

    if h is None:
        return False   
    miss_km_val = obj.get('miss_km') or obj.get('miss_ld', 0) * 384_400
    if obj_type == 'comet':
        miss_km_val = (obj.get('dist', 0) * AU_KM)
    if not miss_km_val or miss_km_val <= 0:
        return False

    mag = apparent_mag(float(h), float(miss_km), is_comet)

    tier_id   = tier_for_mag(mag)['id']
    sub_limit = thresholds.get('visibility_tier', 'telescope')
    if TIER_ORDER.index(tier_id) > subscriber_tier_index(sub_limit):
        return False


    max_ld = thresholds.get('max_dist_ld')
    if max_ld is not None:
        miss_ld = obj.get('miss_ld') or miss_km / 384_400
        if float(miss_ld) > float(max_ld):
            return False


    min_diam = thresholds.get('min_diameter_m')
    if min_diam is not None:
        diam = obj.get('diam_avg') or obj.get('diameter', 0)
        if diam is None or float(diam) < float(min_diam):
            return False

    return True

# ─── Supabase helpers ────────────────────────────────────────────────────────
SUPA_HEADERS = {
    'apikey':        SUPABASE_SERVICE_KEY,
    'Authorization': f'Bearer {SUPABASE_SERVICE_KEY}',
    'Content-Type':  'application/json',
    'Prefer':        'return=representation',
}

def supa_get(path: str, params: dict = None) -> list:
    url = f'{SUPABASE_URL}/rest/v1/{path}'
    if params:
        url += '?' + urllib.parse.urlencode(params)
    r = httpx.get(url, headers=SUPA_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def supa_post(path: str, payload: dict) -> dict:
    r = httpx.post(f'{SUPABASE_URL}/rest/v1/{path}', json=payload, headers=SUPA_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json() if r.text else {}

# ─── Resend helper ───────────────────────────────────────────────────────────
def send_email(to: str, subject: str, html: str) -> bool:
    r = httpx.post(
        'https://api.resend.com/emails',
        json={'from': FROM_EMAIL, 'to': [to], 'subject': subject, 'html': html},
        headers={'Authorization': f'Bearer {RESEND_API_KEY}', 'Content-Type': 'application/json'},
        timeout=30,
    )
    if not r.is_success:
        print(f'  Resend error {r.status_code}: {r.text}')
        return False
    return True

# ─── Email template ──────────────────────────────────────────────────────────
def render_email(subscriber: dict, objects: list) -> tuple[str, str]:
    unsub_token = subscriber['token']
    unsub_url   = f'{SITE_URL}/unsubscribe?token={unsub_token}'
    rows = ''
    for obj in objects:
        h       = obj.get('h') or obj.get('magnitude')
        miss_km = obj.get('miss_km') or obj.get('miss_ld', 0) * 384_400
        if obj.get('type') == 'comet':
            miss_km = obj.get('dist', 0) * AU_KM
        is_comet = obj.get('type') == 'comet'
        mag = apparent_mag(float(h), float(miss_km), is_comet) if h else None
        tier = tier_for_mag(mag)['label'] if mag is not None else 'Unknown'
        mag_str = f'{mag:.1f}' if mag is not None else '—'
        caveat = ' (comet — estimate uncertain)' if is_comet else ''
        dist_str = f"{obj.get('miss_ld', obj.get('dist', '—')):.2f} {'LD' if 'miss_ld' in obj else 'AU'}"
        rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38">
            <strong style="color:#b8d8f8">{obj.get('name','Unknown')}</strong>
            {'&nbsp;<span style="color:#ff5c1a;font-size:11px">⚠ PHA</span>' if obj.get('hazardous') else ''}
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#3a6080">{obj.get('date','—')}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#00c0ff">{dist_str}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#9cc4e8">{tier}{caveat}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#3a6080">mag {mag_str}</td>
        </tr>"""
    count = len(objects)
    subject = f'Space Sentinel: {count} close approach{"es" if count != 1 else ""} in the next 30 days'
    html = f"""
    <!DOCTYPE html><html><head><meta charset="UTF-8"></head>
    <body style="margin:0;padding:0;background:#050a12;font-family:'Courier New',monospace;color:#b8d8f8">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr><td align="center" style="padding:32px 16px">
          <table width="600" cellpadding="0" cellspacing="0" style="background:#070d1a;border:1px solid #0c1e38;max-width:600px">
            <tr><td style="padding:24px 28px;border-bottom:1px solid #0c1e38">
              <div style="font-size:18px;font-weight:700;color:#00c0ff;letter-spacing:4px">◈ SPACE SENTINEL</div>
              <div style="font-size:9px;color:#3a6080;letter-spacing:3px;margin-top:4px">CLOSE-CALL ALERT DIGEST</div>
            </td></tr>
            <tr><td style="padding:20px 28px">
              <p style="margin:0 0 16px;color:#9cc4e8;font-size:12px;line-height:1.8">
                {count} object{"s" if count != 1 else ""} matching your alert threshold
                will make close approaches to Earth in the next 30 days.
              </p>
              <table width="100%" cellpadding="0" cellspacing="0" style="font-size:11px">
                <tr style="background:#0a1422">
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">OBJECT</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">DATE</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">DISTANCE</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">VISIBILITY</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">MAG</th>
                </tr>
                {rows}
              </table>
            </td></tr>
            <tr><td style="padding:16px 28px;border-top:1px solid #0c1e38">
              <p style="margin:0;font-size:9px;color:#162840;letter-spacing:1px;line-height:1.8">
                Magnitude estimates are approximate. Comet brightness is inherently uncertain.<br>
                <a href="{SITE_URL}" style="color:#3a6080">View on Space Sentinel</a> &nbsp;·&nbsp;
                <a href="{unsub_url}" style="color:#3a6080">Unsubscribe</a>
              </p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>"""
    return subject, html

# ─── Main ────────────────────────────────────────────────────────────────────
def main():
    now       = datetime.now(timezone.utc)
    cutoff    = now + timedelta(days=LOOK_AHEAD_DAYS)
    today_str = now.date().isoformat()

    # 1. Load cached close-approach data (written by the other GitHub Actions)
    all_objects = []
    for fname in ['data/asteroids.json', 'data/close-calls-events.json']:
        if not os.path.exists(fname):
            print(f'Warning: {fname} not found, skipping.')
            continue
        with open(fname) as f:
            data = json.load(f)
        records = data.get('asteroids') or data.get('events') or []
        all_objects.extend(records)

    # 2. Filter to the look-ahead window
    upcoming = []
    for obj in all_objects:
        d = obj.get('date')
        if not d:
            continue
        try:
            obj_date = datetime.strptime(d, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now <= obj_date <= cutoff:
            upcoming.append(obj)

    print(f'{len(upcoming)} objects in next {LOOK_AHEAD_DAYS} days.')

    if not upcoming:
        print('Nothing to alert on — exiting.')
        return

    # 3. Load verified active subscribers
    subscribers = supa_get('subscribers', {
        'select':       'id,email,token,thresholds',
        'verified':     'eq.true',
        'unsubscribed_at': 'is.null',
    })
    print(f'{len(subscribers)} active subscribers.')

    sent_total = 0
    for sub in subscribers:
        sid       = sub['id']
        thresholds = sub.get('thresholds') or {}

        # 4. Filter objects for this subscriber
        qualifying = [obj for obj in upcoming if object_meets_threshold(obj, thresholds)]
        if not qualifying:
            continue

        # 5. Check alert_log — skip objects already sent to this subscriber
        already_sent = set()
        logs = supa_get('alert_log', {
            'select':        'object_des,approach_date',
            'subscriber_id': f'eq.{sid}',
            'alert_type':    'eq.close_approach',
        })
        for row in logs:
            already_sent.add((row['object_des'], row['approach_date']))

        new_objects = [
            obj for obj in qualifying
            if (obj.get('des') or obj.get('id') or obj.get('name'), obj.get('date')) not in already_sent
        ]
        if not new_objects:
            continue

        # 6. Send digest email
        subject, html = render_email(sub, new_objects)
        success = send_email(sub['email'], subject, html)
        if success:
            sent_total += 1
            # 7. Log to alert_log (prevents future duplicates)
            for obj in new_objects:
                des = obj.get('des') or obj.get('id') or obj.get('name', 'unknown')
                try:
                    supa_post('alert_log', {
                        'subscriber_id': sid,
                        'object_des':    des,
                        'approach_date': obj.get('date'),
                        'alert_type':    'close_approach',
                        'email_status':  'sent',
                    })
                except Exception as e:
                    print(f'  Warning: failed to log alert for {des}: {e}')
            print(f'  Sent digest to {sub["email"]}: {len(new_objects)} object(s)')
        else:
            print(f'  Failed to send to {sub["email"]}')
        time.sleep(0.25)   # rate limit headroom

    print(f'Done. Sent {sent_total} digest email(s).')

if __name__ == '__main__':
    main()
