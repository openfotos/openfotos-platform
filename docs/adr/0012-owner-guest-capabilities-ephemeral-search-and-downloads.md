# ADR 0012: Owner/guest capabilities, ephemeral search, and original downloads

- **Status:** Accepted and implemented
- **Date:** 2026-09-18
- **Depends on:** ADRs 0009–0011
- **Supersedes:** The interim event token/PIN flow and the remaining future-capability details in
  ADR 0008

## Context

Session 8 replaces the temporary event-wide visitor credential with the customer handoff needed by
the pilot. The photographer needs one private management link for the main customer. That owner may
share reusable family links to the whole wedding or one sub-event without forwarding management
authority. Search must remain a convenience over already-visible photos, and a submitted reference
photo must not become retained biometric material.

There is no production database to migrate. The design can remove the interim token/PIN fields
instead of carrying two authorization systems. Production provider selection and deployment remain
Session 9 work; Session 8 is verifiable with local/CI PostgreSQL plus pgvector and an S3-compatible
storage boundary.

## Decision

### Capability and authority model

- An event has at most one `OwnerCapability`. The photographer issues it only while the event is
  published. Revocation invalidates the owner, every guest derived from it, and their search-result
  state. Reissuing the owner replaces its secret, PIN, and access version rather than creating a
  second owner.
- The owner can browse/search/download the whole event or an active sub-event and create/revoke
  guest links. It cannot upload, change photos, alter event structure, publish, or perform other
  photographer controls.
- A `GuestCapability` is immutable in authority: it grants browse/search/download access to either
  the whole event or exactly one active sub-event. It cannot create links or widen its scope.
- The common family link is a reusable whole-event guest capability. New visible content in active
  sub-events is included by that scope after a later republish. The owner may also create a
  sub-event-scoped link.
- At most 100 unexpired, unrevoked guest capabilities may exist for one owner. A guest may have an
  optional private label of at most 80 visible characters. Labels are visible to the owner and
  photographer but excluded from audit metadata and logs.
- The photographer can view and revoke guest capabilities. Owner-link forwarding is not the guest
  sharing model.

### Credentials, expiry, and sessions

- Every owner and guest record has a UUID route identifier, an independent 32-byte URL secret, and
  an independently generated four-digit ASCII PIN. Only a SHA-256 secret digest and an Argon2 hash
  of `SHARE_PIN_PEPPER + PIN` are stored.
- The raw URL secret is transported in the browser fragment. A no-store landing page removes the
  fragment from history and posts the secret into a short-lived, path-scoped, HttpOnly presentation
  cookie. It therefore does not enter HTTP request targets, proxy access logs, or referrers.
- The link and PIN are shown once on a no-store credential card that supports copy and local text
  download. They are not recoverable. Losing either requires revocation and reissue.
- Owner and guest capabilities default to and are capped at 365 days, further capped by the event
  expiry and, for a guest, the owner expiry. The PIN remains stable for the lifetime of that link.
- Owner access sessions last at most 12 hours; guest sessions last at most 24 hours. Capability,
  owner, event, tenant, publication, expiry, revocation, access-version, and sub-event state are
  re-evaluated on every protected request.
- Unpublishing immediately pauses all links and rotates the event access version, invalidating
  residual sessions. Unexpired credentials remain recorded and can be presented again after
  republishing.
- `SHARE_PIN_PEPPER` is independent from `DJANGO_SECRET_KEY` and has no periodic rotation. Suspected
  compromise requires the explicit all-links reset command, verification of revocation, and only
  then replacement of the pepper. Silent rotation is prohibited.
- Link expiry disables access but does not automatically delete capabilities, photos, face-index
  rows, or objects. Session 9 must add a controlled, audited manual retention purge instead of
  ad-hoc provider-console deletion.

### Reference-photo search

- Owner and guest sessions may submit JPEG, PNG, WebP, HEIC, or HEIF from a camera or file picker.
  Each submission requires an explicit consent checkbox, is capped at 20 MiB and 40 megapixels,
  and must yield exactly one usable face under the accepted YuNet/SFace contract.
- Django performs detection and embedding with the accepted, hash-verified model artifacts. Zero
  usable faces, multiple usable faces, invalid input, or incompatible processing fail closed with a
  stable user-facing outcome.
- Queries call the Session 7 exact pgvector boundary: event and optional sub-event predicates are
  applied before the fixed distance threshold; assets are deduplicated and ranked by their best
  face. Search never authorizes a photo.
- Raw upload bytes, decoded pixels, crops, and the query vector exist only during the request and
  are discarded on every outcome. The database retains only the ordered matched asset UUIDs and
  capability/scope references for one hour. Result rendering rechecks current capability and photo
  visibility. Clear deletes the record immediately; an idempotent cleanup command deletes expired
  records, and Session 9 will schedule it at least every 15 minutes.
- Search is limited to 10 attempts per browser/link pair and 100 attempts per capability in each
  15-minute window. Stored rate-limit identities are keyed digests, not raw client addresses or
  cookies.

### Original delivery

- The photographer, owner, and guests can download any currently visible original inside their
  existing event/sub-event authority. Search-result membership does not add or remove download
  authority.
- Each request rechecks tenant, event, capability, sub-event, archive, object-verification, and
  gallery visibility before issuing a five-minute signed object-store GET with attachment
  disposition.
- Delivery is one exact uploaded original at a time; Session 8 adds no ZIP generation. Exact bytes
  may retain source EXIF/GPS metadata, so the photographer workflow must warn about that property.

## Consequences

The capability hierarchy expresses the actual handoff without adding customer accounts or making
face matching a permission system. Whole-event scope naturally includes later visible content,
while immutable child scope and database predicates prevent identifier substitution from widening
access. A lost link cannot be recovered, which is operationally less convenient but avoids storing
recoverable credentials.

The four-digit PIN remains low entropy. Its security depends on the unguessable fragment secret,
Argon2 plus a separate pepper, path-scoped cookies, fixed-window throttling, expiry, revocation, and
auditing as one combined control. A signed object URL can remain usable for at most five minutes
after authorization changes; that bounded residual risk is retained from the existing gallery
design.

One-hour result rows contain no biometric input but do reveal which assets matched a search. They
are non-authorizing, scoped to one capability, removed on clear/revocation/expiry cleanup, and never
written to object storage.

## Rejected alternatives

- **Keep or migrate the interim event token.** There is no production compatibility need, and two
  authorization systems would create ambiguous revocation and scope behavior.
- **Let guests forward the owner link.** That grants guest-management authority and prevents
  independent revocation.
- **Use a query parameter or path secret.** It would enter request-target, proxy, analytics, and
  referrer surfaces.
- **Permit user-selected or periodically changed PINs.** User choice weakens PIN generation and
  periodic changes add recovery burden without changing the high-entropy capability boundary.
- **Persist the query vector or reference crop.** Pagination needs only ordered asset identifiers;
  retained biometric input adds privacy risk without product value.
- **Authorize downloads only for face matches.** This would turn model relevance into access
  control and contradict ADR 0009.
- **Delete media automatically when a link expires.** Link expiry and event retention are different
  lifecycle decisions; deletion needs an explicit, auditable policy and rehearsal.

## Verification

- Tenant/host, UUID, fragment secret, PIN, expiry, publication, revocation, and cookie-version tests
  fail closed without revealing event metadata.
- Owner reset invalidates old secret, PIN, cookie, every guest, and associated search rows.
- A PostgreSQL/pgvector acceptance test gives a sub-event guest an embedding that also matches a
  sibling photo and proves the sibling is absent and no signed URL is issued.
- Zero/multiple-face inputs leave no result state; successful processing stores only ordered asset
  IDs; uploads are closed; the cleanup command preserves active rows and removes expired rows.
- Original download tests re-evaluate visibility and sign the verified original with attachment
  disposition; unauthorized sibling routes do not contact object storage.
