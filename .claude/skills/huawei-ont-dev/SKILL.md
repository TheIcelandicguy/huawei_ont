---
name: huawei-ont-dev
description: Reference for developing and operating Davíð's huawei_ont Home Assistant integration (v1.3.1) — E:\huawei_ont (domain huawei_ont, repo TheIcelandicguy/huawei_ont, deployed to Z:\custom_components\huawei_ont), which scrapes the Huawei OptiXstar V261a-20 ONT's web UI for WAN status, client counts, per-device trackers, Wi-Fi switches and a reboot button. Use whenever the ONT, "the Huawei", "the router integration", huawei_ont, its device trackers, client counts or Wi-Fi switches come up, before touching api.py, the device-list handshake or tracker pruning, and when deploying it. ALWAYS use it after any work in the router's own web UI — the router allows one admin session, so the integration gets disabled for that work and must be re-enabled afterwards (it once sat disabled for days). Read it before answering from memory; the July 2026 device-list change and the single-session rule are easy to get wrong.
---

# huawei_ont — integration development and operation

*Verified against source 2026-10-07.*

A Home Assistant custom integration for the **Huawei OptiXstar V261a-20** (GE
uplink, not GPON). No SNMP, no TR-069, no JSON API: `api.py` logs in to the
router's HTTPS web UI, scrapes `.asp` pages that emit JavaScript constructors,
and turns them into a `RouterData`. Everything HA-facing is thin.

- Source `E:\huawei_ont` (git, `main`, repo `TheIcelandicguy/huawei_ont`,
  public, HACS custom repo). Deployed to `Z:\custom_components\huawei_ont`.
  Only branch is `main` — nothing else to track, and no open PRs.
- Domain `huawei_ont`, **v1.3.1** (`manifest.json`), `iot_class: local_polling`,
  `requirements: []` — `api.py` uses only `requests`, which HA ships.
- Platforms: `sensor`, `binary_sensor`, `button`, `device_tracker`, `switch`.
  **Two services**, `set_static_ip` and `clear_static_ip` (added in v1.1.0's
  DHCP static IP reservations feature — see below). **No options flow** (host,
  credentials, interval and the `use_https` box are set only at setup;
  re-adding the same IP aborts as `already_configured`).

## Which docs to trust

`E:\huawei_ont\CLAUDE.md` is current — rewritten against source in Sep 2026 and
gated by `check_docs.py`. It has the full page-by-page account of the login,
token and parsing mechanics; this skill carries only what you need to work
safely and the history that explains the odd bits. `README.md` is the user
view. When they disagree, source wins; then fix `CLAUDE.md`.

## The one operational rule: re-enable it after router UI work

The ONT allows **one admin session at a time**. A browser login to the router's
web UI and the integration fight for it, each side stealing it back, so the
integration is **disabled** (config entry, `disabled_by: user`) whenever
someone works in the router UI. It then has to be re-enabled by hand — and it
was not: on 11 Sep 2026 it was found disabled from a router session days
earlier, with every entity gone and nothing alerting, because a disabled entry
is not an error.

So, after **any** router web-UI session, in the same conversation:

1. Re-enable the config entry (HA connector: `ha_get_integration` to find the
   entry, `ha_set_integration` to enable it; or Settings → Devices & services
   → Huawei ONT → Enable).
2. Verify against the healthy baseline below. Sensors read `unknown` for one
   poll cycle (30 s) after enabling; longer than that means the login failed
   (wrong password, or the UI session is still open and holding the slot).

**Healthy baseline (11 Sep 2026, after re-enable):** 72 connected clients
(50 LAN / 22 Wi-Fi), 72 of 74 device trackers `home`, board temperature ~57 °C.
Client counts far below that with the WAN sensor still fine means the device
list did not arrive (`device_list_valid` false) — see the July change, not a
quiet house.

If you are about to open the router UI yourself (through Cowork or the
browser), disable the integration first, do the work, re-enable, verify. Say
the re-enable step out loud in the plan so it cannot be forgotten again.

## The July 2026 device-list change and its fix

On 27 July the connected-device list started coming back empty: every client
count sat at 0 and every tracker went unavailable, while WAN, CPU and
temperature stayed healthy. The router does **not serve the device list
live**. It renders it to a file only on request, so a plain `GET
getuserdevinfo.asp` returns the literal `NONE` (never built) or a stale
snapshot from whenever it was last generated.

The fix mirrors what the router's own `userdevinfo1.asp` page does
(`_fetch_user_devices_once` in `api.py`):

```
POST setajax.cgi   x.State=Creating      ask for a rebuild
POST getajax.cgi   State                 poll every 0.25 s, up to 12 s
POST getuserdevinfo.asp                  read the fresh list
```

Two details that were wrong once each and are now pinned by tests:

- The poll must wait for the state to **leave** `Completed` and come back.
  Seeing `Completed` immediately proves nothing — the previous poll left it
  there — and a stale snapshot then parsed as fresh. If it never leaves within
  2 s, accept a possibly-stale list rather than hang.
- The endpoints need the `onttoken` from `userdevinfo1.asp`, cached per login
  session. A re-login mid-sequence voids the token in flight, so the whole
  fetch is retried once on the fresh session (`_auth_generation`).

The same week: rows arrive **one per IP family**, so a device with an IPv6
link-local address appeared twice under one MAC and the counts read 85 where
63 devices were present — `_parse_user_devices` collapses by MAC, prefers
`Online`, prefers IPv4, backfills the hostname from the sibling row. `\xNN`
escapes carry raw UTF-8 bytes, so `_decode_hex` round-trips `latin-1 → utf-8`
or the SSID `Ásgarður` becomes mojibake in the Wi-Fi switch names. Phones on
randomised MACs are followed by DHCP hostname (`_find_rotated_twin`), and every
guard there fails closed — a stranded duplicate is easy to delete, a wrong
merge silently corrupts presence.

**The three-week blind spot (fixed 12 Aug).** `get_router_data()` never
raised, so a poll where nothing came back looked exactly like a healthy router
with no traffic and nobody home: sensors read 0, every tracker went away, the
coordinator reported success. The rules that came out of it:

- `get_router_data()` raises `HuaweiOntConnectionError` when **zero** pages
  came back. Single-page failures leave defaults; that is fine.
- `RouterData.device_list_valid` is `False` unless a list was actually parsed.
  `device_tracker.py` never adds, prunes or ages a tracker while it is `False`.
  Client counts are `None` (→ `unknown`), never `0`.
- `_post` and `_fetch_page` keep `allow_redirects=False`. A dead session
  answers `302`/`403`; following the redirect turns session loss into a 200
  carrying the login form and the client stays logged out forever.

**Dead sockets (v1.3.x).** The ONT closes idle keep-alive sockets silently, so
a login on the pooled connection can die with `RemoteDisconnected`.
`authenticate()` retries once on a fresh session after `LOGIN_RETRY_DELAY`
(2 s); only a second failure or a rejected login starts the 60 s backoff
(`_auth_blocked_until`). The coordinator absorbs one failed poll in a row
(`TOLERATED_MISSES`, keeping the last data). The `use_https` setup option
(default true) switches to plain `http://` for firmwares that serve only that.

A firmware update can shift the **positional constructor indices**
(`_DI_SERIAL`, `_WI_IP`, `_UD_MAC`, `_GE_SPEED`, …) because
`_parse_constructors` drops `null` and nested `new …` arguments. The symptom
is plausible values in the wrong fields, not an exception. Compare against
the baseline after any firmware change (current: `V5R023C10S319`).

## DHCP static IP reservations (added v1.1.0, extended through v1.2.0)

Missing from earlier passes over this skill — a whole feature landed since:
the ONT's *Advanced > LAN > DHCP Static IP* page (`dhcpstatic.asp`) holds up
to 16 MAC→IP reservations, parsed as `new stDhcp(domain, enable, ip, mac)`.
Surfaces:

- Services `huawei_ont.set_static_ip` (entity_id or mac_address, optional
  ip_address — omitted pins the device's current address) and
  `huawei_ont.clear_static_ip`.
- One `switch` per device, **disabled by default**, `EntityCategory.CONFIG`,
  on a sub-device **"Static IP reservations"** linked to the ONT via
  `via_device_id`. A switch exists only for a device that is online, holds a
  reservation, or whose switch the user already enabled; other disabled
  leftovers are dropped each poll.
- `sensor` *Free addresses* (same sub-device) counts unused hosts of the
  router's assumed /24 and lists them in an (unrecorded) `addresses`
  attribute.
- A `static_ip` attribute on trackers; `RouterData.static_bindings` is `None`
  only when the page wasn't read, never "no reservations".
- `set_static_ip`/`clear_static_ip` refuse a duplicate IP or a full table,
  then **re-read the page and raise if the router didn't keep the change**
  (it answers 200 either way). A new address is refused if an online device
  holds it right now (`holder_of`) unless `force: true` is passed.

**Device naming** also landed alongside this: `coordinator.known_name(mac)`
looks the MAC up in HA's device registry (as ESPHome/Shelly/Hue register
`CONNECTION_NETWORK_MAC`) and trackers/switches show that name ahead of the
DHCP hostname or vendor label — live, not written to the registry, so nothing
gets pinned by it. `_migrate_rotated_macs` follows a device that returns on a
fresh randomised MAC by matching DHCP hostname and re-keying the registry
entry; every guard in `_find_rotated_twin` fails closed (no/ambiguous
hostname, more than one candidate, a non-random old MAC all abort the match).

Tested by `tests/test_static_ip.py` (17 tests).

## Trackers

Keyed on MAC (`ScannerEntity.unique_id` is a property returning
`mac_address`; `_attr_unique_id` is shadowed). A tracker exists only while the
device is on the router's list and is dropped after `PRUNE_AFTER` — **10
minutes of wall-clock time**, not polls. Measured before the change: a 3-poll
(90 s) grace deleted and re-created 75 trackers 21,956 times in 30 days, and
98.9 % of those devices were back within 10 minutes. A user-renamed tracker is
pinned (never pruned, shows `not_home`) — renaming is the presence opt-in.
`online_duration` is parsed but deliberately **not** an attribute: it moved
every poll, so every poll wrote a recorder row per tracker (2.2 M rows a
month). Keep attributes to things that change when something happens.

## Tests, CI

```
pytest -q tests/test_api.py          # the only part that runs on Windows (70)
pytest                               # full suite (107), needs HA — Linux/WSL only
```

(`test_api.py` 70, `test_device_tracker.py` 20, `test_static_ip.py` 17;
`conftest.py` sets `collect_ignore` for the latter two when HA isn't
importable, which is every Windows run.)

Locally on Windows go through `cmd`, not PowerShell, and disable plugin
autoload or the HA test plugin dies on `fcntl` before `conftest.py` runs:

```
cmd /c "set PYTEST_DISABLE_PLUGIN_AUTOLOAD=1&& .venv\Scripts\pytest.exe -q tests\test_api.py"
```

`api.py` and `oui.py` must stay free of HA imports — `conftest.load_standalone()`
imports them straight off disk so `test_api.py` runs anywhere. CI
(`.github/workflows/tests.yml`) runs on pushes to `main` and PRs only: `full`
on Ubuntu/py3.13 and `api` on Windows/py3.13. First green run was 11 Sep 2026
(59 + 39). No linter is configured; match the style by hand (4 spaces, ~79
cols). `.gitattributes` forces LF.

## Deploy and verify

```
.\deploy.ps1            # wrapper over E:\tools\deploy-to-ha.ps1
.\deploy.ps1 -DryRun
```

`robocopy /E /R:2 /W:2` with the standard exclusions (`tests` and `test_*.py`
are excluded; `tools/test_connection.py` sits outside the component anyway). Exit codes 0–7 are
success — 1 means files were copied; only ≥8 is a failure. The tool refuses to
run unless `Z:\configuration.yaml` exists, warns about newer files on `Z:`,
lists files on `Z:` the repo no longer has (`/E` never deletes; remove a stale
module by hand), and checks the deployed manifest version.

Then restart HA (pre-authorised) and verify through the HA connector rather
than reporting "deployed": the config entry is **enabled**, `sensor.*connected
clients` is near the baseline, trackers are back, and
`ha_get_logs(search="huawei_ont")` shows no new traceback. Bump `version` in
`manifest.json` in the same commit as the change.

## Traps on this machine

- Commit with `git commit -F <tempfile>`; PowerShell mangles `-m` quoting.
- PowerShell, not WSL, for anything touching `Z:`; put anything with `$` in a
  script file. The full test suite is the one thing that runs in WSL.
- DHCP hostnames are ASCII (RFC 1123): `Davíð` arrives as `David`. Rename in
  HA, which also pins the tracker. Devices behind a downstream NAT router are
  invisible to the ONT.
- `tools/test_connection.py` logs in and prints one fetch without HA; run it
  from the repo root: `python tools/test_connection.py <password> [host] [username]`.
  It lives outside the component so it never ships or deploys. Log out of the
  router UI first (one admin session).

Before ending a session, run `python check_docs.py` and update `CLAUDE.md`.
