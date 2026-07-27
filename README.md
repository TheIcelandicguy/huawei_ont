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
- **prunes** a tracker once its device has been offline for a short grace
  period (so the list reflects what's actually on the network),
- **keeps any tracker you rename** — a rename pins the device, which then
  behaves as a normal presence sensor (`home` / `not_home`) instead of being
  pruned.

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

## Credits

- MAC-vendor data from the public [IEEE OUI registry](https://standards-oui.ieee.org/).

## License

[MIT](LICENSE)
