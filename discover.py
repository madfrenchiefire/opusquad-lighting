#!/usr/bin/env python3
"""
Art-Net node discovery — sends an ArtPoll broadcast and prints every
device that replies, including its IP, name, and universe count.

Usage:
    python discover.py

The PKnight ARS2048B will appear as something like:
    192.168.x.x  or  2.0.0.1  — "ARS2048B"  ports=4

Once you have the IP, paste it into config.yaml under artnet.host.
"""

import socket
import struct
import time

ARTNET_PORT = 6454
ARTPOLL_OPCODE = 0x2000
ARTPOLL_REPLY_OPCODE = 0x2100


def build_artpoll() -> bytes:
    pkt = bytearray()
    pkt += b"Art-Net\x00"
    pkt += struct.pack("<H", ARTPOLL_OPCODE)
    pkt += struct.pack(">H", 14)          # ProtVer
    pkt += bytes([0b00000110, 0])         # TalkToMe: send reply on change, unicast replies
    pkt += bytes([0x00])                  # Priority (all)
    return bytes(pkt)


def parse_artpoll_reply(data: bytes, addr: tuple) -> dict | None:
    if len(data) < 207:
        return None
    if not data.startswith(b"Art-Net\x00"):
        return None
    opcode = struct.unpack_from("<H", data, 8)[0]
    if opcode != ARTPOLL_REPLY_OPCODE:
        return None

    ip = ".".join(str(data[10 + i]) for i in range(4))
    port = struct.unpack_from("<H", data, 14)[0]
    short_name = data[26:44].rstrip(b"\x00").decode("ascii", errors="replace")
    long_name = data[44:108].rstrip(b"\x00").decode("ascii", errors="replace")
    num_ports = struct.unpack_from("<H", data, 173)[0]

    return {
        "ip": ip,
        "port": port,
        "short_name": short_name.strip(),
        "long_name": long_name.strip(),
        "num_ports": num_ports,
        "sender": addr[0],
    }


def discover(timeout: float = 3.0) -> list[dict]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.2)
    sock.bind(("", ARTNET_PORT))

    poll = build_artpoll()
    # Send to both Art-Net default subnet and local broadcast
    for dest in ("2.255.255.255", "255.255.255.255"):
        try:
            sock.sendto(poll, (dest, ARTNET_PORT))
        except OSError:
            pass

    found: dict[str, dict] = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(1024)
            info = parse_artpoll_reply(data, addr)
            if info:
                found[info["sender"]] = info
        except socket.timeout:
            continue
    sock.close()
    return list(found.values())


if __name__ == "__main__":
    print("Scanning for Art-Net nodes (3 seconds)...\n")
    nodes = discover()

    if not nodes:
        print("No Art-Net nodes found.")
        print("\nTroubleshooting:")
        print("  • Make sure your PC and the ARS2048B are on the same network")
        print("  • If the node is on the 2.x.x.x subnet, set your PC adapter")
        print("    to a static IP like 2.0.0.2 / 255.0.0.0")
        print("  • Try pinging 2.0.0.1 first to confirm basic connectivity")
    else:
        print(f"Found {len(nodes)} node(s):\n")
        for n in nodes:
            print(f"  IP:    {n['sender']}")
            print(f"  Name:  {n['short_name']}  ({n['long_name']})")
            print(f"  Ports: {n['num_ports']}")
            print(f"  → Add this to config.yaml:  host: \"{n['sender']}\"")
            print()
