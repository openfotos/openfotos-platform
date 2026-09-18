import pytest

from openfotos_server.start import StartupConfigurationError, gunicorn_arguments


def test_default_gunicorn_arguments_are_bounded_for_the_pilot() -> None:
    assert gunicorn_arguments({}) == [
        "gunicorn",
        "openfotos_server.wsgi:application",
        "--bind",
        "0.0.0.0:8000",
        "--workers",
        "1",
        "--threads",
        "4",
        "--error-logfile",
        "-",
    ]


def test_explicit_gunicorn_arguments_are_parsed_without_a_shell() -> None:
    arguments = gunicorn_arguments(
        {"PORT": "9000", "GUNICORN_WORKERS": "2", "GUNICORN_THREADS": "8"}
    )

    assert arguments[3] == "0.0.0.0:9000"
    assert arguments[5] == "2"
    assert arguments[7] == "8"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PORT", "0"),
        ("PORT", "65536"),
        ("GUNICORN_WORKERS", "0"),
        ("GUNICORN_WORKERS", "33"),
        ("GUNICORN_THREADS", "many"),
        ("GUNICORN_THREADS", "65"),
    ],
)
def test_invalid_gunicorn_values_fail_before_launch(name: str, value: str) -> None:
    with pytest.raises(StartupConfigurationError, match=name):
        gunicorn_arguments({name: value})
