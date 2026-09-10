"""Minimal desktop entry point; feature screens arrive in Session 3."""

import sys


def main() -> int:
    try:
        from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
    except ImportError as exc:
        raise SystemExit(
            "PySide6 is unavailable. Run `uv sync --extra desktop` before starting the app."
        ) from exc

    application = QApplication(sys.argv)
    window = QMainWindow()
    window.setWindowTitle("OpenFotos")
    window.setCentralWidget(QLabel("OpenFotos desktop setup is ready."))
    window.resize(640, 360)
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
