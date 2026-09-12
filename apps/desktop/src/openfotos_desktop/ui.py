"""Qt Widgets screens for Session 3 local inventory."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import UUID

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QObject, QSize, Qt, QThread, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtSvgWidgets import QSvgWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
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
from .ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    InventoryStatus,
    LocalUploadState,
    ScanCancelled,
    ScanProgress,
    ScanSummary,
)
from .ports import DesktopGateway
from .theme import apply_corporate_theme, asset_path

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

SUPPORTED_PROCESSING_PROFILE_ID = "pilot-profile-v1"


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


class StatCard(QFrame):
    def __init__(self, label: str, *, tone: str = "accent") -> None:
        super().__init__()
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(3)
        self.value = QLabel("—")
        self.value.setObjectName("StatValue")
        self.value.setProperty("tone", tone)
        caption = QLabel(label.upper())
        caption.setObjectName("StatLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)


class BrandHeader(QFrame):
    def __init__(self, *, demo: bool) -> None:
        super().__init__()
        self.setObjectName("AppHeader")
        self.setFixedHeight(72)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(28, 12, 28, 12)
        layout.setSpacing(16)
        self.logo = QSvgWidget(str(asset_path("ofts.svg")))
        self.logo.setFixedSize(118, 36)
        layout.addWidget(self.logo)

        product = QVBoxLayout()
        product.setSpacing(1)
        name = QLabel("Desktop")
        name.setObjectName("HeaderProduct")
        self.context = QLabel("Private event ingestion")
        self.context.setObjectName("HeaderContext")
        product.addWidget(name)
        product.addWidget(self.context)
        layout.addLayout(product)
        layout.addStretch()
        mode = QLabel("Demo workspace" if demo else "Secure workspace")
        mode.setObjectName("ModeBadge")
        layout.addWidget(mode)

    def set_context(self, text: str) -> None:
        self.context.setText(text)


def format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        amount /= 1024
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
    raise AssertionError("unreachable")


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


class LoginPage(QWidget):
    lead_requested = Signal(str, str, str, str)
    uploader_requested = Signal(str, str, str)
    resume_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 34, 52, 36)
        layout.setSpacing(22)
        layout.addWidget(
            PageHeading(
                "Workspace access",
                "Sign in to OpenFotos",
                "Authenticate as an event lead or enroll an authorized upload workstation.",
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
        hero_title = QLabel("Desktop media ingestion")
        hero_title.setObjectName("HeroTitle")
        hero_copy = QLabel(
            "Prepare and validate event media before upload. Source files remain on this "
            "workstation until the contribution is approved."
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
        tabs = QTabWidget()
        access_layout.addWidget(tabs, 1)

        lead = QWidget()
        lead_form = QFormLayout(lead)
        lead_form.setContentsMargins(20, 22, 20, 20)
        lead_form.setHorizontalSpacing(18)
        lead_form.setVerticalSpacing(14)
        self.lead_server = QLineEdit("https://openfotos.example")
        self.username = QLineEdit()
        self.username.setPlaceholderText("photographer@example.com")
        self.password = QLineEdit()
        self.password.setPlaceholderText("Workspace password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.lead_device_label = QLineEdit()
        self.lead_device_label.setPlaceholderText("e.g. Lead editing workstation")
        self.lead_button = _style_button(
            QPushButton("Sign in as event lead"),
            kind="primary",
        )
        self.lead_button.clicked.connect(self._request_lead)
        lead_form.addRow("Server", self.lead_server)
        lead_form.addRow("Username", self.username)
        lead_form.addRow("Password", self.password)
        lead_form.addRow("Device label", self.lead_device_label)
        lead_form.addRow(self.lead_button)
        tabs.addTab(lead, "Lead sign in")

        uploader = QWidget()
        uploader_form = QFormLayout(uploader)
        uploader_form.setContentsMargins(20, 22, 20, 20)
        uploader_form.setHorizontalSpacing(18)
        uploader_form.setVerticalSpacing(14)
        self.uploader_server = QLineEdit("https://openfotos.example")
        self.invitation = QLineEdit()
        self.invitation.setPlaceholderText("Paste a one-time invitation")
        self.invitation.setEchoMode(QLineEdit.EchoMode.Password)
        self.device_label = QLineEdit()
        self.device_label.setPlaceholderText("e.g. Reception laptop 2")
        self.uploader_button = _style_button(
            QPushButton("Enroll this workstation"),
            kind="primary",
        )
        self.uploader_button.clicked.connect(self._request_uploader)
        uploader_form.addRow("Server", self.uploader_server)
        uploader_form.addRow("Invitation", self.invitation)
        uploader_form.addRow("Device label", self.device_label)
        uploader_form.addRow(self.uploader_button)
        tabs.addTab(uploader, "Upload invitation")

        self.error = QLabel()
        self.error.setObjectName("ErrorBanner")
        self.error.setWordWrap(True)
        self.error.hide()
        access_layout.addWidget(self.error)
        self.resume_button = _style_button(QPushButton("Resume saved session"), kind="ghost")
        self.resume_button.clicked.connect(
            lambda: self.resume_requested.emit(self.lead_server.text().strip())
        )
        access_layout.addWidget(self.resume_button)
        self.notice = QLabel(
            "Credentials and invitations are never written to the local photo checkpoint."
        )
        self.notice.setObjectName("InfoBanner")
        self.notice.setWordWrap(True)
        access_layout.addWidget(self.notice)
        content.addWidget(access_panel, 6)
        layout.addLayout(content, 1)

    @Slot()
    def _request_lead(self) -> None:
        self.error.clear()
        self.error.hide()
        self.lead_requested.emit(
            self.lead_server.text().strip(),
            self.username.text().strip(),
            self.password.text(),
            self.lead_device_label.text().strip(),
        )

    @Slot()
    def _request_uploader(self) -> None:
        self.error.clear()
        self.error.hide()
        self.uploader_requested.emit(
            self.uploader_server.text().strip(),
            self.invitation.text(),
            self.device_label.text().strip(),
        )

    def show_error(self, message: str) -> None:
        self.password.clear()
        self.invitation.clear()
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
            QPushButton("Open local inventory"),
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


class PreviewPolicyPage(QWidget):
    confirmed = Signal(object)
    back_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._event: EventCache | None = None
        self._custom_logo_path: Path | None = None
        self._sample_path: Path | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 28, 52, 34)
        layout.setSpacing(16)
        layout.addWidget(
            PageHeading(
                "Preview policy",
                "Choose gallery preview branding",
                "These settings are locked for the event. They affect previews only; "
                "thumbnails stay clean and original downloads keep their exact bytes.",
                "Lead setup",
            )
        )

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
        self.logo.addItem("Built-in OFTS wordmark", WatermarkLogoKind.OFTS)
        self.logo.addItem("Custom transparent PNG", WatermarkLogoKind.CUSTOM)
        self.logo.addItem("No logo", WatermarkLogoKind.NONE)
        self.logo.currentIndexChanged.connect(self._logo_changed)
        form.addRow("Logo", self.logo)
        self.custom_logo = _style_button(QPushButton("Choose custom PNG"), kind="ghost")
        self.custom_logo.clicked.connect(self._choose_custom_logo)
        form.addRow("Custom file", self.custom_logo)
        self.text = QLineEdit()
        self.text.setMaxLength(MAX_WATERMARK_TEXT_LENGTH)
        self.text.setPlaceholderText("Optional, e.g. © OFTS Studio")
        self.text.textChanged.connect(self._render_preview)
        form.addRow("Watermark text", self.text)
        self.sample = _style_button(QPushButton("Use a local photo sample"), kind="ghost")
        self.sample.clicked.connect(self._choose_sample)
        form.addRow("Preview sample", self.sample)
        self.note = QLabel(
            "The sample stays on this workstation. Custom logos preserve their original "
            "colors and transparency."
        )
        self.note.setObjectName("BodyMuted")
        self.note.setWordWrap(True)
        form.addRow(self.note)
        content.addWidget(controls, 4)

        preview_panel = QFrame()
        preview_panel.setObjectName("HeroPanel")
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(18, 16, 18, 16)
        preview_title = QLabel("Preview result")
        preview_title.setObjectName("SectionTitle")
        preview_layout.addWidget(preview_title)
        self.preview = QLabel()
        self.preview.setMinimumSize(520, 320)
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
        self.back = _style_button(QPushButton("Back to events"), kind="ghost")
        self.back.clicked.connect(self.back_requested)
        self.confirm = _style_button(QPushButton("Confirm and lock settings"), kind="primary")
        self.confirm.clicked.connect(self._confirm)
        footer.addWidget(self.back)
        footer.addStretch()
        footer.addWidget(self.confirm)
        layout.addLayout(footer)
        self._watermark_toggled(False)

    def show_event(self, event: EventCache) -> None:
        self._event = event
        self._custom_logo_path = None
        self._sample_path = None
        self.enabled.setChecked(False)
        self.template.setCurrentIndex(0)
        self.logo.setCurrentIndex(0)
        self.text.clear()
        self._render_preview()

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

    @Slot(bool)
    def _watermark_toggled(self, enabled: bool) -> None:
        for control in (self.template, self.logo, self.text):
            control.setEnabled(enabled)
        self.custom_logo.setEnabled(
            enabled and WatermarkLogoKind(self.logo.currentData()) is WatermarkLogoKind.CUSTOM
        )
        self._render_preview()

    @Slot()
    def _logo_changed(self) -> None:
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
            self, "Choose a local preview sample", "", "JPEG photos (*.jpg *.jpeg)"
        )
        if filename:
            self._sample_path = Path(filename)
            self.sample.setText(Path(filename).name)
            self._render_preview()

    @Slot()
    def _render_preview(self) -> None:
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

    @Slot()
    def _confirm(self) -> None:
        try:
            draft = self.draft()
        except WatermarkCompositionError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        choice = QMessageBox.question(
            self,
            "Lock preview settings?",
            "These preview settings apply to every contribution and cannot change after "
            "processing starts. Originals will remain untouched. Continue?",
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.confirmed.emit(draft)


class SelectionPage(QWidget):
    add_files_requested = Signal()
    add_folder_requested = Signal()
    remove_selection_requested = Signal()
    scan_requested = Signal()
    pause_requested = Signal()
    new_batch_requested = Signal()
    invitation_requested = Signal()
    intake_requested = Signal()
    finalize_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._batch_frozen = False
        self._intake_open = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 30, 52, 34)
        layout.setSpacing(18)
        layout.addWidget(
            PageHeading(
                "Contribution setup",
                "Add source media",
                "Select individual photos, complete folders, or both. "
                "Folders are scanned recursively.",
                "2 of 3",
            )
        )

        event_panel = QFrame()
        event_panel.setObjectName("HeroPanel")
        event_layout = QHBoxLayout(event_panel)
        event_layout.setContentsMargins(22, 17, 22, 17)
        event_copy = QVBoxLayout()
        event_copy.setSpacing(3)
        self.heading = QLabel()
        self.heading.setObjectName("SectionTitle")
        self.batch_meta = QLabel()
        self.batch_meta.setObjectName("BatchMeta")
        event_copy.addWidget(self.heading)
        event_copy.addWidget(self.batch_meta)
        event_layout.addLayout(event_copy, 1)
        self.batch_status = QLabel("COLLECTING")
        self.batch_status.setObjectName("StatusBadge")
        event_layout.addWidget(self.batch_status)
        self.invitation = _style_button(QPushButton("Copy uploader invitation"), kind="ghost")
        self.invitation.clicked.connect(self.invitation_requested)
        event_layout.addWidget(self.invitation)
        self.intake = _style_button(QPushButton("Close intake"), kind="ghost")
        self.intake.clicked.connect(self.intake_requested)
        event_layout.addWidget(self.intake)
        self.finalize = _style_button(QPushButton("Finalize ingestion"), kind="primary")
        self.finalize.clicked.connect(self.finalize_requested)
        event_layout.addWidget(self.finalize)
        layout.addWidget(event_panel)

        source_actions = QHBoxLayout()
        source_actions.setSpacing(14)
        self.add_files = QToolButton()
        self.add_files.setText("Add photos")
        self.add_files.setToolTip("Choose one or multiple individual photo files")
        self.add_files.setIcon(_asset_icon("file.svg"))
        self.add_files.setIconSize(QSize(30, 30))
        self.add_files.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_files.setProperty("actionCard", True)
        self.add_files.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.add_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_files.clicked.connect(self.add_files_requested)
        self.add_folder = QToolButton()
        self.add_folder.setText("Add a folder")
        self.add_folder.setToolTip("Choose a folder to discover photos recursively")
        self.add_folder.setIcon(_asset_icon("folder.svg"))
        self.add_folder.setIconSize(QSize(30, 30))
        self.add_folder.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_folder.setProperty("actionCard", True)
        self.add_folder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.add_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_folder.clicked.connect(self.add_folder_requested)
        source_actions.addWidget(self.add_files)
        source_actions.addWidget(self.add_folder)
        layout.addLayout(source_actions)

        selection_panel = QFrame()
        selection_panel.setObjectName("Panel")
        selection_layout = QVBoxLayout(selection_panel)
        selection_layout.setContentsMargins(18, 15, 18, 16)
        selection_layout.setSpacing(10)
        selection_header = QHBoxLayout()
        selected_title = QLabel("Selected sources")
        selected_title.setObjectName("SectionTitle")
        self.selection_count = QLabel("0 SOURCES")
        self.selection_count.setObjectName("StatusBadge")
        selection_header.addWidget(selected_title)
        selection_header.addStretch()
        selection_header.addWidget(self.selection_count)
        selection_layout.addLayout(selection_header)
        self.selections = QListWidget()
        self.selections.setAlternatingRowColors(True)
        self.selections.setMinimumHeight(130)
        selection_layout.addWidget(self.selections, 1)
        self.empty_hint = QLabel("No sources yet. Add photos, a folder, or both to begin.")
        self.empty_hint.setObjectName("BodyMuted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        selection_layout.addWidget(self.empty_hint)
        layout.addWidget(selection_panel, 1)

        self.progress_panel = QFrame()
        self.progress_panel.setObjectName("ProgressPanel")
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(18, 13, 18, 13)
        progress_layout.setSpacing(9)
        progress_header = QHBoxLayout()
        progress_title = QLabel("Checking local photos")
        progress_title.setObjectName("SectionTitle")
        self.pause = _style_button(QPushButton("Pause scan"), icon="pause.svg")
        self.pause.clicked.connect(self.pause_requested)
        progress_header.addWidget(progress_title)
        progress_header.addStretch()
        progress_header.addWidget(self.pause)
        progress_layout.addLayout(progress_header)
        self.progress = QProgressBar()
        progress_layout.addWidget(self.progress)
        self.progress_text = QLabel()
        self.progress_text.setObjectName("BodyMuted")
        self.progress_text.setWordWrap(True)
        progress_layout.addWidget(self.progress_text)
        self.progress_panel.hide()
        layout.addWidget(self.progress_panel)

        buttons = QHBoxLayout()
        self.remove_selection = _style_button(
            QPushButton("Remove selected"),
            icon="trash.svg",
            kind="ghost",
        )
        self.remove_selection.clicked.connect(self.remove_selection_requested)
        self.scan = _style_button(
            QPushButton("Scan and validate"),
            kind="primary",
        )
        self.scan.clicked.connect(self.scan_requested)
        self.new_batch = _style_button(
            QPushButton("New contribution"),
            icon="plus.svg",
        )
        self.new_batch.clicked.connect(self.new_batch_requested)
        buttons.addWidget(self.remove_selection)
        buttons.addStretch()
        buttons.addWidget(self.new_batch)
        buttons.addWidget(self.scan)
        layout.addLayout(buttons)

    def show_batch(self, event: EventCache, batch_id: UUID, store: CheckpointStore) -> None:
        self.heading.setText(event.name)
        self.batch_meta.setText(f"Contribution  {batch_id}")
        self.selections.clear()
        for selection in store.list_selections(batch_id):
            icon = "folder.svg" if selection.kind.value == "folder" else "file.svg"
            item = QListWidgetItem(
                _asset_icon(icon),
                f"{selection.kind.value.title()}    {selection.source_path}",
            )
            item.setSizeHint(QSize(0, 48))
            item.setData(Qt.ItemDataRole.UserRole, selection.id)
            self.selections.addItem(item)
        batch = store.get_batch(batch_id)
        self._batch_frozen = batch.frozen
        self.batch_status.setText("FROZEN" if batch.frozen else "COLLECTING")
        self.batch_status.setProperty("status", "ready" if batch.frozen else "collecting")
        self.batch_status.style().unpolish(self.batch_status)
        self.batch_status.style().polish(self.batch_status)
        count = self.selections.count()
        self.selection_count.setText(f"{count} {'SOURCE' if count == 1 else 'SOURCES'}")
        self.empty_hint.setVisible(count == 0)
        self.add_files.setEnabled(not batch.frozen)
        self.add_folder.setEnabled(not batch.frozen)
        self.remove_selection.setEnabled(not batch.frozen)
        is_lead = event.role == "lead"
        intake_open = event.intake_state == "open"
        self._intake_open = intake_open
        self.invitation.setVisible(is_lead)
        self.invitation.setEnabled(is_lead and intake_open)
        self.intake.setVisible(is_lead)
        self.intake.setText("Close intake" if intake_open else "Reopen intake")
        self.finalize.setVisible(is_lead)
        self.finalize.setEnabled(is_lead and not intake_open)
        self.add_files.setEnabled(not batch.frozen and intake_open)
        self.add_folder.setEnabled(not batch.frozen and intake_open)
        self.remove_selection.setEnabled(not batch.frozen and intake_open)
        self.scan.setEnabled(intake_open)
        self.new_batch.setEnabled(intake_open)
        self.progress_panel.hide()
        self.progress_text.clear()

    def scan_started(self) -> None:
        self.progress.setRange(0, 0)
        self.progress_panel.show()
        for button in (
            self.add_files,
            self.add_folder,
            self.remove_selection,
            self.scan,
            self.new_batch,
            self.invitation,
            self.intake,
            self.finalize,
        ):
            button.setEnabled(False)
        self.progress_text.setText("Discovering and validating local files…")

    def scan_progressed(self, progress: ScanProgress) -> None:
        self.progress_text.setText(
            f"Checked {progress.processed_count} files: {progress.accepted_count} accepted, "
            f"{progress.rejected_count} rejected. Current: {progress.current_path}"
        )

    def scan_stopped(self) -> None:
        self.progress_panel.hide()
        self.add_files.setEnabled(not self._batch_frozen and self._intake_open)
        self.add_folder.setEnabled(not self._batch_frozen and self._intake_open)
        self.remove_selection.setEnabled(not self._batch_frozen and self._intake_open)
        self.scan.setEnabled(self._intake_open)
        self.new_batch.setEnabled(self._intake_open)
        self.invitation.setEnabled(self._intake_open)
        self.intake.setEnabled(True)
        self.finalize.setEnabled(not self._intake_open)


class ValidationPage(QWidget):
    approve_requested = Signal()
    rescan_requested = Signal()
    export_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 30, 52, 34)
        layout.setSpacing(18)
        layout.addWidget(
            PageHeading(
                "Local inventory",
                "Review inventory",
                "Accepted photos are ready. Resolve any blocking rows before freezing the batch.",
                "3 of 3",
            )
        )

        stats = QGridLayout()
        stats.setHorizontalSpacing(12)
        self.accepted_stat = StatCard("Accepted", tone="success")
        self.rejected_stat = StatCard("Rejected", tone="danger")
        self.blocking_stat = StatCard("Blocking", tone="warning")
        self.reused_stat = StatCard("Checkpoints reused", tone="accent")
        for column, card in enumerate(
            (self.accepted_stat, self.rejected_stat, self.blocking_stat, self.reused_stat)
        ):
            stats.addWidget(card, 0, column)
        layout.addLayout(stats)

        self.summary = QLabel()
        self.summary.setObjectName("InfoBanner")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        review_panel = QFrame()
        review_panel.setObjectName("Panel")
        review_layout = QVBoxLayout(review_panel)
        review_layout.setContentsMargins(18, 15, 18, 16)
        review_layout.setSpacing(10)
        review_title = QLabel("Items needing attention")
        review_title.setObjectName("SectionTitle")
        review_layout.addWidget(review_title)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Local file", "Status", "Reason"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        review_layout.addWidget(self.table, 1)
        self.review_empty = QLabel("Everything passed local validation.")
        self.review_empty.setObjectName("BodyMuted")
        self.review_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        review_layout.addWidget(self.review_empty)
        layout.addWidget(review_panel, 1)

        buttons = QHBoxLayout()
        self.approve = _style_button(
            QPushButton("Approve contribution"),
            kind="primary",
        )
        self.approve.clicked.connect(self.approve_requested)
        self.rescan = _style_button(QPushButton("Back to sources"), icon="arrow-left.svg")
        self.rescan.clicked.connect(self.rescan_requested)
        self.export = _style_button(
            QPushButton("Export redacted diagnostics"),
            icon="download.svg",
            kind="ghost",
        )
        self.export.clicked.connect(self.export_requested)
        buttons.addWidget(self.export)
        buttons.addStretch()
        buttons.addWidget(self.rescan)
        buttons.addWidget(self.approve)
        layout.addLayout(buttons)

    def show_summary(self, summary: ScanSummary, store: CheckpointStore, *, batch_id: UUID) -> None:
        self.summary.setText(
            f"{format_bytes(summary.accepted_bytes)} accepted and "
            f"{format_bytes(summary.rejected_bytes)} rejected. "
            "Only local metadata and checksums have been created; no photos were transferred."
        )
        self.accepted_stat.value.setText(str(summary.accepted_count))
        self.rejected_stat.value.setText(str(summary.rejected_count))
        self.blocking_stat.value.setText(
            str(summary.blocking_item_count + summary.blocking_issue_count)
        )
        self.reused_stat.value.setText(str(summary.reused_count))
        rows: list[tuple[str, str, str]] = []
        for item in store.list_items(batch_id):
            if item.status is not InventoryStatus.ACCEPTED:
                rows.append(
                    (
                        str(item.source_path),
                        item.status.value,
                        item.reason.value.replace("_", " ").title() if item.reason else "",
                    )
                )
        for issue in store.list_scan_issues(batch_id):
            rows.append(
                (
                    str(issue.source_path),
                    "blocking" if issue.blocking else "warning",
                    issue.reason.value.replace("_", " ").title(),
                )
            )
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                item = QTableWidgetItem(value)
                if column_index == 1:
                    color = "#ff8294" if value in {"rejected", "blocking"} else "#f7c66d"
                    item.setForeground(QColor(color))
                self.table.setItem(row_index, column_index, item)
        self.review_empty.setVisible(not rows)
        self.approve.setEnabled(summary.can_approve and summary.state is BatchState.NEEDS_REVIEW)


class ApprovedPage(QWidget):
    upload_requested = Signal(int)
    pause_requested = Signal()
    new_batch_requested = Signal()
    verify_requested = Signal()
    export_requested = Signal()
    cleanup_requested = Signal()
    invitation_requested = Signal()
    intake_requested = Signal()
    finalize_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(52, 34, 52, 38)
        layout.setSpacing(22)
        layout.addWidget(
            PageHeading(
                "Local approval",
                "Contribution approved",
                "The verified batch is frozen and protected from accidental source changes.",
                "Complete",
            )
        )

        hero = QFrame()
        hero.setObjectName("HeroPanel")
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(28, 24, 28, 24)
        success_icon = QLabel("✓")
        success_icon.setObjectName("SuccessMark")
        success_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_layout.addWidget(success_icon, 0, Qt.AlignmentFlag.AlignTop)
        copy = QVBoxLayout()
        title = QLabel("Local inventory locked")
        title.setObjectName("HeroTitle")
        copy.addWidget(title)
        self.message = QLabel()
        self.message.setObjectName("HeroCopy")
        self.message.setWordWrap(True)
        copy.addWidget(self.message)
        hero_layout.addLayout(copy, 1)
        ready = QLabel("READY")
        ready.setObjectName("StatusBadge")
        ready.setProperty("status", "ready")
        hero_layout.addWidget(ready, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(hero)

        stages = QHBoxLayout()
        stages.setSpacing(12)
        for number, title_text, detail in (
            ("01", "Inventory", "Photos checked locally"),
            ("02", "Approval", "Contribution frozen"),
            ("03", "Cloud upload", "Ready for private transfer"),
        ):
            stage = QFrame()
            stage.setObjectName("StatCard")
            stage_layout = QVBoxLayout(stage)
            stage_layout.setContentsMargins(18, 16, 18, 16)
            stage_number = QLabel(number)
            stage_number.setObjectName("PageEyebrow")
            stage_title = QLabel(title_text)
            stage_title.setObjectName("SectionTitle")
            stage_detail = QLabel(detail)
            stage_detail.setObjectName("BodyMuted")
            if number == "03":
                self.cloud_stage_detail = stage_detail
            stage_layout.addWidget(stage_number)
            stage_layout.addWidget(stage_title)
            stage_layout.addWidget(stage_detail)
            stages.addWidget(stage, 1)
        layout.addLayout(stages)

        transfer_panel = QFrame()
        transfer_panel.setObjectName("Panel")
        transfer_layout = QVBoxLayout(transfer_panel)
        transfer_layout.setContentsMargins(22, 18, 22, 18)
        transfer_layout.setSpacing(10)
        transfer_heading = QHBoxLayout()
        transfer_title = QLabel("Private cloud transfer")
        transfer_title.setObjectName("SectionTitle")
        transfer_heading.addWidget(transfer_title)
        transfer_heading.addStretch()
        transfer_heading.addWidget(QLabel("Concurrent uploads"))
        self.transfer_limit = QSpinBox()
        self.transfer_limit.setRange(1, 4)
        self.transfer_limit.setValue(3)
        transfer_heading.addWidget(self.transfer_limit)
        transfer_layout.addLayout(transfer_heading)
        self.upload_progress = QProgressBar()
        self.upload_progress.setRange(0, 1)
        self.upload_progress.setValue(0)
        self.original_progress = self.upload_progress
        transfer_layout.addWidget(QLabel("Originals"))
        transfer_layout.addWidget(self.upload_progress)
        transfer_layout.addWidget(QLabel("Gallery previews and thumbnails"))
        self.derivative_progress = QProgressBar()
        self.derivative_progress.setRange(0, 1)
        self.derivative_progress.setValue(0)
        transfer_layout.addWidget(self.derivative_progress)
        transfer_buttons = QHBoxLayout()
        self.upload = _style_button(QPushButton("Process and upload contribution"), kind="primary")
        self.upload.clicked.connect(lambda: self.upload_requested.emit(self.transfer_limit.value()))
        self.pause = _style_button(QPushButton("Pause after active uploads"), kind="ghost")
        self.pause.clicked.connect(self.pause_requested)
        self.pause.setEnabled(False)
        transfer_buttons.addStretch()
        transfer_buttons.addWidget(self.pause)
        transfer_buttons.addWidget(self.upload)
        transfer_layout.addLayout(transfer_buttons)
        layout.addWidget(transfer_panel)

        self.lead_panel = QFrame()
        self.lead_panel.setObjectName("Panel")
        lead_layout = QHBoxLayout(self.lead_panel)
        lead_layout.setContentsMargins(22, 16, 22, 16)
        lead_copy = QLabel("Lead controls")
        lead_copy.setObjectName("SectionTitle")
        lead_layout.addWidget(lead_copy)
        lead_layout.addStretch()
        self.invitation = _style_button(QPushButton("Copy uploader invitation"), kind="ghost")
        self.invitation.clicked.connect(self.invitation_requested)
        self.intake = _style_button(QPushButton("Close intake"), kind="ghost")
        self.intake.clicked.connect(self.intake_requested)
        self.finalize = _style_button(QPushButton("Finalize ingestion"), kind="primary")
        self.finalize.clicked.connect(self.finalize_requested)
        lead_layout.addWidget(self.invitation)
        lead_layout.addWidget(self.intake)
        lead_layout.addWidget(self.finalize)
        layout.addWidget(self.lead_panel)

        buttons = QHBoxLayout()
        self.new_batch = _style_button(
            QPushButton("Create another contribution"),
            kind="primary",
        )
        self.new_batch.clicked.connect(self.new_batch_requested)
        self.verify = _style_button(QPushButton("Verify sources again"), icon="refresh.svg")
        self.verify.clicked.connect(self.verify_requested)
        self.export = _style_button(
            QPushButton("Export diagnostics"),
            icon="download.svg",
            kind="ghost",
        )
        self.export.clicked.connect(self.export_requested)
        self.cleanup = _style_button(
            QPushButton("Remove local checkpoint"),
            icon="trash.svg",
            kind="danger",
        )
        self.cleanup.clicked.connect(self.cleanup_requested)
        buttons.addWidget(self.export)
        buttons.addWidget(self.cleanup)
        buttons.addStretch()
        buttons.addWidget(self.verify)
        buttons.addWidget(self.new_batch)
        layout.addLayout(buttons)
        layout.addStretch()

    def show_batch(
        self,
        batch_id: UUID,
        *,
        state: BatchState,
        event: EventCache,
        excluded_count: int = 0,
        derivative_failure_count: int = 0,
        derivatives_complete: bool = False,
    ) -> None:
        originals_complete = state is BatchState.COMPLETE
        complete = originals_complete and derivatives_complete
        if complete:
            if excluded_count:
                self.message.setText(
                    f"Batch {batch_id} is resolved with {excluded_count} lead-approved "
                    "exclusion(s). New photos belong in a new contribution."
                )
                self.cloud_stage_detail.setText("Verified originals stored; exclusions recorded")
            else:
                self.message.setText(
                    f"Batch {batch_id} and its gallery media are verified in private object "
                    "storage. New photos belong in a new contribution."
                )
                self.cloud_stage_detail.setText("Originals and gallery media verified")
        elif originals_complete:
            if derivative_failure_count:
                self.message.setText(
                    f"Batch {batch_id} has verified originals, but {derivative_failure_count} "
                    "photo(s) need gallery processing retry or lead review."
                )
                self.cloud_stage_detail.setText("Gallery derivative failure")
            else:
                self.message.setText(
                    f"Batch {batch_id} has verified originals. Gallery previews and clean "
                    "thumbnails are ready to resume."
                )
                self.cloud_stage_detail.setText("Gallery processing pending")
        else:
            self.message.setText(
                f"Batch {batch_id} is locally verified and ready for private upload. "
                "New photos belong in a new contribution."
            )
            self.cloud_stage_detail.setText("Resume-safe transfer pending")
        can_sync = event.intake_state == "open" or originals_complete
        self.upload.setEnabled(not complete and can_sync)
        self.pause.setEnabled(False)
        self.transfer_limit.setEnabled(True)
        self.verify.setEnabled(state is BatchState.APPROVED)
        self.new_batch.setEnabled(event.intake_state == "open")
        is_lead = event.role == "lead"
        self.lead_panel.setVisible(is_lead)
        self.invitation.setEnabled(is_lead and event.intake_state == "open")
        self.intake.setText("Reopen intake" if event.intake_state == "closed" else "Close intake")
        self.finalize.setEnabled(is_lead and event.intake_state == "closed")

    @Slot()
    def upload_started(self) -> None:
        self.upload.setEnabled(False)
        self.pause.setEnabled(True)
        self.transfer_limit.setEnabled(False)
        self.cloud_stage_detail.setText("Uploading originals")

    @Slot(int, int)
    def upload_progressed(self, completed: int, total: int) -> None:
        self.original_progress.setRange(0, max(total, 1))
        self.original_progress.setValue(completed)
        self.cloud_stage_detail.setText(f"{completed} of {total} originals verified")

    @Slot(str, int, int)
    def stage_progressed(self, stage: str, completed: int, total: int) -> None:
        if stage == "originals":
            self.upload_progressed(completed, total)
            return
        self.derivative_progress.setRange(0, max(total, 1))
        self.derivative_progress.setValue(completed)
        self.cloud_stage_detail.setText(f"{completed} of {total} gallery derivatives verified")

    def upload_stopped(self, *, paused: bool = False) -> None:
        self.pause.setEnabled(False)
        self.upload.setEnabled(True)
        self.transfer_limit.setEnabled(True)
        if paused:
            self.cloud_stage_detail.setText("Paused; verified files will not restart")


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        store: CheckpointStore,
        gateway: DesktopGateway,
        demo_event: EventCache | None = None,
    ) -> None:
        super().__init__()
        application = QApplication.instance()
        if isinstance(application, QApplication):
            apply_corporate_theme(application)
        self.store = store
        self.gateway = gateway
        self.current_event: EventCache | None = None
        self.current_batch_id: UUID | None = None
        self.scan_thread: QThread | None = None
        self.scan_stop: Event | None = None
        self.upload_thread: QThread | None = None
        self.upload_stop: Event | None = None

        shell = QWidget()
        shell.setObjectName("AppShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.header = BrandHeader(demo=demo_event is not None)
        shell_layout.addWidget(self.header)

        self.stack = QStackedWidget()
        self.stack.setObjectName("PageStack")
        self.login = LoginPage()
        self.events = EventSelectorPage()
        self.preview_policy = PreviewPolicyPage()
        self.selection = SelectionPage()
        self.validation = ValidationPage()
        self.approved = ApprovedPage()
        for page in (
            self.login,
            self.events,
            self.preview_policy,
            self.selection,
            self.validation,
            self.approved,
        ):
            self.stack.addWidget(page)
        shell_layout.addWidget(self.stack, 1)
        self.setCentralWidget(shell)
        self.setWindowTitle("OpenFotos • Desktop Ingestion")
        self.setWindowIcon(QIcon(str(asset_path("ofts.svg"))))
        self.setMinimumSize(900, 650)
        self.resize(1180, 780)
        self._connect_actions()

        if demo_event is None:
            cached_origins = {
                event.server_url for event in self.store.list_events() if event.server_url
            }
            if len(cached_origins) == 1:
                [origin] = cached_origins
                self.login.lead_server.setText(origin)
                self.login.uploader_server.setText(origin)
            self.stack.setCurrentWidget(self.login)
        else:
            self.store.cache_event(demo_event)
            self.events.set_events([demo_event], demo=True)
            self.stack.setCurrentWidget(self.events)

    def _connect_actions(self) -> None:
        self.login.lead_requested.connect(self._lead_login)
        self.login.uploader_requested.connect(self._enroll_uploader)
        self.login.resume_requested.connect(self._resume_session)
        self.events.selected.connect(self._open_event)
        self.preview_policy.confirmed.connect(self._confirm_preview_policy)
        self.preview_policy.back_requested.connect(lambda: self.stack.setCurrentWidget(self.events))
        self.selection.add_files_requested.connect(self._add_files)
        self.selection.add_folder_requested.connect(self._add_folder)
        self.selection.remove_selection_requested.connect(self._remove_selection)
        self.selection.scan_requested.connect(self._start_scan)
        self.selection.pause_requested.connect(self._pause_scan)
        self.selection.new_batch_requested.connect(self._new_batch)
        self.selection.invitation_requested.connect(self._create_invitation)
        self.selection.intake_requested.connect(self._toggle_intake)
        self.selection.finalize_requested.connect(self._finalize_ingestion)
        self.validation.approve_requested.connect(self._approve_batch)
        self.validation.rescan_requested.connect(self._show_selection)
        self.validation.export_requested.connect(self._export_diagnostics)
        self.approved.new_batch_requested.connect(self._new_batch)
        self.approved.verify_requested.connect(self._show_selection)
        self.approved.export_requested.connect(self._export_diagnostics)
        self.approved.cleanup_requested.connect(self._cleanup_event)
        self.approved.upload_requested.connect(self._start_upload)
        self.approved.pause_requested.connect(self._pause_upload)
        self.approved.invitation_requested.connect(self._create_invitation)
        self.approved.intake_requested.connect(self._toggle_intake)
        self.approved.finalize_requested.connect(self._finalize_ingestion)

    @Slot(str, str, str, str)
    def _lead_login(
        self,
        server_url: str,
        username: str,
        password: str,
        device_label: str,
    ) -> None:
        try:
            events = list(self.gateway.sign_in_lead(server_url, username, password, device_label))
        except RuntimeError as exc:
            self.login.show_error(str(exc))
            return
        self.login.password.clear()
        self._show_persistence_warning()
        for event in events:
            self.store.cache_event(event)
        self.events.set_events(events, demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot(str, str, str)
    def _enroll_uploader(self, server_url: str, invitation: str, device_label: str) -> None:
        try:
            event = self.gateway.enroll_uploader(server_url, invitation, device_label)
        except RuntimeError as exc:
            self.login.show_error(str(exc))
            return
        self.login.invitation.clear()
        self._show_persistence_warning()
        self.store.cache_event(event)
        self.events.set_events([event], demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot(str)
    def _resume_session(self, server_url: str) -> None:
        try:
            events = list(self.gateway.resume(server_url))
        except RuntimeError as exc:
            self.login.show_error(str(exc))
            return
        self._show_persistence_warning()
        self.events.set_events(events, demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot(object)
    def _open_event(self, event: EventCache) -> None:
        self.current_event = event
        self.header.set_context(event.name)
        if event.role == "lead" and event.preview_policy is None:
            self.preview_policy.show_event(event)
            self.stack.setCurrentWidget(self.preview_policy)
            return
        self._open_event_inventory()

    def _open_event_inventory(self) -> None:
        if self.current_event is None:
            return
        event = self.current_event
        batches = self.store.list_batches(event.id)
        self.current_batch_id = batches[-1].id if batches else self.store.create_batch(event.id)
        batch = self.store.get_batch(self.current_batch_id)
        if batch.state in {
            BatchState.APPROVED,
            BatchState.RESERVED,
            BatchState.UPLOADING,
            BatchState.COMPLETE,
        }:
            self._show_approved()
        elif batch.state is BatchState.NEEDS_REVIEW:
            self._show_validation(self.store.summary(batch.id))
        else:
            self._show_selection()

    @Slot(object)
    def _confirm_preview_policy(self, draft: dict) -> None:
        if self.current_event is None:
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
            self.preview_policy.error.setText(str(exc))
            self.preview_policy.error.show()
            return
        self.current_event = event
        self.store.cache_event(event)
        self._open_event_inventory()

    @Slot()
    def _new_batch(self) -> None:
        if self.current_event is None:
            return
        self.current_batch_id = self.store.create_batch(self.current_event.id)
        self._show_selection()

    @Slot()
    def _show_selection(self) -> None:
        if self.current_event is None or self.current_batch_id is None:
            return
        self.selection.show_batch(self.current_event, self.current_batch_id, self.store)
        self.stack.setCurrentWidget(self.selection)

    @Slot()
    def _add_files(self) -> None:
        if self.current_batch_id is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose source files", "", "All files (*)")
        if paths:
            try:
                self.store.add_files(self.current_batch_id, paths)
            except ValueError as exc:
                self._show_error(str(exc))
            self._show_selection()

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
            self._show_selection()

    @Slot()
    def _remove_selection(self) -> None:
        item = self.selection.selections.currentItem()
        if item is None:
            return
        try:
            self.store.remove_selection(item.data(Qt.ItemDataRole.UserRole))
        except ValueError as exc:
            self._show_error(str(exc))
        self._show_selection()

    @Slot()
    def _start_scan(self) -> None:
        if self.current_batch_id is None or self.scan_thread is not None:
            return
        if not self.store.list_selections(self.current_batch_id):
            self._show_error("Add at least one file or folder before scanning.")
            return
        self.selection.scan_started()
        self.scan_stop = Event()
        thread = QThread(self)
        worker = ScanWorker(
            InventoryScanner(self.store),
            self.current_batch_id,
            self.scan_stop,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self.selection.scan_progressed)
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
    def _pause_scan(self) -> None:
        if self.scan_stop is not None:
            self.scan_stop.set()
            self.selection.progress_text.setText("Pausing after the current file…")

    @Slot(object)
    def _scan_completed(self, summary: ScanSummary) -> None:
        self.selection.scan_stopped()
        if summary.state is BatchState.APPROVED and summary.warning_count == 0:
            self._show_approved()
        else:
            self._show_validation(summary)

    @Slot()
    def _scan_cancelled(self) -> None:
        self.selection.scan_stopped()
        self.selection.progress_text.setText("Scan paused. Run it again to resume checkpoints.")

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self.selection.scan_stopped()
        self._show_error(f"Inventory failed: {message}")

    @Slot()
    def _scan_thread_finished(self) -> None:
        if self.scan_thread:
            self.scan_thread.deleteLater()
        self.scan_thread = None
        self.scan_stop = None
        self._scan_worker = None

    def _show_validation(self, summary: ScanSummary) -> None:
        if self.current_batch_id is None:
            return
        self.validation.show_summary(summary, self.store, batch_id=self.current_batch_id)
        self.stack.setCurrentWidget(self.validation)

    @Slot()
    def _approve_batch(self) -> None:
        if self.current_batch_id is None:
            return
        try:
            self.store.approve_batch(
                self.current_batch_id,
                supported_profile_id=SUPPORTED_PROCESSING_PROFILE_ID,
            )
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self._show_approved()

    def _show_approved(self) -> None:
        if self.current_event is None or self.current_batch_id is None:
            return
        batch = self.store.get_batch(self.current_batch_id)
        upload_checkpoints = self.store.list_upload_checkpoints(batch.id)
        derivative_checkpoints = self.store.list_derivative_checkpoints(batch.id)
        excluded_items = {
            checkpoint.item_id
            for checkpoint in (*upload_checkpoints, *derivative_checkpoints)
            if checkpoint.state is LocalUploadState.EXCLUDED
        }
        failed_derivative_items = {
            checkpoint.item_id
            for checkpoint in derivative_checkpoints
            if checkpoint.state is LocalUploadState.FAILED
        }
        self.approved.show_batch(
            batch.id,
            state=batch.state,
            event=self.current_event,
            excluded_count=len(excluded_items),
            derivative_failure_count=len(failed_derivative_items),
            derivatives_complete=self.store.derivatives_complete(batch.id),
        )
        self.stack.setCurrentWidget(self.approved)

    @Slot(int)
    def _start_upload(self, transfer_limit: int) -> None:
        if self.current_batch_id is None or self.upload_thread is not None:
            return
        self.approved.upload_started()
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
        worker.progressed.connect(self.approved.upload_progressed)
        worker.stage_progressed.connect(self.approved.stage_progressed)
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
    def _pause_upload(self) -> None:
        if self.upload_stop is not None:
            self.upload_stop.set()
            self.approved.cloud_stage_detail.setText("Pausing after active uploads…")

    @Slot()
    def _upload_completed(self) -> None:
        self._show_approved()

    @Slot()
    def _upload_cancelled(self) -> None:
        self.approved.upload_stopped(paused=True)

    @Slot(str)
    def _upload_failed(self, message: str) -> None:
        self.approved.upload_stopped()
        self._show_error(f"Upload stopped: {message}")

    @Slot()
    def _upload_thread_finished(self) -> None:
        if self.upload_thread:
            self.upload_thread.deleteLater()
        self.upload_thread = None
        self.upload_stop = None
        self._upload_worker = None

    @Slot()
    def _create_invitation(self) -> None:
        if self.current_event is None:
            return
        try:
            invitation = self.gateway.create_invitation(self.current_event.id)
        except RuntimeError as exc:
            self._show_error(str(exc))
            return
        clipboard = QApplication.clipboard()
        clipboard.setText(invitation)
        QMessageBox.information(
            self,
            "Uploader invitation copied",
            "The 72-hour uploader invitation was copied to the clipboard.",
        )

    @Slot()
    def _toggle_intake(self) -> None:
        if self.current_event is None:
            return
        selection_visible = self.stack.currentWidget() is self.selection
        try:
            if self.current_event.intake_state == "open":
                event = self.gateway.close_intake(self.current_event.id)
            else:
                event = self.gateway.reopen_intake(self.current_event.id)
        except RuntimeError as exc:
            self._show_error(str(exc))
            return
        self.current_event = event
        self.store.cache_event(event)
        if selection_visible:
            self._show_selection()
        else:
            self._show_approved()

    @Slot()
    def _finalize_ingestion(self) -> None:
        if self.current_event is None:
            return
        try:
            result = self.gateway.finalize(self.current_event.id)
        except RuntimeError as exc:
            self._show_error(str(exc))
            return
        QMessageBox.information(
            self,
            "Ingestion finalized",
            f"Generation {result['generation']} committed with {result['asset_count']} originals.",
        )
        self.selection.finalize.setEnabled(False)
        self.approved.finalize.setEnabled(False)

    def _show_persistence_warning(self) -> None:
        warning = getattr(self.gateway, "persistence_warning", None)
        if warning:
            self.login.show_notice(warning)

    @Slot()
    def _export_diagnostics(self) -> None:
        if self.current_batch_id is None:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Export redacted diagnostics",
            "openfotos-diagnostic.json",
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
            "This removes local OpenFotos paths and checksums for this event. "
            "It never deletes source photographs. Continue?",
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_local_event(self.current_event.id)
        self.current_event = None
        self.current_batch_id = None
        self.header.set_context("Private event ingestion")
        remaining = self.store.list_events()
        self.events.set_events(remaining, demo=False)
        self.stack.setCurrentWidget(self.events if remaining else self.login)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "OpenFotos", message)

    def closeEvent(self, event: QCloseEvent) -> None:
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
