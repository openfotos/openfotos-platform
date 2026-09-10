"""Dark visual system and packaged artwork for the desktop client."""

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

_ASSET_DIRECTORY = Path(__file__).with_name("assets")


def asset_path(name: str) -> Path:
    path = _ASSET_DIRECTORY / name
    if not path.is_file():
        raise FileNotFoundError(f"Missing packaged desktop asset: {name}")
    return path


def apply_dark_theme(application: QApplication) -> None:
    application.setStyle("Fusion")
    application.setFont(QFont("Segoe UI", 10))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#080b12"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e7ecf5"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0c111b"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#111827"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e7ecf5"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#151d2a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#e7ecf5"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#7c5cff"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#64748b"))
    application.setPalette(palette)
    application.setStyleSheet(DARK_STYLESHEET)


DARK_STYLESHEET = """
* {
    color: #e7ecf5;
    font-family: "Segoe UI", "SF Pro Display", "Inter", sans-serif;
    font-size: 14px;
}

QMainWindow, QWidget#AppShell, QStackedWidget#PageStack {
    background: #080b12;
}

QFrame#AppHeader {
    background: #0c111b;
    border-bottom: 1px solid #202a39;
}

QLabel#HeaderProduct {
    color: #f8fafc;
    font-size: 15px;
    font-weight: 700;
}

QLabel#HeaderContext, QLabel#BodyMuted, QLabel#FieldHint, QLabel#BatchMeta {
    color: #8b98ab;
}

QLabel#ModeBadge, QLabel#StatusBadge, QLabel#StepBadge {
    background: #191433;
    border: 1px solid #4d3eb0;
    border-radius: 11px;
    color: #c8beff;
    font-size: 11px;
    font-weight: 700;
    padding: 4px 10px;
}

QLabel#StatusBadge[status="ready"] {
    background: #0b2924;
    border-color: #176c5d;
    color: #73e7cb;
}

QLabel#PageEyebrow {
    color: #8f7dff;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 1px;
}

QLabel#PageTitle {
    color: #f8fafc;
    font-size: 30px;
    font-weight: 750;
}

QLabel#PageDescription {
    color: #93a0b4;
    font-size: 15px;
}

QLabel#SectionTitle {
    color: #f5f7fb;
    font-size: 17px;
    font-weight: 700;
}

QLabel#HeroTitle {
    color: #ffffff;
    font-size: 25px;
    font-weight: 750;
}

QLabel#HeroCopy {
    color: #aab5c7;
    font-size: 15px;
}

QLabel#FeatureItem {
    color: #c8d1df;
    font-size: 14px;
    font-weight: 600;
    padding: 4px 0;
}

QFormLayout QLabel {
    color: #9aa7ba;
    font-weight: 600;
}

QFrame#Panel, QFrame#HeroPanel, QFrame#ProgressPanel, QFrame#EmptyPanel {
    background: #101722;
    border: 1px solid #222d3d;
    border-radius: 16px;
}

QFrame#HeroPanel {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 1,
        stop: 0 #171331, stop: 0.55 #111827, stop: 1 #0e2130
    );
    border-color: #342c68;
}

QFrame#StatCard {
    background: #0e1520;
    border: 1px solid #202b3a;
    border-radius: 13px;
}

QLabel#StatValue {
    color: #f8fafc;
    font-size: 24px;
    font-weight: 750;
}

QLabel#StatLabel {
    color: #7f8ba0;
    font-size: 12px;
    font-weight: 600;
}

QLabel#StatValue[tone="success"] { color: #69e6c4; }
QLabel#StatValue[tone="warning"] { color: #f7c66d; }
QLabel#StatValue[tone="danger"] { color: #ff8294; }
QLabel#StatValue[tone="accent"] { color: #b7aaff; }

QLineEdit {
    min-height: 42px;
    padding: 0 13px;
    color: #f2f5fa;
    background: #0a1019;
    border: 1px solid #283548;
    border-radius: 9px;
    selection-background-color: #6f52e8;
}

QLineEdit:hover { border-color: #3b4a61; }
QLineEdit:focus { border: 1px solid #8067ff; background: #0d1420; }

QPushButton, QToolButton {
    min-height: 40px;
    padding: 0 17px;
    background: #17202d;
    border: 1px solid #2b384b;
    border-radius: 9px;
    color: #dbe2ed;
    font-weight: 650;
}

QPushButton:hover, QToolButton:hover {
    background: #202b3a;
    border-color: #42516a;
}

QPushButton:pressed, QToolButton:pressed { background: #111925; }
QPushButton:disabled, QToolButton:disabled {
    color: #566174;
    background: #111722;
    border-color: #202835;
}

QPushButton[kind="primary"] {
    color: #ffffff;
    background: #7254ed;
    border-color: #8e76ff;
}

QPushButton[kind="primary"]:hover { background: #8063f4; border-color: #a491ff; }
QPushButton[kind="danger"] { color: #ff9aaa; background: #24131a; border-color: #5e2835; }
QPushButton[kind="ghost"] { background: transparent; border-color: transparent; color: #98a6ba; }
QPushButton[kind="ghost"]:hover { background: #141c28; color: #e7ecf5; }

QToolButton[actionCard="true"] {
    min-height: 82px;
    padding: 14px 18px;
    background: #101925;
    border: 1px solid #2a3749;
    border-radius: 13px;
    color: #f2f5fa;
    font-size: 15px;
    font-weight: 700;
}

QToolButton[actionCard="true"]:hover {
    background: #171f33;
    border: 1px solid #765df0;
}

QTabWidget::pane {
    background: #101722;
    border: 1px solid #222d3d;
    border-radius: 12px;
    top: -1px;
}

QTabBar::tab {
    min-width: 145px;
    padding: 12px 20px;
    color: #7f8ba0;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 650;
}

QTabBar::tab:selected { color: #dcd5ff; border-bottom-color: #8067ff; }
QTabBar::tab:hover { color: #c5cddb; }

QListWidget, QTableWidget {
    color: #dfe5ee;
    background: #0b111a;
    alternate-background-color: #0e1520;
    border: 1px solid #202b3a;
    border-radius: 10px;
    outline: none;
    padding: 5px;
}

QListWidget::item {
    min-height: 44px;
    padding: 7px 10px;
    border-radius: 7px;
}

QListWidget::item:hover { background: #151f2d; }
QListWidget::item:selected { background: #2b225d; color: #f5f3ff; }

QTableWidget {
    gridline-color: transparent;
    padding: 0;
}

QTableWidget::item {
    min-height: 38px;
    padding: 8px 10px;
    border-bottom: 1px solid #182230;
}

QTableWidget::item:selected { background: #2b225d; }

QHeaderView::section {
    color: #8390a4;
    background: #0e1520;
    border: none;
    border-bottom: 1px solid #263245;
    padding: 11px 10px;
    font-size: 12px;
    font-weight: 700;
}

QProgressBar {
    min-height: 9px;
    max-height: 9px;
    background: #202837;
    border: none;
    border-radius: 4px;
    text-align: center;
}

QProgressBar::chunk {
    border-radius: 4px;
    background: qlineargradient(x1: 0, y1: 0, x2: 1, y2: 0, stop: 0 #6e50ed, stop: 1 #42c9ba);
}

QLabel#ErrorBanner {
    color: #ffadb9;
    background: #2b141c;
    border: 1px solid #713043;
    border-radius: 9px;
    padding: 11px 13px;
}

QLabel#InfoBanner {
    color: #aab7ca;
    background: #0d1d2b;
    border: 1px solid #24435e;
    border-radius: 9px;
    padding: 11px 13px;
}

QScrollBar:vertical {
    width: 10px;
    margin: 2px;
    background: transparent;
}

QScrollBar::handle:vertical { min-height: 28px; background: #344054; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QToolTip {
    color: #eef2f7;
    background: #161f2b;
    border: 1px solid #3b485c;
    padding: 6px;
}
"""
