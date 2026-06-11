"""
Parser for the Open Fixture Library (OFL) JSON format.

OFL fixture JSONs can be downloaded from https://open-fixture-library.org.
Each file describes one fixture model; it may contain multiple modes, each
of which becomes a separate FixtureProfile.

Usage
-----
    from fixture_import import import_ofl_file, import_ofl_directory

    profiles = import_ofl_file("path/to/chauvet/slimpar-56.json")
    all_profiles = import_ofl_directory("fixtures/ofl/")
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .fixture_library import ChannelDef, FixtureProfile

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OFL capability type → ChannelDef type
# ---------------------------------------------------------------------------

# Colour-intensity channels are keyed by (capability_type, color).
_COLOR_MAP: dict[str, str] = {
    "Red":    "red",
    "Green":  "green",
    "Blue":   "blue",
    "Amber":  "amber",
    "White":  "white",
    # OFL also uses these spellings:
    "UV":     "other",
    "Cyan":   "other",
    "Magenta": "other",
    "Yellow": "other",
    "Lime":   "other",
    "Indigo": "other",
    "Warm White": "white",
    "Cold White": "white",
}

# Capability type name → ChannelDef type (for non-ColorIntensity capabilities).
_CAPABILITY_TYPE_MAP: dict[str, str] = {
    "ShutterStrobe":    "strobe",
    "Intensity":        "intensity",
    "Pan":              "pan",
    "PanContinuous":    "pan",
    "Tilt":             "tilt",
    "TiltContinuous":   "tilt",
    "Zoom":             "zoom",
    "GoboIndex":        "gobo",
    "GoboRotation":     "gobo",
    "GoboStacking":     "gobo",
    "ColorWheelIndex":  "color_wheel",
    "ColorWheelRotation": "color_wheel",
    "Programs":         "program",
    "ProgramSpeed":     "speed",
    "EffectSpeed":      "speed",
    "RotationSpeed":    "speed",
    "Rotation":         "speed",
    "Prism":            "other",
    "PrismRotation":    "other",
    "Focus":            "other",
    "Iris":             "other",
    "Frost":            "other",
    "Maintenance":      "other",
    "NoFunction":       "other",
}


def _capability_to_channel_type(capability: dict[str, Any]) -> str:
    """Map an OFL capability dict to a ChannelDef type string."""
    cap_type: str = capability.get("type", "")

    if cap_type == "ColorIntensity":
        color = capability.get("color", "")
        return _COLOR_MAP.get(color, "other")

    mapped = _CAPABILITY_TYPE_MAP.get(cap_type)
    if mapped:
        return mapped

    log.debug("Unmapped OFL capability type %r; using 'other'", cap_type)
    return "other"


# ---------------------------------------------------------------------------
# OFL category → tag
# ---------------------------------------------------------------------------

def _category_to_tag(category: str) -> str:
    """Convert an OFL category string to a lowercase snake_case tag."""
    # Replace non-alphanumeric runs with a single underscore, lowercase.
    tag = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")
    return tag


# ---------------------------------------------------------------------------
# Channel resolution
# ---------------------------------------------------------------------------

def _resolve_channel(channel_name: str, available_channels: dict[str, Any]) -> ChannelDef:
    """
    Build a ChannelDef for *channel_name* using the availableChannels dict.

    OFL channels may have a single `capability` (dict) or a list
    `capabilities`.  We look at the first capability to determine the type.
    If the channel is not found in availableChannels we fall back to "other".
    """
    ch_data = available_channels.get(channel_name)
    if ch_data is None:
        log.warning("Channel %r not found in availableChannels; type will be 'other'", channel_name)
        return ChannelDef(name=channel_name, type="other", default=0)

    # Single capability (most common) or list.
    capability: dict[str, Any] | None = None
    if "capability" in ch_data:
        capability = ch_data["capability"]
    elif "capabilities" in ch_data:
        caps = ch_data["capabilities"]
        if caps:
            capability = caps[0]

    if capability is None:
        ch_type = "other"
    else:
        ch_type = _capability_to_channel_type(capability)

    # OFL has no per-channel default field; 0 is always safe.
    return ChannelDef(name=channel_name, type=ch_type, default=0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_ofl_file(path: str) -> list[FixtureProfile]:
    """
    Parse an OFL JSON file and return one FixtureProfile per mode.

    Parameters
    ----------
    path:
        Filesystem path to a single OFL fixture JSON file.

    Returns
    -------
    list[FixtureProfile]
        One profile per mode defined in the file.  Returns an empty list if
        the file cannot be parsed.
    """
    log.info("Importing OFL file: %s", path)

    try:
        with open(path, encoding="utf-8") as fh:
            data: dict[str, Any] = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to load OFL file %r: %s", path, exc)
        return []

    fixture_name: str = data.get("name", os.path.splitext(os.path.basename(path))[0])
    manufacturer: str = str(data.get("manufacturer", "Unknown"))
    categories: list[str] = data.get("categories", [])
    tags: list[str] = [_category_to_tag(c) for c in categories]

    available_channels: dict[str, Any] = data.get("availableChannels", {})
    modes: list[dict[str, Any]] = data.get("modes", [])

    if not modes:
        log.warning("OFL file %r has no modes; skipping", path)
        return []

    profiles: list[FixtureProfile] = []
    for mode in modes:
        mode_name: str = mode.get("name", "default")
        channel_names: list[str] = mode.get("channels", [])

        channel_defs: list[ChannelDef] = []
        for ch_name in channel_names:
            if ch_name is None:
                # OFL uses null to indicate an unused channel slot.
                channel_defs.append(ChannelDef(name="Unused", type="other", default=0))
            else:
                channel_defs.append(_resolve_channel(ch_name, available_channels))

        profile_name = f"{fixture_name} ({mode_name})"
        profile = FixtureProfile(
            manufacturer=manufacturer,
            name=profile_name,
            channels=channel_defs,
            tags=list(tags),  # copy so each mode has its own list
        )
        log.debug(
            "Imported profile %r: %d channels, tags=%s",
            profile_name, profile.channel_count, tags,
        )
        profiles.append(profile)

    log.info("Imported %d profile(s) from %s", len(profiles), path)
    return profiles


def import_ofl_directory(directory: str) -> list[FixtureProfile]:
    """
    Walk *directory* recursively and import every OFL JSON file found.

    Non-JSON files and files that fail to parse are skipped with a warning.

    Parameters
    ----------
    directory:
        Path to a directory tree of OFL JSON files.

    Returns
    -------
    list[FixtureProfile]
        Concatenated list of all profiles found across all files.
    """
    log.info("Importing OFL directory: %s", directory)

    if not os.path.isdir(directory):
        log.error("OFL directory %r does not exist or is not a directory", directory)
        return []

    all_profiles: list[FixtureProfile] = []
    for root, _dirs, files in os.walk(directory):
        for filename in sorted(files):
            if not filename.lower().endswith(".json"):
                continue
            filepath = os.path.join(root, filename)
            profiles = import_ofl_file(filepath)
            all_profiles.extend(profiles)

    log.info(
        "OFL directory import complete: %d profile(s) from %s",
        len(all_profiles), directory,
    )
    return all_profiles
