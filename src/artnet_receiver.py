"""
Art-Net DMX input receiver.

The PKnight ARS2048B is bidirectional — any port can be configured as a
DMX INPUT in the node's web UI. When a physical DMX controller is plugged
into that port, the node re-broadcasts the received DMX as standard ArtDmx
UDP packets on port 6454, exactly like any other Art-Net source.

This receiver listens on port 6454 for those inbound ArtDmx packets and
exposes the latest DMX channel values. You can then map faders / buttons
from the physical controller to show selection, brightness, color, etc.

Setup on the ARS2048B web UI:
  1. Open the node's web page (browse to its IP address).
  2. Find Port 3 (or whichever port your controller is plugged into).
  3. Set that port's direction to "Input" (instead of "Output").
  4. Set the Input Universe to match artnet_receiver.universe (default 2).
  5. Plug your DMX controller into that port's XLR.

The incoming DMX values will then appear in ArtNetReceiver.channels[].
"""

import socket
import struct
import threading
import logging
from typing import Callable

log = logging.getLogger(__name__)

ARTNET_PORT = 6454
ARTDMX_OPCODE = 0x5000


class ArtNetReceiver:
    """
    Listens for incoming ArtDmx packets (from a node's DMX-input port)
    and keeps the latest 512-channel frame for a given universe.

    Usage:
        recv = ArtNetReceiver(universe=2)
        recv.on_change(lambda ch, val: print(f"ch{ch+1} = {val}"))
        recv.start()
        ...
        brightness = recv.channels[0] / 255.0   # fader on ch1
    """

    def __init__(self, universe: int = 2, port: int = ARTNET_PORT):
        self.universe = universe
        self.channels: list[int] = [0] * 512   # latest DMX frame
        self._port = port
        self._running = False
        self._thread: threading.Thread | None = None
        self._callbacks: list[Callable[[int, int], None]] = []
        # Map of channel index → callback for targeted triggers
        self._channel_callbacks: dict[int, list[Callable[[int], None]]] = {}

    def on_change(self, callback: Callable[[int, int], None]) -> None:
        """Called with (channel_index, value) on every changed channel."""
        self._callbacks.append(callback)

    def on_channel(self, channel: int, callback: Callable[[int], None]) -> None:
        """Register a callback fired only when a specific channel changes."""
        self._channel_callbacks.setdefault(channel, []).append(callback)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._listen, daemon=True, name="artnet-receiver"
        )
        self._thread.start()
        log.info("Art-Net receiver listening on UDP:%d for universe %d", self._port, self.universe)

    def stop(self) -> None:
        self._running = False

    def get(self, channel: int, default: int = 0) -> int:
        """Get a channel value by 1-based channel number (as on the controller)."""
        if 1 <= channel <= 512:
            return self.channels[channel - 1]
        return default

    def get_float(self, channel: int) -> float:
        """Get a channel value as 0.0–1.0 float."""
        return self.get(channel) / 255.0

    def _listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        try:
            sock.bind(("", self._port))
        except OSError as e:
            log.error("Art-Net receiver cannot bind to port %d: %s", self._port, e)
            return

        while self._running:
            try:
                data, addr = sock.recvfrom(600)
                self._handle(data)
            except socket.timeout:
                continue
            except Exception as e:
                log.debug("Receiver error: %s", e)
        sock.close()

    def _handle(self, data: bytes) -> None:
        if len(data) < 18:
            return
        if not data.startswith(b"Art-Net\x00"):
            return

        opcode = struct.unpack_from("<H", data, 8)[0]
        if opcode != ARTDMX_OPCODE:
            return

        universe = struct.unpack_from("<H", data, 14)[0] & 0x7FFF
        if universe != self.universe:
            return

        length = struct.unpack_from(">H", data, 16)[0]
        dmx = data[18: 18 + length]
        if not dmx:
            return

        changed = []
        for i, v in enumerate(dmx):
            if i >= 512:
                break
            if self.channels[i] != v:
                self.channels[i] = v
                changed.append((i, v))

        for ch, val in changed:
            for cb in self._callbacks:
                try:
                    cb(ch, val)
                except Exception as e:
                    log.error("Receiver callback error: %s", e)
            for cb in self._channel_callbacks.get(ch, []):
                try:
                    cb(val)
                except Exception as e:
                    log.error("Channel callback error: %s", e)

        if changed:
            log.debug("DMX input universe %d: %d channels changed", self.universe, len(changed))


# ---------------------------------------------------------------------------
# Convenience: map a DMX input receiver to the lighting engine
# ---------------------------------------------------------------------------

def map_controller_to_engine(receiver: ArtNetReceiver, engine) -> None:
    """
    Wire a physical DMX controller (plugged into the ARS2048B input port)
    to the lighting engine using a simple default channel mapping:

      Ch 1  — Master brightness (fader)
      Ch 2  — Show select (0-36=beat_strobe, 37-72=color_cycle, 73-109=pulse,
                           110-145=chase, 146-182=fire, 183-218=rainbow,
                           219-255=thunderstorm)

    Call this after both receiver.start() and engine.start().
    You can replace this with any mapping you like.
    """
    from .shows import ALL_SHOWS
    show_names = [n for n in ALL_SHOWS if n != "idle"]
    num_shows = len(show_names)

    def on_brightness(val: int) -> None:
        engine.set_brightness(val / 255.0)

    def on_show_select(val: int) -> None:
        idx = min(int(val / 255 * num_shows), num_shows - 1)
        engine.switch_show(show_names[idx])

    receiver.on_channel(0, on_brightness)   # ch1 (0-based = 0)
    receiver.on_channel(1, on_show_select)  # ch2 (0-based = 1)

    log.info(
        "Controller mapped: ch1=brightness, ch2=show-select (%d shows). "
        "Universe=%d", num_shows, receiver.universe
    )
