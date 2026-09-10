"""Operating-system locations for private desktop application state."""

import os
import sys
from pathlib import Path


def user_data_directory() -> Path:
    if sys.platform == "win32":
        parent = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        parent = Path.home() / "Library" / "Application Support"
    else:
        parent = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    destination = parent / "OpenFotos"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        destination.chmod(0o700)
    return destination


def default_checkpoint_path() -> Path:
    return user_data_directory() / "checkpoint.sqlite3"
