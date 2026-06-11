"""
OSC server for the lighting engine.

Uses the `python-osc` library (``pip install python-osc``).  If the library is
not installed the server becomes a graceful no-op so the rest of the engine can
keep running without it.

Registered address patterns
----------------------------
/lighting/show      <string>           -- switch show by name
/lighting/brightness <float>           -- set master brightness 0.0-1.0
/lighting/blackout  <int>              -- 1 = blackout on, 0 = off
/lighting/bpm       <float>            -- override fallback BPM
/lighting/color     <int> <int> <int>  -- set all fixtures to R,G,B (0-255)
/lighting/scene     <string>           -- trigger scene by name
/lighting/tap       (no args)          -- tap tempo signal

Usage::

    server = create_osc_server(engine, host="0.0.0.0", port=8000)
    server.start()
    ...
    server.stop()
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

try:
    from pythonosc import dispatcher as _dispatcher_mod  # type: ignore
    from pythonosc import osc_server as _osc_server_mod   # type: ignore
    _OSC_AVAILABLE = True
except ImportError:  # pragma: no cover
    _OSC_AVAILABLE = False


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_osc_server(
    engine,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> "OscServer":
    """Create and return a configured :class:`OscServer` bound to *engine*."""
    return OscServer(engine, host=host, port=port)


# ---------------------------------------------------------------------------
# OscServer
# ---------------------------------------------------------------------------

class OscServer:
    """Listens for OSC messages and drives the lighting engine.

    Parameters
    ----------
    engine:
        The lighting engine instance.  Expected attributes / methods:

        * ``brightness`` (float property)
        * ``blackout`` (bool property)
        * ``fallback_bpm`` (float property, optional)
        * ``select_show(name)``
        * ``tap_tempo()``
        * ``set_all_color(r, g, b)`` (optional)
        * ``scene_manager.trigger(name)`` (optional)

    host:
        Interface to bind to.  ``"0.0.0.0"`` listens on all interfaces.
    port:
        UDP port number (default 8000).
    """

    def __init__(
        self,
        engine,
        host: str = "0.0.0.0",
        port: int = 8000,
    ) -> None:
        self._engine = engine
        self._host = host
        self._port = port
        self._server = None
        self._thread: threading.Thread | None = None

        if not _OSC_AVAILABLE:
            logger.warning("python-osc is not installed — OSC server unavailable.")
        self.available: bool = _OSC_AVAILABLE

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Build the OSC dispatcher, open the UDP socket, and start a daemon thread."""
        if not self.available:
            return

        disp = _dispatcher_mod.Dispatcher()
        engine = self._engine

        # /lighting/show <string>
        def _show(address, show_name: str, *args) -> None:
            logger.debug("OSC %s %r", address, show_name)
            try:
                engine.select_show(show_name)
            except Exception as exc:
                logger.error("OSC show error: %s", exc)

        disp.map("/lighting/show", _show)

        # /lighting/brightness <float>
        def _brightness(address, value: float, *args) -> None:
            logger.debug("OSC %s %s", address, value)
            engine.brightness = max(0.0, min(1.0, float(value)))

        disp.map("/lighting/brightness", _brightness)

        # /lighting/blackout <int>
        def _blackout(address, value: int, *args) -> None:
            logger.debug("OSC %s %s", address, value)
            engine.blackout = bool(int(value))

        disp.map("/lighting/blackout", _blackout)

        # /lighting/bpm <float>
        def _bpm(address, value: float, *args) -> None:
            logger.debug("OSC %s %s", address, value)
            if hasattr(engine, "fallback_bpm"):
                engine.fallback_bpm = float(value)
            else:
                logger.warning("Engine has no fallback_bpm attribute; OSC /lighting/bpm ignored.")

        disp.map("/lighting/bpm", _bpm)

        # /lighting/color <int> <int> <int>
        def _color(address, r: int, g: int, b: int, *args) -> None:
            logger.debug("OSC %s r=%s g=%s b=%s", address, r, g, b)
            if hasattr(engine, "set_all_color"):
                engine.set_all_color(int(r), int(g), int(b))
            else:
                logger.warning("Engine has no set_all_color method; OSC /lighting/color ignored.")

        disp.map("/lighting/color", _color)

        # /lighting/scene <string>
        def _scene(address, scene_name: str, *args) -> None:
            logger.debug("OSC %s %r", address, scene_name)
            if hasattr(engine, "scene_manager") and engine.scene_manager is not None:
                try:
                    engine.scene_manager.trigger(scene_name)
                except Exception as exc:
                    logger.error("OSC scene error: %s", exc)
            else:
                logger.warning("Engine has no scene_manager; OSC /lighting/scene ignored.")

        disp.map("/lighting/scene", _scene)

        # /lighting/tap  (no arguments expected)
        def _tap(address, *args) -> None:
            logger.debug("OSC %s", address)
            engine.tap_tempo()

        disp.map("/lighting/tap", _tap)

        # Catch-all for unrecognised addresses
        def _default(address, *args) -> None:
            logger.debug("OSC unhandled: %s %s", address, args)

        disp.set_default_handler(_default)

        try:
            self._server = _osc_server_mod.ThreadingOSCUDPServer(
                (self._host, self._port), disp
            )
        except Exception as exc:
            logger.error(
                "Failed to bind OSC server to %s:%s — %s", self._host, self._port, exc
            )
            self.available = False
            return

        logger.info("OSC server listening on %s:%s", self._host, self._port)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="osc-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the OSC server and join the listener thread."""
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
