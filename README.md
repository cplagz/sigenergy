# Sigenergy → PVOutput for Home Assistant

Pushes live and historical solar, battery, and grid data from a **Sigenergy inverter** to [PVOutput](https://pvoutput.org) via the Home Assistant recorder and PVOutput batch API.

---

## What's included

| File | Purpose |
|---|---|
| `sigenergy_pvoutput.yaml` | HA package — template sensors, REST command, automation |
| `pvoutput_historical_upload.py` | One-off script to backfill historical data |

---

## Requirements

- Home Assistant with the [Sigenergy integration](https://github.com/lopexy/sigenergy-local-modbus) installed and working
- A [PVOutput](https://pvoutput.org) account
- **PVOutput Donation Mode** for extended data fields v7–v12 (battery, grid, temperature)
- The **Terminal & SSH** addon (for running the historical upload script)

---

## Package installation (`sigenergy_pvoutput.yaml`)

### 1. Enable packages in Home Assistant

Add the following to your `configuration.yaml` if not already present:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

### 2. Create the packages folder

```
/config/packages/
```

Drop `sigenergy_pvoutput.yaml` into that folder.

### 3. Add credentials to `secrets.yaml`

```yaml
pvoutput_api_key: "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
pvoutput_system_id: "123456"
```

Get these from [pvoutput.org/account.jsp](https://pvoutput.org/account.jsp).

### 4. Restart Home Assistant

Developer Tools → YAML → Check Configuration, then restart.

### 5. Enable the automation

Go to **Settings → Automations** and enable **"Update Sigenergy to PVOutput"**.  
Trigger it once manually to confirm it's working — check **Settings → Logs** for any warnings.

---

## Sensor map

### Standard fields (all accounts)

| PVOutput | Label | Source entity | Conversion |
|---|---|---|---|
| v1 | PV Energy Today | `sensor.sigen_plant_daily_pv_energy` | kWh × 1000 → Wh |
| v2 | PV Power Now | `sensor.sigen_plant_pv_power` | kW × 1000 → W |
| v3 | Load Energy Today | `sensor.sigen_plant_daily_load_consumption` | kWh × 1000 → Wh |
| v4 | Load Power Now | `sensor.sigen_plant_consumed_power` | kW × 1000 → W |

### Extended fields (Donation Mode required)

| PVOutput | Label | Source entity | Notes |
|---|---|---|---|
| v7 | Battery SoC | `sensor.sigen_plant_battery_state_of_charge` | % |
| v8 | Battery Power | `sensor.sigen_plant_battery_power` | kW × 1000 → W, negative = discharging |
| v9 | Grid Import | `sensor.sigen_plant_grid_import_power` | kW × 1000 → W |
| v10 | Grid Export | `sensor.sigen_plant_grid_export_power` | kW × 1000 → W |
| v11 | Battery Temp | `sensor.sigen_inverter_battery_average_cell_temperature` | °C |
| v12 | Inverter Temp | `sensor.sigen_inverter_pcs_internal_temperature` | °C |

---

## PVOutput extended data configuration

In PVOutput go to **Settings → System → Edit → Extended Data** and configure as follows:

| Field | Label | Unit | Axis | Credit/Debit |
|---|---|---|---|---|
| v7 | Battery SoC | % | 2 (right) | None |
| v8 | Battery Power | W | 1 (left) | None |
| v9 | Grid Import | W | 1 (left) | Debit |
| v10 | Grid Export | W | 1 (left) | Credit |
| v11 | Battery Temp | C | 2 (right) | None |
| v12 | Inverter Temp | C | 2 (right) | None |

> Battery SoC, Battery Temp, and Inverter Temp go on **Axis 2 (right)** so they have their own scale separate from the watt values on the left axis.  
> Grid Import as **Debit** and Grid Export as **Credit** enables PVOutput's cost/earnings reports.

---

## Historical upload (`pvoutput_historical_upload.py`)

Fetches 5-minute history from the HA recorder via the REST API and pushes it to PVOutput in batches of 30, including all extended fields.

### 1. Install dependencies

From the HA Terminal addon:

```bash
pip install requests
```

### 2. Run it

```bash
python3 pvoutput_historical_upload.py
```

On first run the script will:

1. **Read** `pvoutput_api_key` and `pvoutput_system_id` directly from `/config/secrets.yaml` — no editing the script needed
2. **Prompt** for your HA Long-Lived Access Token (Profile → Long-Lived Access Tokens → Create Token)
3. **Prompt** for a start and end date
4. **Save** the token and dates to `.pvoutput_upload_config.json` next to the script

On subsequent runs, saved values are shown as defaults — just press Enter to reuse them or type new ones.

Output looks like:

```
============================================================
  Sigenergy -> PVOutput Historical Upload (with extended data)
============================================================

Range  : 2026-04-01 to 2026-04-24
Fields : v1-v4 (standard) + v7-v12 (extended)
Interval: every 5 minutes

  2026-04-01 ... 144 points uploaded.
  2026-04-02 ... 138 points uploaded.
  ...

Complete: 3312 points across 24 days pushed to PVOutput.
```

### Notes

- **No credentials in the script** — API key and System ID are read from `/config/secrets.yaml`. Only the HA token is saved locally (in `.pvoutput_upload_config.json`, chmod 600)
- **Re-running is safe** — PVOutput overwrites existing entries rather than duplicating them
- **Gaps in HA history** (e.g. after a restart) are skipped cleanly rather than uploading zeroes
- A 2-second pause between each batch of 30 keeps the script within PVOutput's rate limits
- The script uses forward-fill resampling: each 5-minute slot gets the most recent reading recorded at or before that time

---

## Automation behaviour

The automation runs every 5 minutes and will **skip** a push if:

- Outside the 05:30–20:30 window (Perth AWST — adjust in the YAML if needed)
- The inverter running state is not `Running`
- The PV power sensor is `unavailable` or `unknown` (e.g. comms dropout)

Successful pushes are logged at **debug** level. Failures are logged at **warning** level and include the HTTP status and PVOutput response body for easy diagnosis.

---

## Troubleshooting

**Config check fails on startup**  
Make sure `configuration.yaml` has the `packages:` key under `homeassistant:` and that the file is saved as plain UTF-8 with no special characters.

**PVOutput shows "no data available"**  
The integration reads from PVOutput — it needs at least one successful push before it shows data. Trigger the automation manually and check the logs.

**Extended data not appearing**  
Donation Mode must be active on your PVOutput account and the v7–v12 labels must be configured under System settings before data will be stored.

**Historical script returns HTTP 400**  
PVOutput rejects entries with future timestamps or malformed data. Check that `END_DATE` is yesterday or earlier and that your system timezone is set correctly in HA.

**Wrong values (e.g. all zeros)**  
Run this in Developer Tools → Template to verify the source sensors are returning real values:
```
{{ states('sensor.sigen_plant_pv_power') }}
{{ states('sensor.sigen_plant_battery_state_of_charge') }}
```
