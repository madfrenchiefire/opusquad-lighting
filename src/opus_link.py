"""
Pioneer Pro DJ Link listener for the Opus Quad.

The Opus Quad broadcasts beat packets on UDP port 50001 from players 9-12
(internal decks 1-4). We listen passively — no pairing required.

Beat packet format (from Deep Symmetry / kyleawayan analysis):
  Bytes 0x00-0x0a: "Qspt1WmJOL" header
  Byte  0x04:      packet type (0x28 = beat)
  Bytes 0x58-0x59: BPM * 100 (big-endian uint16)
  Byte  0x5c:      beat number within bar (1-4)
  Byte  0x21:      player number (9-12 for Opus Quad decks 1-4)
"""

import socket
import struct
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)

BEAT_PORT = 50001
STATUS_PORT = 50002
ANNOUNCE_PORT = 50000

PDJL_HEADER = b"\x51\x73\x70\x74\x31\x57\x6d\x4a\x4f\x4c"  # "Qspt1WmJOL"
BEAT_PACKET_TYPE = 0x28
STATUS_PACKET_TYPE = 0x29


@dataclass
class BeatInfo:
    player: int          # 9-12 → deck 1-4
    beat_number: int     # 1-4 within bar
    bpm: float
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def deck(self) -> int:
        return self.player - 8  # convert to 1-4


@dataclass
class PlayerStatus:
    player: int
    bpm: float
    is_playing: bool
    pitch: float         # 1.0 = no pitch change
    track_number: int
    timestamp: float = field(default_factory=time.monotonic)

    @property
    def deck(self) -> int:
        return self.player - 8


class OpusQuadLink:
    """
    Listens for Pro DJ Link packets from the Opus Quad and exposes
    beat/BPM info via callbacks and a shared state dict.
    """

    def __init__(self, local_ip: str = "", beat_port: int = BEAT_PORT):
        self._local_ip = local_ip
        self._beat_port = beat_port
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._beat_callbacks: list[Callable[[BeatInfo], None]] = []
        self._status_callbacks: list[Callable[[PlayerStatus], None]] = []

        # Latest known state per deck (deck 1-4 as keys)
        self.deck_bpm: dict[int, float] = {}
        self.deck_playing: dict[int, bool] = {}
        self.last_beat: Optional[BeatInfo] = None

    def on_beat(self, callback: Callable[[BeatInfo], None]) -> None:
        self._beat_callbacks.append(callback)

    def on_status(self, callback: Callable[[PlayerStatus], None]) -> None:
        self._status_callbacks.append(callback)

    @property
    def master_bpm(self) -> Optional[float]:
        """BPM of the first playing deck found, or None."""
        for deck in range(1, 5):
            if self.deck_playing.get(deck) and deck in self.deck_bpm:
                return self.deck_bpm[deck]
        if self.deck_bpm:
            return next(iter(self.deck_bpm.values()))
        return None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True, name="djlink-listener")
        self._thread.start()
        log.info("DJ Link listener started on port %d", self._beat_port)

    def stop(self) -> None:
        self._running = False

    def _listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("", self._beat_port))
        except OSError as e:
            log.error("Cannot bind to DJ Link port %d: %s", self._beat_port, e)
            return

        log.info("Listening for Opus Quad beat packets on UDP:%d", self._beat_port)
        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                self._handle_packet(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                log.debug("Packet error: %s", e)
        sock.close()

    def _handle_packet(self, data: bytes, addr: tuple) -> None:
        if len(data) < 0x68:
            return
        if not data.startswith(PDJL_HEADER):
            return

        pkt_type = data[0x0a]

        if pkt_type == BEAT_PACKET_TYPE:
            self._parse_beat(data, addr)

    def _parse_beat(self, data: bytes, addr: tuple) -> None:
        try:
            player = data[0x21]
            if not (9 <= player <= 12):
                return  # not an Opus Quad deck

            bpm_raw = struct.unpack_from(">H", data, 0x58)[0]
            bpm = bpm_raw / 100.0
            beat_number = data[0x5c]

            info = BeatInfo(player=player, beat_number=beat_number, bpm=bpm)
            deck = info.deck
            self.deck_bpm[deck] = bpm
            self.deck_playing[deck] = True
            self.last_beat = info

            log.debug("Beat: deck=%d bpm=%.1f beat=%d", deck, bpm, beat_number)
            for cb in self._beat_callbacks:
                try:
                    cb(info)
                except Exception as e:
                    log.error("Beat callback error: %s", e)
        except (struct.error, IndexError) as e:
            log.debug("Failed to parse beat packet: %s", e)
