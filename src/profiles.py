"""Config profile manager — load/save named YAML configs."""

import logging
import os
import shutil

import yaml

logger = logging.getLogger(__name__)


class ProfileManager:
    def __init__(self, profiles_dir: str = "profiles"):
        self._dir = profiles_dir
        os.makedirs(profiles_dir, exist_ok=True)
        logger.debug("ProfileManager ready at '%s'", profiles_dir)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self._dir, f"{name}.yaml")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[str]:
        """Return profile names (without .yaml extension), sorted."""
        names = []
        for entry in os.scandir(self._dir):
            if entry.is_file() and entry.name.endswith(".yaml"):
                names.append(entry.name[:-5])
        names.sort()
        logger.debug("list_profiles -> %s", names)
        return names

    def save_profile(self, name: str, config: dict) -> None:
        """Serialise *config* to profiles/<name>.yaml."""
        path = self._path(name)
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(config, fh, default_flow_style=False, allow_unicode=True)
        logger.info("Saved profile '%s' -> %s", name, path)

    def load_profile(self, name: str) -> dict:
        """Deserialise and return profiles/<name>.yaml as a dict.

        Raises FileNotFoundError if the profile does not exist.
        """
        path = self._path(name)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"Profile '{name}' not found at {path}")
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        logger.info("Loaded profile '%s' from %s", name, path)
        return data

    def delete_profile(self, name: str) -> None:
        """Delete profiles/<name>.yaml.  No-op if it does not exist."""
        path = self._path(name)
        if os.path.isfile(path):
            os.remove(path)
            logger.info("Deleted profile '%s'", name)
        else:
            logger.warning("delete_profile: '%s' not found, skipping", name)

    def export_current(self, config_path: str, name: str) -> None:
        """Snapshot an existing config file as a named profile.

        Copies *config_path* verbatim into profiles/<name>.yaml so the full
        YAML formatting is preserved.

        Raises FileNotFoundError if *config_path* does not exist.
        """
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Source config not found: {config_path}")
        dest = self._path(name)
        shutil.copy2(config_path, dest)
        logger.info("Exported '%s' -> profile '%s' (%s)", config_path, name, dest)
