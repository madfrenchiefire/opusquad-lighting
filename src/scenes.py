"""Scene and cue system backed by SQLite."""

import logging
import sqlite3
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class FixtureState:
    fixture_name: str
    r: int
    g: int
    b: int
    dimmer: int
    strobe: int


@dataclass
class Scene:
    id: int | None
    name: str
    fixture_states: list[FixtureState]
    fade_time: float = 0.0  # seconds to fade into this scene


@dataclass
class CueEntry:
    scene_name: str
    trigger: str  # "manual", "beat_1", "bar_1", "bar_4", "bar_8"
    auto_advance: bool = False
    advance_after_bars: int = 4


class SceneManager:
    def __init__(self, db_path: str = "data/scenes.db"):
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
                CREATE TABLE IF NOT EXISTS scenes (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    name      TEXT    NOT NULL UNIQUE,
                    fade_time REAL    NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS fixture_states (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    scene_id     INTEGER NOT NULL
                                 REFERENCES scenes(id) ON DELETE CASCADE,
                    fixture_name TEXT    NOT NULL,
                    r            INTEGER NOT NULL DEFAULT 0,
                    g            INTEGER NOT NULL DEFAULT 0,
                    b            INTEGER NOT NULL DEFAULT 0,
                    dimmer       INTEGER NOT NULL DEFAULT 0,
                    strobe       INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS cues (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    list_name          TEXT    NOT NULL,
                    position           INTEGER NOT NULL,
                    scene_name         TEXT    NOT NULL,
                    trigger            TEXT    NOT NULL DEFAULT 'manual',
                    auto_advance       INTEGER NOT NULL DEFAULT 0,
                    advance_after_bars INTEGER NOT NULL DEFAULT 4
                );

                CREATE INDEX IF NOT EXISTS idx_fixture_states_scene
                    ON fixture_states(scene_id);
                CREATE INDEX IF NOT EXISTS idx_cues_list
                    ON cues(list_name, position);
            """)
        logger.debug("SceneManager DB initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Scene CRUD
    # ------------------------------------------------------------------

    def save_scene(self, scene: Scene) -> "Scene":
        """Insert or replace a scene and return it with id set."""
        with self._connect() as conn:
            if scene.id is None:
                conn.execute(
                    "INSERT INTO scenes (name, fade_time) VALUES (?, ?)"
                    " ON CONFLICT(name) DO UPDATE SET fade_time=excluded.fade_time",
                    (scene.name, scene.fade_time),
                )
                row = conn.execute(
                    "SELECT id FROM scenes WHERE name = ?", (scene.name,)
                ).fetchone()
                scene_id = row["id"]
            else:
                conn.execute(
                    "INSERT INTO scenes (id, name, fade_time) VALUES (?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                    " fade_time=excluded.fade_time",
                    (scene.id, scene.name, scene.fade_time),
                )
                scene_id = scene.id

            conn.execute(
                "DELETE FROM fixture_states WHERE scene_id = ?", (scene_id,)
            )
            conn.executemany(
                "INSERT INTO fixture_states"
                " (scene_id, fixture_name, r, g, b, dimmer, strobe)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        scene_id,
                        fs.fixture_name,
                        fs.r,
                        fs.g,
                        fs.b,
                        fs.dimmer,
                        fs.strobe,
                    )
                    for fs in scene.fixture_states
                ],
            )

        logger.info("Saved scene '%s' (id=%s)", scene.name, scene_id)
        return Scene(
            id=scene_id,
            name=scene.name,
            fixture_states=scene.fixture_states,
            fade_time=scene.fade_time,
        )

    def delete_scene(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM scenes WHERE name = ?", (name,))
        logger.info("Deleted scene '%s'", name)

    def list_scenes(self) -> list[Scene]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, fade_time FROM scenes ORDER BY name"
            ).fetchall()
            return [self._load_scene(conn, row) for row in rows]

    def get_scene(self, name: str) -> "Scene | None":
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, fade_time FROM scenes WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            return self._load_scene(conn, row)

    def _load_scene(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Scene:
        fs_rows = conn.execute(
            "SELECT fixture_name, r, g, b, dimmer, strobe"
            " FROM fixture_states WHERE scene_id = ?",
            (row["id"],),
        ).fetchall()
        fixture_states = [
            FixtureState(
                fixture_name=r["fixture_name"],
                r=r["r"],
                g=r["g"],
                b=r["b"],
                dimmer=r["dimmer"],
                strobe=r["strobe"],
            )
            for r in fs_rows
        ]
        return Scene(
            id=row["id"],
            name=row["name"],
            fixture_states=fixture_states,
            fade_time=row["fade_time"],
        )

    # ------------------------------------------------------------------
    # Cue list CRUD
    # ------------------------------------------------------------------

    def save_cue_list(self, name: str, entries: list[CueEntry]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM cues WHERE list_name = ?", (name,))
            conn.executemany(
                "INSERT INTO cues"
                " (list_name, position, scene_name, trigger, auto_advance, advance_after_bars)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        name,
                        pos,
                        e.scene_name,
                        e.trigger,
                        int(e.auto_advance),
                        e.advance_after_bars,
                    )
                    for pos, e in enumerate(entries)
                ],
            )
        logger.info("Saved cue list '%s' (%d entries)", name, len(entries))

    def get_cue_list(self, name: str) -> list[CueEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT scene_name, trigger, auto_advance, advance_after_bars"
                " FROM cues WHERE list_name = ? ORDER BY position",
                (name,),
            ).fetchall()
        return [
            CueEntry(
                scene_name=r["scene_name"],
                trigger=r["trigger"],
                auto_advance=bool(r["auto_advance"]),
                advance_after_bars=r["advance_after_bars"],
            )
            for r in rows
        ]

    def list_cue_lists(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT list_name FROM cues ORDER BY list_name"
            ).fetchall()
        return [r["list_name"] for r in rows]

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def capture_scene(self, name: str, fixture_instances: list) -> Scene:
        """Snapshot current fixture states into a new scene.

        Each element in *fixture_instances* must expose at minimum:
            .name (str), .r, .g, .b, .dimmer, .strobe (int)
        """
        states = [
            FixtureState(
                fixture_name=f.name,
                r=int(getattr(f, "r", 0)),
                g=int(getattr(f, "g", 0)),
                b=int(getattr(f, "b", 0)),
                dimmer=int(getattr(f, "dimmer", 0)),
                strobe=int(getattr(f, "strobe", 0)),
            )
            for f in fixture_instances
        ]
        scene = Scene(id=None, name=name, fixture_states=states)
        saved = self.save_scene(scene)
        logger.info(
            "Captured scene '%s' from %d fixtures", name, len(fixture_instances)
        )
        return saved
