# OpenFotos

OpenFotos is a supervised pilot for private wedding photo delivery and event-scoped face-based
photo discovery. A photographer organizes one main wedding into sub-events such as Haldi, Reception, and
Marriage, then processes edited JPEGs with one account across tracked desktop installations.

The pilot deliberately targets one photographer, wedding-sized events below roughly 20–25 GB, up
to 50 sub-events, one to ten active desktop installations, and a supervised delivery window. The complete product and
architecture decisions are in the
[product and technical plan](OpenFotos_Markdown/OpenFotos_Product_and_Technical_Plan.md). The
[pilot access decision](docs/adr/0002-pilot-accounts-tenancy-and-event-access.md) records the exact
Session 2 authorization and PIN tradeoffs. The
[superseded multi-uploader decision](docs/adr/0003-multi-uploader-desktop-ingestion.md),
[exact-object lease decision](docs/adr/0004-exact-object-upload-leases.md), and
[reconciliation decision](docs/adr/0005-contribution-lifecycle-and-reconciliation.md) define how
independent desktop installations safely contribute to the same event. The
[preview and gallery decision](docs/adr/0006-preview-policy-derivatives-and-private-gallery.md)
defines optional event watermarking, local derivative generation, and private browsing.
The [post-Session-5 revision](docs/adr/0008-main-events-sub-events-and-search-only-face-index.md)
defines mandatory sub-events, one photographer identity, four-digit PINs, and the search-only face
index that supersedes collections and clustering.
The [full-gallery search decision](docs/adr/0009-full-gallery-guest-search-and-recall-first-matching.md)
removes selfie-only authorization and makes face search a recall-first filter over photos a guest
may already browse.
The [Session 6 benchmark method](docs/session6/benchmark-method.md) freezes the candidate artifacts,
privacy-safe measurement protocol, quality gates, and reproducible commands.
The [accepted face-model contract](docs/adr/0010-accepted-yunet-sface-model-contract.md) records the
YuNet/SFace hashes, preprocessing, vector and quality invariants, fixed threshold, and evidence that
unblocked Session 7. The
[direct face-index decision](docs/adr/0011-direct-per-photo-face-index.md) records the strict
desktop-to-server document, retry/reset behavior, publication gate, and exact SQL search scope. The
[Session 8 sharing decision](docs/adr/0012-owner-guest-capabilities-ephemeral-search-and-downloads.md)
records owner/guest authority, fragment-secret/PIN delivery, expiry/revocation, ephemeral search,
and visibility-derived exact-original downloads.

## Repository map

```text
apps/
  desktop/       PySide6 application entry point and future UI/controllers
  server/        Django project
packages/
  contracts/     Shared event states and API/model contracts
  storage/       Object-key and storage boundary code
  vision/        Replaceable face detection and embedding boundary
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
uv sync --extra server --extra desktop --extra vision
uv run python apps/server/manage.py migrate
uv run python apps/server/manage.py createsuperuser
uv run python apps/server/manage.py check
uv run python apps/server/manage.py runserver
```

The health endpoint is `http://127.0.0.1:8000/health/`. Start the real desktop client with:

```bash
uv run python -m openfotos_desktop
```

For a photographer with slug `demo`, use `http://demo.localhost:8000` as the server. Photographer
login, durable refresh, private direct upload, pause/resume, intake closure, and manifest
finalization use the desktop API. Before creating a contribution, the desktop requires one active
sub-event selected from the server snapshot. The photographer confirms optional preview
watermarking; the same sync action uploads originals, generates private previews/thumbnails, and
then uploads strict per-photo face-analysis results. Use **Face model settings** in the desktop
header to download and SHA-256-verify the accepted YuNet/SFace files, or to locate existing copies.
Weights remain in the private desktop application-data directory and are never committed. To run
local inventory without a server, start the
explicit synthetic demo instead:

```bash
uv run python -m openfotos_desktop --demo
```

The demo accepts recursive folders, one or multiple files, and mixed selections. It validates and
checkpoints JPEGs locally; it does not transfer anything to object storage. To collect a redacted
timing report on representative, consented local data without retaining a benchmark checkpoint:

```bash
uv run python -m openfotos_desktop.benchmark_inventory /path/to/representative/folder \
  --output openfotos-inventory-benchmark.json
```

For the browser flow, open `http://localhost:8000/admin/` and provision records in this order:
Django user, photographer, photographer membership, then event. Create at least one sub-event in
the photographer dashboard before upload/publication. Use the event admin actions to move a sample
through its legal lifecycle. A photographer with slug `demo` signs in at
`http://demo.localhost:8000/login/`; after publication the dashboard can issue the one owner link.
Its fragment-secret URL and generated four-digit PIN are shown once. The owner uses that private
management link to create whole-event or sub-event guest links; it should not be forwarded as the
family guest link.

For development checks:

```bash
uv sync --extra server --extra desktop --extra vision
./scripts/check.sh
```

The default fast suite uses SQLite. The Session 7–8 CI job runs migrations, exact cosine-scope
tests, and the public capability/search/download boundary against `pgvector/pgvector:pg16`. To
exercise the same boundary locally with the repository service:

```bash
docker compose -f infra/docker/compose.yaml up -d postgres
DATABASE_URL=postgresql://openfotos:openfotos@127.0.0.1:5432/openfotos \
  uv run pytest tests/integration/test_session7_face_index.py \
    tests/integration/test_session8_sharing.py
```

Migration `0005_face_index` enables pgvector and intentionally aborts if any event is already
published. Unpublish and index those events deliberately before applying it.

Copy `.env.example` to `.env` for local values and set `SHARE_PIN_PEPPER` independently from
`DJANGO_SECRET_KEY`. Configure `FACE_DETECTOR_MODEL_PATH` and `FACE_RECOGNIZER_MODEL_PATH` to the
accepted hash-verified YuNet/SFace files; weights stay outside the repository. Never commit `.env`,
model weights, customer photos, reference images, face embeddings, or production credentials. The
application-source licence is still an explicit pre-publication decision; pretrained model weights
retain separate terms.

Search-result rows contain only ordered asset IDs and expire after one hour. Run the idempotent
cleanup command manually in development; Session 9 must schedule it at least every 15 minutes:

```bash
uv run python apps/server/manage.py purge_expired_face_searches
```

`reset_share_access --confirm RESET-ALL-SHARE-ACCESS` is an emergency-only operation to revoke all
owner/guest links before replacing a suspected-compromised `SHARE_PIN_PEPPER`. Capability expiry
does not delete event media; a controlled, audited retention purge is Session 9 work.

Session 8 does not need production Railway or Supabase accounts. Local/CI PostgreSQL with pgvector
and the storage abstraction are sufficient. Live regions, secrets, schedules, domains, backups,
retention deletion, and deployment rehearsal begin in Session 9.

The server needs an S3-compatible bucket plus object read/write credentials. For Cloudflare R2,
set the account ID, bucket name, access key ID, and secret; the endpoint is derived automatically.
No account API token is used at runtime. For MinIO, start a local server and create a private test
bucket in its console, then set `OBJECT_STORAGE_ENDPOINT_URL`, `R2_BUCKET_NAME`, and the MinIO
access and secret keys. The opt-in provider contract test uses synthetic bytes and cleans its keys:

```bash
OPENFOTOS_LIVE_S3=1 \
OPENFOTOS_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
OPENFOTOS_TEST_S3_BUCKET=openfotos-session4 \
OPENFOTOS_TEST_S3_ACCESS_KEY=replace-me \
OPENFOTOS_TEST_S3_SECRET_KEY=replace-me \
uv run pytest tests/integration/test_s3_object_store_live.py
```

## Implementation sessions

The pilot is divided into nine Codex sessions. Each session ends with a runnable slice and a clean
handoff. See [docs/SESSION_PLAN.md](docs/SESSION_PLAN.md) for scope, dependencies, and completion
checks.
