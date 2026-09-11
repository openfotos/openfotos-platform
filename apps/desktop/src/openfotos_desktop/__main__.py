"""OpenFotos desktop entry point."""

import argparse
import sys
from pathlib import Path
from uuid import UUID

from .ingestion import CheckpointStore, EventCache
from .paths import default_checkpoint_path

DEMO_EVENT = EventCache(
    id=UUID("00000000-0000-4000-8000-000000000003"),
    name="Session 3 synthetic reception",
    storage_limit_bytes=25_000_000_000,
    processing_profile_id="pilot-profile-v1",
)


def main() -> int:
    try:
        from PySide6.QtCore import QLockFile
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:
        raise SystemExit(
            "PySide6 is unavailable. Run `uv sync --extra desktop` before starting the app."
        ) from exc

    from .network import DesktopNetworkService
    from .ports import Session3Gateway
    from .ui import MainWindow

    parser = argparse.ArgumentParser(description="OpenFotos desktop ingestion")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use a clearly labeled synthetic event without network authentication",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="override the per-user application data directory for testing",
    )
    arguments, qt_arguments = parser.parse_known_args()
    application = QApplication([sys.argv[0], *qt_arguments])
    if arguments.state_dir:
        database = arguments.state_dir / "checkpoint.sqlite3"
    else:
        default_database = default_checkpoint_path()
        database = (
            default_database.with_name("demo-checkpoint.sqlite3")
            if arguments.demo
            else default_database
        )
    database.parent.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(database.with_suffix(".lock")))
    instance_lock.setStaleLockTime(0)
    if not instance_lock.tryLock(0):
        raise SystemExit("Another OpenFotos instance is already using this local checkpoint.")
    store = CheckpointStore(database)
    gateway = Session3Gateway() if arguments.demo else DesktopNetworkService(store)
    window = MainWindow(
        store=store,
        gateway=gateway,
        demo_event=DEMO_EVENT if arguments.demo else None,
    )
    window.instance_lock = instance_lock
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
