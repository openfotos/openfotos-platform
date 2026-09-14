# ADR 0008: Main events, sub-events, and search-only face indexing

- **Status:** Accepted
- **Date:** 2026-09-13
- **Supersedes:** ADR 0003; parts of ADRs 0002, 0005, 0006, and 0007

## Context

After Session 5, the client clarified that a delivered wedding is one main event containing
sections such as Haldi, Reception, Marriage, and Pre-wedding. The photographer decides those
sections while uploading. The main customer must eventually receive the complete wedding and be
able to share either the whole event or one section with full browsing or selfie-only discovery.

The earlier plan treated contributor devices as different lead/uploader identities and planned to
cluster uploaded face embeddings into customer-visible collections before delivery. That creates
identity, reconciliation, correction, and authorization concepts the client does not want. The
client wants direct photo search only. The client also requires four-digit access PINs.

Sessions 1–5 already implemented tenancy, lifecycle, desktop inventory, upload/manifests,
derivatives, and a private gallery. Face embeddings, share links, selfie search, and original
downloads were not implemented, so this decision retrofits only affected completed work and revises
the later session plan.

## Decision

### Event and sub-event ownership

- `Event` remains the main wedding and the only tenant, lifecycle, storage-quota, intake-generation,
  manifest, preview-policy, security, and retention boundary.
- `SubEvent` is a lightweight ordered section owned by one event. It has no child lifecycle, quota,
  preview policy, or nested sub-events.
- At least one active sub-event is required before upload and publication. Every local and server
  contribution batch has one non-null sub-event. Photos are never attached directly to the main
  event.
- The server validates event/sub-event membership under the event transaction. A wrong-tenant,
  wrong-event, missing, or archived child fails closed.
- The main gallery aggregates assets from all active sub-events. A sub-event route filters the same
  authorized set. List, photo, navigation, future search, and future downloads carry that filter.
- The photographer can create, rename, order, archive, and restore sub-events while the event is
  unpublished. Archiving hides its assets and blocks new upload/share/processing work.
- A complete batch, not an individual photo, can be reassigned to another active child before
  publication. The change is atomic and audited.

### Photographer installations

- Remove lead/uploader roles, upload-only sessions, and uploader invitations.
- One active photographer account membership authorizes event management.
- Up to ten desktop installations per event are registered to that account, labeled, audited, and
  independently revocable. Installation identity remains an operational retry/lease boundary, not
  a separate user role.
- Event-wide management operations remain account-authorized. Revocation blocks new transfer work
  on that installation but does not discard already reserved or uploaded state.

### PIN and future capabilities

- Every PIN is exactly four ASCII digits, generated with a cryptographically secure generator and
  displayed once. Users cannot choose a PIN.
- Store only an Argon2 hash of the PIN combined with a dedicated server-side
  `EVENT_PIN_PEPPER`. Keep database throttling, uniform errors, expiry, revocation, audit redaction,
  and short sessions.
- A four-digit PIN has only 10,000 possibilities, so every future owner/guest link must also carry
  an independent high-entropy URL secret. A forwarded owner link is not the guest-sharing model.
- Session 8 will add one photographer-created event owner capability. The owner can create/revoke
  independent guest capabilities scoped to the event or one active sub-event and to `full` or
  `selfie_only` mode. Every capability has its own generated four-digit PIN and required expiry.

### Search-only face index

- Remove all planned and documented clustering, cluster review, collection, featured collection,
  and collection-share behavior.
- In Session 7, the photographer desktop computes a model-versioned embedding for every usable face
  in each photo and uploads the per-photo analysis directly. Django validates and persists it.
- Each visible asset must reach terminal analysis (`indexed` or `no_usable_face`) before publication.
- In Session 8, a search input must contain exactly one usable face. Zero or multiple usable faces
  are rejected. The raw image and crop are ephemeral and never stored.
- Similarity queries filter by event and optional sub-event before distance/threshold evaluation.
  Results are photos, not people or collections.

### Downloads

- Remove the Session 5 original-download policy field, form, API/service, and dashboard control.
- Session 8 will allow original downloads for every photo visible to the current authorization:
  all scoped photos for full access and only current returned matches for selfie-only access.

## Compatibility and migration

- There is no non-test production database or media checkpoint to preserve.
- Django still uses a forward migration. Existing server batches are assigned to an `Imported
  photos` sub-event per event. Upload-only sessions are removed; unmatched legacy devices become
  revoked; matching account installations are retained.
- The local SQLite schema is versioned forward. Because an old batch has no legitimate server
  sub-event identifier, incompatible unsubmitted local batches are discarded during the role-to-
  sub-event migration. Event metadata remains, then a fresh server snapshot supplies active
  sub-events.
- Historical migrations and superseded ADR text retain old names only to explain/execute history.

## Consequences

The event remains a stable change boundary while sub-event categorization can evolve without
duplicating quota, lifecycle, policy, or object storage. Moving a batch changes metadata rather than
immutable object keys. One account model removes invitation/user-role policy and makes authority
consistent across desktop installations.

Face processing becomes substantially simpler: the system validates a direct per-photo search
index and avoids cluster reconciliation, false-merge correction, minimum-cluster rules, collection
ranking, and collection-scoped authorization. Search still needs strong model compatibility,
quality benchmarking, scope filtering, and privacy cleanup.

Four-digit PINs increase online and database-leak guessing risk. The accepted design compensates
with high-entropy URL capabilities, a server-side pepper, Argon2, rate limits, expiry, revocation,
and audit. A separate Session 8 design must define capability schema, expiry defaults/caps, and
pepper rotation before those links ship.

## Rejected alternatives

- **Make each ceremony a separate event.** This duplicates lifecycle, quota, preview policy,
  customer handoff, retention, and event-wide browsing/search.
- **Allow photos directly on the main event.** This makes section completeness and scoped sharing
  ambiguous.
- **Put sub-event names in object keys.** Renaming/reassignment would require copying immutable
  media and would couple presentation taxonomy to storage.
- **Retain uploader invitations alongside the photographer account.** The client has one operator
  identity; the extra role and invitation lifecycle provide no required product behavior.
- **Pre-cluster faces but hide collections.** This retains processing/reconciliation/error concepts
  without a consumer and delays delivery unnecessarily.
- **Use only a four-digit PIN.** It is too small to be the primary capability.
- **Keep a separate download-policy switch.** Visibility already defines the authorization set and
  a second switch creates contradictory combinations.

## Verification

- Shared event/contribution contracts require canonical active sub-events and a non-null child ID.
- Django and SQLite migrations reach the new schema from prior versions.
- Tests cover tenant and child mismatch, archived-child rejection, installation caps/revocation,
  whole-batch reassignment, main/filtered gallery behavior, generated peppered four-digit PINs,
  lifecycle gates, retries, quota, and privacy redaction.
- Repository search finds old uploader/collection concepts only in historical migrations or
  explicitly superseded ADR context.
