# OneNodeAI Studio

OneNodeAI Studio is a supervised pilot for private wedding photo delivery and event-scoped
face-based photo discovery. A photographer organizes one main wedding into sub-events such as Haldi,
Reception, and Marriage, then processes edited JPEG/JPG, PNG, WebP, HEIC, or HEIF images with one
account across tracked desktop installations. RAW formats are intentionally unsupported. The
photographer desktop is branded **OneNodeAI Studio**; the Python package and Django application
names remain `openfotos_*` internally.

The pilot deliberately targets one photographer, wedding-sized events with up to 50,000,000,000
bytes of originals and 10,000 photos, up to 50 sub-events, one to ten active desktop installations,
and a supervised delivery window. The complete product and architecture decisions are in the
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
is retained as superseded history. The
[photographer UX revision](docs/adr/0014-name-based-event-access-and-simplified-studio-workflow.md)
replaces owner/guest capabilities with one name-based event link and event PIN, upload-until-publish
snapshot semantics, and event-wide browsing, face search, and exact-original downloads.
The [pre-Session 9 workflow decision](docs/adr/0013-studio-portfolio-portal-formats-and-retention.md)
records photographer-created events, portfolio/portal authority, accepted image formats, one-bucket
storage, and 365+30-day retention. The
[parallel ingestion decision](docs/adr/0015-parallel-and-overlapped-desktop-ingestion.md)
records pooled derivative/face workers and overlapped upload, render, and indexing stages on the
desktop.

## Repository map

```text
apps/
  desktop/       PySide6 photographer ingestion application
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

The process-only health endpoint is `http://127.0.0.1:8000/health/live/`; dependency readiness is
`http://127.0.0.1:8000/health/ready/`. Start the real desktop client with:

```bash
uv run python -m openfotos_desktop
```

For a photographer with slug `demo`, use `http://demo.localhost:8000` as the server. Photographer
login, durable refresh, private direct upload, pause/resume, and atomic publication use the desktop
API. Before creating a contribution, the desktop requires one active sub-event selected from the
server snapshot. Uploads are accepted at any time until the event is published. The desktop
downloads the accepted face models automatically on launch and shows an inline retry if that fails;
only the face-embedding stage waits for them. Use **Watermark settings** on the event screen to
optionally configure preview branding before the first submission. Weights remain in the private
desktop application-data directory and are never committed. To run local inventory without a
server, start the explicit synthetic demo instead:

```bash
uv run python -m openfotos_desktop --demo
```

The demo accepts recursive folders, one or multiple files, and mixed selections. It validates and
checkpoints supported edited-image formats locally; it does not transfer anything to object storage. To collect a redacted
timing report on representative, consented local data without retaining a benchmark checkpoint:

```bash
uv run python -m openfotos_desktop.benchmark_inventory /path/to/representative/folder \
  --output openfotos-inventory-benchmark.json
```

For the browser flow, open `http://localhost:8000/admin/` and provision records in this order:
Django user, Photographer tenant with permanent DNS slug, then an active Photographer membership
joining them. A photographer with slug `demo` signs in at `http://demo.localhost:8000/login/` and
creates the main event—with mandatory name, cover, and consent attestation—from the dashboard.
Create at least one sub-event before upload/publication. The portfolio is the tenant root at
`http://demo.localhost:8000/`. First publication assigns the frozen event slug, lists the event
card, and reveals the dedicated event PIN once. The public gallery lives at
`/portfolio/events/<event-slug>/`: the cover page is public, and the single four-digit PIN unlocks
all active sub-events, face search, and exact-original downloads. A suspected leak is handled by
rotating the event PIN or unpublishing the event.

For development checks:

```bash
uv sync --extra server --extra desktop --extra vision
./scripts/check.sh
```

The default fast suite uses SQLite. The Session 7–8 CI job runs migrations, exact cosine-scope
tests, and the public portal/search/download boundary against `pgvector/pgvector:pg16`. To
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
application source is AGPL-3.0-only. Release packages include the repository licence, generated
dependency licences, and separate YuNet MIT and SFace Apache-2.0 notices. Pretrained model weights
are downloaded at runtime, hash-verified, and never bundled.

Search-result rows contain only ordered asset IDs and expire after one hour. Run the idempotent
cleanup command manually in development and every 15 minutes in deployed environments:

```bash
uv run python apps/server/manage.py purge_expired_face_searches
```

`reset_share_access --confirm RESET-ALL-SHARE-ACCESS` is an emergency-only operation to revoke every
event PIN before replacing a suspected-compromised `SHARE_PIN_PEPPER`. Event expiry does not itself
delete event media. After an event's 365-day lifetime and 30-day grace period, an operator verifies
the exact event and backup state, then runs the audited purge:

```bash
uv run python apps/server/manage.py purge_expired_event_media \
  --event-id 00000000-0000-4000-8000-000000000000 --confirm
```

The purge verifies deletion of private media/manifests/marks, removes face/search/portal state,
and keeps the consented event title and sanitized cover as a non-clickable portfolio card.

For verified privacy removal, use `erase_event_for_privacy`; it immediately unpublishes and revokes
the entire event before deleting all cloud media and redacting identifying database fields. For
photographer recovery, `reset_photographer_password` prompts interactively and revokes that user's
browser and desktop sessions. Both commands require explicit confirmation.

Production and rehearsal setup, provider-side manual steps, restore testing, monitoring, load
evidence, incident handling, and clean cutover are specified in the
[production runbook](docs/operations/production-runbook.md). Record only non-secret results in the
[launch evidence log](docs/operations/launch-evidence.md).

The server needs one private S3-compatible bucket per environment plus object read/write
credentials. It never creates a bucket per event: server-owned `events/<UUID>/...` keys and narrow
signed leases provide event isolation. For Cloudflare R2,
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
