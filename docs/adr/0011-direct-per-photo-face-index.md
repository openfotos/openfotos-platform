# ADR 0011: Direct per-photo face index

- **Status:** Accepted
- **Date:** 2026-09-17
- **Depends on:** ADR 0010

## Context

Session 7 must turn the accepted YuNet/SFace contract into a searchable index without creating
people, identities, clusters, or customer-visible collections. Desktop processing is long-running
and resumable, several installations can contribute to one event, and embeddings are sensitive
personal data. The write boundary therefore has to bind every result to its tenant, event,
sub-event, originating installation, immutable source, and exact model contract before persistence.

Publication also needs a visible, recoverable definition of completion. A photo with no usable face
is a valid outcome; a technical failure is not. Archived or deliberately gallery-excluded photos
must not block publication or participate in search, while their analysis may remain retained for
the event's later retention policy.

## Decision

- Add one `FaceAnalysis` per asset and zero or more `FaceEmbedding` rows. Do not add a person,
  identity, cluster, or collection model.
- Store vectors in PostgreSQL `vector(128)` fields. Every search query applies the exact event and
  optional sub-event predicates in SQL, excludes archived sub-events and gallery-excluded assets,
  collapses multiple faces to each asset's best cosine distance, and uses the fixed ADR 0010
  threshold. Session 7 exposes this service boundary but no public search UI.
- Run detection and embedding generation only on the active workstation that originated the batch,
  one photo at a time with one OpenCV thread. The server never downloads originals to run the face
  model.
- Acquire YuNet and SFace from their upstream OpenCV Zoo locations into the private desktop data
  directory, or import existing local files. Verify the two accepted SHA-256 hashes before use;
  never track weights in Git.
- Transfer a strict canonical analysis document containing asset/source identity, the complete
  accepted model compatibility boundary, detected and usable counts, and only each usable face's
  ordinal, detector confidence, bounding-box width/height, and normalized vector. Do not persist
  crops, landmarks, coordinates, local paths, filenames, or runtime details.
- Treat `indexed` and `no_usable_face` as terminal successes. Technical failures may be attempted
  five times. A fifth failure blocks publication until the photographer either performs an audited
  per-photo reset and retries or records a gallery exclusion. A different valid terminal document
  becomes `conflict` and requires the same explicit audited reset; failure reporting cannot bypass
  that state.
- Identical document retries are idempotent and create no duplicate vectors. The idempotency key
  includes the canonical document digest so a genuinely different document reaches the semantic
  conflict check.
- Record face-index readiness by intake generation. Every visible, verified original in the current
  committed manifest must have a matching-source, matching-model terminal analysis before
  publication. Gallery review remains available as soon as derivatives are ready.
- Keep the desktop SQLite checkpoint free of embeddings. It stores only state, attempts, stable
  failure code, and detected/usable counts needed to resume.
- Refuse the Session 7 migration if any event is already published. Operators must unpublish and
  deliberately index existing events rather than silently changing the publication contract.

## Consequences

Vector generation remains resumable and private to the photographer workstation, while Django is
the sole authorization and validation boundary. Retried uploads are deterministic; malformed,
cross-tenant, cross-sub-event, wrong-installation, wrong-content, and incompatible-model documents
write no analysis or vector rows.

Exact scans are intentionally preferred over an approximate-nearest-neighbor index for the pilot.
This keeps scope predicates and ranking simple and independently testable. A later performance
change requires measured evidence and must preserve the same authorization-first query boundary.

An event cannot publish until face analysis catches up with its current generation. Operators can
see indexed, no-face, pending, and blocked counts in the dashboard, reset one photo without an
event-wide rebuild, and see corresponding progress/recovery state in the desktop.

Embeddings for archived or gallery-excluded assets remain stored but are excluded in SQL. Their
retention/deletion schedule remains a Session 9 privacy and operations decision.

## Verification

- API tests prove a valid document aimed at a sibling sub-event writes neither analysis nor vector,
  and malformed model/vector inputs fail before a write.
- Service tests prove same-document retries do not duplicate vectors, changed documents conflict,
  five failures require reset, and another installation cannot process the asset.
- PostgreSQL/pgvector tests execute cosine search and prove exact event/sub-event, archived-child,
  and gallery-exclusion scope.
- Lifecycle tests prove publication requires current-generation face readiness.
- Desktop tests prove checkpoints persist only resumable metadata, the third processing stage
  resumes, and model acquisition verifies hashes without leaving partial files.

## Rejected alternatives

- **Server-side model execution.** It adds private-original processing infrastructure and weakens the
  established desktop/server boundary without a pilot need.
- **Store face crops or landmarks for debugging.** They are not needed for search and expand the
  sensitive-data surface.
- **Use an ANN index immediately.** The pilot has no evidence that approximate lookup is needed, and
  it complicates exact authorization-first filtering.
- **Treat no face as failure or allow a manual no-face override.** Both either retry valid decor
  photos forever or let an operator bypass the accepted model outcome.
- **Allow any installation to rebuild any asset.** Local files and immutable batch ownership belong
  to the originating installation; broadening that scope creates an unnecessary authorization path.
