# ADR 0013: Studio portfolio, public event portal, image formats, and retention

- **Status:** Accepted and implemented
- **Date:** 2026-09-18
- **Depends on:** ADRs 0008–0012
- **Supersedes:** Admin-only main-event creation, JPEG-only original ingestion, and the unresolved
  retention/licence decisions in the Session 8 plan

## Context

The first photography company has five to six operators who work as one small studio. They need a
simple public portfolio like the supplied reference: studio branding, searchable event covers, and
one event gallery behind a four-digit PIN. Their workflow also includes edited PNG, WebP, and phone
HEIC/HEIF files. They require four-digit PINs for usability and accept a supervised first-company
pilot before stronger public-edge abuse controls are installed.

Session 8 already has private owner and guest capabilities. Reusing the owner link for a portfolio
card would expose guest-management authority through a publicly discoverable page. Creating one R2
bucket per event would also move storage administration into the application and multiply provider
configuration without adding authorization: OpenFotos already has server-owned event prefixes and
exact signed leases.

## Decision

### Studio workflow and web/desktop boundary

- Photographers create main events in the tenant web dashboard. Creation requires an event name, a
  cover image, and a versioned attestation that the studio has authority for upload, private
  sharing, persistent per-event face indexing, and public display of the title/sanitized cover.
- The web dashboard manages main events, sub-events, readiness, review, publication, portfolio
  branding, owner issuance, guest inspection/revocation, and portal PIN rotation. It is Django 5.2
  server-rendered HTML with vanilla CSS/JavaScript; owner, guest, and portfolio pages use the same
  stack and add no SPA framework.
- One studio login can be shared during the pilot. Up to ten installations per event are still
  tracked and revocable, but audit records identify the shared account rather than the individual
  human. This attribution tradeoff is accepted for the small first studio.
- The PySide6 desktop is the ingestion/processing workstation: event and sub-event selection;
  single files, multiple files, one or more folders, or mixtures; local validation/checkpointing;
  exact-original transfer; derivative rendering; and per-photo face analysis. It does not create
  public links or receive provider credentials.

### Portfolio and authority

- Each active photographer slug maps to `<slug>.onenodeai.com`. The landing page shows the studio
  display name/logo, optional phone and Instagram URL, title search, and responsive event cards.
- Every currently Published event is listed automatically. Draft, Uploading, Processing, Review,
  Archived, Failed, and Cancelled events are hidden. A card retains its title and cover after the
  private event media is purged but is non-clickable.
- A submitted cover/logo is decoded, bounded, oriented, converted to sRGB, and re-encoded without
  source metadata before object storage. Only that public derivative is retained; the submitted
  original is closed and discarded.
- First publication creates one separate whole-event `PortalCapability` and reveals a generated
  four-digit PIN once. The portal has browse, face-search, and original-download authority over the
  visible event, including active sub-events, but never owner/guest-management authority.
- The photographer separately issues one private owner capability. Only that owner creates guest
  capabilities; the photographer may inspect/revoke them. There is no second photographer-created
  general guest link.

### PIN and abuse posture

- All PINs remain exactly four generated ASCII digits, stored only as a peppered Argon2 hash. They
  are never user-selected. Owner and guest links retain their high-entropy fragment secrets.
- The public portal deliberately cannot have a hidden fragment because its URL appears on the
  portfolio. Its UUID plus PIN and current throttling are accepted only for the supervised first
  company. A distributed attacker can evade client-scoped limiting; Turnstile (or an equivalent
  challenge) plus global per-event throttling is mandatory before onboarding company two.

### Images and object storage

- Edited originals may be JPEG/JPG, PNG, WebP, HEIC, or HEIF. Extension and decoded format must
  agree, images must be static and within existing byte/pixel caps, and corruption fails closed.
  RAW, TIFF, GIF/animated images, BMP, and video are not accepted.
- Exact originals retain their accepted MIME type and extension in the server-owned key. Gallery
  preview and thumbnail derivatives remain JPEG. Exact-original downloads use a generic filename
  with the original format extension.
- One private R2 bucket is used per deployment environment. Server-owned UUID prefixes isolate
  events; the server issues exact short-lived PUT/GET URLs. Event creation never creates buckets,
  and desktop clients never receive R2 parent or bucket-administration credentials.

### Publication, retention, and deployment posture

- First publication fixes `first_published_at`, access expiry at 365 days, and purge eligibility 30
  days later. Unpublishing does not reset these dates.
- A confirmed, exact-event management command performs retention purge. It deletes and verifies all
  asset variants, ingestion manifests, and watermark objects before database cleanup; removes face
  analyses/embeddings, ephemeral search state, and owner/guest/portal capabilities; redacts source
  filenames/checksums; rotates residual access state; and preserves the consented portfolio title
  and cover.
- The initial region plan is Railway Singapore, Supabase Singapore, and R2 automatic/APAC placement
  where available. USD 20 Railway plus USD 25 Supabase is an initial pilot budget, not evidence of
  capacity. Supabase daily backups and an approximately 24-hour RPO are accepted for the first
  pilot, but an independent restore rehearsal remains a Session 9 gate.
- Windows and macOS builds may be unsigned for the supervised first pilot. The source licence is
  AGPL-3.0. Licence files/notices, native signing/notarization, and final release distribution remain
  Session 9 work.

## Consequences

The portfolio cannot accidentally inherit owner authority, and storage isolation remains a single
auditable key policy rather than provider-side bucket sprawl. Public cards incur short-lived signed
GET generation because the backing bucket stays private. Shared studio credentials make onboarding
simple but limit individual attribution; moving to per-person accounts is required when that tradeoff
is no longer acceptable.

The public four-digit portal is intentionally weaker than private capability links. It is suitable
only within the documented supervised boundary and cannot be presented as brute-force resistant.
Retention preserves a marketing card while deleting private media and biometric/search state.

## Rejected alternatives

- **Link a public card to owner access.** It would expose guest-management authority and conflate
  marketing discovery with the main customer's private capability.
- **Let photographers create all guest links.** The agreed handoff gives one owner the authority to
  manage ordinary family links while leaving the photographer with inspection/revocation support.
- **Create one R2 bucket per event.** Prefix-scoped keys and server-issued leases already express the
  boundary; bucket creation adds credentials and operational failure modes.
- **Retain uploaded cover originals.** They may include EXIF/GPS data and are unnecessary after the
  sanitized derivative is generated.
- **Accept every Pillow-readable format.** Broad implicit support would admit animation and formats
  the desktop/server contract has not tested.
- **Reset retention on republish.** That would let private media persist indefinitely through state
  toggles and make the consented lifetime unpredictable.

## Verification

- Integration tests create an event through the tenant dashboard, inspect the stored cover as a
  metadata-free JPEG, and verify the versioned attestation.
- Portfolio tests prove drafts are absent, the portal PIN unlocks the whole-event gallery, and owner
  controls are absent.
- Retention tests prove a pre-deadline purge writes/deletes nothing and a due purge deletes exact
  private objects/state while retaining the portfolio cover.
- Desktop and server tests cover PNG/WebP acceptance, mismatched/corrupt rejection, MIME-aware object
  leases/verification, and format-correct original keys/download names.
- CI imports and runs PySide6 tests with `libegl1`; no GUI test is skipped to mask the dependency.
