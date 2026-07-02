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
SUPABASE_SERVICE_KEY = os.environ['SUPABASE_SERVICE_KEY']   # bypasses RLS
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
        return TIER_ORDER.index('telescope')   # sensible default

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
        return False   # can't classify without magnitude

    try:
        miss_km_val = float(miss_km)
    except (TypeError, ValueError):
        return False
    if not miss_km_val or miss_km_val <= 0:
        return False

    mag = apparent_mag(float(h), miss_km_val, is_comet)
    if mag is None:
        return False

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

def dist_plain_english(miss_ld) -> str:
    """One-line plain-English context for a lunar-distance value."""
    try:
        ld = float(miss_ld)
    except (TypeError, ValueError):
        return ''
    if ld < 1:    return 'closer than the Moon'
    if ld < 3:    return f'about {ld:.0f}\u00d7 the Moon\u2019s distance'
    if ld < 20:   return f'roughly {ld:.0f}\u00d7 the Moon\u2019s distance'
    return f'{ld:.0f} lunar distances \u2014 a comfortable miss'

def render_email(subscriber: dict, objects: list, reminder_label: str = '', showers: list = None) -> tuple[str, str]:
    unsub_token = subscriber['token']
    manage_url  = f'{SITE_URL}/preferences.html?token={unsub_token}'
    showers = showers or []
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
        if 'miss_ld' in obj:
            dist_str = f"{obj['miss_ld']:.2f} LD"
            dist_context = dist_plain_english(obj['miss_ld'])
        else:
            dist_str = f"{obj.get('dist', 0):.2f} AU"
            dist_context = 'a distant pass, far beyond the Moon'
        rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38">
            <strong style="color:#b8d8f8">{obj.get('name','Unknown')}</strong>
            {'&nbsp;<span style="color:#ff5c1a;font-size:11px">⚠ PHA</span>' if obj.get('hazardous') else ''}
          </td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#3a6080;white-space:nowrap">{obj.get('date','—')}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#00c0ff;white-space:nowrap">{dist_str}<br><span style="color:#3a6080;font-size:9px">{dist_context}</span></td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#9cc4e8">{tier}{caveat}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#3a6080;white-space:nowrap">mag {mag_str}</td>
        </tr>"""
    shower_rows = ''
    for sh in showers:
        hemi_label = {'N':'Northern hemisphere','S':'Southern hemisphere','B':'Both hemispheres'}[sh['hemi']]
        shower_rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38"><strong style="color:#00e890">☄ {sh['name']}</strong></td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#3a6080;white-space:nowrap">{sh['peak_date']}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#00c0ff;white-space:nowrap">~{sh['zhr']} meteors/hr<br><span style="color:#3a6080;font-size:9px">under perfect conditions</span></td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#9cc4e8">Naked eye · {hemi_label}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #0c1e38;color:#3a6080;white-space:nowrap">no gear needed</td>
        </tr>"""

    obj_count = len(objects)
    sh_count  = len(showers)
    total     = obj_count + sh_count
    parts = []
    if obj_count: parts.append(f'{obj_count} close approach{"es" if obj_count != 1 else ""}')
    if sh_count:  parts.append(f'{sh_count} meteor shower{"s" if sh_count != 1 else ""}')
    header_line = ' and '.join(parts)

    if reminder_label:
        subject = f'Space Sentinel {reminder_label}: {header_line} coming up'
        intro   = f'{header_line} matching your preferences will happen in the coming days.'
    else:
        subject = f'Space Sentinel: {header_line} in the next 30 days'
        intro   = f'{header_line} matching your preferences are coming up.'

    table_html = ''
    if rows or shower_rows:
        table_html = f"""
              <table width="100%" cellpadding="0" cellspacing="0" style="font-size:11px">
                <tr style="background:#0a1422">
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">EVENT</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">DATE</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">DETAIL</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">VISIBILITY</th>
                  <th style="padding:8px 14px;text-align:left;color:#3a6080;letter-spacing:1.5px">NOTES</th>
                </tr>
                {rows}
                {shower_rows}
              </table>"""

    html = f"""
    <!DOCTYPE html><html><head><meta charset="UTF-8"></head>
    <body style="margin:0;padding:0;background:#050a12;font-family:'Courier New',monospace;color:#b8d8f8">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr><td align="center" style="padding:32px 16px">
          <table width="600" cellpadding="0" cellspacing="0" style="background:#070d1a;border:1px solid #0c1e38;max-width:600px">
            <tr><td style="padding:24px 28px;border-bottom:1px solid #0c1e38">
              <div style="font-size:18px;font-weight:700;color:#00c0ff;letter-spacing:4px">◈ SPACE SENTINEL</div>
              <div style="font-size:9px;color:#3a6080;letter-spacing:3px;margin-top:4px">{reminder_label.upper() if reminder_label else 'ALERT DIGEST'}</div>
            </td></tr>
            <tr><td style="padding:20px 28px">
              <p style="margin:0 0 16px;color:#9cc4e8;font-size:12px;line-height:1.8">{intro}</p>
              {table_html}
            </td></tr>
            <tr><td style="padding:16px 28px;border-top:1px solid #0c1e38">
              <p style="margin:0;font-size:9px;color:#162840;letter-spacing:1px;line-height:1.8">
                Magnitude estimates are approximate. Comet brightness is inherently uncertain.<br>
                <a href="{SITE_URL}" style="color:#3a6080">View on Space Sentinel</a> &nbsp;·&nbsp;
                <a href="{manage_url}" style="color:#3a6080">Manage preferences or unsubscribe</a>
              </p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body></html>"""
    return subject, html

def main():
    now       = datetime.now(timezone.utc)
    cutoff    = now + timedelta(days=LOOK_AHEAD_DAYS)
    today_str = now.date().isoformat()

    all_objects = []
    for fname in ['data/asteroids.json', 'data/close-calls-events.json']:
        if not os.path.exists(fname):
            print(f'Warning: {fname} not found, skipping.')
            continue
        with open(fname) as f:
            data = json.load(f)
        records = data.get('asteroids') or data.get('events') or []
        all_objects.extend(records)

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

    def obj_key(obj):
        return (obj.get('des') or obj.get('id') or obj.get('name', 'unknown'), obj.get('date'))

    def days_until(obj):
        try:
            d = datetime.strptime(obj['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
            return (d - now).days
        except (KeyError, ValueError):
            return None

    if not os.path.exists('data/meteor-showers.json'):
        print('Warning: data/meteor-showers.json not found, skipping meteor alerts.')
        METEOR_SHOWERS = []
    else:
        with open('data/meteor-showers.json') as f:
            METEOR_SHOWERS = json.load(f)['showers']

    def next_shower_peak(sh):
        y = now.year
        d = datetime(y, sh['peakMonth'], sh['peakDay'], tzinfo=timezone.utc)
        if (d - now).days < -1:
            d = datetime(y + 1, sh['peakMonth'], sh['peakDay'], tzinfo=timezone.utc)
        return d

    def shower_matches_prefs(sh, thresholds):
        if not thresholds.get('include_meteors', True):
            return False
        sub_hemi = thresholds.get('meteor_hemisphere', 'both')
        if sub_hemi == 'both' or sh['hemi'] == 'B':
            return True
        return sh['hemi'] == sub_hemi

    upcoming_showers = []
    for sh in METEOR_SHOWERS:
        peak = next_shower_peak(sh)
        d_until = (peak - now).days
        if 0 <= d_until <= LOOK_AHEAD_DAYS:
            upcoming_showers.append({**sh, 'peak_date': peak.date().isoformat(), 'days_until': d_until})

    passes = [
        ('close_approach', None,  '',                None),
        ('30_day_warning', 30,    '30-day heads-up', 'reminders_30d'),
        ('7_day_warning',  7,     '7-day reminder',  'reminders_7d'),
    ]

    sent_total = 0
    for sub in subscribers:
        sid        = sub['id']
        thresholds = sub.get('thresholds') or {}
        qualifying = [obj for obj in upcoming if object_meets_threshold(obj, thresholds)]

        for alert_type, days_target, label, reminder_key in passes:
            if reminder_key is not None and not thresholds.get(reminder_key, True):
                continue

            if days_target is None:
                obj_batch = qualifying
            else:
                obj_batch = [o for o in qualifying if (du := days_until(o)) is not None and abs(du - days_target) <= 1]

            if days_target is None:
                shower_batch = []
            else:
                shower_batch = [
                    sh for sh in upcoming_showers
                    if shower_matches_prefs(sh, thresholds) and abs(sh['days_until'] - days_target) <= 1
                ]

            if not obj_batch and not shower_batch:
                continue

            already = set()
            logs = supa_get('alert_log', {
                'select':        'object_des,approach_date',
                'subscriber_id': f'eq.{sid}',
                'alert_type':    f'eq.{alert_type}',
            })
            for row in logs:
                already.add((row['object_des'], row['approach_date']))

            new_objects = [o for o in obj_batch if obj_key(o) not in already]
            new_showers = [sh for sh in shower_batch if (sh['id'], sh['peak_date']) not in already]
            if not new_objects and not new_showers:
                continue

            subject, html = render_email(sub, new_objects, reminder_label=label, showers=new_showers)
            if send_email(sub['email'], subject, html):
                sent_total += 1
                for obj in new_objects:
                    try:
                        supa_post('alert_log', {
                            'subscriber_id': sid,
                            'object_des':    obj_key(obj)[0],
                            'approach_date': obj.get('date'),
                            'alert_type':    alert_type,
                            'email_status':  'sent',
                        })
                    except Exception as e:
                        print(f'  Warning: failed to log {alert_type} for {obj_key(obj)[0]}: {e}')
                for sh in new_showers:
                    try:
                        supa_post('alert_log', {
                            'subscriber_id': sid,
                            'object_des':    sh['id'],
                            'approach_date': sh['peak_date'],
                            'alert_type':    alert_type,
                            'email_status':  'sent',
                        })
                    except Exception as e:
                        print(f'  Warning: failed to log shower {sh["id"]}: {e}')
                print(f'  Sent {alert_type} to {sub["email"]}: {len(new_objects)} object(s), {len(new_showers)} shower(s)')
            else:
                print(f'  Failed {alert_type} to {sub["email"]}')
            time.sleep(0.25)   

    print(f'Done. Sent {sent_total} email(s).')

if __name__ == '__main__':
    main()
