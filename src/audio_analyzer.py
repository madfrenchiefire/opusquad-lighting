"""
Real-time audio analysis module for the Behringer XR-18 (ASIO, via sounddevice).

Provides frequency band energy, beat detection, and BPM estimation from live audio.
"""

import logging
import os
import queue
import threading
import time
from typing import Callable, List, Optional

# Ensure ASIO support is enabled before sounddevice is imported anywhere
if not os.environ.get("SD_ENABLE_ASIO"):
    os.environ["SD_ENABLE_ASIO"] = "1"

log = logging.getLogger(__name__)

try:
    import numpy as np
    import sounddevice as sd

    _SOUNDDEVICE_AVAILABLE = True
except ImportError as _sd_import_error:
    log.warning(
        "sounddevice or numpy is not installed — AudioAnalyzer will not be available. "
        "(%s)",
        _sd_import_error,
    )
    _SOUNDDEVICE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Device enumeration
# ---------------------------------------------------------------------------

def list_audio_devices() -> List[dict]:
    """Return all audio input devices detected by sounddevice.

    Each entry is a dict with keys:
        index       int   — sounddevice device index
        name        str   — device name
        channels    int   — maximum input channels
        samplerate  float — default sample rate
        is_asio     bool  — True when the name contains 'ASIO'
    """
    if not _SOUNDDEVICE_AVAILABLE:
        log.warning("list_audio_devices() called but sounddevice is not installed.")
        return []

    devices = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        devices.append(
            {
                "index": idx,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "samplerate": dev["default_samplerate"],
                "is_asio": "asio" in dev["name"].lower(),
            }
        )
    log.debug("Found %d audio input device(s).", len(devices))
    return devices


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _band_energy(magnitudes: "np.ndarray", freqs: "np.ndarray", lo: float, hi: float) -> float:
    """Return mean magnitude energy for a frequency band [lo, hi] Hz."""
    mask = (freqs >= lo) & (freqs <= hi)
    if not mask.any():
        return 0.0
    return float(np.mean(magnitudes[mask]))


def _ema(current: float, new: float, alpha: float) -> float:
    """Exponential moving average: alpha blends toward new value."""
    return current + alpha * (new - current)


# ---------------------------------------------------------------------------
# Worker thread that consumes FFT blocks off the queue
# ---------------------------------------------------------------------------

class _AnalysisWorker(threading.Thread):
    """Processes raw audio blocks from the sounddevice callback on a worker thread."""

    # Beat detection constants
    _BEAT_MIN_INTERVAL = 0.3       # seconds — ignore onsets closer than this
    _ONSET_THRESHOLD_FACTOR = 1.5  # energy spike must exceed N× recent average
    _RMS_HISTORY_SIZE = 43         # ~1 second at 1024/48000 ≈ 21 ms per block
    _BPM_HISTORY_SIZE = 16         # keep last N inter-beat intervals for BPM

    # Frequency bands (Hz)
    _BASS_LO, _BASS_HI = 20, 200
    _MID_LO, _MID_HI = 200, 2000
    _HIGH_LO, _HIGH_HI = 2000, 20000

    def __init__(self, blocksize: int, samplerate: int) -> None:
        super().__init__(name="AudioAnalysisWorker", daemon=True)
        self._blocksize = blocksize
        self._samplerate = samplerate
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._stop_event = threading.Event()
        self.beat_callbacks: List[Callable[[], None]] = []

        # Pre-compute FFT frequency bins
        self._freqs = np.fft.rfftfreq(blocksize, d=1.0 / samplerate)

        # Smoothed output values (protected by a lock so the main thread reads safely)
        self._lock = threading.Lock()
        self._bass = 0.0
        self._mid = 0.0
        self._high = 0.0
        self._energy = 0.0
        self._bpm = 0.0
        self._beat_confidence = 0.0

        # Beat detection state (worker-thread only — no lock needed)
        self._rms_history: List[float] = []
        self._last_beat_time: float = 0.0
        self._beat_intervals: List[float] = []

    # ------------------------------------------------------------------
    # Properties read by AudioAnalyzer (thread-safe)
    # ------------------------------------------------------------------

    @property
    def bass(self) -> float:
        with self._lock:
            return self._bass

    @property
    def mid(self) -> float:
        with self._lock:
            return self._mid

    @property
    def high(self) -> float:
        with self._lock:
            return self._high

    @property
    def energy(self) -> float:
        with self._lock:
            return self._energy

    @property
    def bpm(self) -> float:
        with self._lock:
            return self._bpm

    @property
    def beat_confidence(self) -> float:
        with self._lock:
            return self._beat_confidence

    # ------------------------------------------------------------------
    # Queue interface for the sounddevice callback
    # ------------------------------------------------------------------

    def submit(self, block: "np.ndarray") -> None:
        """Non-blocking enqueue.  Drops the block if the queue is full."""
        try:
            self._queue.put_nowait(block.copy())
        except queue.Full:
            log.debug("Analysis queue full — dropping audio block.")

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()
        # Unblock the get() call
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def run(self) -> None:
        log.debug("AudioAnalysisWorker started.")
        while not self._stop_event.is_set():
            try:
                block = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if block is None:
                break

            self._process(block)

        log.debug("AudioAnalysisWorker stopped.")

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    def _process(self, block: "np.ndarray") -> None:
        # Mix to mono if multi-channel
        if block.ndim > 1:
            mono = block.mean(axis=1)
        else:
            mono = block.astype(np.float32)

        # --- RMS energy ---
        rms = float(np.sqrt(np.mean(mono ** 2)))
        rms_norm = min(rms, 1.0)

        # --- FFT ---
        windowed = mono * np.hanning(len(mono))
        spectrum = np.abs(np.fft.rfft(windowed))
        # Normalise spectrum magnitude to 0-1-ish range
        mag_norm = spectrum / (len(mono) / 2.0)

        bass_raw = min(_band_energy(mag_norm, self._freqs, self._BASS_LO, self._BASS_HI) * 10.0, 1.0)
        mid_raw  = min(_band_energy(mag_norm, self._freqs, self._MID_LO,  self._MID_HI)  * 10.0, 1.0)
        high_raw = min(_band_energy(mag_norm, self._freqs, self._HIGH_LO, self._HIGH_HI) * 10.0, 1.0)

        # --- Beat detection ---
        beat_fired, new_bpm, new_confidence = self._detect_beat(rms_norm)

        # --- Smooth and publish ---
        with self._lock:
            self._bass  = _ema(self._bass,  bass_raw,      0.3)
            self._mid   = _ema(self._mid,   mid_raw,       0.3)
            self._high  = _ema(self._high,  high_raw,      0.3)
            self._energy = _ema(self._energy, rms_norm,    0.1)
            self._bpm = new_bpm
            self._beat_confidence = new_confidence

        if beat_fired:
            self._fire_beat()

    def _detect_beat(self, rms: float):
        """Sliding-window onset detector.  Returns (beat_fired, bpm, confidence)."""
        self._rms_history.append(rms)
        if len(self._rms_history) > self._RMS_HISTORY_SIZE:
            self._rms_history.pop(0)

        if len(self._rms_history) < 4:
            return False, self._bpm, self._beat_confidence

        recent_avg = float(np.mean(self._rms_history[:-1]))
        now = time.monotonic()
        elapsed_since_last = now - self._last_beat_time

        beat_fired = False
        if (
            rms > self._ONSET_THRESHOLD_FACTOR * recent_avg
            and rms > 0.02  # ignore near-silence
            and elapsed_since_last >= self._BEAT_MIN_INTERVAL
        ):
            beat_fired = True
            if self._last_beat_time > 0:
                self._beat_intervals.append(elapsed_since_last)
                if len(self._beat_intervals) > self._BPM_HISTORY_SIZE:
                    self._beat_intervals.pop(0)
            self._last_beat_time = now

        # Estimate BPM and confidence from interval history
        bpm = self._bpm
        confidence = self._beat_confidence
        if len(self._beat_intervals) >= 2:
            avg_interval = float(np.mean(self._beat_intervals))
            std_interval = float(np.std(self._beat_intervals))
            if avg_interval > 0:
                bpm = 60.0 / avg_interval
                cv = std_interval / avg_interval
                confidence = max(0.0, min(1.0, 1.0 - cv))

        return beat_fired, bpm, confidence

    def _fire_beat(self) -> None:
        for cb in self.beat_callbacks:
            try:
                cb()
            except Exception:
                log.exception("Exception in beat callback.")


# ---------------------------------------------------------------------------
# Public AudioAnalyzer class
# ---------------------------------------------------------------------------

class AudioAnalyzer:
    """Real-time audio analyser backed by sounddevice (ASIO-capable).

    Usage::

        analyzer = AudioAnalyzer(device_index=3, channels=2)
        analyzer.on_beat(lambda: print("beat!"))
        analyzer.start()
        ...
        print(analyzer.bass, analyzer.bpm)
        ...
        analyzer.stop()
    """

    available: bool = _SOUNDDEVICE_AVAILABLE

    def __init__(
        self,
        device_index: int,
        channels: int = 2,
        samplerate: int = 48000,
        blocksize: int = 1024,
    ) -> None:
        if not _SOUNDDEVICE_AVAILABLE:
            raise RuntimeError(
                "sounddevice is not installed; use AudioAnalyzerStub instead."
            )

        self._device_index = device_index
        self._channels = channels
        self._samplerate = samplerate
        self._blocksize = blocksize

        self._worker: Optional[_AnalysisWorker] = None
        self._stream: Optional["sd.InputStream"] = None
        self._running = False
        self._pending_beat_callbacks: List[Callable[[], None]] = []

        log.debug(
            "AudioAnalyzer created: device=%d channels=%d samplerate=%d blocksize=%d",
            device_index,
            channels,
            samplerate,
            blocksize,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the audio stream and begin analysis."""
        if self._running:
            log.warning("AudioAnalyzer.start() called while already running.")
            return

        self._worker = _AnalysisWorker(self._blocksize, self._samplerate)

        # Wire up any callbacks registered before start()
        for cb in self._pending_beat_callbacks:
            self._worker.beat_callbacks.append(cb)

        self._worker.start()

        self._stream = sd.InputStream(
            device=self._device_index,
            channels=self._channels,
            samplerate=self._samplerate,
            blocksize=self._blocksize,
            dtype="float32",
            callback=self._audio_callback,
        )
        self._stream.start()
        self._running = True
        log.info(
            "AudioAnalyzer started on device %d (%s Hz, %d ch).",
            self._device_index,
            self._samplerate,
            self._channels,
        )

    def stop(self) -> None:
        """Stop the stream and worker thread."""
        if not self._running:
            return

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.exception("Error closing audio stream.")
            self._stream = None

        if self._worker is not None:
            self._worker.stop()
            self._worker.join(timeout=2.0)
            self._worker = None

        self._running = False
        log.info("AudioAnalyzer stopped.")

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_beat(self, callback: Callable[[], None]) -> None:
        """Register a callable to be invoked on each detected beat.

        Safe to call before or after start().  The callback fires from
        the worker thread — keep it short and non-blocking.
        """
        if self._worker is not None:
            self._worker.beat_callbacks.append(callback)
        else:
            self._pending_beat_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def bass(self) -> float:
        """Energy in 20-200 Hz, 0.0-1.0."""
        return self._worker.bass if self._worker else 0.0

    @property
    def mid(self) -> float:
        """Energy in 200-2000 Hz, 0.0-1.0."""
        return self._worker.mid if self._worker else 0.0

    @property
    def high(self) -> float:
        """Energy in 2000-20000 Hz, 0.0-1.0."""
        return self._worker.high if self._worker else 0.0

    @property
    def energy(self) -> float:
        """Overall RMS level, 0.0-1.0."""
        return self._worker.energy if self._worker else 0.0

    @property
    def bpm(self) -> float:
        """Estimated BPM from onset detection. 0.0 if unknown."""
        return self._worker.bpm if self._worker else 0.0

    @property
    def beat_confidence(self) -> float:
        """Confidence in the BPM estimate, 0.0-1.0."""
        return self._worker.beat_confidence if self._worker else 0.0

    # ------------------------------------------------------------------
    # sounddevice callback (runs in the audio thread — MUST be fast)
    # ------------------------------------------------------------------

    def _audio_callback(
        self,
        indata: "np.ndarray",
        frames: int,
        time_info,
        status: "sd.CallbackFlags",
    ) -> None:
        if status:
            log.debug("Audio callback status: %s", status)
        if self._worker is not None:
            self._worker.submit(indata)


# ---------------------------------------------------------------------------
# Stub — drop-in replacement when sounddevice is unavailable
# ---------------------------------------------------------------------------

class AudioAnalyzerStub:
    """No-op drop-in for AudioAnalyzer used when sounddevice is not available
    or no device is selected.  All numeric properties return 0.0.
    """

    available: bool = False

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        log.debug("AudioAnalyzerStub instantiated (no-op mode).")

    def start(self) -> None:
        log.debug("AudioAnalyzerStub.start() — no-op.")

    def stop(self) -> None:
        log.debug("AudioAnalyzerStub.stop() — no-op.")

    def on_beat(self, callback: Callable[[], None]) -> None:
        log.debug("AudioAnalyzerStub.on_beat() — callback will never fire.")

    @property
    def bass(self) -> float:
        return 0.0

    @property
    def mid(self) -> float:
        return 0.0

    @property
    def high(self) -> float:
        return 0.0

    @property
    def energy(self) -> float:
        return 0.0

    @property
    def bpm(self) -> float:
        return 0.0

    @property
    def beat_confidence(self) -> float:
        return 0.0
