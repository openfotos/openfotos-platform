from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from openfotos_server.database import configure_deployed_search_path


@override_settings(IS_DEPLOYED=True)
def test_deployed_postgresql_connection_sets_and_verifies_private_search_path() -> None:
    connection = MagicMock(vendor="postgresql")
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("openfotos",)

    configure_deployed_search_path(connection=connection)

    assert cursor.execute.call_args_list[0].args == (
        "SELECT set_config('search_path', %s, false)",
        ["openfotos,extensions"],
    )
    assert cursor.execute.call_args_list[1].args == ("SELECT current_schema()",)


@override_settings(IS_DEPLOYED=True)
def test_deployed_connection_rejects_missing_private_schema() -> None:
    connection = MagicMock(vendor="postgresql")
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("public",)

    with pytest.raises(ImproperlyConfigured, match="cannot select the OpenFotos schema"):
        configure_deployed_search_path(connection=connection)


@override_settings(IS_DEPLOYED=False)
def test_local_connection_does_not_override_search_path() -> None:
    connection = MagicMock(vendor="postgresql")

    configure_deployed_search_path(connection=connection)

    connection.cursor.assert_not_called()
