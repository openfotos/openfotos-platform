# OpenFotos

OpenFotos is a supervised pilot for private wedding photo delivery and face-based photo
discovery. A photographer processes and uploads edited JPEGs with a desktop application;
customers browse a PIN-protected web gallery and can submit an ephemeral selfie to find likely
photos.

The pilot deliberately targets one photographer, one reception, less than 20 GB of photographs,
and a ten-day delivery window. The complete product and architecture decisions are in the
[product and technical plan](OpenFotos_Markdown/OpenFotos_Product_and_Technical_Plan.md). The
[pilot access decision](docs/adr/0002-pilot-accounts-tenancy-and-event-access.md) records the exact
Session 2 authorization and PIN tradeoffs.

## Repository map

```text
apps/
  desktop/       PySide6 application entry point and future UI/controllers
  server/        Django project
packages/
  contracts/     Shared event states and API/model contracts
  storage/       Object-key and storage boundary code
  vision/        Replaceable detection, embedding, and clustering boundary
infra/
  docker/        Local PostgreSQL and server container definitions
  railway/       Railway deployment notes
tests/
  unit/          Fast tests without provider accounts or real client media
  integration/   Cross-component and provider-emulator tests
  fixtures/      Synthetic or explicitly consented fixtures only
docs/            Architecture decisions and Codex session plan
scripts/         Repeatable developer checks
```

## Local setup

Install [uv](https://docs.astral.sh/uv/), then run:

```bash
uv sync --extra server
uv run python apps/server/manage.py migrate
uv run python apps/server/manage.py createsuperuser
uv run python apps/server/manage.py check
uv run python apps/server/manage.py runserver
```

The health endpoint is `http://127.0.0.1:8000/health/`. Start the desktop shell with the desktop
extra installed:

```bash
uv sync --extra desktop
uv run python -m openfotos_desktop
```

For the Session 2 browser flow, open `http://localhost:8000/admin/` and provision records in this
order: Django user, photographer, photographer membership, then event. Event PINs are write-only
six-digit values. Use the event admin actions to move a sample through its legal lifecycle or to
revoke visitor sessions. A photographer with slug `demo` signs in at
`http://demo.localhost:8000/login/`; its visitor links use the same tenant host.

For development checks:

```bash
uv sync --extra server
./scripts/check.sh
```

Copy `.env.example` to `.env` for local values. Never commit `.env`, model weights, customer
photos, selfies, face embeddings, or production credentials. The application-source licence is
still an explicit pre-publication decision; pretrained model weights retain separate terms.

## Implementation sessions

The pilot is divided into nine Codex sessions. Each session ends with a runnable slice and a clean
handoff. See [docs/SESSION_PLAN.md](docs/SESSION_PLAN.md) for scope, dependencies, and completion
checks.
