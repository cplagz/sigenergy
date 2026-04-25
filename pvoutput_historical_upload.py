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
INTERVAL_MINUTES = 5    # must match your PVOutput system interval setting
RATE_LIMIT_SLEEP = 2    # seconds between batch API calls
 
# =============================================================================
# SENSOR MAP
# Standard fields
#   v1 = PV energy today (kWh -> Wh)
#   v2 = PV power now (kW -> W)
#   v3 = Load energy today (kWh -> Wh)
#   v4 = Load power now (kW -> W)
# Extended fields (Donation Mode required)
#   v7  = Battery SOC (%)
#   v8  = Battery power (kW -> W, positive=charging, negative=discharging)
#   v9  = Grid import power (kW -> W)
#   v10 = Grid export power (kW -> W)
#   v11 = Battery temperature (C)
#   v12 = Inverter temperature (C)
# =============================================================================
 
SENSORS = {
    "pv_energy_kwh":    "sensor.sigen_plant_daily_pv_energy",
    "pv_power_kw":      "sensor.sigen_plant_pv_power",
    "load_energy_kwh":  "sensor.sigen_plant_daily_load_consumption",
    "load_power_kw":    "sensor.sigen_plant_consumed_power",
    "battery_soc":      "sensor.sigen_plant_battery_state_of_charge",
    "battery_power_kw": "sensor.sigen_plant_battery_power",
    "grid_import_kw":   "sensor.sigen_plant_grid_import_power",
    "grid_export_kw":   "sensor.sigen_plant_grid_export_power",
    "battery_temp_c":   "sensor.sigen_inverter_battery_average_cell_temperature",
    "inverter_temp_c":  "sensor.sigen_inverter_pcs_internal_temperature",
}
 
# Which sensors need kW -> W conversion (multiply by 1000)
KW_TO_W = {"pv_energy_kwh", "pv_power_kw", "load_energy_kwh", "load_power_kw",
            "battery_power_kw", "grid_import_kw", "grid_export_kw"}
 
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
 
 
def fetch_history(entity_id, start, end):
    url = f"{HA_URL}/api/history/period/{start.isoformat()}"
    params = {
        "end_time": end.isoformat(),
        "filter_entity_id": entity_id,
        "minimal_response": "true",
        "no_attributes": "true",
    }
    resp = requests.get(url, headers=HA_HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"    WARNING: Could not fetch {entity_id} (HTTP {resp.status_code})")
        return []
    data = resp.json()
    return data[0] if data else []
 
 
def parse_states(raw_states):
    result = {}
    for entry in raw_states:
        try:
            val = float(entry["state"])
            ts_str = entry.get("last_changed") or entry.get("last_updated")
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            result[ts] = val
        except (ValueError, KeyError, TypeError):
            pass
    return result
 
 
def forward_fill(states, slots):
    """For each slot, return the most recent reading at or before that time."""
    if not states:
        return {s: None for s in slots}
    sorted_ts = sorted(states.keys())
    result = {}
    idx = 0
    for slot in slots:
        while idx + 1 < len(sorted_ts) and sorted_ts[idx + 1] <= slot:
            idx += 1
        result[slot] = states[sorted_ts[idx]] if sorted_ts[idx] <= slot else None
    return result
 
 
def to_w(key, value):
    """Convert kW to W for relevant sensors, round to int."""
    if value is None:
        return None
    if key in KW_TO_W:
        return int(round(value * 1000))
    return round(value, 1)
 
 
def push_batch(data_points):
    payload = "data=" + ";".join(data_points)
    resp = requests.post(
        "https://pvoutput.org/service/r2/addbatchstatus.jsp",
        headers=PVO_HEADERS,
        data=payload,
        timeout=30,
    )
    if resp.status_code == 200:
        return True
    print(f"    PVOutput error {resp.status_code}: {resp.text.strip()}")
    return False
 
 
def upload_day(date):
    day_start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=timezone.utc)
    day_end   = day_start + timedelta(days=1) - timedelta(seconds=1)
 
    slots = []
    slot = day_start
    while slot <= day_end:
        slots.append(slot)
        slot += timedelta(minutes=INTERVAL_MINUTES)
 
    # Fetch and resample all sensors
    sampled = {}
    for key, entity_id in SENSORS.items():
        raw = fetch_history(entity_id, day_start, day_end)
        states = parse_states(raw)
        sampled[key] = forward_fill(states, slots)
 
    data_points = []
    for slot in slots:
        vals = {key: to_w(key, sampled[key][slot]) for key in SENSORS}
 
        # Skip slots where we have no generation data at all
        if vals["pv_energy_kwh"] is None and vals["pv_power_kw"] is None:
            continue
 
        local_slot = slot.astimezone()
        date_str = local_slot.strftime("%Y%m%d")
        time_str = local_slot.strftime("%H:%M")
 
        def f(v):
            return str(v) if v is not None else ""
 
        # Standard fields
        v1  = f(vals["pv_energy_kwh"])
        v2  = f(vals["pv_power_kw"])
        v3  = f(vals["load_energy_kwh"])
        v4  = f(vals["load_power_kw"])
        # Extended fields
        v7  = f(vals["battery_soc"])
        v8  = f(vals["battery_power_kw"])
        v9  = f(vals["grid_import_kw"])
        v10 = f(vals["grid_export_kw"])
        v11 = f(vals["battery_temp_c"])
        v12 = f(vals["inverter_temp_c"])
 
        # Format: date,time,v1,v2,v3,v4,,,v7,v8,v9,v10,v11,v12
        # v5 and v6 are efficiency and temperature (unused), so leave blank
        data_points.append(f"{date_str},{time_str},{v1},{v2},{v3},{v4},,,{v7},{v8},{v9},{v10},{v11},{v12}")
 
    if not data_points:
        print(f"  {date}: no data found, skipping.")
        return 0
 
    total_pushed = 0
    for i in range(0, len(data_points), 30):
        batch = data_points[i:i + 30]
        if push_batch(batch):
            total_pushed += len(batch)
        sleep(RATE_LIMIT_SLEEP)
 
    return total_pushed
 
 
# =============================================================================
# MAIN
# =============================================================================
 
def main():
    print("=" * 60)
    print("  Sigenergy -> PVOutput Historical Upload (with extended data)")
    print("=" * 60)
 
    if "YOUR_" in HA_TOKEN or "YOUR_" in PVOUTPUT_API_KEY:
        print("\nERROR: Fill in your credentials in the CONFIG section.")
        sys.exit(1)
 
    start = datetime.strptime(START_DATE, "%Y-%m-%d").date()
    end   = datetime.strptime(END_DATE,   "%Y-%m-%d").date()
 
    print(f"\nRange  : {START_DATE} to {END_DATE}")
    print(f"Fields : v1-v4 (standard) + v7-v12 (extended)")
    print(f"Interval: every {INTERVAL_MINUTES} minutes\n")
 
    total_days = total_points = 0
    current = start
 
    while current <= end:
        print(f"  {current} ... ", end="", flush=True)
        pushed = upload_day(current)
        print(f"{pushed} points uploaded.")
        total_points += pushed
        total_days   += 1
        current += timedelta(days=1)
 
    print(f"\nComplete: {total_points} points across {total_days} days pushed to PVOutput.")
 
 
if __name__ == "__main__":
    main()