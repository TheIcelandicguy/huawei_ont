# CLAUDE.md — huawei_ont

Home Assistant custom integration for **Huawei OptiXstar** ONTs. Domain
`huawei_ont`, **v1.0.4** (`custom_components/huawei_ont/manifest.json`),
`integration_type: hub`, `iot_class: local_polling`, `requirements: []` — no
third-party deps; `api.py` uses only `requests`, which HA already ships. Repo
`TheIcelandicguy/huawei_ont`, branch `main`; HACS custom repo, min HA `2024.1.0`.

Before ending a session, run `python check_docs.py` and update this file.

Developed against a **Huawei OptiXstar V261a-20 GE Terminal**, firmware
`V5R023C10S319` (README). **GE uplink, not GPON**: no SFP, so optical TX/RX power
read `--` while board temperature and supply voltage still work. Model, serial and
HW/SW versions come off the device at runtime from `stDeviceInfo`;
`RouterData.model` merely defaults to `"V261a-20"`.

## Layout

```
custom_components/huawei_ont/
  api.py          945 lines — ALL device I/O and scraping. No HA imports.
  coordinator.py  DataUpdateCoordinator, executor wrapper around api
  __init__.py     setup/unload; config_flow.py one step, unique_id = host
  const.py        DOMAIN, CONF_*, DEFAULT_HOST 192.168.0.1, admin, 30
  sensor.py binary_sensor.py switch.py button.py device_tracker.py
  oui.py + oui_db.csv   offline IEEE OUI -> vendor; strings.json; translations/
tests/  conftest.py, test_api.py (39), test_device_tracker.py (20)
```

## How it talks to the ONT (the non-obvious part)

No SNMP, no TR-069, no JSON API — it drives the router's own web UI over HTTPS
with a self-signed cert (`verify=False`) and scrapes `.asp` pages.

**Login** (`HuaweiOntApi.authenticate`): `POST /asp/GetRandCount.asp` for a nonce
(raw text, BOM to strip), then `POST /login.cgi` with `UserName`, `PassWord` =
**base64 of the plaintext password** (not a hash), `Language=english`,
`x.X_HW_Token` = the nonce, `allow_redirects=False`. Success = `200` **and** a
`CookieHttps` cookie in the `requests.Session`.

**Token dance.** Every write (Wi-Fi toggle, reboot, device-list rebuild) needs an
`onttoken` hidden field scraped from the HTML of the page owning that action
(`RE_ONT_TOKEN`). Tokens are bound to the login session, so `authenticate()`
clears the cached one and bumps `_auth_generation`, letting a caller mid-sequence
detect that the session moved under it.

**Session eviction.** The ONT allows **one admin session at a time**. A dead
session answers `302`/`403`, so `_fetch_page`/`_post` must keep
`allow_redirects=False` — following the redirect turns session loss into a 200
carrying the login form and the client stays logged out forever. On 302/403 it
re-authenticates once and retries; a failed login backs off 60 s
(`_auth_blocked_until`) to stay clear of the brute-force lockout.

### Parsing: JavaScript constructors, not JSON

Pages emit `new stDeviceInfo('a','b',…)` and `var cpuUsed = '12%';`.
`_parse_constructors(text, "ClassName")` does a quote/paren-aware scan and drops
`null` and nested `new …` args — **which shifts every later index**, hence the
positional constants (`_DI_SERIAL`, `_WI_IP`, `_UD_MAC`, `_GE_SPEED`, …).
`_decode_hex` expands `\xNN` escapes carrying raw **UTF-8 bytes** then round-trips
`latin-1 -> utf-8`; without it SSID `Ásgarður` becomes `ÃsgarÃ°ur`.

Pages per cycle (`get_router_data`): `deviceinfo.asp` (CPU/mem/ident),
`wan_list_cache_wan.asp` (`WanIP`, the entry whose service contains `INTERNET`),
`get_wan_list_ipwanstat.asp` (`WaninfoStats`, matched on WAN domain path),
`ontstate.asp`, user-device list, `opticinfo.asp`, `ethinfo.asp` (`GEInfo` → LAN
ports), `WlanBasic.asp?2G`.

### The device-list rebuild handshake

The connected-device list is **not served live** — a plain read returns `"NONE"`
or a stale snapshot. `_fetch_user_devices_once()` reads build state via
`POST /getajax.cgi?x=…UserDevInfo`, posts `x.State=Creating` to `…/setajax.cgi`,
then polls every 0.25 s up to 12 s for the state to **leave** `Completed` and come
back — seeing `Completed` at once proves nothing, the previous poll left it there;
if it never leaves within 2 s it accepts a possibly-stale list. Finally it posts
`…/getuserdevinfo.asp`, rerunning the whole sequence once on a fresh session if it
failed after a re-login (`_auth_generation`). Rows arrive **one per IP family**,
so `_parse_user_devices` collapses by MAC, prefers `Online`, prefers IPv4, and
backfills the hostname from the duplicate.

### Failure semantics — do not break these

- `get_router_data()` raises `HuaweiOntConnectionError` when **zero** pages came
  back — an empty `RouterData` reads as a healthy router with no traffic and
  nobody connected. Single-page failures leave defaults; that is fine.
- `RouterData.device_list_valid` is `False` unless a list was actually parsed;
  `device_tracker.py` must never add, prune or age a tracker while it is `False`.
  Client counts are `None` (→ `unknown`), not `0`.
- `_compute_rates` runs only when the WAN-stats page returned; default zeroes
  would fake a multi-GB spike. Counters are 32-bit and wrap (`COUNTER_WRAP`);
  deltas above 10 000 Mbit/s are discarded as reboots.

**Writes:**
- `set_wifi_enabled(instance, enabled)` → `POST /html/amp/wlanbasic/set.cgi`,
  `x=…LANDevice.1.WLANConfiguration.<n>`, sending `x.Enable` and
  `x.SSIDAdvertisementEnabled=1` plus a fresh token.
- `reboot()` → `POST /html/ssmp/cfgfile/set.cgi` with `x=…DBSave` and
  `y=…ResetBoard` (save config, reset board), then drops session state.

## Polling

Plain `DataUpdateCoordinator[RouterData]`, interval from the config entry,
**default 30 s**. `api` is fully blocking, so everything goes through
`hass.async_add_executor_job`; any exception becomes `UpdateFailed`. A poll is not
cheap — 8 page fetches plus the rebuild handshake; timeouts are GET 15 s, POST
25 s, state POSTs 10 s. Keep the worst case under the interval.

## Entities

`PLATFORMS = ["sensor", "binary_sensor", "button", "device_tracker", "switch"]`,
all on one device (`identifiers={(DOMAIN, entry.entry_id)}`). **No services.**

- **sensor** (15, description-driven with `value_fn`): CPU %, memory %, WAN IP,
  WAN uptime (`Nd Nh Nm`), download/upload rate (Mbit/s from counter deltas),
  connected/Wi-Fi/LAN client counts, optical temperature (°C), optical voltage
  (mV). `bytes_*`/`packets_*` are `TOTAL_INCREASING`, disabled by default. Wi-Fi
  count = ONT radios only; a downstream AP's clients land in the LAN count.
- **binary_sensor**: WAN connection, ONT status (both `CONNECTIVITY`), plus one
  per LAN port from `GEInfo` with `speed`/`duplex` attrs (`LAN_SPEED_MAP`: 0=10M,
  1=100M, 2=1000M, 3=2.5G, 4=10G, 6=5G, 7=25G).
- **switch**: one per `WLANConfiguration` instance — main and guest, 2.4G
  (instance ≤4) and 5G; guest = `{3,4,7,8}`. Guest instances can appear after
  setup, so `switch.py` re-runs its add pass on every coordinator update. Attrs:
  ssid, band, channel, standard (e.g. `11be`), guest.
- **button**: Reboot (`RESTART`). **device_tracker**: `ScannerEntity`,
  `SourceType.ROUTER`, only-online mode.

Trackers are keyed on **MAC** — `ScannerEntity.unique_id` is a property returning
`mac_address`, so `_attr_unique_id` is shadowed and useless here. The ONT returns
its whole DHCP lease history, so a tracker exists only while the device is online
and is dropped once absent for `PRUNE_AFTER` (10 min, wall-clock, not polls);
inside that window it shows `not_home`. Measured, not guessed: the old 3-poll
(90 s) grace deleted and re-created 75 trackers 21,956 times in 30 days, and
98.9% of those devices were back within 10 min. **A user-renamed tracker is
pinned** (never pruned; shows `not_home`) — renaming is the presence opt-in.

`online_duration` is parsed into `ConnectedDevice` but is deliberately **not** a
tracker attribute: it moves every poll, so it made every poll a new state and a
recorder row per tracker (2.2M rows/month from 72 trackers). Keep attributes to
values that change only when something actually happens.

`_migrate_rotated_macs` follows a device returning on a fresh randomised MAC
(locally-administered bit, `int(mac[0:2],16) & 0x02`) by matching DHCP hostname
and re-keying the registry entry. Every guard in `_find_rotated_twin` fails
closed — no hostname, an ambiguous hostname among online devices, more than one
candidate, a third MAC in lease history with the same name, or a non-random old
MAC all abort the match. Keep it that way: a stranded duplicate is trivial to
delete, a wrong merge silently corrupts presence. Devices with no hostname get an
`oui.py` label (`"Canon 3e24"`) and a `vendor` attribute.

## Tests / lint / CI

```bash
pip install -r requirements-test.txt
pytest                       # full suite (Linux/macOS/WSL)
pytest -q tests/test_api.py  # the only part that runs on Windows
```

Local venv: `E:\huawei_ont\.venv\Scripts\pytest.exe` (Python 3.14). HA does not
import on Windows (`homeassistant.runner` needs `fcntl`), so `conftest.py` sets
`collect_ignore = ["test_device_tracker.py"]` and prints a `pytest_report_header`
saying so. That venv also has HA installed, though, so pytest auto-loads the
`pytest_homeassistant_custom_component` plugin and dies on `fcntl` before
`conftest.py` runs — CI's Windows job never has it installed. Locally:
`cmd /c "set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .venv\Scripts\pytest.exe -q tests\test_api.py"`
(via `cmd`, because PowerShell mangles `$env:` through the MCP layer). The full
suite runs in WSL Ubuntu from `/mnt/e/huawei_ont` with
`~/ha-test-venv/bin/python -m pytest -q` (phcc 0.13.316, HA 2026.2.3, py3.12). `api.py` is kept HA-free precisely so `test_api.py` runs anywhere, and
`conftest.load_standalone()` imports `api`/`oui` straight off disk to bypass the
package `__init__` — **do not add HA imports to `api.py` or `oui.py`**.

CI (`.github/workflows/tests.yml`), on push to `main` and PRs: job `full`
(ubuntu, py3.13, `pytest -q`) and job `api` (windows, py3.13,
`pip install pytest "requests>=2.31"`, `pytest -q tests/test_api.py`). **No
linter is configured** — no ruff/flake8/pyproject/setup.cfg; match the existing
style by hand (4 spaces, ~79 cols). `.gitattributes` forces LF.

## Deploy

Source on `E:`, the HA config root is mapped to `Z:`:

```powershell
robocopy "E:\huawei_ont\custom_components\huawei_ont" "Z:\custom_components\huawei_ont" /MIR /XD __pycache__ /NFL /NDL /NJH /NP
if ($LASTEXITCODE -lt 8) { "robocopy OK (exit $LASTEXITCODE)" } else { "robocopy FAILED (exit $LASTEXITCODE)" }
```

Robocopy exit codes below 8 are success — **do not read exit 1 or 3 as failure**.
Then restart Home Assistant. `/MIR` deletes anything on the target not in the
source, hence `/XD __pycache__`. If HA has modules or `.pyc`s open the copy fails
with permission/EPERM errors — expected Windows file-lock friction, not a broken
command; retry, or restart HA first.

## Gotchas

- **The deployed copy can drift.** Nothing keeps `Z:` in step with the repo
  except running `deploy.ps1`, which prints both versions (`from … (vX)` /
  `to … (vY)`). Compare `Z:\custom_components\huawei_ont\manifest.json` with the
  repo before debugging live behaviour, and bump `version` in the same commit as
  the change.
- **One admin session.** A browser login to the ONT web UI fights the
  integration for it; each side steals it back. Disable the integration first.
- **No options flow.** Host, credentials and scan interval are settable only at
  setup — changing one means deleting and re-adding the entry; `unique_id` is the
  host, so re-adding the same IP aborts as `already_configured`.
- **Positional constructor indices are fragile.** A firmware change that adds or
  drops an argument silently shifts every `_XX_*` index in `api.py`; the symptom
  is plausible values in the wrong fields, not an exception.
- **`_parse_ont_state` has a dead branch** — its `gpon` and `else` arms both call
  `_parse_constructors(html, "OntStateInfo")`. Harmless (this unit reports
  `PonMode == "ge"`, deriving ONT state from WAN status) but unfinished code.
- **`test_connection.py` ships inside the component dir** and does a bare
  `from api import …`, so it runs only with cwd set there, and deploys to `Z:`
  too. Usage: `python test_connection.py <password> [host] [username]`.
- **DHCP hostnames are ASCII** (RFC 1123): `Davíð` arrives as `David` and the
  accented form never reaches the ONT — rename in HA, which also pins. Devices
  behind a downstream NAT router are invisible; the ONT sees only its address.
