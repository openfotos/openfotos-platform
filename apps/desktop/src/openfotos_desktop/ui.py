"""Qt Widgets screens for the OneNodeAI Studio photographer workflow."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import UUID

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from openfotos_contracts import (
    MAX_WATERMARK_TEXT_LENGTH,
    WatermarkLogoKind,
    WatermarkTemplate,
)

from .branding import WatermarkCompositionError, compose_watermark_mark
from .derivatives import DerivativeError, RenderPolicy, render_for_review
from .diagnostics import RedactedDiagnosticExporter
from .face_models import FaceModelSetupError, FaceModelStore
from .ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    InventoryStatus,
    LocalFaceState,
    LocalUploadState,
    ScanCancelled,
    ScanProgress,
    ScanSummary,
    SubEventCache,
)
from .ports import DesktopGateway
from .theme import apply_corporate_theme, asset_path

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

SUPPORTED_PROCESSING_PROFILE_ID = "pilot-profile-v1"
DEFAULT_TRANSFER_LIMIT = 3
LOGGER = logging.getLogger(__name__)

_PROGRESS_SCALE = 1000
_STAGE_WEIGHTS = (
    ("originals", 500),
    ("derivatives", 300),
    ("face-index", 200),
)


def _asset_icon(name: str) -> QIcon:
    return QIcon(str(asset_path(name)))


def _style_button(
    button: QPushButton,
    *,
    icon: str | None = None,
    kind: str | None = None,
) -> QPushButton:
    if icon is not None:
        button.setIcon(_asset_icon(icon))
        button.setIconSize(QSize(18, 18))
    if kind is not None:
        button.setProperty("kind", kind)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        amount /= 1024
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
    raise AssertionError("unreachable")


class PageHeading(QFrame):
    def __init__(self, eyebrow: str, title: str, description: str, step: str) -> None:
        super().__init__()
        self.setObjectName("PageHeading")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        copy = QVBoxLayout()
        copy.setSpacing(5)
        eyebrow_label = QLabel(eyebrow.upper())
        eyebrow_label.setObjectName("PageEyebrow")
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        self.description = QLabel(description)
        self.description.setObjectName("PageDescription")
        self.description.setWordWrap(True)
        copy.addWidget(eyebrow_label)
        copy.addWidget(self.title)
        copy.addWidget(self.description)
        layout.addLayout(copy, 1)
        badge = QLabel(step)
        badge.setObjectName("StepBadge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)


class BrandHeader(QFrame):
    retry_models_requested = Signal()
    sign_out_requested = Signal()

    def __init__(self, *, demo: bool) -> None:
        super().__init__()
        self.setObjectName("AppHeader")
        self.setFixedHeight(72)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 12, 28, 12)
        layout.setSpacing(14)
        self.product_name = QLabel("OneNodeAI Studio")
        self.product_name.setObjectName("HeaderBrand")
        layout.addWidget(self.product_name)

        product = QVBoxLayout()
        product.setSpacing(1)
        name = QLabel("Photographer desktop")
        name.setObjectName("HeaderProduct")
        self.context = QLabel("Private event ingestion")
        self.context.setObjectName("HeaderContext")
        product.addWidget(name)
        product.addWidget(self.context)
        layout.addLayout(product)
        layout.addStretch()

        self.model_status = QLabel()
        self.model_status.setObjectName("ModelStatus")
        self.model_status.hide()
        layout.addWidget(self.model_status)
        self.model_retry = _style_button(QPushButton("Retry download"), kind="ghost")
        self.model_retry.hide()
        self.model_retry.clicked.connect(self.retry_models_requested)
        layout.addWidget(self.model_retry)

        mode = QLabel("Demo workspace" if demo else "Secure workspace")
        mode.setObjectName("ModeBadge")
        layout.addWidget(mode)
        self.sign_out = _style_button(QPushButton("Sign out"), kind="ghost")
        self.sign_out.hide()
        self.sign_out.clicked.connect(self.sign_out_requested)
        layout.addWidget(self.sign_out)

    def set_context(self, text: str) -> None:
        self.context.setText(text)

    def set_session_active(self, active: bool) -> None:
        self.sign_out.setVisible(active)

    def set_model_state(self, state: str, message: str) -> None:
        self.model_status.setText(message)
        self.model_status.setProperty("state", state)
        self.model_status.style().unpolish(self.model_status)
        self.model_status.style().polish(self.model_status)
        self.model_status.show()
        self.model_retry.setVisible(state == "error")


class ScanWorker(QObject):
    progressed = Signal(object)
    completed = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, scanner: InventoryScanner, batch_id: UUID, stop: Event) -> None:
        super().__init__()
        self.scanner = scanner
        self.batch_id = batch_id
        self.stop = stop

    @Slot()
    def run(self) -> None:
        try:
            summary = self.scanner.scan(
                self.batch_id,
                on_progress=self.progressed.emit,
                is_cancelled=self.stop.is_set,
            )
        except ScanCancelled:
            self.cancelled.emit()
        except Exception as exc:  # Qt must surface worker failures to the local operator.
            self.failed.emit(str(exc))
        else:
            self.completed.emit(summary)
        finally:
            QThread.currentThread().quit()


class UploadWorker(QObject):
    progressed = Signal(int, int)
    stage_progressed = Signal(str, int, int)
    completed = Signal()
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        gateway: DesktopGateway,
        batch_id: UUID,
        transfer_limit: int,
        stop: Event,
    ) -> None:
        super().__init__()
        self.gateway = gateway
        self.batch_id = batch_id
        self.transfer_limit = transfer_limit
        self.stop = stop

    @Slot()
    def run(self) -> None:
        try:
            self.gateway.upload(
                self.batch_id,
                transfer_limit=self.transfer_limit,
                on_progress=self.progressed.emit,
                is_cancelled=self.stop.is_set,
                on_stage=self.stage_progressed.emit,
            )
        except Exception as exc:  # Qt must surface worker failures to the local operator.
            self.failed.emit(str(exc))
        else:
            if self.stop.is_set():
                self.cancelled.emit()
            else:
                self.completed.emit()
        finally:
            QThread.currentThread().quit()


class FaceModelDownloadWorker(QObject):
    progressed = Signal(int, int)
    completed = Signal()
    cancelled = Signal()
    failed = Signal(str)

    def __init__(self, store: FaceModelStore, stop: Event) -> None:
        super().__init__()
        self.store = store
        self.stop = stop

    @Slot()
    def run(self) -> None:
        try:
            self.store.download(
                progress=self.progressed.emit,
                is_cancelled=self.stop.is_set,
            )
        except FaceModelSetupError as exc:
            if self.stop.is_set() or exc.code == "face_model_download_cancelled":
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
        except OSError as exc:
            self.failed.emit(str(exc))
        except Exception:
            LOGGER.exception("Unexpected face-model download failure")
            self.failed.emit("Unexpected model download failure. Check the application log.")
        else:
            self.completed.emit()
        finally:
            QThread.currentThread().quit()


class ResumeWorker(QObject):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, gateway: DesktopGateway, server_url: str) -> None:
        super().__init__()
        self.gateway = gateway
        self.server_url = server_url

    @Slot()
    def run(self) -> None:
        try:
            events = list(self.gateway.resume(self.server_url))
        except Exception as exc:  # Auto-resume never blocks sign-in on an unexpected failure.
            self.failed.emit(str(exc))
        else:
            self.completed.emit(events)
        finally:
            QThread.currentThread().quit()


class LoginPage(QWidget):
    photographer_requested = Signal(str, str, str, str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 34, 52, 36)
        layout.setSpacing(22)
        layout.addWidget(
            PageHeading(
                "Workspace access",
                "Sign in to OneNodeAI Studio",
                "Use the photographer account assigned to the studio workspace.",
                "Secure access",
            )
        )

        content = QHBoxLayout()
        content.setSpacing(22)
        hero = QFrame()
        hero.setObjectName("HeroPanel")
        hero.setMinimumWidth(340)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(30, 30, 30, 30)
        hero_layout.setSpacing(14)
        hero_title = QLabel("Event photo delivery")
        hero_title.setObjectName("HeroTitle")
        hero_copy = QLabel(
            "Prepare and validate event media before upload. Source files remain on this "
            "workstation until the contribution is submitted."
        )
        hero_copy.setObjectName("HeroCopy")
        hero_copy.setWordWrap(True)
        hero_layout.addWidget(hero_title)
        hero_layout.addWidget(hero_copy)
        hero_layout.addStretch()
        for text in (
            "Event-scoped contribution batches",
            "Concurrent photographer workstations",
            "Resumable checksum validation",
        ):
            feature = QLabel(f"✓  {text}")
            feature.setObjectName("FeatureItem")
            hero_layout.addWidget(feature)
        content.addWidget(hero, 4)

        access_panel = QFrame()
        access_panel.setObjectName("Panel")
        access_layout = QVBoxLayout(access_panel)
        access_layout.setContentsMargins(26, 22, 26, 24)
        access_layout.setSpacing(14)
        access_title = QLabel("Workspace access")
        access_title.setObjectName("SectionTitle")
        access_layout.addWidget(access_title)
        sign_in = QWidget()
        sign_in_form = QFormLayout(sign_in)
        sign_in_form.setContentsMargins(20, 22, 20, 20)
        sign_in_form.setHorizontalSpacing(18)
        sign_in_form.setVerticalSpacing(14)
        self.server = QLineEdit("https://studio.example")
        self.username = QLineEdit()
        self.username.setPlaceholderText("photographer@example.com")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Workspace password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.device_label = QLineEdit()
        self.device_label.setPlaceholderText("e.g. Studio workstation 2")
        self.sign_in_button = _style_button(
            QPushButton("Sign in as photographer"),
            kind="primary",
        )
        self.sign_in_button.clicked.connect(self._request_photographer)
        sign_in_form.addRow("Server", self.server)
        sign_in_form.addRow("Username", self.username)
        sign_in_form.addRow("Password", self.password)
        sign_in_form.addRow("Installation label", self.device_label)
        sign_in_form.addRow(self.sign_in_button)
        access_layout.addWidget(sign_in, 1)

        self.error = QLabel()
        self.error.setObjectName("ErrorBanner")
        self.error.setWordWrap(True)
        self.error.hide()
        access_layout.addWidget(self.error)
        self.notice = QLabel("Credentials are never written to the local photo checkpoint.")
        self.notice.setObjectName("InfoBanner")
        self.notice.setWordWrap(True)
        access_layout.addWidget(self.notice)
        content.addWidget(access_panel, 6)
        layout.addLayout(content, 1)

    @Slot()
    def _request_photographer(self) -> None:
        self.error.clear()
        self.error.hide()
        self.photographer_requested.emit(
            self.server.text().strip(),
            self.username.text().strip(),
            self.password.text(),
            self.device_label.text().strip(),
        )

    def show_error(self, message: str) -> None:
        self.password.clear()
        self.error.setText(message)
        self.error.show()

    def show_notice(self, message: str) -> None:
        self.notice.setText(message)


class EventSelectorPage(QWidget):
    selected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 34, 52, 38)
        layout.setSpacing(22)
        self.heading = QLabel("Choose an event")
        self.heading.setObjectName("PageTitle")
        heading = QFrame()
        heading_layout = QHBoxLayout(heading)
        heading_layout.setContentsMargins(0, 0, 0, 0)
        copy = QVBoxLayout()
        eyebrow = QLabel("EVENT WORKSPACE")
        eyebrow.setObjectName("PageEyebrow")
        description = QLabel(
            "Each event has its own private contribution batches and storage allowance."
        )
        description.setObjectName("PageDescription")
        copy.addWidget(eyebrow)
        copy.addWidget(self.heading)
        copy.addWidget(description)
        heading_layout.addLayout(copy, 1)
        step = QLabel("1 of 3")
        step.setObjectName("StepBadge")
        heading_layout.addWidget(step, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(heading)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 22)
        panel_layout.setSpacing(14)
        title = QLabel("Available events")
        title.setObjectName("SectionTitle")
        panel_layout.addWidget(title)
        self.events = QListWidget()
        self.events.setAlternatingRowColors(True)
        self.events.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.events.itemDoubleClicked.connect(lambda _item: self._select())
        panel_layout.addWidget(self.events, 1)
        layout.addWidget(panel, 1)
        footer = QHBoxLayout()
        hint = QLabel("Double-click an event or select it and continue.")
        hint.setObjectName("BodyMuted")
        footer.addWidget(hint)
        footer.addStretch()
        self.open_button = _style_button(
            QPushButton("Open event"),
            kind="primary",
        )
        self.open_button.clicked.connect(self._select)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)

    def set_events(self, events: list[EventCache], *, demo: bool) -> None:
        self.events.clear()
        self.heading.setText("Select a synthetic demo event" if demo else "Choose an event")
        for event in events:
            remaining_bytes = max(0, event.storage_limit_bytes - event.reserved_original_bytes)
            item = QListWidgetItem(
                _asset_icon("calendar.svg"),
                f"{event.name}    ·    {format_bytes(remaining_bytes)} remaining"
                f"    ·    {format_bytes(event.verified_original_bytes)} verified",
            )
            item.setSizeHint(QSize(0, 58))
            item.setData(Qt.ItemDataRole.UserRole, event)
            self.events.addItem(item)
        if self.events.count():
            self.events.setCurrentRow(0)

    @Slot()
    def _select(self) -> None:
        item = self.events.currentItem()
        if item:
            self.selected.emit(item.data(Qt.ItemDataRole.UserRole))


class SubEventSelectorPage(QWidget):
    selected = Signal(object)
    back_requested = Signal()
    watermark_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 34, 52, 38)
        layout.setSpacing(22)
        self.heading = PageHeading(
            "Event sections",
            "Choose where these photos belong",
            "One contribution belongs to exactly one active section.",
            "2 of 3",
        )
        layout.addWidget(self.heading)

        panel = QFrame()
        panel.setObjectName("Panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(22, 20, 22, 22)
        title = QLabel("Active sections")
        title.setObjectName("SectionTitle")
        panel_layout.addWidget(title)
        self.sub_events = QListWidget()
        self.sub_events.setAlternatingRowColors(True)
        self.sub_events.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.sub_events.itemDoubleClicked.connect(lambda _item: self._select())
        panel_layout.addWidget(self.sub_events, 1)
        self.empty = QLabel(
            "This event has no active sections. Create or restore one in the web dashboard."
        )
        self.empty.setObjectName("InfoBanner")
        self.empty.setWordWrap(True)
        self.empty.hide()
        panel_layout.addWidget(self.empty)
        layout.addWidget(panel, 1)

        policy_row = QHBoxLayout()
        self.policy_note = QLabel()
        self.policy_note.setObjectName("BodyMuted")
        self.policy_note.setWordWrap(True)
        policy_row.addWidget(self.policy_note, 1)
        self.watermark = _style_button(
            QPushButton("Watermark settings"),
            icon="file.svg",
            kind="ghost",
        )
        self.watermark.clicked.connect(self.watermark_requested)
        policy_row.addWidget(self.watermark)
        layout.addLayout(policy_row)

        footer = QHBoxLayout()
        back = _style_button(QPushButton("Back to events"), kind="ghost")
        back.clicked.connect(self.back_requested)
        footer.addWidget(back)
        footer.addStretch()
        self.open_button = _style_button(QPushButton("Upload photos"), kind="primary")
        self.open_button.clicked.connect(self._select)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)

    def show_event(self, event: EventCache) -> None:
        self.heading.title.setText(event.name)
        self.sub_events.clear()
        for sub_event in event.sub_events:
            item = QListWidgetItem(
                _asset_icon("calendar.svg"),
                f"{sub_event.position:02d}    {sub_event.name}",
            )
            item.setSizeHint(QSize(0, 54))
            item.setData(Qt.ItemDataRole.UserRole, sub_event)
            self.sub_events.addItem(item)
        has_sub_events = self.sub_events.count() > 0
        self.empty.setVisible(not has_sub_events)
        self.open_button.setEnabled(has_sub_events)
        if has_sub_events:
            self.sub_events.setCurrentRow(0)
        if event.preview_policy is None:
            self.policy_note.setText(
                "Previews stay clean unless you configure an optional watermark before the "
                "first contribution is submitted."
            )
        else:
            self.policy_note.setText(
                "Watermark settings are already recorded for this event; unpublishing does "
                "not reopen them."
            )

    @Slot()
    def _select(self) -> None:
        item = self.sub_events.currentItem()
        if item:
            self.selected.emit(item.data(Qt.ItemDataRole.UserRole))


class WatermarkSettingsDialog(QDialog):
    confirmed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._event: EventCache | None = None
        self._custom_logo_path: Path | None = None
        self._sample_path: Path | None = None
        self._locked = False
        self.setWindowTitle("Watermark settings")
        self.setModal(True)
        self.setMinimumSize(760, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        heading = QLabel("Optional gallery watermark")
        heading.setObjectName("SectionTitle")
        layout.addWidget(heading)
        self.note = QLabel()
        self.note.setObjectName("BodyMuted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        self.locked_note = QLabel(
            "These settings are already recorded for this event and can no longer change."
        )
        self.locked_note.setObjectName("InfoBanner")
        self.locked_note.setWordWrap(True)
        self.locked_note.hide()
        layout.addWidget(self.locked_note)

        content = QHBoxLayout()
        content.setSpacing(18)
        controls = QFrame()
        controls.setObjectName("Panel")
        form = QFormLayout(controls)
        form.setContentsMargins(22, 20, 22, 20)
        form.setSpacing(13)
        self.enabled = QCheckBox("Add a watermark to gallery previews")
        self.enabled.setChecked(False)
        self.enabled.toggled.connect(self._watermark_toggled)
        form.addRow(self.enabled)
        self.template = QComboBox()
        for label, template in (
            ("Compact · bottom right", WatermarkTemplate.COMPACT_BOTTOM_RIGHT),
            ("Balanced · bottom center", WatermarkTemplate.BOTTOM_CENTER),
            ("Large brand · center", WatermarkTemplate.CENTER_BRAND),
            ("Repeated · diagonal", WatermarkTemplate.REPEATED_DIAGONAL),
        ):
            self.template.addItem(label, template)
        self.template.currentIndexChanged.connect(self._render_preview)
        form.addRow("Template", self.template)
        self.logo = QComboBox()
        self.logo.addItem("OneNodeAI wordmark", WatermarkLogoKind.ONENODEAI)
        self.logo.addItem("Custom transparent PNG", WatermarkLogoKind.CUSTOM)
        self.logo.addItem("No logo", WatermarkLogoKind.NONE)
        self.logo.currentIndexChanged.connect(self._logo_changed)
        form.addRow("Logo", self.logo)
        self.custom_logo = _style_button(QPushButton("Choose custom PNG"), kind="ghost")
        self.custom_logo.clicked.connect(self._choose_custom_logo)
        form.addRow("Custom file", self.custom_logo)
        self.text = QLineEdit()
        self.text.setMaxLength(MAX_WATERMARK_TEXT_LENGTH)
        self.text.setPlaceholderText("Optional, e.g. © OneNodeAI")
        self.text.textChanged.connect(self._render_preview)
        form.addRow("Watermark text", self.text)
        self.sample = _style_button(QPushButton("Use a local photo sample"), kind="ghost")
        self.sample.clicked.connect(self._choose_sample)
        form.addRow("Preview sample", self.sample)
        content.addWidget(controls, 4)

        preview_panel = QFrame()
        preview_panel.setObjectName("HeroPanel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(18, 16, 18, 16)
        preview_title = QLabel("Preview result")
        preview_title.setObjectName("SectionTitle")
        preview_layout.addWidget(preview_title)
        self.preview = QLabel()
        self.preview.setMinimumSize(480, 300)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #111827; border-radius: 8px;")
        preview_layout.addWidget(self.preview, 1)
        self.error = QLabel()
        self.error.setObjectName("ErrorBanner")
        self.error.setWordWrap(True)
        self.error.hide()
        preview_layout.addWidget(self.error)
        content.addWidget(preview_panel, 6)
        layout.addLayout(content, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        self.cancel = _style_button(QPushButton("Cancel"), kind="ghost")
        self.cancel.clicked.connect(self.reject)
        footer.addWidget(self.cancel)
        self.save = _style_button(QPushButton("Save watermark settings"), kind="primary")
        self.save.clicked.connect(self._save)
        footer.addWidget(self.save)
        layout.addLayout(footer)
        self._watermark_toggled(False)

    def show_event(self, event: EventCache) -> None:
        self._event = event
        self._custom_logo_path = None
        self._sample_path = None
        self.error.clear()
        self.error.hide()
        policy = event.preview_policy
        self._set_locked(policy is not None)
        if policy is None:
            self.note.setText(
                "Watermarking is off. Saving clean previews locks the choice once a "
                "contribution is submitted."
            )
            self.enabled.setChecked(False)
            self.template.setCurrentIndex(0)
            self.logo.setCurrentIndex(0)
            self.text.clear()
            self._render_preview()
            return
        self.note.setText("This event already has a locked preview setting.")
        self.enabled.setChecked(policy.enabled)
        self.template.setCurrentIndex(max(0, self.template.findData(policy.template)))
        self.logo.setCurrentIndex(max(0, self.logo.findData(policy.logo_kind)))
        self.text.setText(policy.text)
        self.enabled.setChecked(policy.enabled)
        if self._locked:
            self.preview.clear()
            self.preview.setText("Saved preview settings are locked for this event.")

    def _set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.locked_note.setVisible(locked)
        self.save.setVisible(not locked)
        for control in (
            self.enabled,
            self.template,
            self.logo,
            self.text,
            self.sample,
        ):
            control.setEnabled(not locked)
        self.custom_logo.setEnabled(
            not locked
            and self.enabled.isChecked()
            and WatermarkLogoKind(self.logo.currentData()) is WatermarkLogoKind.CUSTOM
        )

    def draft(self) -> dict:
        enabled = self.enabled.isChecked()
        template = WatermarkTemplate(self.template.currentData())
        if not enabled:
            return {
                "enabled": False,
                "template": template,
                "text": "",
                "logo_kind": WatermarkLogoKind.NONE,
                "mark_png": b"",
            }
        logo_kind = WatermarkLogoKind(self.logo.currentData())
        text = self.text.text().strip()
        return {
            "enabled": True,
            "template": template,
            "text": text,
            "logo_kind": logo_kind,
            "mark_png": compose_watermark_mark(
                logo_kind=logo_kind,
                text=text,
                custom_logo_path=self._custom_logo_path,
            ),
        }

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()

    @Slot()
    def _save(self) -> None:
        try:
            draft = self.draft()
        except WatermarkCompositionError as exc:
            self.show_error(str(exc))
            return
        self.confirmed.emit(draft)

    @Slot(bool)
    def _watermark_toggled(self, enabled: bool) -> None:
        if not self._locked:
            for control in (self.template, self.logo, self.text):
                control.setEnabled(enabled)
            self.custom_logo.setEnabled(
                enabled and WatermarkLogoKind(self.logo.currentData()) is WatermarkLogoKind.CUSTOM
            )
        self._render_preview()

    @Slot()
    def _logo_changed(self) -> None:
        if not self._locked:
            self.custom_logo.setEnabled(
                self.enabled.isChecked()
                and WatermarkLogoKind(self.logo.currentData()) is WatermarkLogoKind.CUSTOM
            )
        self._render_preview()

    @Slot()
    def _choose_custom_logo(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Choose a transparent logo", "", "PNG images (*.png)"
        )
        if filename:
            self._custom_logo_path = Path(filename)
            self.custom_logo.setText(Path(filename).name)
            self._render_preview()

    @Slot()
    def _choose_sample(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a local preview sample",
            "",
            "Supported photos (*.jpg *.jpeg *.png *.webp *.heic *.heif)",
        )
        if filename:
            self._sample_path = Path(filename)
            self.sample.setText(Path(filename).name)
            self._render_preview()

    @Slot()
    def _render_preview(self) -> None:
        if self._locked:
            return
        try:
            draft = self.draft()
            rendered = render_for_review(
                self._sample_image(),
                RenderPolicy(
                    enabled=draft["enabled"],
                    template=draft["template"],
                    mark_png=draft["mark_png"],
                ),
            )
        except (OSError, UnidentifiedImageError, WatermarkCompositionError, DerivativeError) as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.error.hide()
        pixmap = QPixmap.fromImage(ImageQt(rendered))
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _sample_image(self) -> Image.Image:
        if self._sample_path is not None:
            with Image.open(self._sample_path) as opened:
                opened.load()
                return ImageOps.exif_transpose(opened).convert("RGB")
        image = Image.new("RGB", (1200, 760), "#172554")
        draw = ImageDraw.Draw(image)
        for y in range(image.height):
            ratio = y / image.height
            draw.line(
                (0, y, image.width, y),
                fill=(round(23 + 75 * ratio), round(37 + 82 * ratio), round(84 + 66 * ratio)),
            )
        draw.ellipse((110, 90, 570, 550), fill="#d97706")
        draw.rounded_rectangle((480, 200, 1090, 680), radius=70, fill="#0f766e")
        draw.text((54, 680), "LOCAL PREVIEW SAMPLE", fill="#f8fafc")
        return image


class UploadPage(QWidget):
    """One page for selection, inline verification, submission, and progress."""

    add_files_requested = Signal()
    add_folder_requested = Signal()
    remove_selection_requested = Signal()
    submit_requested = Signal()
    pause_resume_requested = Signal()
    new_batch_requested = Signal()
    back_requested = Signal()
    export_requested = Signal()
    rescan_requested = Signal()
    cleanup_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._batch_id: UUID | None = None
        self._event: EventCache | None = None
        self._store: CheckpointStore | None = None
        self._uploadable = True
        self._busy = False
        self._stage_counts = {name: (0, 0) for name, _weight in _STAGE_WEIGHTS}
        self._detail_rows: list[tuple[str, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 28, 52, 32)
        layout.setSpacing(14)
        self.heading = PageHeading(
            "Contribution",
            "Upload photos",
            "Add photos or a folder. Verification runs inline and skipped files never block "
            "the contribution.",
            "3 of 3",
        )
        layout.addWidget(self.heading)

        hero = QFrame()
        hero.setObjectName("HeroPanel")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 15, 22, 15)
        hero_copy = QVBoxLayout()
        hero_copy.setSpacing(3)
        self.event_label = QLabel()
        self.event_label.setObjectName("SectionTitle")
        self.batch_meta = QLabel()
        self.batch_meta.setObjectName("BatchMeta")
        hero_copy.addWidget(self.event_label)
        hero_copy.addWidget(self.batch_meta)
        hero_layout.addLayout(hero_copy, 1)
        self.batch_status = QLabel("COLLECTING")
        self.batch_status.setObjectName("StatusBadge")
        hero_layout.addWidget(self.batch_status)
        layout.addWidget(hero)

        source_actions = QHBoxLayout()
        source_actions.setSpacing(14)
        self.add_files = self._source_action("Add photos", "Choose photo files", "file.svg")
        self.add_files.clicked.connect(self.add_files_requested)
        self.add_folder = self._source_action(
            "Add a folder", "Choose a folder to discover photos recursively", "folder.svg"
        )
        self.add_folder.clicked.connect(self.add_folder_requested)
        source_actions.addWidget(self.add_files)
        source_actions.addWidget(self.add_folder)
        layout.addLayout(source_actions)

        selection_panel = QFrame()
        selection_panel.setObjectName("Panel")
        selection_layout = QVBoxLayout(selection_panel)
        selection_layout.setContentsMargins(18, 14, 18, 15)
        selection_layout.setSpacing(9)
        selection_header = QHBoxLayout()
        selected_title = QLabel("Selected sources")
        selected_title.setObjectName("SectionTitle")
        self.selection_count = QLabel("0 SOURCES")
        self.selection_count.setObjectName("StatusBadge")
        self.remove_selection = _style_button(
            QPushButton("Remove selected"), icon="trash.svg", kind="ghost"
        )
        self.remove_selection.clicked.connect(self.remove_selection_requested)
        selection_header.addWidget(selected_title)
        selection_header.addStretch()
        selection_header.addWidget(self.remove_selection)
        selection_header.addWidget(self.selection_count)
        selection_layout.addLayout(selection_header)
        self.selections = QListWidget()
        self.selections.setAlternatingRowColors(True)
        self.selections.setMinimumHeight(110)
        selection_layout.addWidget(self.selections, 1)
        self.empty_hint = QLabel("No photos selected yet. Add photos, a folder, or both.")
        self.empty_hint.setObjectName("BodyMuted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        selection_layout.addWidget(self.empty_hint)
        layout.addWidget(selection_panel, 1)

        verification_panel = QFrame()
        verification_panel.setObjectName("ProgressPanel")
        verification_layout = QVBoxLayout(verification_panel)
        verification_layout.setContentsMargins(18, 13, 18, 14)
        verification_layout.setSpacing(8)
        verification_header = QHBoxLayout()
        verification_title = QLabel("Verification")
        verification_title.setObjectName("SectionTitle")
        self.verification = QLabel("No files verified yet.")
        self.verification.setObjectName("BodyMuted")
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_toggle.toggled.connect(self._details_toggled)
        self.details_toggle.hide()
        verification_header.addWidget(verification_title)
        verification_header.addWidget(self.verification, 1)
        verification_header.addWidget(self.details_toggle)
        verification_layout.addLayout(verification_header)
        self.details = QListWidget()
        self.details.setAlternatingRowColors(True)
        self.details.setMaximumHeight(150)
        self.details.hide()
        verification_layout.addWidget(self.details)
        layout.addWidget(verification_panel)

        progress_panel = QFrame()
        progress_panel.setObjectName("ProgressPanel")
        progress_layout = QVBoxLayout(progress_panel)
        progress_layout.setContentsMargins(18, 13, 18, 14)
        progress_layout.setSpacing(8)
        progress_header = QHBoxLayout()
        self.stage_text = QLabel("Add photos to begin.")
        self.stage_text.setObjectName("BodyMuted")
        self.pause = _style_button(QPushButton("Pause"), icon="pause.svg", kind="ghost")
        self.pause.clicked.connect(self.pause_resume_requested)
        self.pause.hide()
        progress_header.addWidget(self.stage_text, 1)
        progress_header.addWidget(self.pause)
        progress_layout.addLayout(progress_header)
        self.progress = QProgressBar()
        self.progress.setRange(0, _PROGRESS_SCALE)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        progress_layout.addWidget(self.progress)
        layout.addWidget(progress_panel)

        footer = QHBoxLayout()
        self.back = _style_button(
            QPushButton("Back to sections"), icon="arrow-left.svg", kind="ghost"
        )
        self.back.clicked.connect(self.back_requested)
        self.overflow = QToolButton()
        self.overflow.setText("More")
        self.overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.overflow.setCursor(Qt.CursorShape.PointingHandCursor)
        menu = QMenu(self.overflow)
        menu.addAction("Export redacted diagnostics", self.export_requested.emit)
        menu.addAction("Verify sources again", self.rescan_requested.emit)
        menu.addAction("Remove local checkpoint", self.cleanup_requested.emit)
        self.overflow.setMenu(menu)
        self.new_batch = _style_button(
            QPushButton("New contribution"), icon="plus.svg", kind="ghost"
        )
        self.new_batch.clicked.connect(self.new_batch_requested)
        self.submit = _style_button(QPushButton("Submit contribution"), kind="primary")
        self.submit.clicked.connect(self.submit_requested)
        footer.addWidget(self.back)
        footer.addWidget(self.overflow)
        footer.addStretch()
        footer.addWidget(self.new_batch)
        footer.addWidget(self.submit)
        layout.addLayout(footer)

    def _source_action(self, text: str, tooltip: str, icon: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setIcon(_asset_icon(icon))
        button.setIconSize(QSize(30, 30))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setProperty("actionCard", True)
        button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def show_batch(self, event: EventCache, batch_id: UUID, store: CheckpointStore) -> None:
        self._event = event
        self._batch_id = batch_id
        self._store = store
        batch = store.get_batch(batch_id)
        sub_event = next(
            (value for value in event.sub_events if value.id == batch.sub_event_id),
            None,
        )
        section_name = sub_event.name if sub_event else "Archived section"
        self.event_label.setText(f"{event.name}  /  {section_name}")
        self.batch_meta.setText(f"Contribution  {batch_id}")
        self._uploadable = event.intake_state == "open"
        self._refresh_sources(store, batch)
        self._refresh_status(batch)
        self.refresh_verification(store)
        self.refresh_progress(store, batch)

    def _refresh_sources(self, store: CheckpointStore, batch) -> None:
        self.selections.clear()
        for selection in store.list_selections(batch.id):
            icon = "folder.svg" if selection.kind.value == "folder" else "file.svg"
            item = QListWidgetItem(
                _asset_icon(icon),
                f"{selection.kind.value.title()}    {selection.source_path}",
            )
            item.setSizeHint(QSize(0, 44))
            item.setData(Qt.ItemDataRole.UserRole, selection.id)
            self.selections.addItem(item)
        count = self.selections.count()
        self.selection_count.setText(f"{count} {'SOURCE' if count == 1 else 'SOURCES'}")
        self.empty_hint.setVisible(count == 0)
        editable = not batch.frozen and self._uploadable
        self.add_files.setEnabled(editable)
        self.add_folder.setEnabled(editable)
        self.remove_selection.setEnabled(editable)
        self.new_batch.setEnabled(self._uploadable)

    def _refresh_status(self, batch) -> None:
        if batch.state is BatchState.NOT_INCLUDED:
            label, status = "NOT INCLUDED", "collecting"
        elif batch.state is BatchState.COMPLETE:
            label, status = "COMPLETE", "ready"
        elif batch.state in {BatchState.APPROVED, BatchState.NEEDS_REVIEW}:
            label, status = "READY", "ready"
        elif batch.state in {BatchState.RESERVED, BatchState.UPLOADING}:
            label, status = batch.state.value.upper(), "ready"
        else:
            label, status = "COLLECTING", "collecting"
        self.batch_status.setText(label)
        self.batch_status.setProperty("status", status)
        self.batch_status.style().unpolish(self.batch_status)
        self.batch_status.style().polish(self.batch_status)

    def refresh_verification(
        self,
        store: CheckpointStore,
        *,
        summary: ScanSummary | None = None,
    ) -> None:
        if self._batch_id is None:
            return
        batch_id = self._batch_id
        if summary is None:
            summary = store.summary(batch_id)
        attention = summary.blocking_item_count + summary.blocking_issue_count
        if summary.accepted_count == 0 and summary.rejected_count == 0 and attention == 0:
            text = "No files verified yet."
        else:
            parts = [
                f"{summary.accepted_count} accepted",
                f"{summary.rejected_count} skipped",
            ]
            if attention:
                parts.append(f"{attention} need attention")
            text = " · ".join(parts)
            if summary.accepted_bytes:
                text += f" · {format_bytes(summary.accepted_bytes)}"
        self.verification.setText(text)
        self._detail_rows = self._verification_details(store, batch_id)
        self.details_toggle.setVisible(bool(self._detail_rows))
        self.details.clear()
        for path, reason in self._detail_rows:
            self.details.addItem(f"{path}  —  {reason}")
        self.details.setVisible(self.details_toggle.isChecked() and bool(self._detail_rows))
        self._update_submit_state(store, summary)

    def _verification_details(
        self, store: CheckpointStore, batch_id: UUID
    ) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        for item in store.list_items(batch_id):
            if item.status is InventoryStatus.ACCEPTED:
                continue
            reason = item.reason.value.replace("_", " ").title() if item.reason else ""
            rows.append((str(item.source_path), f"{item.status.value} · {reason}"))
        for issue in store.list_scan_issues(batch_id):
            label = "blocking" if issue.blocking else "warning"
            rows.append(
                (
                    str(issue.source_path),
                    f"{label} · {issue.reason.value.replace('_', ' ').title()}",
                )
            )
        return rows

    def _update_submit_state(self, store: CheckpointStore, summary: ScanSummary) -> None:
        if self._batch_id is None:
            return
        batch = store.get_batch(self._batch_id)
        if batch.state is BatchState.NOT_INCLUDED:
            self.submit.setText("Not included in published event")
            self.submit.setEnabled(False)
            return
        fully_complete = batch.state is BatchState.COMPLETE and (
            store.derivatives_complete(batch.id) and store.face_analysis_complete(batch.id)
        )
        if fully_complete:
            self.submit.setText("Contribution complete")
            self.submit.setEnabled(False)
            return
        resumable = batch.state in {BatchState.RESERVED, BatchState.UPLOADING} or (
            batch.state is BatchState.COMPLETE
        )
        if resumable:
            self.submit.setText("Resume upload")
            self.submit.setEnabled(self._uploadable and not self._busy)
            return
        self.submit.setText("Submit contribution")
        self.submit.setEnabled(
            self._uploadable
            and not self._busy
            and summary.can_approve
            and batch.state in {BatchState.NEEDS_REVIEW, BatchState.APPROVED}
        )

    def refresh_progress(self, store: CheckpointStore, batch) -> None:
        uploads = store.list_upload_checkpoints(batch.id)
        derivatives = store.list_derivative_checkpoints(batch.id)
        faces = store.list_face_analysis_checkpoints(batch.id)
        self._stage_counts["originals"] = (
            sum(
                checkpoint.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                for checkpoint in uploads
            ),
            len(uploads),
        )
        self._stage_counts["derivatives"] = (
            sum(
                checkpoint.state in {LocalUploadState.VERIFIED, LocalUploadState.EXCLUDED}
                for checkpoint in derivatives
            ),
            len(derivatives),
        )
        self._stage_counts["face-index"] = (
            sum(
                checkpoint.state
                in {LocalFaceState.INDEXED, LocalFaceState.NO_USABLE_FACE, LocalFaceState.EXCLUDED}
                for checkpoint in faces
            ),
            len(faces),
        )
        complete = (
            batch.state is BatchState.COMPLETE
            and store.derivatives_complete(batch.id)
            and store.face_analysis_complete(batch.id)
        )
        if complete:
            self.stage_text.setText("Contribution complete; every file is verified on the server.")
            self.progress.setValue(_PROGRESS_SCALE)
            self.pause.hide()
            return
        if self._busy:
            return
        stage = self._first_incomplete_stage()
        if stage is None:
            self.progress.setValue(0)
            self.stage_text.setText(
                "Ready to submit."
                if batch.state is not BatchState.NOT_INCLUDED
                else "Event published — not included."
            )
            self.pause.hide()
            return
        self._render_stage(stage)
        if batch.state in {BatchState.RESERVED, BatchState.UPLOADING}:
            self.pause.setVisible(True)
            self.pause.setText("Resume")
            self.pause.setIcon(_asset_icon("refresh.svg"))
            self.pause.setEnabled(True)
            self.stage_text.setText(f"{self.stage_text.text()} — ready to resume.")

    def _first_incomplete_stage(self) -> str | None:
        for name, _weight in _STAGE_WEIGHTS:
            completed, total = self._stage_counts[name]
            if total and completed < total:
                return name
        return None

    def _render_stage(self, stage: str) -> None:
        completed, total = self._stage_counts[stage]
        labels = {
            "originals": "Uploading originals",
            "derivatives": "Generating thumbnails and previews",
            "face-index": "Embedding faces",
        }
        self.stage_text.setText(f"{labels[stage]} — {completed} of {total}")
        self._render_progress_value()

    def _render_progress_value(self) -> None:
        total = 0.0
        for name, weight in _STAGE_WEIGHTS:
            completed, count = self._stage_counts[name]
            if count:
                total += weight * (completed / count)
        self.progress.setRange(0, _PROGRESS_SCALE)
        self.progress.setValue(round(total))

    @Slot(bool)
    def _details_toggled(self, checked: bool) -> None:
        self.details.setVisible(checked and bool(self._detail_rows))

    def scan_started(self) -> None:
        self.progress.setRange(0, 0)
        self.stage_text.setText("Checking local photos…")
        for button in (self.add_files, self.add_folder, self.remove_selection, self.submit):
            button.setEnabled(False)
        self.back.setEnabled(True)

    def scan_progressed(self, progress: ScanProgress) -> None:
        self.stage_text.setText(
            f"Checked {progress.processed_count} files: {progress.accepted_count} accepted, "
            f"{progress.rejected_count} skipped. Current: {progress.current_path}"
        )

    def scan_stopped(self) -> None:
        self.progress.setRange(0, _PROGRESS_SCALE)
        if self._store is not None and self._batch_id is not None:
            batch = self._store.get_batch(self._batch_id)
            self._refresh_sources(self._store, batch)
            self._refresh_status(batch)

    def submit_started(self) -> None:
        self._busy = True
        self.pause.show()
        self.pause.setText("Pause")
        self.pause.setIcon(_asset_icon("pause.svg"))
        self.pause.setEnabled(True)
        self.submit.setEnabled(False)
        self.new_batch.setEnabled(False)
        self.add_files.setEnabled(False)
        self.add_folder.setEnabled(False)
        self.remove_selection.setEnabled(False)
        self.stage_text.setText("Preparing contribution…")
        self.progress.setRange(0, _PROGRESS_SCALE)

    def stage_progressed(self, stage: str, completed: int, total: int) -> None:
        name = stage if stage in self._stage_counts else "derivatives"
        self._stage_counts[name] = (completed, total)
        self._render_stage(name)

    def upload_stopped(self, *, paused: bool = False) -> None:
        self._busy = False
        if paused:
            self.stage_text.setText("Paused. Verified files will not restart.")
            self.pause.setText("Resume")
            self.pause.setIcon(_asset_icon("refresh.svg"))
            self.pause.setEnabled(True)
        else:
            self.pause.hide()
        if self._event is not None:
            self._uploadable = self._event.intake_state == "open"


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        store: CheckpointStore,
        gateway: DesktopGateway,
        demo_event: EventCache | None = None,
        face_model_store: FaceModelStore | None = None,
    ) -> None:
        super().__init__()
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_corporate_theme(application)
        self.store = store
        self.gateway = gateway
        self.demo = demo_event is not None
        self.face_model_store = face_model_store or FaceModelStore()
        self.current_event: EventCache | None = None
        self.current_sub_event: SubEventCache | None = None
        self.current_batch_id: UUID | None = None
        self.scan_thread: QThread | None = None
        self.scan_stop: Event | None = None
        self.upload_thread: QThread | None = None
        self.upload_stop: Event | None = None
        self.model_thread: QThread | None = None
        self.model_stop: Event | None = None
        self.resume_thread: QThread | None = None
        self.watermark_dialog: WatermarkSettingsDialog | None = None
        self._resume_worker: ResumeWorker | None = None
        self._model_worker: FaceModelDownloadWorker | None = None
        self._scan_worker: ScanWorker | None = None
        self._upload_worker: UploadWorker | None = None

        shell = QWidget()
        shell.setObjectName("AppShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.header = BrandHeader(demo=self.demo)
        shell_layout.addWidget(self.header)

        self.stack = QStackedWidget()
        self.stack.setObjectName("PageStack")
        self.login = LoginPage()
        self.events = EventSelectorPage()
        self.sub_events = SubEventSelectorPage()
        self.upload_page = UploadPage()
        for page in (self.login, self.events, self.sub_events, self.upload_page):
            self.stack.addWidget(page)
        shell_layout.addWidget(self.stack, 1)
        self.setCentralWidget(shell)
        self.setWindowTitle("OneNodeAI Studio")
        self.setWindowIcon(_asset_icon("logo.png"))
        self.setMinimumSize(940, 680)
        self.resize(1220, 820)
        self._connect_actions()

        if self.demo:
            self.store.cache_event(demo_event)  # type: ignore[arg-type]
            self.events.set_events([demo_event], demo=True)  # type: ignore[list-item]
            self.stack.setCurrentWidget(self.events)
            self.header.set_model_state("ready", "Face models not required in demo")
        else:
            self._prefill_server_origin()
            self.stack.setCurrentWidget(self.login)
            self._start_auto_resume()
            self._start_model_download()

    def _prefill_server_origin(self) -> None:
        cached_origins = {
            event.server_url for event in self.store.list_events() if event.server_url
        }
        if len(cached_origins) == 1:
            [origin] = cached_origins
            self.login.server.setText(origin)

    def _connect_actions(self) -> None:
        self.header.retry_models_requested.connect(self._retry_model_download)
        self.header.sign_out_requested.connect(self._sign_out)
        self.login.photographer_requested.connect(self._photographer_login)
        self.events.selected.connect(self._open_event)
        self.sub_events.selected.connect(self._open_sub_event)
        self.sub_events.back_requested.connect(lambda: self.stack.setCurrentWidget(self.events))
        self.sub_events.watermark_requested.connect(self._open_watermark_settings)
        self.upload_page.add_files_requested.connect(self._add_files)
        self.upload_page.add_folder_requested.connect(self._add_folder)
        self.upload_page.remove_selection_requested.connect(self._remove_selection)
        self.upload_page.submit_requested.connect(self._submit_batch)
        self.upload_page.pause_resume_requested.connect(self._pause_or_resume_upload)
        self.upload_page.new_batch_requested.connect(self._new_batch)
        self.upload_page.back_requested.connect(self._back_to_sub_events)
        self.upload_page.export_requested.connect(self._export_diagnostics)
        self.upload_page.rescan_requested.connect(self._rescan_sources)
        self.upload_page.cleanup_requested.connect(self._cleanup_event)

    # ---------- Face models ----------

    def _start_model_download(self) -> None:
        if self.face_model_store.verified_paths() is not None:
            self.header.set_model_state("ready", "Face models ready")
            return
        if self.model_thread is not None:
            return
        self.header.set_model_state("downloading", "Downloading face models…")
        self.model_stop = Event()
        thread = QThread(self)
        worker = FaceModelDownloadWorker(self.face_model_store, self.model_stop)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self._model_download_progressed)
        worker.completed.connect(self._model_download_completed)
        worker.cancelled.connect(self._model_download_cancelled)
        worker.failed.connect(self._model_download_failed)
        for signal in (worker.completed, worker.cancelled, worker.failed):
            signal.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._model_thread_finished)
        self.model_thread = thread
        self._model_worker = worker
        thread.start()

    @Slot()
    def _retry_model_download(self) -> None:
        if self.model_thread is not None:
            return
        self._start_model_download()

    @Slot(int, int)
    def _model_download_progressed(self, completed: int, total: int) -> None:
        self.header.set_model_state(
            "downloading",
            f"Downloading face models — {completed} of {total}",
        )

    @Slot()
    def _model_download_completed(self) -> None:
        self.header.set_model_state("ready", "Face models ready")

    @Slot()
    def _model_download_cancelled(self) -> None:
        self.header.set_model_state("error", "Face model download stopped")
        self.header.model_retry.setToolTip("Retry the verified model download.")

    @Slot(str)
    def _model_download_failed(self, message: str) -> None:
        self.header.set_model_state("error", "Face models unavailable")
        self.header.model_retry.setToolTip(message)

    @Slot()
    def _model_thread_finished(self) -> None:
        if self.model_thread is not None:
            self.model_thread.deleteLater()
        self.model_thread = None
        self.model_stop = None
        self._model_worker = None

    # ---------- Sessions ----------

    def _start_auto_resume(self) -> None:
        try:
            origin = self.gateway.saved_session_origin()
        except Exception:  # Simple test gateways may not implement the optional probe.
            origin = None
        if not origin:
            return
        self.login.show_notice("Restoring the saved session…")
        self.login.sign_in_button.setEnabled(False)
        thread = QThread(self)
        worker = ResumeWorker(self.gateway, origin)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._resume_completed)
        worker.failed.connect(self._resume_failed)
        for signal in (worker.completed, worker.failed):
            signal.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._resume_thread_finished)
        self.resume_thread = thread
        self._resume_worker = worker
        thread.start()

    @Slot(object)
    def _resume_completed(self, events: list[EventCache]) -> None:
        self._show_persistence_warning()
        for event in events:
            self.store.cache_event(event)
        self.header.set_session_active(True)
        self.events.set_events(events, demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot(str)
    def _resume_failed(self, message: str) -> None:
        del message
        self.login.show_notice("The saved session could not be restored. Sign in to continue.")
        self.stack.setCurrentWidget(self.login)

    @Slot()
    def _resume_thread_finished(self) -> None:
        if self.resume_thread is not None:
            self.resume_thread.deleteLater()
        self.resume_thread = None
        self._resume_worker = None
        self.login.sign_in_button.setEnabled(True)

    @Slot(str, str, str, str)
    def _photographer_login(
        self,
        server_url: str,
        username: str,
        password: str,
        device_label: str,
    ) -> None:
        try:
            events = list(
                self.gateway.sign_in_photographer(
                    server_url,
                    username,
                    password,
                    device_label,
                )
            )
        except RuntimeError as exc:
            self.login.show_error(str(exc))
            return
        self.login.password.clear()
        self.login.show_notice("Credentials are never written to the local photo checkpoint.")
        self._show_persistence_warning()
        for event in events:
            self.store.cache_event(event)
        self.header.set_session_active(True)
        self.events.set_events(events, demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot()
    def _sign_out(self) -> None:
        if not self.demo:
            with contextlib.suppress(RuntimeError):
                self.gateway.sign_out()
        self.current_event = None
        self.current_sub_event = None
        self.current_batch_id = None
        self.header.set_context("Private event ingestion")
        self.header.set_session_active(False)
        self.login.show_notice("You have signed out. Sign in to continue.")
        self.stack.setCurrentWidget(self.login)

    # ---------- Event navigation ----------

    @Slot(object)
    def _open_event(self, event: EventCache) -> None:
        self.current_event = event
        self.current_sub_event = None
        self.header.set_context(event.name)
        self._show_sub_events()

    def _show_sub_events(self) -> None:
        if self.current_event is None:
            return
        self.sub_events.show_event(self.current_event)
        self.stack.setCurrentWidget(self.sub_events)

    @Slot()
    def _back_to_sub_events(self) -> None:
        if self.current_event is None:
            return
        self.current_sub_event = None
        self.current_batch_id = None
        self.header.set_context(self.current_event.name)
        self._show_sub_events()

    @Slot(object)
    def _open_sub_event(self, sub_event: SubEventCache) -> None:
        if self.current_event is None or sub_event not in self.current_event.sub_events:
            return
        self.current_sub_event = sub_event
        self.header.set_context(f"{self.current_event.name} / {sub_event.name}")
        self._show_upload_page()

    def _show_upload_page(self) -> None:
        if self.current_event is None or self.current_sub_event is None:
            return
        event = self.current_event
        batches = [
            batch
            for batch in self.store.list_batches(event.id)
            if batch.sub_event_id == self.current_sub_event.id
        ]
        resumable = [batch for batch in batches if self._batch_needs_attention(batch)]
        self.current_batch_id = (
            resumable[0].id
            if resumable
            else (
                batches[-1].id
                if batches
                else self.store.create_batch(event.id, self.current_sub_event.id)
            )
        )
        batch = self.store.get_batch(self.current_batch_id)
        self.upload_page.show_batch(event, batch.id, self.store)
        self.stack.setCurrentWidget(self.upload_page)
        if batch.state in {BatchState.DRAFT, BatchState.SCANNING} and (
            self.store.list_selections(batch.id)
        ):
            self._start_scan()

    def _batch_needs_attention(self, batch) -> bool:
        if batch.state in {
            BatchState.SCANNING,
            BatchState.PAUSED,
            BatchState.NEEDS_REVIEW,
            BatchState.APPROVED,
            BatchState.RESERVED,
            BatchState.UPLOADING,
        }:
            return True
        if batch.state is BatchState.DRAFT:
            return bool(self.store.list_selections(batch.id))
        if batch.state is BatchState.COMPLETE:
            return not (
                self.store.derivatives_complete(batch.id)
                and self.store.face_analysis_complete(batch.id)
            )
        return False

    # ---------- Watermark settings ----------

    @Slot()
    def _open_watermark_settings(self) -> None:
        if self.current_event is None:
            return
        if self.watermark_dialog is None:
            dialog = WatermarkSettingsDialog(self)
            dialog.confirmed.connect(self._confirm_watermark)
            self.watermark_dialog = dialog
        self.watermark_dialog.show_event(self.current_event)
        self.watermark_dialog.show()
        self.watermark_dialog.raise_()
        self.watermark_dialog.activateWindow()

    @Slot(object)
    def _confirm_watermark(self, draft: dict) -> None:
        if self.current_event is None or self.watermark_dialog is None:
            return
        try:
            event = self.gateway.confirm_preview_policy(
                self.current_event.id,
                enabled=draft["enabled"],
                template=draft["template"],
                text=draft["text"],
                logo_kind=draft["logo_kind"],
                mark_png=draft["mark_png"],
            )
        except RuntimeError as exc:
            self.watermark_dialog.show_error(str(exc))
            return
        self.current_event = event
        self.store.cache_event(event)
        self.sub_events.show_event(event)
        self.watermark_dialog.accept()

    # ---------- Sources and verification ----------

    @Slot()
    def _new_batch(self) -> None:
        if self.current_event is None or self.current_sub_event is None:
            return
        self.current_batch_id = self.store.create_batch(
            self.current_event.id,
            self.current_sub_event.id,
        )
        self._show_upload_page()

    @Slot()
    def _add_files(self) -> None:
        if self.current_batch_id is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose source files",
            "",
            "Supported photos (*.jpg *.jpeg *.png *.webp *.heic *.heif);;All files (*)",
        )
        if paths:
            try:
                self.store.add_files(self.current_batch_id, paths)
            except ValueError as exc:
                self._show_error(str(exc))
            self._show_upload_page()

    @Slot()
    def _add_folder(self) -> None:
        if self.current_batch_id is None:
            return
        path = QFileDialog.getExistingDirectory(self, "Choose a source folder")
        if path:
            try:
                self.store.add_folder(self.current_batch_id, path)
            except ValueError as exc:
                self._show_error(str(exc))
            self._show_upload_page()

    @Slot()
    def _remove_selection(self) -> None:
        item = self.upload_page.selections.currentItem()
        if item is None:
            return
        try:
            self.store.remove_selection(item.data(Qt.ItemDataRole.UserRole))
        except ValueError as exc:
            self._show_error(str(exc))
        self._show_upload_page()

    @Slot()
    def _rescan_sources(self) -> None:
        if self.current_batch_id is None:
            return
        batch = self.store.get_batch(self.current_batch_id)
        started_uploading = bool(self.store.list_upload_checkpoints(batch.id))
        if (
            batch.state
            in {
                BatchState.RESERVED,
                BatchState.UPLOADING,
                BatchState.COMPLETE,
                BatchState.NOT_INCLUDED,
            }
            or started_uploading
        ):
            self._show_error(
                "A contribution that started uploading cannot be reverified; "
                "create a new contribution for changed files."
            )
            return
        self._start_scan()

    @Slot()
    def _start_scan(self) -> None:
        if self.current_batch_id is None or self.scan_thread is not None:
            return
        if not self.store.list_selections(self.current_batch_id):
            self.upload_page.refresh_verification(self.store)
            return
        self.upload_page.scan_started()
        self.scan_stop = Event()
        thread = QThread(self)
        worker = ScanWorker(
            InventoryScanner(self.store),
            self.current_batch_id,
            self.scan_stop,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self.upload_page.scan_progressed)
        worker.completed.connect(self._scan_completed)
        worker.cancelled.connect(self._scan_cancelled)
        worker.failed.connect(self._scan_failed)
        for signal in (worker.completed, worker.cancelled, worker.failed):
            signal.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._scan_thread_finished)
        self.scan_thread = thread
        self._scan_worker = worker
        thread.start()

    @Slot()
    def _scan_cancelled(self) -> None:
        self.upload_page.scan_stopped()
        self._show_upload_page()

    @Slot(object)
    def _scan_completed(self, summary: ScanSummary) -> None:
        self.upload_page.scan_stopped()
        self.upload_page.refresh_verification(self.store, summary=summary)
        if self.current_batch_id is not None:
            self.upload_page.refresh_progress(
                self.store, self.store.get_batch(self.current_batch_id)
            )

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self.upload_page.scan_stopped()
        self._show_error(f"Verification failed: {message}")

    @Slot()
    def _scan_thread_finished(self) -> None:
        if self.scan_thread is not None:
            self.scan_thread.deleteLater()
        self.scan_thread = None
        self.scan_stop = None
        self._scan_worker = None

    # ---------- Submission ----------

    @Slot()
    def _submit_batch(self) -> None:
        if self.current_batch_id is None or self.upload_thread is not None:
            return
        batch = self.store.get_batch(self.current_batch_id)
        if batch.state is BatchState.DRAFT or batch.state is BatchState.SCANNING:
            self._show_error("Let verification finish before submitting the contribution.")
            return
        if batch.state is BatchState.NEEDS_REVIEW:
            summary = self.store.summary(self.current_batch_id)
            if not summary.can_approve:
                self._show_error(
                    "Resolve the blocking verification items before submitting the contribution."
                )
                return
            try:
                self.store.approve_batch(
                    self.current_batch_id,
                    supported_profile_id=SUPPORTED_PROCESSING_PROFILE_ID,
                )
            except ValueError as exc:
                self._show_error(str(exc))
                return
        elif batch.state not in {
            BatchState.APPROVED,
            BatchState.RESERVED,
            BatchState.UPLOADING,
            BatchState.COMPLETE,
        }:
            self._show_error("This contribution cannot be submitted in its current state.")
            return
        self._start_upload(DEFAULT_TRANSFER_LIMIT)

    @Slot(int)
    def _start_upload(self, transfer_limit: int) -> None:
        if self.current_batch_id is None or self.upload_thread is not None:
            return
        self.upload_page.submit_started()
        self.upload_stop = Event()
        thread = QThread(self)
        worker = UploadWorker(
            self.gateway,
            self.current_batch_id,
            transfer_limit,
            self.upload_stop,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.stage_progressed.connect(self.upload_page.stage_progressed)
        worker.completed.connect(self._upload_completed)
        worker.cancelled.connect(self._upload_cancelled)
        worker.failed.connect(self._upload_failed)
        for signal in (worker.completed, worker.cancelled, worker.failed):
            signal.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._upload_thread_finished)
        self.upload_thread = thread
        self._upload_worker = worker
        thread.start()

    @Slot()
    def _pause_or_resume_upload(self) -> None:
        if self.upload_thread is not None:
            if self.upload_stop is not None:
                self.upload_stop.set()
                self.upload_page.pause.setText("Pausing…")
            return
        if self.current_batch_id is None:
            return
        batch = self.store.get_batch(self.current_batch_id)
        if batch.state in {
            BatchState.APPROVED,
            BatchState.RESERVED,
            BatchState.UPLOADING,
        }:
            self._start_upload(DEFAULT_TRANSFER_LIMIT)

    @Slot()
    def _upload_completed(self) -> None:
        self._show_upload_page()

    @Slot()
    def _upload_cancelled(self) -> None:
        self.upload_page.upload_stopped(paused=True)

    @Slot(str)
    def _upload_failed(self, message: str) -> None:
        self.upload_page.upload_stopped()
        self._show_error(f"Upload stopped: {message}")

    @Slot()
    def _upload_thread_finished(self) -> None:
        if self.upload_thread is not None:
            self.upload_thread.deleteLater()
        self.upload_thread = None
        self.upload_stop = None
        self._upload_worker = None

    def _show_persistence_warning(self) -> None:
        warning = getattr(self.gateway, "persistence_warning", None)
        if warning:
            self.login.show_notice(warning)

    # ---------- Local maintenance ----------

    @Slot()
    def _export_diagnostics(self) -> None:
        if self.current_batch_id is None:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Export redacted diagnostics",
            "onenodeai-studio-diagnostic.json",
            "JSON (*.json)",
        )
        if destination:
            RedactedDiagnosticExporter(self.store).export(
                self.current_batch_id, destination=Path(destination)
            )

    @Slot()
    def _cleanup_event(self) -> None:
        if self.current_event is None:
            return
        choice = QMessageBox.question(
            self,
            "Remove local checkpoint?",
            "This removes local OneNodeAI Studio paths and checksums for this event. "
            "It never deletes source photographs. Continue?",
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_local_event(self.current_event.id)
        self.current_event = None
        self.current_sub_event = None
        self.current_batch_id = None
        self.header.set_context("Private event ingestion")
        remaining = self.store.list_events()
        self.events.set_events(remaining, demo=False)
        self.stack.setCurrentWidget(self.events if remaining else self.login)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "OneNodeAI Studio", message)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.model_thread is not None:
            if self.model_stop is not None:
                self.model_stop.set()
            if not self.model_thread.wait(5_000):
                event.ignore()
                return
        if self.resume_thread is not None and not self.resume_thread.wait(5_000):
            event.ignore()
            return
        if self.scan_stop is not None:
            self.scan_stop.set()
        if self.scan_thread is not None and not self.scan_thread.wait(5_000):
            event.ignore()
            return
        if self.upload_stop is not None:
            self.upload_stop.set()
        if self.upload_thread is not None and not self.upload_thread.wait(30_000):
            event.ignore()
            return
        close_gateway = getattr(self.gateway, "close", None)
        if close_gateway is not None:
            close_gateway()
        self.store.close()
        event.accept()
