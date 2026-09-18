FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY apps ./apps
COPY packages ./packages
COPY licenses ./licenses
COPY scripts/fetch_face_models.py ./scripts/fetch_face_models.py

RUN uv sync --frozen --no-dev --extra server
RUN python scripts/fetch_face_models.py --destination /opt/openfotos/models
RUN .venv/bin/python apps/server/manage.py collectstatic --noinput
RUN useradd --create-home --uid 10001 openfotos

ENV PATH="/app/.venv/bin:$PATH" \
    FACE_DETECTOR_MODEL_PATH="/opt/openfotos/models/face_detection_yunet_2023mar.onnx" \
    FACE_RECOGNIZER_MODEL_PATH="/opt/openfotos/models/face_recognition_sface_2021dec.onnx"

USER 10001:10001

CMD ["python", "-m", "openfotos_server.database_tls", "--", "python", "-m", "openfotos_server.start"]
