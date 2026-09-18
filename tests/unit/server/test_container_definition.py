from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
DOCKERFILE = REPOSITORY / "infra/docker/server.Dockerfile"


def test_server_image_sources_are_immutable_and_startup_has_no_shell() -> None:
    definition = DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "FROM python:3.12-slim@sha256:"
        "78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime" in definition
    )
    assert (
        "COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:"
        "3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 " in definition
    )
    assert '"sh", "-c"' not in definition
    assert (
        'CMD ["python", "-m", "openfotos_server.database_tls", "--", '
        '"python", "-m", "openfotos_server.start"]' in definition
    )
