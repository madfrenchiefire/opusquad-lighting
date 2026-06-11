"""
MIDI input handler for the lighting engine.

Uses the `mido` library with the `python-rtmidi` backend. Both are optional —
if either is missing the controller silently becomes unavailable and all public
methods are no-ops so the rest of the engine can keep running.

Usage::

    ctrl = MidiController()          # opens first available port
    ctrl.map_to_engine(engine)
    ctrl.start()
    ...
    ctrl.stop()
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import mido  # type: ignore
    _MIDO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _MIDO_AVAILABLE = False


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def list_midi_ports() -> list[str]:
    """Return a list of MIDI input port names, or [] if mido is not installed."""
    if not _MIDO_AVAILABLE:
        return []
    try:
        return mido.get_input_names()
    except Exception as exc:  # rtmidi not installed, no MIDI hardware, etc.
        logger.debug("Could not enumerate MIDI ports: %s", exc)
        return []


# ---------------------------------------------------------------------------
# MidiController
# ---------------------------------------------------------------------------

class MidiController:
    """Listens to a MIDI input port and dispatches CC/note events to callbacks.

    Parameters
    ----------
    port_name:
        Exact MIDI port name to open.  If *None* the first available port is
        used.  If *mido* / *python-rtmidi* are not installed the controller
        becomes a no-op and ``available`` is *False*.
    """

    def __init__(self, port_name: str | None = None) -> None:
        self._port_name: str | None = port_name
        self._port = None          # mido.ports.BaseInput once opened
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Mapping tables: (channel, number) -> list[Callable]
        self._cc_map:       dict[tuple[int, int], list[Callable[[int], None]]] = {}
        self._note_on_map:  dict[tuple[int, int], list[Callable[[int], None]]] = {}
        self._note_off_map: dict[tuple[int, int], list[Callable[[], None]]]    = {}

        if not _MIDO_AVAILABLE:
            logger.warning("mido is not installed — MIDI controller unavailable.")
        self.available: bool = _MIDO_AVAILABLE

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the MIDI port and begin listening in a daemon thread."""
        if not self.available:
            return

        ports = list_midi_ports()
        if not ports:
            logger.warning("No MIDI input ports found.")
            self.available = False
            return

        target = self._port_name if self._port_name else ports[0]
        if target not in ports:
            logger.warning("MIDI port %r not found. Available: %s", target, ports)
            self.available = False
            return

        try:
            self._port = mido.open_input(target)
        except Exception as exc:
            logger.error("Failed to open MIDI port %r: %s", target, exc)
            self.available = False
            return

        logger.info("Opened MIDI input port: %s", target)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._listen_loop,
            name="midi-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the listener thread and close the MIDI port."""
        self._stop_event.set()
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------
    # Mapping registration
    # ------------------------------------------------------------------

    def map_cc(
        self,
        channel: int,
        cc: int,
        callback: Callable[[int], None],
    ) -> None:
        """Register *callback* for Control Change messages on *channel* / *cc*.

        The callback receives the value (0-127).
        """
        self._cc_map.setdefault((channel, cc), []).append(callback)

    def map_note_on(
        self,
        channel: int,
        note: int,
        callback: Callable[[int], None],
    ) -> None:
        """Register *callback* for Note On messages.  Receives velocity (0-127)."""
        self._note_on_map.setdefault((channel, note), []).append(callback)

    def map_note_off(
        self,
        channel: int,
        note: int,
        callback: Callable[[], None],
    ) -> None:
        """Register *callback* for Note Off messages."""
        self._note_off_map.setdefault((channel, note), []).append(callback)

    # ------------------------------------------------------------------
    # Convenience: engine integration
    # ------------------------------------------------------------------

    def map_to_engine(self, engine) -> None:
        """Wire up a standard DJ/lighting controller layout to *engine*.

        Default mapping
        ---------------
        CC 7  (volume fader) -> engine.brightness      (0-127 -> 0.0-1.0)
        CC 1  (mod wheel)    -> engine.select_show     (0-127 mapped over available shows)
        Note 48 (C3)         -> engine.toggle_blackout
        Note 50 (D3)         -> engine.next_show
        Note 52 (E3)         -> engine.previous_show
        Note 53 (F3)         -> engine.tap_tempo
        """
        ch = 0  # MIDI channel 1 (0-indexed in mido)

        # CC 7 -> brightness
        def _brightness(value: int) -> None:
            engine.brightness = value / 127.0

        self.map_cc(ch, 7, _brightness)

        # CC 1 -> show select (maps 0-127 across available shows)
        def _show_select(value: int) -> None:
            shows = list(engine.shows) if hasattr(engine, "shows") else []
            if not shows:
                return
            idx = round(value / 127.0 * (len(shows) - 1))
            idx = max(0, min(idx, len(shows) - 1))
            engine.select_show(shows[idx])

        self.map_cc(ch, 1, _show_select)

        # Note 48 (C3) -> blackout toggle
        def _blackout(_velocity: int) -> None:
            engine.toggle_blackout()

        self.map_note_on(ch, 48, _blackout)

        # Note 50 (D3) -> next show
        def _next(_velocity: int) -> None:
            engine.next_show()

        self.map_note_on(ch, 50, _next)

        # Note 52 (E3) -> previous show
        def _prev(_velocity: int) -> None:
            engine.previous_show()

        self.map_note_on(ch, 52, _prev)

        # Note 53 (F3) -> tap tempo
        def _tap(_velocity: int) -> None:
            engine.tap_tempo()

        self.map_note_on(ch, 53, _tap)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _listen_loop(self) -> None:
        """Daemon thread: poll MIDI messages and dispatch to callbacks."""
        logger.debug("MIDI listener thread started.")
        while not self._stop_event.is_set():
            if self._port is None:
                break
            try:
                for msg in self._port.iter_pending():
                    self._dispatch(msg)
            except Exception as exc:
                logger.debug("MIDI read error: %s", exc)
                break
            # Yield briefly to avoid spinning at 100 % CPU
            self._stop_event.wait(timeout=0.005)
        logger.debug("MIDI listener thread stopped.")

    def _dispatch(self, msg) -> None:
        logger.debug("MIDI: %s", msg)

        if msg.type == "control_change":
            key = (msg.channel, msg.control)
            for cb in self._cc_map.get(key, []):
                try:
                    cb(msg.value)
                except Exception as exc:
                    logger.error("MIDI CC callback error: %s", exc)

        elif msg.type == "note_on" and msg.velocity > 0:
            key = (msg.channel, msg.note)
            for cb in self._note_on_map.get(key, []):
                try:
                    cb(msg.velocity)
                except Exception as exc:
                    logger.error("MIDI note_on callback error: %s", exc)

        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            key = (msg.channel, msg.note)
            for cb in self._note_off_map.get(key, []):
                try:
                    cb()
                except Exception as exc:
                    logger.error("MIDI note_off callback error: %s", exc)
