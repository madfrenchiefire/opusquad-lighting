"""
Minimal Art-Net DMX sender (Art-DMX packet, UDP port 6454).

Art-Net is a simple UDP protocol — no library required for basic output.
We implement ArtDmx packets directly for maximum compatibility.
"""

import socket
import struct
import logging

log = logging.getLogger(__name__)

ARTNET_PORT = 6454
ARTDMX_OPCODE = 0x5000  # OpDmx


class ArtNetSender:
    def __init__(self, host: str, universe: int = 0, port: int = ARTNET_PORT):
        self._host = host
        self._universe = universe
        self._port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._sequence = 0
        self._dmx = bytearray(512)

    def set_channel(self, channel: int, value: int) -> None:
        """Set a single DMX channel (0-based index)."""
        if 0 <= channel < 512:
            self._dmx[channel] = max(0, min(255, value))

    def set_channels(self, start: int, values: list[int]) -> None:
        """Set consecutive channels starting at start (0-based)."""
        for i, v in enumerate(values):
            idx = start + i
            if 0 <= idx < 512:
                self._dmx[idx] = max(0, min(255, v))

    def send(self) -> None:
        """Transmit the current DMX frame as an ArtDmx packet."""
        self._sequence = (self._sequence + 1) % 256
        if self._sequence == 0:
            self._sequence = 1  # 0 means "ignore sequence"

        # ArtDmx packet structure
        # ID (8 bytes) + OpCode (2) + ProtVer (2) + Sequence (1) + Physical (1)
        # + Universe (2, little-endian) + Length (2, big-endian) + Data (512)
        packet = bytearray()
        packet += b"Art-Net\x00"                                    # ID
        packet += struct.pack("<H", ARTDMX_OPCODE)                  # OpCode (LE)
        packet += struct.pack(">H", 14)                             # ProtVer (BE) = 14
        packet += bytes([self._sequence, 0])                        # Sequence, Physical
        packet += struct.pack("<H", self._universe & 0x7FFF)        # Universe (LE)
        packet += struct.pack(">H", 512)                            # Length (BE)
        packet += bytes(self._dmx)                                  # DMX data

        try:
            self._sock.sendto(packet, (self._host, self._port))
        except OSError as e:
            log.error("Art-Net send error: %s", e)

    def blackout(self) -> None:
        self._dmx = bytearray(512)
        self.send()

    def close(self) -> None:
        self.blackout()
        self._sock.close()
