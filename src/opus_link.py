"""
Pioneer Pro DJ Link listener for the Opus Quad.

The Opus Quad broadcasts packets on UDP ports 50001 (beats) and 50002 (status)
from players 9-12 (internal decks 1-4). We listen passively — no pairing needed.

NOTE on audio data: Pro DJ Link carries NO audio — only metadata (BPM, beat
timing, play state, track info). Bass/frequency reactive lighting would require
a separate audio capture path (e.g. USB audio interface on the mixer output).

Beat packet (type 0x28) format — from Deep Symmetry / kyleawayan analysis:
  Bytes 0x00-0x09: "Qspt1WmJOL" header
  Byte  0x0a:      packet type
  Byte  0x21:      player number (9-12 for Opus Quad decks 1-4)
  Bytes 0x58-0x59: BPM * 100 (big-endian uint16)
  Byte  0x5c:      beat number within bar (1-4)

Status packet (type 0x0a) format — player state broadcast on port 50002:
  Byte  0x0a:      packet type
  Byte  0x21:      player number
  Byte  0x89:      play state flags  (bit 0x40 = playing, 0x80 = paused/cued)
  Bytes 0x92-0x93: BPM * 100 (big-endian uint16) in status packets
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
STATUS_PACKET_TYPE = 0x0a   # DeviceStatus — carries play/pause state

# Status packet play-state byte (offset 0x89), bit masks
PLAY_FLAG = 0x40    # deck is actively playing
PAUSE_FLAG = 0x80   # deck is paused or cued

# How long without a status packet before we consider a deck gone (seconds)
DECK_TIMEOUT = 10.0


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
    Listens for Pro DJ Link packets from the Opus Quad on two sockets:
      - port 50001: beat packets (BPM + beat-in-bar)
      - port 50002: status packets (play/pause state)
    """

    def __init__(self, local_ip: str = "", beat_port: int = BEAT_PORT,
                 status_port: int = STATUS_PORT):
        self._local_ip = local_ip
        self._beat_port = beat_port
        self._status_port = status_port
        self._running = False
        self._threads: list[threading.Thread] = []
        self._beat_callbacks: list[Callable[[BeatInfo], None]] = []
        self._status_callbacks: list[Callable[[PlayerStatus], None]] = []

        # Latest known state per deck (keys are deck numbers 1-4)
        self.deck_bpm: dict[int, float] = {}
        self.deck_playing: dict[int, bool] = {}       # True = actively playing
        self.deck_last_seen: dict[int, float] = {}    # monotonic timestamp
        self.last_beat: Optional[BeatInfo] = None

    def on_beat(self, callback: Callable[[BeatInfo], None]) -> None:
        self._beat_callbacks.append(callback)

    def on_status(self, callback: Callable[[PlayerStatus], None]) -> None:
        self._status_callbacks.append(callback)

    @property
    def master_bpm(self) -> Optional[float]:
        """BPM of the first playing deck, or None."""
        for deck in range(1, 5):
            if self.deck_playing.get(deck) and deck in self.deck_bpm:
                return self.deck_bpm[deck]
        if self.deck_bpm:
            return next(iter(self.deck_bpm.values()))
        return None

    @property
    def all_paused(self) -> bool:
        """
        True when the Opus Quad is connected but every deck is paused/stopped.
        Returns False if we have no signal at all (so fallback show runs instead).
        """
        now = time.monotonic()
        active_decks = [
            d for d, t in self.deck_last_seen.items()
            if now - t < DECK_TIMEOUT
        ]
        if not active_decks:
            return False  # no signal — can't say they're paused
        return not any(self.deck_playing.get(d, False) for d in active_decks)

    def start(self) -> None:
        self._running = True
        for port, name in [(self._beat_port, "beat"), (self._status_port, "status")]:
            t = threading.Thread(
                target=self._listen, args=(port,),
                daemon=True, name=f"djlink-{name}"
            )
            t.start()
            self._threads.append(t)
        log.info("DJ Link listener started (beat=%d, status=%d)", self._beat_port, self._status_port)

    def stop(self) -> None:
        self._running = False

    def _listen(self, port: int) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("", port))
        except OSError as e:
            log.error("Cannot bind to DJ Link port %d: %s", port, e)
            return

        log.info("Listening on UDP:%d", port)
        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                self._handle_packet(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                log.debug("Packet error on port %d: %s", port, e)
        sock.close()

    def _handle_packet(self, data: bytes, addr: tuple) -> None:
        if len(data) < 0x0b:
            return
        if not data.startswith(PDJL_HEADER):
            return

        pkt_type = data[0x0a]

        if pkt_type == BEAT_PACKET_TYPE and len(data) >= 0x68:
            self._parse_beat(data)
        elif pkt_type == STATUS_PACKET_TYPE and len(data) >= 0x9a:
            self._parse_status(data)

    def _parse_beat(self, data: bytes) -> None:
        try:
            player = data[0x21]
            if not (9 <= player <= 12):
                return

            bpm_raw = struct.unpack_from(">H", data, 0x58)[0]
            bpm = bpm_raw / 100.0
            beat_number = data[0x5c]

            info = BeatInfo(player=player, beat_number=beat_number, bpm=bpm)
            deck = info.deck
            self.deck_bpm[deck] = bpm
            self.deck_playing[deck] = True
            self.deck_last_seen[deck] = info.timestamp
            self.last_beat = info

            log.debug("Beat: deck=%d bpm=%.1f beat=%d", deck, bpm, beat_number)
            for cb in self._beat_callbacks:
                try:
                    cb(info)
                except Exception as e:
                    log.error("Beat callback error: %s", e)
        except (struct.error, IndexError) as e:
            log.debug("Failed to parse beat packet: %s", e)

    def _parse_status(self, data: bytes) -> None:
        try:
            player = data[0x21]
            if not (9 <= player <= 12):
                return

            deck = player - 8
            play_flags = data[0x89]
            is_playing = bool(play_flags & PLAY_FLAG)

            bpm_raw = struct.unpack_from(">H", data, 0x92)[0]
            bpm = bpm_raw / 100.0 if bpm_raw else self.deck_bpm.get(deck, 0.0)

            now = time.monotonic()
            prev_playing = self.deck_playing.get(deck)
            self.deck_playing[deck] = is_playing
            self.deck_last_seen[deck] = now
            if bpm > 0:
                self.deck_bpm[deck] = bpm

            if prev_playing != is_playing:
                state = "playing" if is_playing else "paused"
                log.info("Deck %d %s (flags=0x%02x)", deck, state, play_flags)

            status = PlayerStatus(
                player=player, bpm=bpm, is_playing=is_playing,
                pitch=1.0, track_number=0,
            )
            for cb in self._status_callbacks:
                try:
                    cb(status)
                except Exception as e:
                    log.error("Status callback error: %s", e)
        except (struct.error, IndexError) as e:
            log.debug("Failed to parse status packet: %s", e)
