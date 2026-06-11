"""Track metadata and per-show settings database."""

import logging
import sqlite3

logger = logging.getLogger(__name__)

_BPM_RULES: list[tuple[float, float, str]] = [
    (0.0,   80.0,  "fire"),
    (80.0,  100.0, "color_cycle"),
    (100.0, 120.0, "pulse"),
    (120.0, 135.0, "beat_strobe"),
    (135.0, 150.0, "chase"),
    (150.0, float("inf"), "rainbow"),
]


class TrackDatabase:
    def __init__(self, db_path: str = "data/tracks.db"):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                    track_id TEXT PRIMARY KEY,
                    title    TEXT NOT NULL DEFAULT '',
                    artist   TEXT NOT NULL DEFAULT '',
                    bpm      REAL NOT NULL DEFAULT 0.0,
                    key      TEXT NOT NULL DEFAULT '',
                    genre    TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS track_settings (
                    track_id   TEXT PRIMARY KEY REFERENCES tracks(track_id)
                               ON DELETE CASCADE,
                    show_name  TEXT NOT NULL DEFAULT '',
                    brightness REAL NOT NULL DEFAULT 1.0,
                    notes      TEXT NOT NULL DEFAULT ''
                );
            """)
        logger.debug("TrackDatabase initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Track CRUD
    # ------------------------------------------------------------------

    def upsert_track(
        self,
        track_id: str,
        title: str,
        artist: str,
        bpm: float,
        key: str = "",
        genre: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tracks (track_id, title, artist, bpm, key, genre)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(track_id) DO UPDATE SET"
                "   title=excluded.title,"
                "   artist=excluded.artist,"
                "   bpm=excluded.bpm,"
                "   key=excluded.key,"
                "   genre=excluded.genre",
                (track_id, title, artist, bpm, key, genre),
            )
        logger.info("Upserted track '%s' — %s / %s @ %.1f BPM", track_id, artist, title, bpm)

    def set_track_show(
        self, track_id: str, show_name: str, brightness: float = 1.0, notes: str = ""
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO track_settings (track_id, show_name, brightness, notes)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(track_id) DO UPDATE SET"
                "   show_name=excluded.show_name,"
                "   brightness=excluded.brightness,"
                "   notes=excluded.notes",
                (track_id, show_name, brightness, notes),
            )
        logger.info(
            "Set show for track '%s': show=%s brightness=%.2f", track_id, show_name, brightness
        )

    def get_track_settings(self, track_id: str) -> "dict | None":
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.track_id, t.title, t.artist, t.bpm, t.key, t.genre,"
                "       s.show_name, s.brightness, s.notes"
                " FROM tracks t"
                " LEFT JOIN track_settings s USING (track_id)"
                " WHERE t.track_id = ?",
                (track_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_tracks(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT t.track_id, t.title, t.artist, t.bpm, t.key, t.genre,"
                "       s.show_name, s.brightness, s.notes"
                " FROM tracks t"
                " LEFT JOIN track_settings s USING (track_id)"
                " ORDER BY t.artist, t.title"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Show suggestion
    # ------------------------------------------------------------------

    def suggest_show(self, bpm: float) -> str:
        """Rule-based show suggestion from BPM when no track-specific setting exists.

        < 80          -> fire
        80  – 100     -> color_cycle
        100 – 120     -> pulse
        120 – 135     -> beat_strobe
        135 – 150     -> chase
        > 150         -> rainbow
        """
        for low, high, show in _BPM_RULES:
            if low <= bpm < high:
                logger.debug("suggest_show(%.1f BPM) -> %s", bpm, show)
                return show
        # Fallback — should not be reached given the open upper bound.
        return "rainbow"
