# ADR 0014: Name-based event access and simplified studio workflow

- **Status:** Accepted; implementation complete
- **Date:** 2026-09-19
- **Supersedes:** ADR 0012
- **Amends:** ADR 0013's sharing, publication, and public-portal decisions

## Context

The photographer completed the first production rehearsal and reviewed the workflow against the
studio's current website. The owner/guest capability hierarchy, explicit intake/finalize steps,
mandatory preview-policy page, and desktop review screens add operational work that the studio does
not want. The photographer instead requires one recognizable event URL, one event PIN, uploads that
remain open until publication, and a substantially simpler desktop and gallery experience.

There is no customer production data. The rehearsal infrastructure will be destroyed and recreated,
so this revision does not need compatibility routes or data migration for issued owner/guest links.

## Decision

### Event URL and visitor authority

- An event receives a lowercase, hyphenated slug on first publication. The slug is unique within
  its photographer tenant, using `-2`, `-3`, and so on for collisions, and is frozen after it is
  assigned.
- Every visitor gallery operation is beneath
  `/portfolio/events/<event-slug>/`. UUID-based portfolio routes are removed without redirects.
- `OwnerCapability` and `GuestCapability`, their management flows, labels, secrets, scopes,
  expiries, and audit actions are removed. `PortalCapability` remains only as the internal
  one-to-one record containing the event PIN hash, access version, expiry, and revocation state.
- The generated four-digit event PIN is the only visitor credential. Unlocking it grants browsing,
  face search, and exact-original downloads across the event and all active sub-events. Search
  remains a non-authorizing filter over that already-visible set.
- A suspected link leak is handled by rotating the event PIN or unpublishing the event.

### Publication and upload lifecycle

- Workstations may reserve and upload work while the event is unpublished. User-facing intake
  close/reopen and finalize operations are removed.
- Publication atomically blocks new reservations and leases, snapshots completed photos, and
  verifies that every included photo has its required derivatives and terminal face analysis.
  Publication fails clearly if any snapshot photo is not processed.
- A batch already in flight may finish only its current file. Work outside the publication snapshot
  is excluded, reported to the desktop as not included, and reconciled from object storage.
- The dashboard warns with workstation and outstanding-photo counts before publishing while work is
  in flight.
- Unpublishing returns the event to uploading. Every later publication creates a fresh snapshot and
  re-runs the complete processing checks. Existing generation and manifest invariants remain
  internal implementation controls.

### Desktop workflow

- The primary flow is event, sub-event, one upload/verification page, then Submit. Validation skips
  are summarized inline and do not block accepted files.
- One progress bar communicates upload, thumbnail, and face-embedding stages. Pause and resume stay
  near that progress display; diagnostics and local cleanup move to an overflow menu.
- Watermarking defaults off and is configured only through an optional event-level dialog. The
  clean-preview policy is recorded silently when the first batch is submitted if the dialog was
  never opened. The policy then locks.
- Accepted face models download automatically in the background on launch through the existing
  pinned-hash, HTTPS-only, size-limited path. Only face embedding waits for the models. Failure is
  shown inline with retry; the face-model settings dialog is removed.
- Exactly one saved refresh-token session is resumed automatically. Sign out revokes the refresh
  token, removes its keyring entry, and returns to login. A failed resume returns to login with a
  neutral notice.

### Product presentation

- User-visible desktop and server branding becomes OneNodeAI Studio/OneNodeAI. Python packages and
  Django application names remain unchanged. The keyring service, bundle names, bundle identifier,
  and installer/window icons change with the visible product identity.
- The portfolio, cover, gallery, photo, and unlock pages adopt the agreed studio aesthetic with
  self-hosted open-license typography, icon controls, large event covers, and a justified gallery.
  Face search is closed by default and opens from a gallery action.
- The photographer event dashboard contains only sub-event creation, sub-events, contribution
  assignments, publication/PIN controls, and gallery preview.

## Accepted risks

The photographer explicitly accepts that a guessable event slug plus a four-digit PIN is the sole
visitor-access protection. The existing limit of ten failed PIN attempts per client address in
fifteen minutes does not prevent a distributed attacker from searching the 10,000-PIN space. This
is weaker than the secret-bearing capabilities from ADR 0012.

The photographer also accepts loss of per-guest revocation and sub-event-scoped guest links, plus
one forced workstation login caused by the keyring-service rename. These decisions do not relax
tenant-scoped database queries, private object storage, short-lived signed object URLs, ephemeral
reference-photo disposal, or face-model compatibility checks.

## Consequences

The customer journey and incident response become much easier to explain, and public URLs remain
recognizable after an event rename. In exchange, authorization has lower entropy and coarser
revocation. Publication becomes the single concurrency boundary, so its transaction and snapshot
semantics require explicit race tests.

The retained `PortalCapability` identifier is internal only. It may appear in database relations,
cookie names, and audit metadata, but never in a visitor URL or share surface.

## Verification

- Slug tests cover normalization, per-tenant collision suffixes, immutability after rename,
  UUID-route removal, and host/tenant isolation.
- Sharing-removal tests prove owner/guest routes, controls, tables, and issuance/revocation behavior
  are absent while event PIN browsing, search, downloads, rotation, and unpublish remain bounded.
- Publication race tests cover atomic reservation/lease blocking, deterministic in-flight
  exclusion, processed-photo validation, cleanup, and unpublish/republish.
- Desktop tests cover the consolidated workflow, watermark default/lock, model download states,
  automatic session resume, and sign out.

