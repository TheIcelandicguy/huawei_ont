"""Talk to the ONT without Home Assistant: log in, fetch once, print it.

Run from the repo root:
    python tools/test_connection.py <password> [host] [username]

A development aid, not part of the integration. It lives outside
custom_components/ so it never ships in a release or lands on a live
config. Log out of the router's web UI first: the ONT allows one admin
session, and this script takes it.
"""

import sys
from pathlib import Path

# api.py has no Home Assistant imports, so it loads straight off disk.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "custom_components" / "huawei_ont"))
from api import HuaweiOntApi, HuaweiOntConnectionError  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/test_connection.py <password> [host] [username]")
        sys.exit(1)

    password = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else "192.168.0.1"
    username = sys.argv[3] if len(sys.argv) > 3 else "admin"

    api = HuaweiOntApi(host, username, password)

    print(f"Connecting to {host} as {username}...")
    ok = api.authenticate()
    print(f"Auth: {'OK' if ok else 'FAILED'}")
    if not ok:
        api.close()
        return

    print("\nFetching router data...")
    try:
        data = api.get_router_data()
    except HuaweiOntConnectionError as err:
        print(f"Failed: {err}")
        api.close()
        sys.exit(1)

    print(f"\n--- Device Info ---")
    print(f"  Model:    {data.model}")
    print(f"  Serial:   {data.serial_number}")
    print(f"  SW:       {data.software_version}")
    print(f"  HW:       {data.hardware_version}")
    print(f"  MAC:      {data.mac_address}")
    print(f"  CPU:      {data.cpu_usage}%")
    print(f"  Memory:   {data.memory_usage}%")

    print(f"\n--- WAN ---")
    print(f"  Status:   {data.wan_status}")
    print(f"  IP:       {data.wan_ip}")
    print(f"  Uptime:   {data.wan_uptime}s")
    print(f"  ONT:      {data.ont_state}")

    print(f"\n--- Traffic ---")
    print(f"  Sent:     {data.bytes_sent:,} bytes")
    print(f"  Received: {data.bytes_received:,} bytes")
    print(f"  Pkt Sent: {data.packets_sent:,}")
    print(f"  Pkt Recv: {data.packets_received:,}")

    print(f"\n--- Optical ---")
    print(f"  Temp:     {data.optical_temperature} C")
    print(f"  Voltage:  {data.optical_voltage} mV")

    print(f"\n--- Connected Devices ({data.connected_device_count} online) ---")
    for d in data.devices[:15]:
        status = "+" if d.status == "Online" else "-"
        name = d.hostname if d.hostname and d.hostname != "--" else d.mac_address
        print(f"  [{status}] {name:40s} {d.ip_address:16s} {d.interface}")

    if len(data.devices) > 15:
        print(f"  ... and {len(data.devices) - 15} more")

    api.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
