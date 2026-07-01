#!/usr/bin/env python3
"""
Space Sentinel — close-call data fetcher.

"""
import json
import math
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

CAD_URL = "https://ssd-api.jpl.nasa.gov/cad.api"
SBDB_URL = "https://ssd-api.jpl.nasa.gov/sbdb.api"
DATA_DIR = "data"
EVENTS_FILE = os.path.join(DATA_DIR, "close-calls-events.json")
ELEMENTS_FILE = os.path.join(DATA_DIR, "orbital-elements.json")

DATE_MIN = "1900-01-01"
DATE_MAX = "2200-01-01" 
USER_AGENT = "SpaceSentinel/1.0 (personal project; contact via GitHub repo)"
REQUEST_TIMEOUT = 120
MAX_NEW_ELEMENTS_PER_RUN = 1000 


def fetch_json(url, params):
    qs = urllib.parse.urlencode(params)
    full_url = f"{url}?{qs}"
    req = urllib.request.Request(full_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def process_cad(data, kind):
    if not data or not data.get("count") or not data.get("data"):
        return []
    fields = data["fields"]
    idx = {name: i for i, name in enumerate(fields)}
    out = []
    for row in data["data"]:
        des = row[idx["des"]]
        fullname = (row[idx["fullname"]] or des).strip() if "fullname" in idx else des
        dist = float(row[idx["dist"]])
        cd = row[idx["cd"]] or ""
        try:
            date_only = datetime.strptime(cd.strip(), "%Y-%b-%d %H:%M").strftime("%Y-%m-%d")
        except ValueError:
            date_only = cd.split(" ")[0] 
        h_raw = row[idx["h"]] if "h" in idx else None
        h = float(h_raw) if h_raw not in (None, "") else None
        diam_raw = row[idx["diameter"]] if "diameter" in idx else None
        diameter = float(diam_raw) if diam_raw not in (None, "") else None
        v_inf_raw = row[idx["v_inf"]] if "v_inf" in idx else None
        out.append({
            "des": des,
            "name": fullname,
            "type": kind,
            "date": date_only,
            "jd": float(row[idx["jd"]]),
            "dist": dist,
            "v_rel": float(row[idx["v_rel"]]) if row[idx["v_rel"]] not in (None, "") else None,
            "v_inf": float(v_inf_raw) if v_inf_raw not in (None, "") else None,
            "h": h,
            "diameter": diameter,

            "highlighted": (h is not None and h <= 22) if kind == "asteroid" else (dist < 0.1),
        })
    return out


def fetch_cad(kind):
    params = {
        "date-min": DATE_MIN,
        "date-max": DATE_MAX,
        "dist-max": "0.05" if kind == "asteroid" else "2",
        "neo": "true" if kind == "asteroid" else "false",
        "comet": "false" if kind == "asteroid" else "true",
        "fullname": "true",
        "sort": "date",
    }
    if kind == "comet":
        params["diameter"] = "true"
    data = fetch_json(CAD_URL, params)
    return process_cad(data, kind)


def fetch_elements(des):
    try:
        data = fetch_json(SBDB_URL, {"des": des})
        orbit = data.get("orbit")
        if not orbit:
            return None
        el = {e["name"]: e["value"] for e in orbit.get("elements", [])}
        required = ["a", "e", "i", "om", "w", "ma"]
        if not all(k in el for k in required):
            return None
        e_val = float(el["e"])
        if e_val >= 1.0:

            return None
        a_val = float(el["a"])
        epoch = float(orbit.get("epoch"))
        period_days = float(el["per"]) if "per" in el and el["per"] not in (None, "") \
            else (365.25636 * a_val ** 1.5)
        m0 = float(el["ma"])

        tp = epoch - (m0 / 360.0) * period_days
        result = {
            "a": a_val, "e": e_val, "i": float(el["i"]),
            "om": float(el["om"]), "w": float(el["w"]),
            "Tp": tp, "period": period_days,
        }

        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in result.values()):
            return None
        return result
    except Exception as exc:
        print(f"  ! failed to fetch elements for {des}: {exc}")
        return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)

    print(f"Fetching asteroid close approaches ({DATE_MIN} to {DATE_MAX}, <=0.05 AU)...")
    asteroids = fetch_cad("asteroid")
    print(f"  {len(asteroids)} records")
    time.sleep(2)

    print(f"Fetching comet close approaches ({DATE_MIN} to {DATE_MAX}, <=2 AU)...")
    comets = fetch_cad("comet")
    print(f"  {len(comets)} records")

    events = asteroids + comets
    events.sort(key=lambda r: r["jd"])

    with open(EVENTS_FILE, "w") as f:
        json.dump({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "window": {"min": DATE_MIN, "max": DATE_MAX},
            "count": len(events),
            "events": events,
        }, f, separators=(",", ":"))
    print(f"Wrote {EVENTS_FILE} ({len(events)} events)")

    existing = {}
    if os.path.exists(ELEMENTS_FILE):
        with open(ELEMENTS_FILE) as f:
            existing = json.load(f).get("elements", {})

    all_des = sorted(set(r["des"] for r in events))
    missing = [d for d in all_des if d not in existing]
    print(f"{len(all_des)} distinct objects, {len(missing)} missing orbital elements")

    fetched_this_run = 0
    for des in missing:
        if fetched_this_run >= MAX_NEW_ELEMENTS_PER_RUN:
            remaining = len(missing) - fetched_this_run
            print(f"Hit per-run cap ({MAX_NEW_ELEMENTS_PER_RUN}); {remaining} remaining, picked up in future runs.")
            break
        el = fetch_elements(des)
        if el:
            existing[des] = el
        fetched_this_run += 1
        time.sleep(1)

    with open(ELEMENTS_FILE, "w") as f:
        json.dump({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(existing),
            "elements": existing,
        }, f, separators=(",", ":"))
    print(f"Wrote {ELEMENTS_FILE} ({len(existing)} objects with cached elements)")


if __name__ == "__main__":
    main()
