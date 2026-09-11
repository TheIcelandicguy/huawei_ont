# Huawei OptiXstar ONT — Home Assistant integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A local-polling Home Assistant integration for **Huawei OptiXstar** optical
network terminals / routers. It logs into the device's web UI, scrapes the
status pages, and exposes the data as sensors, switches, a reboot button and
per-device trackers — no SNMP, no cloud, no extra dependencies.

Developed and tested against a **Huawei OptiXstar V261a-20 GE Terminal**
(firmware `V5R023C10S319`). Other OptiXstar models that share the same web UI
will likely work in part; results will vary by firmware.

> **Unofficial.** This project is not affiliated with or endorsed by Huawei.
> It talks to the router's web interface the same way a browser does, by
> reverse-engineering the pages it serves. Use at your own risk.

## Features

| Type | Entities |
|------|----------|
| **Sensors** | CPU usage, memory usage, WAN IP, WAN uptime, download/upload rate (Mbit/s), connected / Wi-Fi / LAN client counts, board temperature, supply voltage, bytes & packets sent/received (disabled by default) |
| **Binary sensors** | WAN connection, ONT status, LAN1–4 link (with speed & duplex attributes) |
| **Switches** | Per-SSID Wi-Fi on/off — main **and** guest networks, 2.4 GHz & 5 GHz |
| **Button** | Reboot the router |
| **Device trackers** | One per connected device, live "only-online" list with automatic pruning and MAC-vendor labelling |

### Device trackers — only-online mode

The router hands back its entire DHCP lease history (hundreds of devices). To
keep the tracker list meaningful, the integration:

- creates a tracker **only for currently-connected devices**,
- **prunes** a tracker once its device has been gone for ten minutes (so the
  list reflects what's actually on the network, without deleting and
  re-creating a tracker every time a device drops off Wi-Fi for a minute),
- **keeps any tracker you rename** — a rename pins the device, which then
  behaves as a normal presence sensor (`home` / `not_home`) instead of being
  pruned.

**Renaming is how you opt a device in to presence.** An unrenamed tracker is
transient: when its device leaves it shows `not_home` for ten minutes and is
then deleted. So if you want to use a device in an automation, rename its
tracker first — that pins it and gives it a stable `entity_id` that survives
the device going away.

### Randomised MAC addresses

Phones and tablets randomise their Wi-Fi MAC. Since trackers are keyed on MAC,
a rotation would otherwise look like a brand-new device: a fresh tracker would
appear while the old (pinned) one sat at `not_home` forever beside it.

To avoid that, when a device appears under a new randomised MAC carrying the
**same DHCP hostname** as a pinned tracker whose device is no longer online,
that tracker is re-pointed at the new MAC. The `entity_id`, your rename and any
automations referencing it all survive; nothing is deleted or duplicated.

The match is deliberately conservative and does nothing unless it is certain.
It is skipped when:

- the device reports **no DHCP hostname** — there is no stable identity left to
  match on, so such a device *will* duplicate across a rotation,
- **more than one online device shares the hostname** (two phones both called
  `iPhone`), or more than one stale tracker matches,
- the old tracker's MAC is **not** randomised — a globally-unique MAC belongs to
  hardware that does not rotate, so a name collision there is a different device.

Skipping is the safe failure: a stranded duplicate is easy to delete by hand,
whereas wrongly merging two devices would silently corrupt their presence. This
behaviour is always on and has no setting.

### What the client-count sensors mean

`Wi-Fi clients` counts only devices associated with the **ONT's own radios**. If
you run another access point in bridge/AP mode, its wireless clients reach the
ONT over a LAN port and are counted in `LAN clients` instead. `Connected
devices` is the total and equals the sum of the two.

Devices behind a second router doing **NAT** are not visible at all — the ONT
only ever sees that router's own address.

### Names and non-ASCII characters

SSIDs are read as UTF-8, so a network called `Ásgarður` or `Þórsheimili` shows
up correctly in the Wi-Fi switch names.

**Device names are a different matter, and the limit is not in this
integration.** A tracker's default name is the DHCP hostname the device
announces, and DHCP hostnames are restricted to ASCII letters, digits and
hyphens ([RFC 1123](https://www.rfc-editor.org/rfc/rfc1123)). Phones therefore
transliterate before sending: a phone named `Anna-sími` announces itself as
`Anna-simi`, and `Davíð Thor's S25 Ultra` arrives as `David-THor-s-S25-Ultra`.
The accented form never reaches the router, so it cannot be recovered here —
rename the tracker in Home Assistant if you want the correct spelling (which
also pins it, see above).

### Vendor labelling

Devices that don't announce a DHCP hostname are labelled by manufacturer using
a bundled copy of the public IEEE OUI registry (`oui_db.csv`). A device with
no hostname shows up as e.g. `Canon 3e24` instead of a bare MAC, and every
tracker gets a `vendor` attribute. The lookup runs entirely offline — no MAC
ever leaves your network.

## Installation

### HACS (custom repository)

1. HACS → **⋮** → **Custom repositories**
2. Add `https://github.com/TheIcelandicguy/huawei_ont`, category **Integration**
3. Install **Huawei OptiXstar ONT**, then restart Home Assistant.

### Manual

Copy `custom_components/huawei_ont/` into your Home Assistant
`config/custom_components/` directory and restart.

## Configuration

**Settings → Devices & Services → Add Integration → Huawei OptiXstar ONT**, then
enter:

| Field | Default | Notes |
|-------|---------|-------|
| Host | `192.168.0.1` | Router IP |
| Username | `admin` | Router admin account |
| Password | — | Router admin password |
| Scan interval | `30` | Seconds between polls |

## Important notes

- **One admin session only.** These routers allow a single admin login at a
  time. While the integration is polling, logging into the web UI in a browser
  will work but the integration will reclaim the session on its next poll (and
  vice-versa). If you need to work in the router UI, disable the integration
  first.
- **Self-signed certificate.** The device serves HTTPS with a self-signed cert,
  so certificate verification is disabled for the local connection.
- **GE terminals have no optical module.** On GE-uplink units the optical
  TX/RX power sensors report `--`; board temperature and supply voltage are
  still available.
- **Login back-off.** After a failed login the integration waits ~60 s before
  retrying, to avoid tripping the router's brute-force lockout.
- **A failed poll is not an empty router.** If a poll brings back nothing, the
  entities go unavailable rather than reporting zero devices, and no tracker is
  pruned. Device trackers are only added or removed on a poll where the router
  actually handed over a device list.

## Development

```bash
pip install -r requirements-test.txt
pytest
```

Home Assistant does not import on Windows (`homeassistant.runner` needs
`fcntl`), so the platform tests only run on Linux, macOS or WSL — pytest prints
a header saying so and collects `tests/test_api.py` alone. That file needs
nothing but `pytest` and `requests`, which is why `api.py` imports no Home
Assistant code. CI runs the full suite on Linux and the API suite on Windows.

## Credits

- MAC-vendor data from the public [IEEE OUI registry](https://standards-oui.ieee.org/).

## License

[MIT](LICENSE)
