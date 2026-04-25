#!/usr/bin/env python3
"""
pvoutput_historical_upload.py
─────────────────────────────────────────────────────────────────────────────
Uploads historical Sigenergy data from Home Assistant to PVOutput.

REQUIREMENTS
  pip install requests

USAGE
  1. Fill in the CONFIG section below.
  2. Run from the HA terminal (Terminal & SSH addon) or any machine
     that can reach your HA instance:

       python3 pvoutput_historical_upload.py

  3. Check the output — it prints a summary for each day uploaded.
     Re-running is safe: PVOutput will overwrite existing entries.

HOW IT WORKS
  - Calls the HA REST API to fetch 5-minute history for your solar sensors
  - Resamples to 5-minute intervals (fills gaps with nearest value)
  - Pushes to PVOutput in batches of 30 via the batch status API
─────────────────────────────────────────────────────────────────────────────
"""

import requests
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from time import sleep

# =============================================================================
# CONFIG — fill these in
# =============================================================================

HA_URL       = "http://localhost:8123"   # change if running from another machine
HA_TOKEN     = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIzZmRlOWUxNWY3NmI0YjRkYTVkOGMwMTAxNGY2M2JiYyIsImlhdCI6MTc3NzA3NjI5OSwiZXhwIjoyMDkyNDM2Mjk5fQ.HV9yDRLqJUOLWXb-UsmQumrJ0p90pJZPSraALGr2Zio"  # HA > Profile > Long-Lived Access Tokens

PVOUTPUT_API_KEY   = "ff98e9a781c1215b25c6203d8c3748e85b6e927a"
PVOUTPUT_SYSTEM_ID = "112497"

# Date range to upload (inclusive). Format: "YYYY-MM-DD"
START_DATE = "2026-04-20"
END_DATE   = "2026-04-24"   # yesterday — today is handled by the live automation

# Interval between uploaded data points in minutes (5 is PVOutput standard)
INTERVAL_MINUTES = 5

# Seconds to wait between batch API calls (be polite to PVOutput)
RATE_LIMIT_SLEEP = 2

# =============================================================================
# SENSORS — no need to change these if using the standard Sigenergy integration
# =============================================================================

SENSORS = {
    "pv_power_kw":          "sensor.sigen_plant_pv_power",
    "pv_energy_kwh":        "sensor.sigen_plant_daily_pv_energy",
    "load_power_kw":        "sensor.sigen_plant_consumed_power",
    "load_energy_kwh":      "sensor.sigen_plant_daily_load_consumption",
}

# =============================================================================
# HELPERS
# =============================================================================

HA_HEADERS = {
    "Authorization": f"Bearer {HA_TOKEN}",
    "Content-Type": "application/json",
}

PVO_HEADERS = {
    "X-Pvoutput-Apikey": PVOUTPUT_API_KEY,
    "X-Pvoutput-SystemId": PVOUTPUT_SYSTEM_ID,
    "Content-Type": "application/x-www-form-urlencoded",
}


def fetch_history(entity_id: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch state history for one entity from the HA REST API."""
    url = f"{HA_URL}/api/history/period/{start.isoformat()}"
    params = {
        "end_time": end.isoformat(),
        "filter_entity_id": entity_id,
        "minimal_response": "true",
        "no_attributes": "true",
    }
    resp = requests.get(url, headers=HA_HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"  ERROR fetching {entity_id}: HTTP {resp.status_code}")
        return []
    data = resp.json()
    return data[0] if data else []


def parse_states(raw_states: list[dict]) -> dict[datetime, float]:
    """Convert raw HA state list into {datetime: float} mapping."""
    result = {}
    for entry in raw_states:
        try:
            val = float(entry["state"])
            ts_str = entry.get("last_changed") or entry.get("last_updated")
            # HA returns UTC ISO strings
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            result[ts] = val
        except (ValueError, KeyError):
            pass
    return result


def resample(states: dict[datetime, float], slots: list[datetime]) -> dict[datetime, float | None]:
    """For each 5-min slot, find the nearest recorded value (forward fill)."""
    if not states:
        return {s: None for s in slots}

    sorted_ts = sorted(states.keys())
    result = {}
    idx = 0

    for slot in slots:
        # Advance pointer to the last reading at or before this slot
        while idx + 1 < len(sorted_ts) and sorted_ts[idx + 1] <= slot:
            idx += 1
        if sorted_ts[idx] <= slot:
            result[slot] = states[sorted_ts[idx]]
        else:
            result[slot] = None

    return result


def push_batch(data_points: list[str]) -> bool:
    """Push up to 30 data points to PVOutput batch API. Returns True on success."""
    payload = "data=" + ";".join(data_points)
    resp = requests.post(
        "https://pvoutput.org/service/r2/addbatchstatus.jsp",
        headers=PVO_HEADERS,
        data=payload,
        timeout=30,
    )
    if resp.status_code == 200:
        return True
    print(f"  PVOutput error {resp.status_code}: {resp.text.strip()}")
    return False


def upload_day(date: datetime.date) -> int:
    """Fetch and upload one full day. Returns number of points pushed."""

    day_start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=timezone.utc)
    day_end   = day_start + timedelta(days=1) - timedelta(seconds=1)

    # Build 5-minute time slots for the day
    slots = []
    slot = day_start
    while slot <= day_end:
        slots.append(slot)
        slot += timedelta(minutes=INTERVAL_MINUTES)

    # Fetch all sensors
    raw = {}
    for key, entity_id in SENSORS.items():
        raw_states = fetch_history(entity_id, day_start, day_end)
        raw[key] = parse_states(raw_states)

    # Resample each sensor to our 5-min slots
    sampled = {key: resample(states, slots) for key, states in raw.items()}

    # Build PVOutput data strings
    # Format per point: YYYYMMDD,HH:MM,v1,v2,v3,v4
    #   v1 = energy generated today (Wh) — cumulative
    #   v2 = power generating now (W)
    #   v3 = energy consumed today (Wh) — cumulative
    #   v4 = power consuming now (W)
    data_points = []

    for slot in slots:
        pv_energy  = sampled["pv_energy_kwh"][slot]
        pv_power   = sampled["pv_power_kw"][slot]
        ld_energy  = sampled["load_energy_kwh"][slot]
        ld_power   = sampled["load_power_kw"][slot]

        # Skip slots with no data at all
        if all(v is None for v in [pv_energy, pv_power, ld_energy, ld_power]):
            continue

        # PVOutput wants integers in W / Wh; use empty string for missing fields
        v1 = str(int(round(pv_energy * 1000))) if pv_energy is not None else ""
        v2 = str(int(round(pv_power  * 1000))) if pv_power  is not None else ""
        v3 = str(int(round(ld_energy * 1000))) if ld_energy is not None else ""
        v4 = str(int(round(ld_power  * 1000))) if ld_power  is not None else ""

        # Local time string for PVOutput (it stores in your system timezone)
        # PVOutput expects local time — convert from UTC using local offset
        local_slot = slot.astimezone()
        date_str = local_slot.strftime("%Y%m%d")
        time_str = local_slot.strftime("%H:%M")

        data_points.append(f"{date_str},{time_str},{v1},{v2},{v3},{v4}")

    if not data_points:
        print(f"  {date}: no data found, skipping.")
        return 0

    # Push in batches of 30 (PVOutput limit)
    total_pushed = 0
    for i in range(0, len(data_points), 30):
        batch = data_points[i:i + 30]
        ok = push_batch(batch)
        if ok:
            total_pushed += len(batch)
        sleep(RATE_LIMIT_SLEEP)

    return total_pushed


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 60)
    print("  Sigenergy → PVOutput Historical Upload")
    print("=" * 60)

    # Validate config
    if "YOUR_" in HA_TOKEN or "YOUR_" in PVOUTPUT_API_KEY:
        print("\nERROR: Please fill in your HA token and PVOutput credentials in the CONFIG section.")
        sys.exit(1)

    start = datetime.strptime(START_DATE, "%Y-%m-%d").date()
    end   = datetime.strptime(END_DATE,   "%Y-%m-%d").date()

    print(f"\nUploading {START_DATE} to {END_DATE} in {INTERVAL_MINUTES}-minute intervals.\n")

    total_days   = 0
    total_points = 0
    current = start

    while current <= end:
        print(f"Processing {current} ...", end=" ", flush=True)
        pushed = upload_day(current)
        print(f"{pushed} points uploaded.")
        total_points += pushed
        total_days   += 1
        current += timedelta(days=1)

    print(f"\nDone. {total_points} data points across {total_days} days uploaded to PVOutput.")


if __name__ == "__main__":
    main()
