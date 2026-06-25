"""
backend/settings_store.py

Persistent store for user-configurable app identity settings.
Values are written to prism_settings.json in the project root.
Settings saved here take priority over environment-variable equivalents.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).parent.parent / "prism_settings.json"


@dataclass
class AppIdentity:
    owner_name: str = ""
    owner_title: str = ""
    owner_email: str = ""
    team_name: str = ""
    company_name: str = ""


class SettingsStore:
    """Simple JSON-file store for app identity settings.

    Reads and writes ``prism_settings.json`` in the project root.
    GIL protects concurrent reads; concurrent writes are rare (admin only).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _DEFAULT_PATH

    def load(self) -> AppIdentity:
        """Return persisted identity, or empty defaults if the file does not exist."""
        if not self._path.exists():
            return AppIdentity()
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return AppIdentity(
                owner_name=data.get("owner_name", ""),
                owner_title=data.get("owner_title", ""),
                owner_email=data.get("owner_email", ""),
                team_name=data.get("team_name", ""),
                company_name=data.get("company_name", ""),
            )
        except Exception as exc:
            logger.warning("settings_store.load: failed — %s", exc)
            return AppIdentity()

    def save(self, identity: AppIdentity) -> None:
        """Persist identity to disk, raising RuntimeError on failure."""
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(asdict(identity), fh, indent=2)
        except Exception as exc:
            logger.error("settings_store.save: failed — %s", exc)
            raise RuntimeError(f"Failed to save settings: {exc}") from exc
