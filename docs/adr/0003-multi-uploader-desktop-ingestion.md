# ADR 0003: Multi-uploader desktop ingestion

- **Status:** Accepted
- **Date:** 2026-09-10

## Context

The original pilot plan described one photographer and one event, but treated one desktop as the
owner of the event's local checkpoint and final manifest. The required workflow has up to ten
active contributor installations, each running OpenFotos on Windows or macOS. They normally divide about
10,000 edited photographs into disjoint sets, but may upload concurrently to the same event or to
different events. Each contributor must be able to choose a recursive folder, one photograph,
multiple photographs, or a mixture of folders and files.

A shared photographer password cannot enforce lead-only finalization or identify a compromised
device. Independent per-device manifests cannot form a stable event manifest while new batches are
arriving, and independently clustered face subsets would split one person across contributors.

## Decision

### Identity and authority

- The existing photographer account is the event lead. Only the lead can close intake, exclude
  persistent failures, cancel a reserved batch, reopen intake, run the authoritative clustering
  pass, or finalize the event.
- A separate high-entropy event invitation enrolls multiple upload-only devices for 72 hours or
  until the lead closes enrollment. It is not the six-digit visitor PIN and grants no gallery or
  event-management access.
- Each redemption mints a named, independently revocable device session. Do not infer or upload an
  operating-system username or hostname. Contributors see their own batches and aggregate event
  counts and remaining allowance, not other contributors' filenames or photographs.
- Session 3 exposes both lead and invitation UI honestly behind a network gateway. Real tokens,
  keyring persistence, invitation lifecycle, and authorization arrive in Session 4. The local
  inventory is demonstrable only through an explicitly labeled `--demo` event meanwhile.

### Contribution and event reconciliation

- Each installation owns one SQLite checkpoint but scopes every batch and file to a cached event.
  Random installation, batch, and local asset UUIDs prevent collisions across independent devices.
- A contribution remains editable while it is a draft. Validation approval freezes its selected
  paths, checksums, counts, and bytes. Later additions create another batch. Selecting the exact
  same path more than once in a batch inventories it once.
- Distinct source paths with identical SHA-256 values remain distinct accepted assets without a
  duplicate warning. They consume quota and later produce separate derivatives, faces, and gallery
  entries. Do not add an event/checksum uniqueness constraint.
- One device actively processes or uploads one batch and queues any others. Each device defaults to
  four direct transfers and may choose one through four or pause. One through ten active devices,
  including lead installations that contribute, therefore run independently without a central
  transfer scheduler.
- The server atomically reserves event bytes. A desktop's cached allowance is advisory; a later
  concurrent reservation may pause when earlier reservations consume the limit. Stale reservations
  are flagged after 24 hours but remain until an audited lead decision.
- The lead closes new intake, lets reserved work drain, explicitly excludes failures that remain
  after retry, and only then finalizes one event-wide manifest. Intake may reopen before publication
  as a new ingestion generation. Published events do not accept incremental uploads in the pilot.
- Successful cloud assets are immutable. A source edited after upload becomes a new asset; replacing
  an asset in place is not permitted.

### Local inventory contract

- Accept individual files, multiple files, recursive folders, and mixed selections from local or
  directly attached USB storage. Network shares, cloud placeholder files, RAW, video, HEIC, TIFF,
  symbolic links, aliases, junction traversal, and other special files are outside the first
  release. Skipped entries remain visible.
- A file is accepted only when a case-insensitive `.jpg` or `.jpeg` extension, JPEG byte signature,
  and full Pillow decode agree. The defaults are 100 MiB and 120 million pixels per file. A stricter
  cached server profile wins.
- Valid JPEGs proceed alongside explicit rejected files. An unreadable selection/directory or a
  file changing during validation makes the scan incomplete and blocks approval.
- Persist size, nanosecond modification/change times, SHA-256, dimensions, state, and stable reason
  codes after each file. A restart enumerates roots but does not decode or hash unchanged completed
  entries. Changed approved files pause for another review.
- A user can rebind a moved source root only after every accepted file at the new location matches
  its checkpoint checksum. Cloud manifests later receive basenames, never client directory paths.
- SQLite contains no credentials, image bytes, EXIF, face crops, or embeddings. Store it in the
  operating system's per-user application-data directory, store tokens in the credential keyring,
  export redacted diagnostics, and remove local event metadata only after explicit user confirmation.
  The pilot accepts OS-user protection instead of SQLite encryption.

### Distributed image work

- Every uploader device later generates derivatives and face embeddings for its own batch using the
  event's pinned application, model, watermark, and derivative profile. Compatibility fails closed
  before approval/reservation.
- After all batches drain, the lead desktop retrieves the event's compatible embeddings and runs one
  authoritative event-wide clustering pass. Per-device cluster merging is not permitted.

## Consequences

The event manifest becomes a server-owned reconciliation of immutable contribution manifests, not
a file posted by whichever desktop finishes last. Session 4 needs invitation/device/batch records,
transactional byte reservation, ten-client idempotency and quota-race tests, intake closure, and
lead-only cancellation/finalization. The asset table must allow repeated event checksums and source
filenames while object keys remain UUID based.

The shared invitation can be reused during its enrollment window. Short expiry, closing enrollment,
per-device tokens, and revocation limit but do not eliminate that exposure. Local paths and checksums
are not encrypted. These are accepted pilot risks.

Native Windows and macOS UI/filesystem smoke tests and a representative 10,000-photo benchmark are
release gates, not claims established by Linux CI. Session 3 ships a redacted benchmark command and
format; no client media enters the repository. Exact upload authorization and the refined active
device limit are recorded in ADRs 0004 and 0005; lead-desktop global clustering remains a Session 7
acceptance test.

## Session 3 acceptance criteria

- A synthetic nested selection reports exact accepted/rejected counts and bytes for folders plus
  individual files.
- A forced exit leaves a valid SQLite checkpoint; restart reuses unchanged per-file work.
- Changed files, incomplete traversal, profile mismatch, byte limits, pixel limits, misleading
  content, and invalid JPEGs produce intentional states and stable errors.
- Ten independent installation databases can target one event without installation, batch, or asset
  identifier collisions.
- Root relocation verifies checksums, diagnostics omit paths/filenames/checksums, and local cleanup
  never deletes source photographs.
- Normal UI mode cannot fake authentication. Explicit demo mode provides event selection,
  mixed-source selection, background progress/pause, validation review, approval, and the visible
  boundary to Session 4 upload.
