"""Standalone test — run: python test_connection.py <password> [host] [username]"""

import sys
from api import HuaweiOntApi


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_connection.py <password> [host] [username]")
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
    data = api.get_router_data()

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


main()
