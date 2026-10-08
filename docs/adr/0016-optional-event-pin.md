# ADR 0016: Optional, dashboard-visible event PIN

- **Status:** Accepted
- **Date:** 2026-10-08
- **Amends:** ADR 0014's mandatory first-publication PIN and one-time reveal

## Context

ADR 0014 made every published event require one four-digit PIN, auto-generated at first publication
and revealed exactly once. The reveal page was the only moment the value was visible, because
`PortalCapability` stored only a peppered Argon2 hash. Two problems followed: the photographer could
not look up or resend a PIN after leaving the reveal page, and there was no way to publish an event
without PIN protection. Unpublish/re-publish already reused the stored hash, but the value stayed
unrecoverable, so the photographer could not confirm which PIN was active.

## Decision

- PIN protection is optional per event and starts off. First publication creates the internal
  `PortalCapability` record with `pin_enabled = False` and performs no PIN issue or reveal; the
  gallery is reachable without an unlock step.
- The Publication section of the event dashboard owns the switch. Turning protection on generates
  the event's single four-digit PIN; turning it off disables the requirement but keeps the stored
  value, so turning it back on reuses the same PIN. **Generate a new PIN** remains the
  leak-response reset and is the only operation that changes the value.
- Whenever protection is on, the PIN is shown in a plain box to every authenticated dashboard member
  for the tenant. No separate admin-only surface is introduced.
- `PortalCapability` gains `pin_enabled` and a recoverable `pin_value` beside the existing peppered
  Argon2 `pin_hash`. Verification still uses the hash; `pin_value` exists only so the dashboard can
  display it. Migration `0017` adds both fields and keeps already-issued PINs enforced by setting
  `pin_enabled = True` where a hash already exists.
- Unpublish/re-publish never generates a PIN; the persistent capability record carries the value
  across the cycle. Per-IP throttling, unlock auditing, and event/PIN-scoped session cookies are
  unchanged. The one-time reveal page and its `share.js` helper are removed.

## Consequences

- A photographer can publish an open gallery and add or remove the PIN at any time without
  republishing.
- Each event has exactly one PIN for its lifetime across toggles and republication; only an explicit
  reset changes it, and a reset invalidates existing visitor sessions through `access_version`.
- The database now holds a recoverable four-digit value so the dashboard can display it. The PIN was
  already a UX control rather than a standalone secret (the pilot boundary in ADR 0014/0013); the
  Argon2 hash remains the verification path and `SHARE_PIN_PEPPER` still protects it.
- Migration `0017` backfills `pin_enabled = True` for capabilities that already had a PIN hash, so
  deployed events do not silently become public.

## Verification

- `test_pin_is_optional_and_stays_the_same_when_reenabled` proves publication leaves protection off,
  enabling issues a four-digit PIN, disabling retains it, and re-enabling reuses it.
- `test_republish_after_unpublish_revalidates_every_included_photo` proves the same PIN and its hash
  survive unpublish/re-publish.
- `test_pin_toggle_shows_the_pin_and_controls_whether_the_gallery_unlocks` proves the dashboard shows
  the PIN to a member, the gallery requires unlock while protection is on, and it opens without an
  unlock when protection is off.
- `scripts/check.sh` passes, including migration drift and Django system checks.
