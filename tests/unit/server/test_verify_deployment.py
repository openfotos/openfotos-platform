from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError
from django.test import override_settings

from openfotos_server.events.management.commands.verify_deployment import (
    _verify_database_migrations,
)


@override_settings(IS_DEPLOYED=True)
def test_deployment_verification_accepts_fully_migrated_database() -> None:
    executor = MagicMock()
    executor.loader.graph.leaf_nodes.return_value = [("events", "0011_event_erasure_tracking")]
    executor.migration_plan.return_value = []

    with patch(
        "openfotos_server.events.management.commands.verify_deployment.MigrationExecutor",
        return_value=executor,
    ):
        _verify_database_migrations()


@override_settings(IS_DEPLOYED=True)
def test_deployment_verification_rejects_pending_migrations() -> None:
    executor = MagicMock()
    executor.loader.graph.leaf_nodes.return_value = [("events", "0011_event_erasure_tracking")]
    executor.migration_plan.return_value = [(MagicMock(), False)]

    with (
        patch(
            "openfotos_server.events.management.commands.verify_deployment.MigrationExecutor",
            return_value=executor,
        ),
        pytest.raises(CommandError, match=r"1 unapplied migration\(s\)"),
    ):
        _verify_database_migrations()


@override_settings(IS_DEPLOYED=True)
def test_deployment_verification_hides_database_failure_details() -> None:
    with (
        patch(
            "openfotos_server.events.management.commands.verify_deployment.MigrationExecutor",
            side_effect=RuntimeError("credential-bearing database detail"),
        ),
        pytest.raises(CommandError, match="migration state could not be verified") as error,
    ):
        _verify_database_migrations()

    assert "credential-bearing" not in str(error.value)
