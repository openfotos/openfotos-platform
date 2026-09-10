"""Qt Widgets screens for Session 3 local inventory."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import UUID

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .diagnostics import RedactedDiagnosticExporter
from .ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    InventoryStatus,
    ScanCancelled,
    ScanProgress,
    ScanSummary,
)
from .ports import OnlineServicesUnavailable, PhotographerSessionGateway

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

SUPPORTED_PROCESSING_PROFILE_ID = "pilot-profile-v1"


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


class LoginPage(QWidget):
    lead_requested = Signal(str, str, str)
    uploader_requested = Signal(str, str, str)

    def __init__(self) -> None:
        super().__init__()
        self._batch_frozen = False
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h1>OpenFotos</h1>"))
        layout.addWidget(QLabel("Connect as the event lead or enroll an upload-only device."))
        tabs = QTabWidget()
        layout.addWidget(tabs)

        lead = QWidget()
        lead_form = QFormLayout(lead)
        self.lead_server = QLineEdit("https://openfotos.example")
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.lead_button = QPushButton("Sign in as lead")
        self.lead_button.clicked.connect(self._request_lead)
        lead_form.addRow("Server", self.lead_server)
        lead_form.addRow("Username", self.username)
        lead_form.addRow("Password", self.password)
        lead_form.addRow(self.lead_button)
        tabs.addTab(lead, "Lead sign in")

        uploader = QWidget()
        uploader_form = QFormLayout(uploader)
        self.uploader_server = QLineEdit("https://openfotos.example")
        self.invitation = QLineEdit()
        self.invitation.setEchoMode(QLineEdit.EchoMode.Password)
        self.device_label = QLineEdit()
        uploader_button = QPushButton("Enroll this device")
        uploader_button.clicked.connect(self._request_uploader)
        uploader_form.addRow("Server", self.uploader_server)
        uploader_form.addRow("Invitation", self.invitation)
        uploader_form.addRow("Device label", self.device_label)
        uploader_form.addRow(uploader_button)
        tabs.addTab(uploader, "Upload invitation")

        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        layout.addStretch()

    @Slot()
    def _request_lead(self) -> None:
        self.error.clear()
        self.lead_requested.emit(
            self.lead_server.text().strip(),
            self.username.text().strip(),
            self.password.text(),
        )

    @Slot()
    def _request_uploader(self) -> None:
        self.error.clear()
        self.uploader_requested.emit(
            self.uploader_server.text().strip(),
            self.invitation.text(),
            self.device_label.text().strip(),
        )

    def show_error(self, message: str) -> None:
        self.password.clear()
        self.invitation.clear()
        self.error.setText(message)


class EventSelectorPage(QWidget):
    selected = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.heading = QLabel("<h2>Select an event</h2>")
        layout.addWidget(self.heading)
        self.events = QListWidget()
        layout.addWidget(self.events)
        self.open_button = QPushButton("Open local inventory")
        self.open_button.clicked.connect(self._select)
        layout.addWidget(self.open_button)

    def set_events(self, events: list[EventCache], *, demo: bool) -> None:
        self.events.clear()
        self.heading.setText(
            "<h2>Select a synthetic demo event</h2>" if demo else "<h2>Select an event</h2>"
        )
        for event in events:
            item = QListWidgetItem(
                f"{event.name} — cached allowance {format_bytes(event.storage_limit_bytes)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, event)
            self.events.addItem(item)
        if self.events.count():
            self.events.setCurrentRow(0)

    @Slot()
    def _select(self) -> None:
        item = self.events.currentItem()
        if item:
            self.selected.emit(item.data(Qt.ItemDataRole.UserRole))


class SelectionPage(QWidget):
    add_files_requested = Signal()
    add_folder_requested = Signal()
    remove_selection_requested = Signal()
    scan_requested = Signal()
    pause_requested = Signal()
    new_batch_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        self.heading = QLabel()
        layout.addWidget(self.heading)
        layout.addWidget(
            QLabel(
                "Add one or many files and one or many folders. Folders are scanned recursively; "
                "links are not followed."
            )
        )
        self.selections = QListWidget()
        layout.addWidget(self.selections)
        buttons = QHBoxLayout()
        self.add_files = QPushButton("Add photos")
        self.add_files.clicked.connect(self.add_files_requested)
        self.add_folder = QPushButton("Add folder")
        self.add_folder.clicked.connect(self.add_folder_requested)
        self.remove_selection = QPushButton("Remove selected")
        self.remove_selection.clicked.connect(self.remove_selection_requested)
        self.scan = QPushButton("Scan and validate")
        self.scan.clicked.connect(self.scan_requested)
        self.new_batch = QPushButton("New contribution")
        self.new_batch.clicked.connect(self.new_batch_requested)
        self.pause = QPushButton("Pause scan")
        self.pause.clicked.connect(self.pause_requested)
        self.pause.hide()
        for button in (
            self.add_files,
            self.add_folder,
            self.remove_selection,
            self.scan,
            self.pause,
            self.new_batch,
        ):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.progress_text = QLabel()
        self.progress_text.setWordWrap(True)
        layout.addWidget(self.progress_text)

    def show_batch(self, event: EventCache, batch_id: UUID, store: CheckpointStore) -> None:
        self.heading.setText(f"<h2>{event.name}</h2><p>Contribution {batch_id}</p>")
        self.selections.clear()
        for selection in store.list_selections(batch_id):
            item = QListWidgetItem(f"{selection.kind.value.title()}: {selection.source_path}")
            item.setData(Qt.ItemDataRole.UserRole, selection.id)
            self.selections.addItem(item)
        batch = store.get_batch(batch_id)
        self._batch_frozen = batch.frozen
        self.add_files.setEnabled(not batch.frozen)
        self.add_folder.setEnabled(not batch.frozen)
        self.remove_selection.setEnabled(not batch.frozen)
        self.progress.hide()
        self.pause.hide()
        self.progress_text.clear()

    def scan_started(self) -> None:
        self.progress.setRange(0, 0)
        self.progress.show()
        self.pause.show()
        for button in (
            self.add_files,
            self.add_folder,
            self.remove_selection,
            self.scan,
            self.new_batch,
        ):
            button.setEnabled(False)
        self.progress_text.setText("Discovering and validating local files…")

    def scan_progressed(self, progress: ScanProgress) -> None:
        self.progress_text.setText(
            f"Checked {progress.processed_count} files: {progress.accepted_count} accepted, "
            f"{progress.rejected_count} rejected. Current: {progress.current_path}"
        )

    def scan_stopped(self) -> None:
        self.progress.hide()
        self.pause.hide()
        self.add_files.setEnabled(not self._batch_frozen)
        self.add_folder.setEnabled(not self._batch_frozen)
        self.remove_selection.setEnabled(not self._batch_frozen)
        self.scan.setEnabled(True)
        self.new_batch.setEnabled(True)


class ValidationPage(QWidget):
    approve_requested = Signal()
    rescan_requested = Signal()
    export_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Review inventory</h2>"))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Local file", "Status", "Reason"])
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.approve = QPushButton("Approve contribution")
        self.approve.clicked.connect(self.approve_requested)
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.rescan_requested)
        export = QPushButton("Export redacted diagnostics")
        export.clicked.connect(self.export_requested)
        buttons.addWidget(self.approve)
        buttons.addWidget(rescan)
        buttons.addWidget(export)
        layout.addLayout(buttons)

    def show_summary(self, summary: ScanSummary, store: CheckpointStore, *, batch_id: UUID) -> None:
        self.summary.setText(
            f"Accepted: {summary.accepted_count} ({format_bytes(summary.accepted_bytes)}). "
            f"Rejected: {summary.rejected_count} ({format_bytes(summary.rejected_bytes)}). "
            f"Blocking items/issues: {summary.blocking_item_count}/"
            f"{summary.blocking_issue_count}. Reused checkpoints: {summary.reused_count}."
        )
        rows: list[tuple[str, str, str]] = []
        for item in store.list_items(batch_id):
            if item.status is not InventoryStatus.ACCEPTED:
                rows.append(
                    (
                        str(item.source_path),
                        item.status.value,
                        item.reason.value if item.reason else "",
                    )
                )
        for issue in store.list_scan_issues(batch_id):
            rows.append(
                (
                    str(issue.source_path),
                    "blocking" if issue.blocking else "warning",
                    issue.reason.value,
                )
            )
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column_index, value in enumerate(row):
                self.table.setItem(row_index, column_index, QTableWidgetItem(value))
        self.approve.setEnabled(summary.can_approve and summary.state is BatchState.NEEDS_REVIEW)


class ApprovedPage(QWidget):
    new_batch_requested = Signal()
    verify_requested = Signal()
    export_requested = Signal()
    cleanup_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<h2>Contribution ready</h2>"))
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        new_batch = QPushButton("Create another contribution")
        new_batch.clicked.connect(self.new_batch_requested)
        verify = QPushButton("Verify sources again")
        verify.clicked.connect(self.verify_requested)
        export = QPushButton("Export redacted diagnostics")
        export.clicked.connect(self.export_requested)
        cleanup = QPushButton("Remove local event checkpoint")
        cleanup.clicked.connect(self.cleanup_requested)
        buttons.addWidget(new_batch)
        buttons.addWidget(verify)
        buttons.addWidget(export)
        buttons.addWidget(cleanup)
        layout.addLayout(buttons)
        layout.addStretch()

    def show_batch(self, batch_id: UUID) -> None:
        self.message.setText(
            f"Batch {batch_id} is frozen and ready for the Session 4 upload service. "
            "No network transfer has occurred. New files belong in a new contribution batch."
        )


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        store: CheckpointStore,
        gateway: PhotographerSessionGateway,
        demo_event: EventCache | None = None,
    ) -> None:
        super().__init__()
        self.store = store
        self.gateway = gateway
        self.current_event: EventCache | None = None
        self.current_batch_id: UUID | None = None
        self.scan_thread: QThread | None = None
        self.scan_stop: Event | None = None

        self.stack = QStackedWidget()
        self.login = LoginPage()
        self.events = EventSelectorPage()
        self.selection = SelectionPage()
        self.validation = ValidationPage()
        self.approved = ApprovedPage()
        for page in (self.login, self.events, self.selection, self.validation, self.approved):
            self.stack.addWidget(page)
        self.setCentralWidget(self.stack)
        self.setWindowTitle("OpenFotos")
        self.resize(920, 620)
        self._connect_actions()

        if demo_event is None:
            self.stack.setCurrentWidget(self.login)
        else:
            self.store.cache_event(demo_event)
            self.events.set_events([demo_event], demo=True)
            self.stack.setCurrentWidget(self.events)

    def _connect_actions(self) -> None:
        self.login.lead_requested.connect(self._lead_login)
        self.login.uploader_requested.connect(self._enroll_uploader)
        self.events.selected.connect(self._open_event)
        self.selection.add_files_requested.connect(self._add_files)
        self.selection.add_folder_requested.connect(self._add_folder)
        self.selection.remove_selection_requested.connect(self._remove_selection)
        self.selection.scan_requested.connect(self._start_scan)
        self.selection.pause_requested.connect(self._pause_scan)
        self.selection.new_batch_requested.connect(self._new_batch)
        self.validation.approve_requested.connect(self._approve_batch)
        self.validation.rescan_requested.connect(self._show_selection)
        self.validation.export_requested.connect(self._export_diagnostics)
        self.approved.new_batch_requested.connect(self._new_batch)
        self.approved.verify_requested.connect(self._show_selection)
        self.approved.export_requested.connect(self._export_diagnostics)
        self.approved.cleanup_requested.connect(self._cleanup_event)

    @Slot(str, str, str)
    def _lead_login(self, server_url: str, username: str, password: str) -> None:
        try:
            events = list(self.gateway.sign_in_lead(server_url, username, password))
        except OnlineServicesUnavailable as exc:
            self.login.show_error(str(exc))
            return
        for event in events:
            self.store.cache_event(event)
        self.events.set_events(events, demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot(str, str, str)
    def _enroll_uploader(self, server_url: str, invitation: str, device_label: str) -> None:
        try:
            event = self.gateway.enroll_uploader(server_url, invitation, device_label)
        except OnlineServicesUnavailable as exc:
            self.login.show_error(str(exc))
            return
        self.store.cache_event(event)
        self.events.set_events([event], demo=False)
        self.stack.setCurrentWidget(self.events)

    @Slot(object)
    def _open_event(self, event: EventCache) -> None:
        self.current_event = event
        batches = self.store.list_batches(event.id)
        self.current_batch_id = batches[-1].id if batches else self.store.create_batch(event.id)
        batch = self.store.get_batch(self.current_batch_id)
        if batch.state is BatchState.APPROVED:
            self.approved.show_batch(batch.id)
            self.stack.setCurrentWidget(self.approved)
        elif batch.state is BatchState.NEEDS_REVIEW:
            self._show_validation(self.store.summary(batch.id))
        else:
            self._show_selection()

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
            if self.current_batch_id is not None:
                self.approved.show_batch(self.current_batch_id)
            self.stack.setCurrentWidget(self.approved)
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
        self.approved.show_batch(self.current_batch_id)
        self.stack.setCurrentWidget(self.approved)

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
        self.store.close()
        event.accept()
