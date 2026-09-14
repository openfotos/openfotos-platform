# ADR 0002: Pilot accounts, tenancy, and event access

- **Status:** Superseded in part by ADR 0008
- **Date:** 2026-09-10

## Context

Session 2 must establish the authorization boundary before OpenFotos accepts media. The pilot has
one manually provisioned photographer, accountless visitors, and no Redis or separate identity
service. Tenant, event, and host identifiers must never widen access when altered.

## Decision

- Keep Django's built-in user model. Link users to photographer tenants through an explicit active
  membership; membership grants every event owned by that tenant and never an event from another
  tenant.
- Resolve photographer pages only from a single-label subdomain under `PUBLIC_BASE_DOMAIN`.
  Reserve platform labels and expose Django admin only on the base host.
- Provision photographers, memberships, events, write-only PINs, expiry, and lifecycle operations
  manually in Django admin. Photographer pages show a read-only event list during Session 2.
- Store a durable 256-bit random event token as a locator. It is not sufficient authorization;
  visitors must also enter the event PIN. An explicit admin action can rotate the token.
- Require exactly six ASCII digits for the pilot event PIN and hash it with Argon2. Apply a
  database-backed limit of five failures per 15-minute window for each subject and direct-client
  pair.
- Allow visitor unlock only for a matching tenant host, random token, Published state, and future
  expiry. Before unlock, reveal no event metadata and return a uniform 404 for mismatches or
  unavailable events.
- Issue a separate, event-scoped, host-only, HttpOnly signed cookie for at most 24 hours. Validate
  tenant, event, lifecycle, expiry, signature, and access version on every protected request. PIN
  changes and session revocation rotate the access version; archiving closes the event.
- Permit admin-only transitions through the shared event state machine. For the Session 2 sample,
  publication requires a PIN, future expiry, and legal transition path. Sessions 4 and 5 must add
  manifest and derivative readiness before any real event is published.
- Record successful, denied, and rate-limited authentication plus administrative tenant,
  membership, event, PIN, token, revocation, and lifecycle changes. Store a keyed client-address
  digest, never a raw address, password, PIN, or attempted secret.

## Consequences

The pilot proves the complete host-to-tenant-to-resource authorization chain without introducing
event-level team permissions, desktop tokens, media storage, or visitor accounts. Visitor access
can be revoked immediately, and altered host or event identifiers fail without disclosing private
metadata. Rate limits add small PostgreSQL write volume but require no extra service.

Six-digit PINs have only one million values. Argon2 and online throttling do not prevent offline
guessing after a database-only compromise because the durable event locator is stored in the same
database. The OpenFotos operator owns this accepted pilot risk and must add a separate PIN pepper
or stronger passcodes before a second photographer or simultaneous live events.

The application currently hashes the direct peer address and deliberately ignores forwarded IP
headers. Session 9 must verify Railway and Cloudflare proxy behavior before enabling a trusted
forwarded-client-address source.
