"""Corporate visual system and packaged artwork for the desktop client."""

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication

_ASSET_DIRECTORY = Path(__file__).with_name("assets")


def asset_path(name: str) -> Path:
    path = _ASSET_DIRECTORY / name
    if not path.is_file():
        raise FileNotFoundError(f"Missing packaged desktop asset: {name}")
    return path


def apply_corporate_theme(application: QApplication) -> None:
    application.setStyle("Fusion")
    application.setFont(QFont("Segoe UI", 10))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#172033"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#f7f9fc"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#172033"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#172033"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#24578f"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#8a96a8"))
    application.setPalette(palette)
    application.setStyleSheet(CORPORATE_STYLESHEET)


CORPORATE_STYLESHEET = """
* {
    color: #172033;
    font-family: "Segoe UI", "SF Pro Text", "Inter", sans-serif;
    font-size: 14px;
}

QMainWindow, QWidget#AppShell, QStackedWidget#PageStack {
    background: #ffffff;
}

QFrame#AppHeader {
    background: #ffffff;
    border-bottom: 1px solid #dfe4ea;
}

QLabel#HeaderBrand {
    color: #101828;
    font-size: 20px;
    font-weight: 700;
}

QLabel#HeaderProduct {
    color: #344054;
    font-size: 14px;
    font-weight: 600;
}

QLabel#HeaderContext, QLabel#BodyMuted, QLabel#FieldHint, QLabel#BatchMeta {
    color: #667085;
}

QLabel#ModeBadge, QLabel#StatusBadge, QLabel#StepBadge {
    background: #f5f7fa;
    border: 1px solid #d9e0e8;
    border-radius: 10px;
    color: #475467;
    font-size: 11px;
    font-weight: 600;
    padding: 3px 9px;
}

QLabel#StatusBadge[status="ready"] {
    background: #ecfdf3;
    border-color: #a7e3bd;
    color: #167044;
}

QLabel#PageEyebrow {
    color: #52657a;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}

QLabel#PageTitle {
    color: #101828;
    font-size: 25px;
    font-weight: 650;
}

QLabel#PageDescription {
    color: #667085;
    font-size: 14px;
}

QLabel#SectionTitle {
    color: #172033;
    font-size: 16px;
    font-weight: 600;
}

QLabel#HeroTitle {
    color: #101828;
    font-size: 21px;
    font-weight: 650;
}

QLabel#HeroCopy {
    color: #526174;
    font-size: 14px;
}

QLabel#FeatureItem {
    color: #344054;
    font-size: 14px;
    padding: 4px 0;
}

QLabel#SuccessMark {
    min-width: 40px;
    max-width: 40px;
    min-height: 40px;
    max-height: 40px;
    color: #147a4b;
    background: #e9f8ef;
    border: 1px solid #b9e4c9;
    border-radius: 20px;
    font-size: 22px;
    font-weight: 700;
}

QFormLayout QLabel {
    color: #344054;
    font-weight: 500;
}

QFrame#Panel, QFrame#HeroPanel, QFrame#ProgressPanel, QFrame#EmptyPanel {
    background: #ffffff;
    border: 1px solid #dfe4ea;
    border-radius: 8px;
}

QFrame#HeroPanel {
    background: #f7f9fc;
    border-color: #d8e0e8;
}

QFrame#StatCard {
    background: #ffffff;
    border: 1px solid #dfe4ea;
    border-radius: 7px;
}

QLabel#StatValue {
    color: #172033;
    font-size: 22px;
    font-weight: 650;
}

QLabel#StatLabel {
    color: #667085;
    font-size: 11px;
    font-weight: 600;
}

QLabel#StatValue[tone="success"] { color: #147a4b; }
QLabel#StatValue[tone="warning"] { color: #a15c07; }
QLabel#StatValue[tone="danger"] { color: #b42318; }
QLabel#StatValue[tone="accent"] { color: #24578f; }

QLineEdit, QComboBox, QSpinBox {
    min-height: 40px;
    padding: 0 12px;
    color: #172033;
    background: #ffffff;
    border: 1px solid #cfd6df;
    border-radius: 6px;
    selection-background-color: #24578f;
}

QLineEdit:hover, QComboBox:hover, QSpinBox:hover { border-color: #aeb8c5; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #24578f; }

QComboBox::drop-down {
    width: 30px;
    border: none;
}

QCheckBox {
    spacing: 9px;
    color: #27364a;
    font-weight: 600;
}

QPushButton, QToolButton {
    min-height: 38px;
    padding: 0 16px;
    background: #ffffff;
    border: 1px solid #cfd6df;
    border-radius: 6px;
    color: #27364a;
    font-weight: 600;
}

QPushButton:hover, QToolButton:hover {
    background: #f7f9fc;
    border-color: #9daab9;
}

QPushButton:pressed, QToolButton:pressed { background: #eef2f6; }
QPushButton:disabled, QToolButton:disabled {
    color: #98a2b3;
    background: #f8fafc;
    border-color: #e4e7ec;
}

QPushButton[kind="primary"] {
    color: #ffffff;
    background: #24578f;
    border-color: #24578f;
}

QPushButton[kind="primary"]:hover {
    background: #1d4776;
    border-color: #1d4776;
}

QPushButton[kind="danger"] {
    color: #b42318;
    background: #ffffff;
    border-color: #e6b8b3;
}

QPushButton[kind="danger"]:hover { background: #fff5f4; }
QPushButton[kind="ghost"] {
    background: transparent;
    border-color: transparent;
    color: #526174;
}

QPushButton[kind="ghost"]:hover { background: #f2f4f7; color: #172033; }

QToolButton[actionCard="true"] {
    min-height: 70px;
    padding: 12px 17px;
    background: #ffffff;
    border: 1px solid #cfd6df;
    border-radius: 7px;
    color: #172033;
    font-size: 14px;
    font-weight: 600;
}

QToolButton[actionCard="true"]:hover {
    background: #f7fafe;
    border: 1px solid #7996b6;
}

QTabWidget::pane {
    background: #ffffff;
    border: 1px solid #dfe4ea;
    border-radius: 7px;
    top: -1px;
}

QTabBar::tab {
    min-width: 145px;
    padding: 11px 20px;
    color: #667085;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 600;
}

QTabBar::tab:selected { color: #1f4e7e; border-bottom-color: #24578f; }
QTabBar::tab:hover { color: #172033; }

QListWidget, QTableWidget {
    color: #27364a;
    background: #ffffff;
    alternate-background-color: #fafbfc;
    border: 1px solid #dfe4ea;
    border-radius: 6px;
    outline: none;
    padding: 4px;
}

QListWidget::item {
    min-height: 42px;
    padding: 6px 10px;
    border-radius: 4px;
}

QListWidget::item:hover { background: #f2f5f8; }
QListWidget::item:selected { background: #e8f0f8; color: #173d63; }

QTableWidget {
    gridline-color: transparent;
    padding: 0;
}

QTableWidget::item {
    min-height: 36px;
    padding: 7px 10px;
    border-bottom: 1px solid #edf0f3;
}

QTableWidget::item:selected { background: #e8f0f8; }

QHeaderView::section {
    color: #526174;
    background: #f7f9fc;
    border: none;
    border-bottom: 1px solid #dfe4ea;
    padding: 10px;
    font-size: 11px;
    font-weight: 600;
}

QProgressBar {
    min-height: 7px;
    max-height: 7px;
    background: #e4e9ef;
    border: none;
    border-radius: 3px;
    text-align: center;
}

QProgressBar::chunk { border-radius: 3px; background: #24578f; }

QLabel#ErrorBanner {
    color: #9f241b;
    background: #fff5f4;
    border: 1px solid #f0c4c0;
    border-radius: 6px;
    padding: 10px 12px;
}

QLabel#InfoBanner {
    color: #526174;
    background: #f7f9fc;
    border: 1px solid #dfe4ea;
    border-radius: 6px;
    padding: 10px 12px;
}

QScrollBar:vertical {
    width: 9px;
    margin: 2px;
    background: transparent;
}

QScrollBar::handle:vertical {
    min-height: 28px;
    background: #c8d0da;
    border-radius: 4px;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

QToolTip {
    color: #ffffff;
    background: #27364a;
    border: 1px solid #27364a;
    padding: 6px;
}
"""
