"""Advertises the LAN demo host on the local network (mDNS / DNS-SD): service type _spinco-ta-demo._tcp, with the host's PRIVATE IPv4 addresses and the backend port.
Nothing secret is advertised (no password, no token, no Drive id). Runs until stopped (Ctrl+C / the launcher stops it).
usage: python demo_advertise.py --port 47831 [--name SpincoDemo-<hostname>]"""
import argparse
import socket
import sys
import time

import ifaddr
from zeroconf import ServiceInfo, Zeroconf


def private_ipv4():
    out = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            a = ip.ip
            parts = [int(x) for x in a.split(".")]
            if parts[0] == 10 or (parts[0] == 172 and 16 <= parts[1] <= 31) or (parts[0] == 192 and parts[1] == 168):
                out.append(a)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name", default="SpincoDemo-" + socket.gethostname())
    args = ap.parse_args()
    addresses = private_ipv4()
    if not addresses:
        print("No private IPv4 address found: is this laptop connected to the Wi-Fi?", file=sys.stderr)
        return 2
    info = ServiceInfo(
        "_spinco-ta-demo._tcp.local.",
        f"{args.name}._spinco-ta-demo._tcp.local.",
        addresses=[socket.inet_aton(a) for a in addresses],
        port=args.port,
        properties={"v": "1", "kind": "demo", "app": "spinco-ta-audit"},
        server=f"{socket.gethostname()}.local.",
    )
    zc = Zeroconf()
    zc.register_service(info)
    print(f"advertising {args.name} on {', '.join(addresses)} port {args.port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        zc.unregister_service(info)
        zc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
