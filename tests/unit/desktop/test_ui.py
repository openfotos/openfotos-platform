from pathlib import Path
from uuid import UUID

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QToolButton

from openfotos_desktop.ingestion import CheckpointStore, EventCache
from openfotos_desktop.ports import Session3Gateway
from openfotos_desktop.ui import LoginPage, MainWindow, PreviewPolicyPage, SelectionPage

DEMO_EVENT = EventCache(
    id=UUID("00000000-0000-4000-8000-000000000003"),
    name="Session 3 synthetic reception",
    storage_limit_bytes=25_000_000_000,
    processing_profile_id="pilot-profile-v1",
)


def application() -> QApplication:
    existing = QApplication.instance()
    return existing if existing is not None else QApplication([])


def test_normal_mode_exposes_honest_session4_boundary(tmp_path: Path) -> None:
    app = application()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "normal.sqlite3"),
        gateway=Session3Gateway(),
    )

    assert isinstance(window.stack.currentWidget(), LoginPage)
    window.login.password.setText("never-store-this")
    window.login.lead_button.click()
    app.processEvents()

    assert "Session 4" in window.login.error.text()
    assert window.login.password.text() == ""
    window.close()


def test_demo_event_opens_functional_local_inventory(tmp_path: Path) -> None:
    app = application()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "demo.sqlite3"),
        gateway=Session3Gateway(),
        demo_event=DEMO_EVENT,
    )

    assert "synthetic demo" in window.events.heading.text()
    assert "remaining" in window.events.events.item(0).text()
    window.events.open_button.click()
    app.processEvents()

    assert isinstance(window.stack.currentWidget(), SelectionPage)
    assert window.current_event == DEMO_EVENT
    assert window.current_batch_id is not None
    window.close()


def test_desktop_shell_packages_corporate_brand_and_source_actions(tmp_path: Path) -> None:
    app = application()
    window = MainWindow(
        store=CheckpointStore(tmp_path / "branded.sqlite3"),
        gateway=Session3Gateway(),
        demo_event=DEMO_EVENT,
    )

    assert window.header.logo.renderer().isValid()
    assert not window.windowIcon().isNull()
    assert app.palette().color(QPalette.ColorRole.Window).name() == "#ffffff"

    window.events.open_button.click()
    app.processEvents()

    assert isinstance(window.selection.add_files, QToolButton)
    assert isinstance(window.selection.add_folder, QToolButton)
    assert not window.selection.add_files.icon().isNull()
    assert not window.selection.add_folder.icon().isNull()
    assert window.selection.add_files.property("actionCard") is True
    assert window.selection.add_folder.property("actionCard") is True
    window.close()


def test_coordination_only_lead_can_finalize_after_closing_intake(tmp_path: Path) -> None:
    application()
    store = CheckpointStore(tmp_path / "lead.sqlite3")
    event = EventCache(
        id=UUID("00000000-0000-4000-8000-000000000004"),
        name="Reception",
        storage_limit_bytes=25_000_000_000,
        processing_profile_id="pilot-profile-v1",
        role="lead",
        intake_state="closed",
    )
    store.cache_event(event)
    batch_id = store.create_batch(event.id)
    page = SelectionPage()

    page.show_batch(event, batch_id, store)

    assert not page.finalize.isHidden()
    assert page.finalize.isEnabled()
    assert not page.scan.isEnabled()
    store.close()


def test_unconfigured_lead_gets_optional_watermark_setup_with_clean_default(
    tmp_path: Path,
) -> None:
    application()
    event = EventCache(
        id=UUID("00000000-0000-4000-8000-000000000005"),
        name="Reception",
        storage_limit_bytes=25_000_000_000,
        processing_profile_id="pilot-profile-v1",
        role="lead",
    )
    window = MainWindow(
        store=CheckpointStore(tmp_path / "policy.sqlite3"),
        gateway=Session3Gateway(),
        demo_event=event,
    )

    window.events.open_button.click()
    assert isinstance(window.stack.currentWidget(), PreviewPolicyPage)
    draft = window.preview_policy.draft()
    assert not draft["enabled"]
    assert draft["mark_png"] == b""

    window.preview_policy.enabled.setChecked(True)
    window.preview_policy.text.setText("OFTS Studio")
    branded = window.preview_policy.draft()
    assert branded["enabled"]
    assert branded["mark_png"].startswith(b"\x89PNG")
    window.close()
