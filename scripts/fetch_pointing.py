#!/usr/bin/env python3
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
DATA_DIR = "data"
EVENTS_FILE = os.path.join(DATA_DIR, "close-calls-events.json")
ASTEROIDS_FILE = os.path.join(DATA_DIR, "asteroids.json")
POINTING_FILE = os.path.join(DATA_DIR, "pointing.json")
WINDOW_DAYS = 60
USER_AGENT = "SpaceSentinel/1.0 (personal project; contact via GitHub repo)"
REQUEST_TIMEOUT = 120


def derive_cad_des(name):
    cleaned = re.sub(r"[()]", "", str(name)).strip()
    if re.match(r"^\d{4} [A-Z]{2}\d*$", cleaned):
        return cleaned
    m = re.match(r"^(\d+) ", cleaned)
    return m.group(1) if m else cleaned


def fetch_text(params):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{HORIZONS_URL}?{qs}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def jd_from_datetime(dt):
    return dt.timestamp() / 86400.0 + 2440587.5


def fetch_pointing(des, kind, approach_date):
    day = datetime.strptime(approach_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start = day - timedelta(hours=12)
    stop = day + timedelta(hours=48)
    if kind == "comet":
        command = f"DES={des}; CAP;"
    elif des.isdigit():
        command = f"{des};"
    else:
        command = f"DES={des};"
    params = {
        "format": "text",
        "COMMAND": f"'{command}'",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": "'500'",
        "START_TIME": f"'{start.strftime('%Y-%m-%d %H:%M')}'",
        "STOP_TIME": f"'{stop.strftime('%Y-%m-%d %H:%M')}'",
        "STEP_SIZE": "'1 h'",
        "QUANTITIES": "'2'",
        "ANG_FORMAT": "DEG",
        "CSV_FORMAT": "YES",
    }
    text = fetch_text(params)
    if "$$SOE" not in text:
        return None
    body = text.split("$$SOE")[1].split("$$EOE")[0]
    ra, dec = [], []
    for line in body.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            ra.append(round(float(parts[3]), 5))
            dec.append(round(float(parts[4]), 5))
        except ValueError:
            continue
    if len(ra) < 55:
        return None
    return {
        "date": approach_date,
        "jd0": round(jd_from_datetime(start), 6),
        "stepHours": 1,
        "ra": ra,
        "dec": dec,
    }


def main():
    with open(EVENTS_FILE) as f:
        events = json.load(f)["events"]
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=WINDOW_DAYS)
    upcoming = {}
    for e in events:
        d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        if today <= d <= horizon:
            if e["des"] not in upcoming or e["date"] < upcoming[e["des"]]["date"]:
                upcoming[e["des"]] = e
    if os.path.exists(ASTEROIDS_FILE):
        with open(ASTEROIDS_FILE) as f:
            asteroids = json.load(f)["asteroids"]
        for r in asteroids:
            des = derive_cad_des(r["name"])
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
            if today <= d <= horizon:
                if des not in upcoming or r["date"] < upcoming[des]["date"]:
                    upcoming[des] = {"des": des, "type": "asteroid", "date": r["date"]}
    print(f"{len(upcoming)} objects with approaches in the next {WINDOW_DAYS} days")

    objects = {}
    for des, e in sorted(upcoming.items(), key=lambda kv: kv[1]["date"]):
        entry = None
        try:
            entry = fetch_pointing(des, e["type"], e["date"])
        except Exception as exc:
            print(f"  ! {des}: {exc}")
        if entry:
            objects[des] = entry
            print(f"  {des} ({e['date']}): {len(entry['ra'])} samples")
        else:
            print(f"  ! {des} ({e['date']}): no usable ephemeris")
        time.sleep(1)

    with open(POINTING_FILE, "w") as f:
        json.dump({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "days": WINDOW_DAYS,
            "objects": objects,
        }, f, separators=(",", ":"))
    print(f"Wrote {POINTING_FILE} ({len(objects)} objects)")


if __name__ == "__main__":
    main()
