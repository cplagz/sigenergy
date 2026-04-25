#!/usr/bin/env python3
"""
pvoutput_historical_upload.py

Uploads historical Sigenergy data from Home Assistant to PVOutput.

Standard fields (v1-v4, v6) uploaded via fast batch API.
Extended fields (v7-v12) uploaded via individual API calls (slower but required).
Reads PVOutput credentials from /config/secrets.yaml automatically.
Prompts for HA token and date range on first run, then saves them.

REQUIREMENTS
  pip install requests

USAGE
  python3 pvoutput_historical_upload.py
"""

import requests
import sys
import json
from datetime import datetime, timedelta, timezone
from time import sleep
from pathlib import Path

# =============================================================================
# PATHS + SETTINGS
# =============================================================================

SECRETS_FILE     = Path("/config/secrets.yaml")
CONFIG_FILE      = Path(__file__).parent / ".pvoutput_upload_config.json"
HA_URL           = "http://localhost:8123"
INTERVAL_MINUTES = 5
BATCH_SLEEP      = 2      # seconds between batch API calls
EXTENDED_SLEEP   = 1      # seconds between individual extended data calls

# =============================================================================
# SENSOR MAP
#
# Standard fields (batch API supported)
#   v1  = PV energy today        kWh -> Wh
#   v2  = PV power now           kW  -> W
#   v3  = Load energy today      kWh -> Wh
#   v4  = Load power now         kW  -> W
#   v6  = Grid voltage           V   (average of 3 phases)
#
# Extended fields (individual API calls only)
#   v7  = Battery SoC            %
#   v8  = Battery power          kW  -> W  (negative = discharging)
#   v9  = Grid import power      kW  -> W
#   v10 = Grid export power      kW  -> W
#   v11 = Battery temperature    C
#   v12 = Inverter temperature   C
# =============================================================================

STANDARD_SENSORS = {
    "pv_energy_kwh":   "sensor.sigen_plant_daily_pv_energy",
    "pv_power_kw":     "sensor.sigen_plant_pv_power",
    "load_energy_kwh": "sensor.sigen_plant_daily_load_consumption",
    "load_power_kw":   "sensor.sigen_plant_consumed_power",
    "phase_a_v":       "sensor.sigen_inverter_phase_a_voltage",
    "phase_b_v":       "sensor.sigen_inverter_phase_b_voltage",
    "phase_c_v":       "sensor.sigen_inverter_phase_c_voltage",
}

EXTENDED_SENSORS = {
    "battery_soc":      "sensor.sigen_plant_battery_state_of_charge",
    "battery_power_kw": "sensor.sigen_plant_battery_power",
    "grid_import_kw":   "sensor.sigen_plant_grid_import_power",
    "grid_export_kw":   "sensor.sigen_plant_grid_export_power",
    "battery_temp":     "sensor.sigen_inverter_battery_average_cell_temperature",
    "inverter_temp":    "sensor.sigen_inverter_pcs_internal_temperature",
}

KW_TO_W = {"pv_energy_kwh", "pv_power_kw", "load_energy_kwh", "load_power_kw",
            "battery_power_kw", "grid_import_kw", "grid_export_kw"}

# =============================================================================
# SECRETS + CONFIG
# =============================================================================

def read_secret(key):
    if not SECRETS_FILE.exists():
        return None
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)

# =============================================================================
# PROMPTS
# =============================================================================

def prompt_token(saved):
    print()
    print("  HA Long-Lived Access Token")
    print("  Profile (bottom-left) -> Long-Lived Access Tokens -> Create Token")
    print()
    if saved:
        masked = saved[:6] + "..." + saved[-4:]
        ans = input(f"  Press Enter to reuse [{masked}], or paste a new one: ").strip()
        return ans if ans else saved
    token = ""
    while not token:
        token = input("  Paste your token: ").strip()
    return token

def prompt_dates(saved_start, saved_end):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    print()
    print("  Date range (YYYY-MM-DD). End date must be yesterday or earlier.")
    print()
    default_start = saved_start or ""
    if default_start:
        ans = input(f"  Start date [{default_start}]: ").strip()
        start = ans if ans else default_start
    else:
        start = ""
        while not start:
            start = input("  Start date: ").strip()
    default_end = saved_end or yesterday
    ans = input(f"  End date   [{default_end}]: ").strip()
    end = ans if ans else default_end

    try:
        start_dt = datetime.strptime(start, "%Y-%m-%d").date()
        end_dt   = datetime.strptime(end,   "%Y-%m-%d").date()
    except ValueError:
        print("\n  ERROR: Use YYYY-MM-DD format.")
        sys.exit(1)

    if end_dt >= datetime.now().date():
        print("  WARNING: End date capped at yesterday.")
        end_dt = datetime.now().date() - timedelta(days=1)
        end = end_dt.strftime("%Y-%m-%d")

    if start_dt > end_dt:
        print("\n  ERROR: Start date is after end date.")
        sys.exit(1)

    return start, end

def prompt_extended(saved):
    print()
    print("  Upload extended data (v7-v12: battery, grid, temps)?")
    print("  Requires PVOutput Donation Mode. Uses individual API calls — slower.")
    print()
    if saved is not None:
        default = "Y" if saved else "N"
        ans = input(f"  Upload extended data? [{default}]: ").strip().upper()
        if ans == "":
            return saved
        return ans != "N"
    ans = input("  Upload extended data? [Y/n]: ").strip().upper()
    return ans != "N"

# =============================================================================
# HA HISTORY
# =============================================================================

def fetch_history(entity_id, start, end, ha_headers):
    url = f"{HA_URL}/api/history/period/{start.isoformat()}"
    params = {
        "end_time": end.isoformat(),
        "filter_entity_id": entity_id,
        "minimal_response": "true",
        "no_attributes": "true",
    }
    try:
        resp = requests.get(url, headers=ha_headers, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data[0] if data else []
    except requests.exceptions.RequestException:
        return []

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

def fetch_all(sensor_map, day_start, day_end, slots, ha_headers):
    sampled = {}
    for key, entity_id in sensor_map.items():
        raw    = fetch_history(entity_id, day_start, day_end, ha_headers)
        states = parse_states(raw)
        sampled[key] = forward_fill(states, slots)
    return sampled

def convert(key, value):
    if value is None:
        return None
    if key in KW_TO_W:
        return int(round(value * 1000))
    return round(value, 1)

def f(v):
    return str(v) if v is not None else ""

# =============================================================================
# PVOUTPUT API
# =============================================================================

def push_batch_standard(data_points, pvo_headers):
    """Batch upload standard fields via addbatchstatus.jsp (up to 30 per call)."""
    payload = "data=" + ";".join(data_points)
    resp = requests.post(
        "https://pvoutput.org/service/r2/addbatchstatus.jsp",
        headers=pvo_headers,
        data=payload,
        timeout=30,
    )
    if resp.status_code == 200:
        return True
    print(f"\n    Batch error {resp.status_code}: {resp.text.strip()}")
    return False

def push_single_extended(date_str, time_str, ext, pvo_headers):
    """Upload extended fields for one time slot via addstatus.jsp."""
    params = []
    if ext.get("v7")  is not None: params.append(f"v7={ext['v7']}")
    if ext.get("v8")  is not None: params.append(f"v8={ext['v8']}")
    if ext.get("v9")  is not None: params.append(f"v9={ext['v9']}")
    if ext.get("v10") is not None: params.append(f"v10={ext['v10']}")
    if ext.get("v11") is not None: params.append(f"v11={ext['v11']}")
    if ext.get("v12") is not None: params.append(f"v12={ext['v12']}")

    if not params:
        return True

    payload = f"d={date_str}&t={time_str}&" + "&".join(params)
    resp = requests.post(
        "https://pvoutput.org/service/r2/addstatus.jsp",
        headers=pvo_headers,
        data=payload,
        timeout=30,
    )
    return resp.status_code == 200

# =============================================================================
# UPLOAD ONE DAY
# =============================================================================

def upload_day(date, do_extended, ha_headers, pvo_headers):
    day_start = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=timezone.utc)
    day_end   = day_start + timedelta(days=1) - timedelta(seconds=1)

    slots = []
    slot = day_start
    while slot <= day_end:
        slots.append(slot)
        slot += timedelta(minutes=INTERVAL_MINUTES)

    # Fetch standard sensors
    std = fetch_all(STANDARD_SENSORS, day_start, day_end, slots, ha_headers)

    # Fetch extended sensors only if requested
    ext = fetch_all(EXTENDED_SENSORS, day_start, day_end, slots, ha_headers) if do_extended else {}

    std_points  = []
    ext_records = []

    for slot in slots:
        local_slot = slot.astimezone()
        date_str   = local_slot.strftime("%Y%m%d")
        time_str   = local_slot.strftime("%H:%M")

        pv_e = convert("pv_energy_kwh",   std["pv_energy_kwh"][slot])
        pv_p = convert("pv_power_kw",     std["pv_power_kw"][slot])
        ld_e = convert("load_energy_kwh", std["load_energy_kwh"][slot])
        ld_p = convert("load_power_kw",   std["load_power_kw"][slot])

        # Skip slots with no generation data
        if pv_e is None and pv_p is None:
            continue

        # Average the three phase voltages for v6
        phases = [std[k][slot] for k in ("phase_a_v", "phase_b_v", "phase_c_v") if std[k][slot] is not None]
        voltage = round(sum(phases) / len(phases), 1) if phases else None

        # Standard batch record: date,time,v1,v2,v3,v4,v5,v6
        std_points.append(f"{date_str},{time_str},{f(pv_e)},{f(pv_p)},{f(ld_e)},{f(ld_p)},,{f(voltage)}")

        # Extended record for individual upload
        if do_extended:
            ext_records.append((date_str, time_str, {
                "v7":  convert("battery_soc",      ext["battery_soc"][slot]),
                "v8":  convert("battery_power_kw", ext["battery_power_kw"][slot]),
                "v9":  convert("grid_import_kw",   ext["grid_import_kw"][slot]),
                "v10": convert("grid_export_kw",   ext["grid_export_kw"][slot]),
                "v11": convert("battery_temp",     ext["battery_temp"][slot]),
                "v12": convert("inverter_temp",    ext["inverter_temp"][slot]),
            }))

    if not std_points:
        return 0, 0

    # Push standard data in batches of 30
    std_pushed = 0
    for i in range(0, len(std_points), 30):
        batch = std_points[i:i + 30]
        if push_batch_standard(batch, pvo_headers):
            std_pushed += len(batch)
        sleep(BATCH_SLEEP)

    # Push extended data one slot at a time
    ext_pushed = 0
    if ext_records:
        for date_str, time_str, ev in ext_records:
            if push_single_extended(date_str, time_str, ev, pvo_headers):
                ext_pushed += 1
            sleep(EXTENDED_SLEEP)

    return std_pushed, ext_pushed

# =============================================================================
# MAIN
# =============================================================================

def main():
    print()
    print("=" * 60)
    print("  Sigenergy -> PVOutput Historical Upload")
    print("=" * 60)

    # Read PVOutput credentials from secrets.yaml
    api_key   = read_secret("pvoutput_api_key")
    system_id = read_secret("pvoutput_system_id")

    if not api_key or not system_id:
        print(f"\n  ERROR: pvoutput_api_key / pvoutput_system_id not found in {SECRETS_FILE}")
        print("\n  Add these to /config/secrets.yaml:")
        print('    pvoutput_api_key: "your_key"')
        print('    pvoutput_system_id: "your_id"')
        sys.exit(1)

    print(f"\n  PVOutput System ID : {system_id}")
    print(f"  API key            : {api_key[:6]}...{api_key[-4:]}")

    cfg = load_config()

    token        = prompt_token(cfg.get("ha_token"))
    start_str, end_str = prompt_dates(cfg.get("start_date"), cfg.get("end_date"))
    do_extended  = prompt_extended(cfg.get("do_extended"))

    # Estimate time for extended uploads
    start_dt = datetime.strptime(start_str, "%Y-%m-%d").date()
    end_dt   = datetime.strptime(end_str,   "%Y-%m-%d").date()
    days     = (end_dt - start_dt).days + 1
    slots_per_day = (24 * 60) // INTERVAL_MINUTES

    if do_extended:
        est_minutes = round((days * slots_per_day * EXTENDED_SLEEP) / 60)
        print(f"\n  Extended data enabled. Estimated extra time: ~{est_minutes} min")

    print(f"\n  Range    : {start_str} to {end_str} ({days} days)")
    print(f"  Extended : {'yes (v7-v12)' if do_extended else 'no (v1-v4, v6 only)'}")
    print()
    ans = input("  Proceed? [Y/n]: ").strip().lower()
    if ans == "n":
        print("  Aborted.")
        sys.exit(0)

    save_config({"ha_token": token, "start_date": start_str,
                 "end_date": end_str, "do_extended": do_extended})
    print(f"  Settings saved to {CONFIG_FILE.name}\n")

    ha_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    pvo_headers = {
        "X-Pvoutput-Apikey": api_key,
        "X-Pvoutput-SystemId": system_id,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Verify HA connection
    print("  Verifying HA connection ... ", end="", flush=True)
    try:
        r = requests.get(f"{HA_URL}/api/", headers=ha_headers, timeout=10)
        if r.status_code == 200:
            print("OK\n")
        else:
            print(f"FAILED (HTTP {r.status_code})")
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(f"FAILED — cannot reach {HA_URL}")
        sys.exit(1)

    # Upload
    total_std = total_ext = 0
    current   = start_dt

    while current <= end_dt:
        print(f"  {current} ... ", end="", flush=True)
        std_n, ext_n = upload_day(current, do_extended, ha_headers, pvo_headers)
        msg = f"{std_n} standard"
        if do_extended:
            msg += f" + {ext_n} extended"
        print(msg)
        total_std += std_n
        total_ext += ext_n
        current += timedelta(days=1)

    print()
    print(f"  Done.")
    print(f"  Standard points : {total_std}  (v1-v4, v6)")
    if do_extended:
        print(f"  Extended points : {total_ext}  (v7-v12)")
    print()


if __name__ == "__main__":
    main()
