import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image
from PySide6.QtGui import QCloseEvent, QPalette
from PySide6.QtWidgets import QApplication, QProgressBar, QPushButton, QToolButton

from openfotos_contracts import WatermarkLogoKind, WatermarkTemplate
from openfotos_desktop.face_models import FaceModelSetupError
from openfotos_desktop.ingestion import (
    BatchState,
    CheckpointStore,
    EventCache,
    InventoryScanner,
    PreviewPolicyCache,
    SubEventCache,
)
from openfotos_desktop.ports import Session3Gateway
from openfotos_desktop.theme import CORPORATE_STYLESHEET, asset_path
from openfotos_desktop.ui import (
    EventSelectorPage,
    LoginPage,
    MainWindow,
    StartupPage,
    SubEventSelectorPage,
    UploadPage,
    WatermarkSettingsDialog,
)

_SUB_EVENT = SubEventCache(
    id=UUID("00000000-0000-4000-8000-000000000006"),
    name="Reception",
    position=1,
)
_DISABLED_POLICY = PreviewPolicyCache(
    id=UUID("00000000-0000-4000-8000-000000000007"),
    enabled=False,
    template=WatermarkTemplate.COMPACT_BOTTOM_RIGHT,
    text="",
    logo_kind=WatermarkLogoKind.NONE,
    renderer_id="watermark-raster-v1",
    derivative_profile_id="gallery-jpeg-v1",
    mark_sha256="",
)

DEMO_EVENT = EventCache(
    id=UUID("00000000-0000-4000-8000-000000000003"),
    name="Session 3 synthetic reception",
    storage_limit_bytes=50_000_000_000,
    processing_profile_id="pilot-profile-v1",
    sub_events=(_SUB_EVENT,),
    preview_policy=_DISABLED_POLICY,
)


def application() -> QApplication:
    existing = QApplication.instance()
    return existing if existing is not None else QApplication([])


def wait_for(app: QApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def photo_file(directory: Path, name: str = "source.jpg") -> Path:
    path = directory / name
    Image.new("RGB", (8, 6), color="navy").save(path, format="JPEG")
    return path


class FakeFaceModelStore:
    def __init__(self, *, ready: bool = False, fail_message: str | None = None) -> None:
        self.ready = ready
        self.fail_message = fail_message
        self.download_calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()

    def verified_paths(self):
        return object() if self.ready else None

    def download(self, *, client=None, progress=None, is_cancelled=None):
        self.download_calls += 1
        self.started.set()
        if progress is not None:
            progress(1, 2)
        while not self.release.wait(0.01):
            if is_cancelled is not None and is_cancelled():
                self.cancelled.set()
                raise FaceModelSetupError("face_model_download_cancelled", "cancelled")
        if self.fail_message is not None:
            raise FaceModelSetupError("face_model_download_failed", self.fail_message)
        self.ready = True
        if progress is not None:
            progress(2, 2)
        return object()


class FakeGateway:
    def __init__(
        self,
        *,
        events=(),
        saved_origin: str | None = None,
        resume_events=(),
        resume_error: str | None = None,
    ) -> None:
        self.events = list(events)
        self.saved_origin = saved_origin
        self.resume_events = list(resume_events)
        self.resume_error = resume_error
        self.sign_out_calls = 0
        self.confirmations: list[tuple] = []
        self.policy_event: EventCache | None = None
        self.persistence_warning = None

    def saved_session_origin(self):
        return self.saved_origin

    def resume(self, server_url: str):
        if self.resume_error is not None:
            raise RuntimeError(self.resume_error)
        return list(self.resume_events)

    def sign_in_photographer(self, server_url, username, password, device_label):
        return list(self.events)

    def sign_out(self) -> None:
        self.sign_out_calls += 1

    def confirm_preview_policy(self, event_id, **kwargs):
        self.confirmations.append((event_id, kwargs))
        if self.policy_event is None:
            raise AssertionError("No policy event configured.")
        return self.policy_event

    def upload(self, *args, **kwargs):
        raise AssertionError("The UI tests never start a real upload.")


class BlockingResumeGateway(FakeGateway):
    """Hold auto-resume open so the startup page can be observed deterministically."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.resume_started = threading.Event()
        self.resume_release = threading.Event()

    def resume(self, server_url: str):
        self.resume_started.set()
        assert self.resume_release.wait(5.0)
        return super().resume(server_url)


def _approved_batch(store: CheckpointStore, event: EventCache, directory: Path):
    batch_id = store.create_batch(event.id, _SUB_EVENT.id)
    store.add_files(batch_id, [photo_file(directory)])
    InventoryScanner(store).scan(batch_id)
    store.approve_batch(batch_id, supported_profile_id="pilot-profile-v1")
    return batch_id


def test_normal_mode_exposes_honest_session4_boundary(tmp_path: Path) -> None:
    app = application()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "normal.sqlite3"),
        gateway=Session3Gateway(),
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert isinstance(window.stack.currentWidget(), LoginPage)
    window.login.password.setText("never-store-this")
    window.login.sign_in_button.click()
    app.processEvents()

    assert "Session 4" in window.login.error.text()
    assert window.login.password.text() == ""
    assert window.header.sign_out.isHidden()
    window.close()


def test_demo_event_opens_upload_page_with_inline_verification(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "demo.sqlite3")
    window = MainWindow(
        store=store,
        gateway=Session3Gateway(),
        demo_event=DEMO_EVENT,
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert "synthetic demo" in window.events.heading.text()
    assert "remaining" in window.events.events.item(0).text()
    window.events.open_button.click()
    app.processEvents()
    assert isinstance(window.stack.currentWidget(), SubEventSelectorPage)
    window.sub_events.open_button.click()
    app.processEvents()

    assert isinstance(window.stack.currentWidget(), UploadPage)
    assert window.current_event == DEMO_EVENT
    assert window.current_batch_id is not None

    store.add_files(window.current_batch_id, [photo_file(tmp_path)])
    window._show_upload_page()
    assert wait_for(
        app,
        lambda: (
            store.get_batch(window.current_batch_id).state
            in {BatchState.NEEDS_REVIEW, BatchState.APPROVED}
        ),
    )
    assert window.upload_page.verification.text().startswith("1 accepted · 0 skipped")
    window.close()


def test_selected_section_can_return_to_section_picker_before_scanning(tmp_path: Path) -> None:
    app = application()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "section-navigation.sqlite3"),
        gateway=Session3Gateway(),
        demo_event=DEMO_EVENT,
        face_model_store=FakeFaceModelStore(ready=True),
    )

    window.events.open_button.click()
    window.sub_events.open_button.click()
    app.processEvents()
    assert isinstance(window.stack.currentWidget(), UploadPage)

    window.upload_page.back.click()
    app.processEvents()

    assert isinstance(window.stack.currentWidget(), SubEventSelectorPage)
    assert window.current_sub_event is None
    assert window.current_batch_id is None
    assert window.header.context.text() == DEMO_EVENT.name
    window.close()


def test_resumable_upload_resumes_before_newer_empty_draft(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "resume-processing.sqlite3")
    window = MainWindow(
        store=store,
        gateway=Session3Gateway(),
        demo_event=DEMO_EVENT,
        face_model_store=FakeFaceModelStore(ready=True),
    )
    resumable_id = _approved_batch(store, DEMO_EVENT, tmp_path)
    store.mark_batch_reserved(resumable_id)
    store.mark_upload_verified(store.list_items(resumable_id)[0].id)
    newer_empty_id = store.create_batch(DEMO_EVENT.id, _SUB_EVENT.id)

    window.events.open_button.click()
    window.sub_events.open_button.click()
    app.processEvents()

    assert window.current_batch_id == resumable_id
    assert window.current_batch_id != newer_empty_id
    assert isinstance(window.stack.currentWidget(), UploadPage)
    assert window.upload_page.submit.text() == "Resume upload"
    assert window.upload_page.submit.isEnabled()
    window.close()


def test_upload_page_renders_one_progress_bar_with_smooth_stage_text(tmp_path: Path) -> None:
    application()
    store = CheckpointStore(tmp_path / "progress.sqlite3")
    store.cache_event(DEMO_EVENT)
    batch_id = _approved_batch(store, DEMO_EVENT, tmp_path)
    page = UploadPage()
    page.show_batch(DEMO_EVENT, batch_id, store)

    assert len(page.findChildren(QProgressBar)) == 1
    page.stage_progressed("originals", 1, 2)
    assert page.stage_text.text() == "Uploading originals — 1 of 2"
    page.stage_progressed("derivatives", 1, 2)
    assert page.stage_text.text() == "Generating thumbnails and previews — 1 of 2"
    page.stage_progressed("face-index", 1, 2)
    assert page.stage_text.text() == "Embedding faces — 1 of 2"
    assert page.progress.value() == 500
    assert page.progress.maximum() == 1000
    store.close()


def test_upload_page_summarizes_skipped_files_inline_without_blocking(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "summary.sqlite3")
    store.cache_event(DEMO_EVENT)
    batch_id = store.create_batch(DEMO_EVENT.id, _SUB_EVENT.id)
    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("not a photo")
    store.add_files(batch_id, [photo_file(tmp_path), unsupported])
    summary = InventoryScanner(store).scan(batch_id)
    page = UploadPage()
    page.show_batch(DEMO_EVENT, batch_id, store)

    assert page.verification.text().startswith("1 accepted · 1 skipped")
    assert not page.details_toggle.isHidden()
    assert page.submit.isEnabled()
    page.details_toggle.setChecked(True)
    app.processEvents()
    assert not page.details.isHidden()
    assert page.details.count() == 1
    assert "Unsupported Extension" in page.details.item(0).text()
    assert summary.can_approve
    store.close()


def test_upload_page_overflow_menu_holds_diagnostics_rescan_and_cleanup() -> None:
    application()
    page = UploadPage()
    labels = [action.text() for action in page.overflow.menu().actions()]

    assert labels == [
        "Export redacted diagnostics",
        "Verify sources again",
        "Remove local checkpoint",
    ]
    assert isinstance(page.add_files, QToolButton)
    assert isinstance(page.add_folder, QToolButton)


def test_intake_and_finalize_controls_are_not_exposed() -> None:
    application()
    page = UploadPage()
    sub_events = SubEventSelectorPage()

    labels = {
        button.text()
        for container in (page, sub_events)
        for button in container.findChildren(QPushButton)
    }
    assert "Close intake" not in labels
    assert "Reopen intake" not in labels
    assert "Finalize ingestion" not in labels
    assert not any("intake" in label.lower() for label in labels)


def test_published_event_disables_upload_controls(tmp_path: Path) -> None:
    application()
    store = CheckpointStore(tmp_path / "published.sqlite3")
    published = replace(DEMO_EVENT, intake_state="closed")
    store.cache_event(published)
    batch_id = _approved_batch(store, published, tmp_path)
    page = UploadPage()

    page.show_batch(published, batch_id, store)

    assert page.verification.text().startswith("1 accepted")
    assert not page.add_files.isEnabled()
    assert not page.add_folder.isEnabled()
    assert not page.new_batch.isEnabled()
    assert not page.submit.isEnabled()
    store.close()


def test_published_in_flight_batch_reports_not_included(tmp_path: Path) -> None:
    application()
    store = CheckpointStore(tmp_path / "not-included.sqlite3")
    store.cache_event(DEMO_EVENT)
    batch_id = _approved_batch(store, DEMO_EVENT, tmp_path)
    store.mark_batch_reserved(batch_id)
    store.mark_batch_not_included(batch_id)
    page = UploadPage()

    page.show_batch(DEMO_EVENT, batch_id, store)

    assert page.batch_status.text() == "NOT INCLUDED"
    assert page.submit.text() == "Not included in published event"
    assert not page.submit.isEnabled()
    store.close()


def test_watermark_dialog_defaults_off_and_locks_a_confirmed_policy() -> None:
    app = application()
    dialog = WatermarkSettingsDialog()
    dialog.show()
    event = EventCache(
        id=uuid4(),
        name="Reception",
        storage_limit_bytes=50_000_000_000,
        processing_profile_id="pilot-profile-v1",
        sub_events=(_SUB_EVENT,),
    )

    dialog.show_event(event)
    draft = dialog.draft()

    assert draft["enabled"] is False
    assert draft["mark_png"] == b""
    assert dialog.enabled.isEnabled()
    assert not dialog.locked_note.isVisible()
    assert not dialog.save.isHidden()

    dialog.enabled.setChecked(True)
    dialog.logo.setCurrentIndex(dialog.logo.findData(WatermarkLogoKind.ONENODEAI))
    dialog.text.setText("OneNodeAI")
    branded = dialog.draft()
    assert branded["enabled"]
    assert branded["mark_png"].startswith(b"\x89PNG")

    locked_event = replace(event, preview_policy=_DISABLED_POLICY)
    dialog.show_event(locked_event)
    app.processEvents()

    assert not dialog.enabled.isEnabled()
    assert dialog.locked_note.isVisible()
    assert dialog.save.isHidden()
    dialog.close()


def test_watermark_confirmation_records_policy_and_closes_dialog(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "watermark.sqlite3")
    event = replace(DEMO_EVENT, id=uuid4(), preview_policy=None, name="Watermark Wedding")
    store.cache_event(event)
    gateway = FakeGateway()
    gateway.policy_event = replace(event, preview_policy=_DISABLED_POLICY)
    window = MainWindow(
        store=store,
        gateway=gateway,
        face_model_store=FakeFaceModelStore(ready=True),
    )
    window.current_event = event
    window._open_watermark_settings()
    app.processEvents()

    window._confirm_watermark(window.watermark_dialog.draft())
    app.processEvents()

    assert len(gateway.confirmations) == 1
    assert gateway.confirmations[0][1]["enabled"] is False
    assert store.get_event(event.id).preview_policy is not None
    assert not window.watermark_dialog.isVisible()
    window.close()


def test_desktop_shell_packages_brand_and_window_icon(tmp_path: Path) -> None:
    app = application()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "branded.sqlite3"),
        gateway=Session3Gateway(),
        demo_event=DEMO_EVENT,
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert window.header.product_name.text() == "OneNodeAI Studio"
    assert window.windowTitle() == "OneNodeAI Studio"
    assert not window.windowIcon().isNull()
    assert app.palette().color(QPalette.ColorRole.Window).name() == "#ffffff"
    assert asset_path("onenodeai.svg").is_file()
    assert asset_path("logo.png").is_file()
    window.close()


def test_qcheckbox_indicator_is_styled_for_the_light_theme() -> None:
    assert "QCheckBox::indicator {" in CORPORATE_STYLESHEET
    assert "QCheckBox::indicator:checked" in CORPORATE_STYLESHEET
    assert "check.svg" in CORPORATE_STYLESHEET
    assert asset_path("check.svg").is_file()


def test_face_models_download_automatically_in_the_background(tmp_path: Path) -> None:
    app = application()
    model_store = FakeFaceModelStore()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "models.sqlite3"),
        gateway=Session3Gateway(),
        face_model_store=model_store,
    )

    assert wait_for(app, lambda: model_store.started.is_set())
    assert "Downloading face models" in window.header.model_status.text()
    assert window.header.model_retry.isHidden()
    assert window.stack.currentWidget() is window.login

    model_store.release.set()
    assert wait_for(app, lambda: window.header.model_status.text() == "Face models ready")
    assert model_store.download_calls == 1
    assert window.header.model_retry.isHidden()
    window.close()


def test_failed_face_model_download_offers_an_inline_retry(tmp_path: Path) -> None:
    app = application()
    model_store = FakeFaceModelStore(fail_message="network down")
    window = MainWindow(
        store=CheckpointStore(tmp_path / "models-failed.sqlite3"),
        gateway=Session3Gateway(),
        face_model_store=model_store,
    )

    assert wait_for(app, lambda: model_store.started.is_set())
    model_store.release.set()
    assert wait_for(app, lambda: "Face models unavailable" in window.header.model_status.text())
    assert not window.header.model_retry.isHidden()
    assert "network down" in window.header.model_retry.toolTip()
    assert wait_for(app, lambda: window.model_thread is None)

    model_store.fail_message = None
    model_store.release.clear()
    window.header.model_retry.click()
    assert wait_for(app, lambda: model_store.download_calls == 2)
    model_store.release.set()
    assert wait_for(app, lambda: window.header.model_status.text() == "Face models ready")
    assert window.header.model_retry.isHidden()
    window.close()


def test_auto_resume_restores_a_single_saved_session(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "auto-resume.sqlite3")
    store.cache_event(DEMO_EVENT)
    gateway = FakeGateway(
        saved_origin="https://studio.example",
        resume_events=[DEMO_EVENT],
    )
    window = MainWindow(
        store=store,
        gateway=gateway,
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert wait_for(app, lambda: isinstance(window.stack.currentWidget(), EventSelectorPage))
    assert not window.header.sign_out.isHidden()
    assert window.header.context.text() != ""
    window.close()


def test_auto_resume_shows_the_branded_startup_page_until_the_session_returns(
    tmp_path: Path,
) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "startup.sqlite3")
    store.cache_event(DEMO_EVENT)
    gateway = BlockingResumeGateway(
        saved_origin="https://studio.example",
        resume_events=[DEMO_EVENT],
    )
    window = MainWindow(
        store=store,
        gateway=gateway,
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert isinstance(window.stack.currentWidget(), StartupPage)
    assert window.startup.product_name.text() == "OneNodeAI Studio"
    assert "saved session" in window.startup.status.text()
    assert wait_for(app, lambda: gateway.resume_started.is_set())
    assert isinstance(window.stack.currentWidget(), StartupPage)

    gateway.resume_release.set()
    assert wait_for(app, lambda: isinstance(window.stack.currentWidget(), EventSelectorPage))
    window.close()


def test_sign_in_form_prefills_the_saved_workstation_label(tmp_path: Path) -> None:
    application()
    store = CheckpointStore(tmp_path / "workstation-label.sqlite3")
    store.set_workstation_label("Yashas Nadig")
    window = MainWindow(
        store=store,
        gateway=Session3Gateway(),
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert window.login.device_label.text() == "Yashas Nadig"
    window.close()


def test_failed_auto_resume_returns_to_login_with_a_neutral_notice(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "auto-resume-failed.sqlite3")
    store.cache_event(DEMO_EVENT)
    gateway = FakeGateway(saved_origin="https://studio.example", resume_error="expired")
    window = MainWindow(
        store=store,
        gateway=gateway,
        face_model_store=FakeFaceModelStore(ready=True),
    )

    assert wait_for(app, lambda: window.resume_thread is None)
    assert isinstance(window.stack.currentWidget(), LoginPage)
    assert "could not be restored" in window.login.notice.text()
    assert window.header.sign_out.isHidden()
    window.close()


def test_sign_out_revokes_the_session_and_returns_to_login(tmp_path: Path) -> None:
    app = application()
    store = CheckpointStore(tmp_path / "sign-out.sqlite3")
    store.cache_event(DEMO_EVENT)
    gateway = FakeGateway(
        saved_origin="https://studio.example",
        resume_events=[DEMO_EVENT],
    )
    window = MainWindow(
        store=store,
        gateway=gateway,
        face_model_store=FakeFaceModelStore(ready=True),
    )
    assert wait_for(app, lambda: isinstance(window.stack.currentWidget(), EventSelectorPage))

    window.header.sign_out.click()
    app.processEvents()

    assert gateway.sign_out_calls == 1
    assert isinstance(window.stack.currentWidget(), LoginPage)
    assert "signed out" in window.login.notice.text()
    assert window.header.sign_out.isHidden()
    window.close()


def test_window_close_cancels_an_active_face_model_download(tmp_path: Path) -> None:
    app = application()
    model_store = FakeFaceModelStore()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "models-close.sqlite3"),
        gateway=Session3Gateway(),
        face_model_store=model_store,
    )
    assert wait_for(app, lambda: model_store.started.is_set())
    close_event = QCloseEvent()

    window.closeEvent(close_event)
    app.processEvents()

    assert close_event.isAccepted()
    assert model_store.cancelled.is_set()
