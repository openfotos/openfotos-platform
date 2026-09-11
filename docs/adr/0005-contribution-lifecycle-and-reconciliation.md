# ADR 0005: Contribution lifecycle and event reconciliation

- **Status:** Accepted
- **Date:** 2026-09-11

## Context

Concurrent contributors need a shared storage limit and a stable definition of when an event is
complete. Device revocation, partial failure, retries, and reopened intake make a single desktop's
manifest insufficient. The Session 4 interview also clarified that the ten-device boundary counts
lead installations that actually contribute, not only invitation-based uploaders.

## Decision

### Sessions and contribution devices

- Desktop access tokens last 15 minutes. Refresh tokens rotate, have an absolute 14-day lifetime,
  and permit a 60-second response-loss retry. Only refresh tokens persist, using the operating
  system credential store; if it is unavailable they remain in memory with a visible warning.
- An invitation is reusable for 72 hours and at most ten successful new-device redemptions. The same
  active installation can redeem it idempotently. Closing intake closes outstanding invitations.
- Each event permits one through ten active contribution devices. Uploading lead installations and
  invitation-based uploaders use the same limit. Revocation frees an active slot but retains the
  device and audit history.

### Reservation and resolution

- Validation approval freezes an entire contribution. Django reserves all of its declared original
  bytes in one database transaction or rejects the entire batch. Only originals are in Session 4;
  the object model remains variant-aware for Session 5.
- Reserved bytes count against the event limit. Cancelling a wholly unverified batch or excluding an
  individual unverified failure releases its bytes. Verified originals remain charged and cannot
  be replaced in place.
- Only the lead can cancel, exclude, revoke, close or reopen intake, and finalize reconciliation. A
  batch can be cancelled only before any original is verified. After partial success, the lead must
  give a reason for each excluded asset. Every such action is audited.
- Revocation immediately blocks new leases and device API access. Existing reservations remain for
  explicit lead cancellation, verification, or reasoned exclusion; they are not silently released.

### Intake generations and publication

- Closing intake blocks invitation enrollment and new reservations but lets already reserved work
  drain. Finalization requires every original to be verified or excluded and at least one verified
  original.
- Django creates one immutable, cumulative `generation-NNNNNN.json` reconciliation document and
  moves the event from Uploading to Processing. Reopening before publication increments the intake
  generation, clears the current-manifest pointer, and moves Processing or Review back to Uploading;
  prior generation objects remain immutable history. Generic administrative state transitions
  cannot bypass finalization or generation incrementing.
- `final.json` is reserved for a later publication artifact. Publishing is blocked unless the
  current ingestion generation has a committed manifest and the same generation has completed its
  derivative pipeline. There is no administrative bypass.

## Consequences

Quota correctness depends on PostgreSQL row locks and a consistent event-to-batch-to-asset lock
order. Cached desktop totals are advisory. Storage failures during cleanup preserve reservations so
capacity is never released while an unverified object may still exist.

The lead owns exceptional resolution. This adds an operational step for abandoned or revoked
contributions but avoids hidden data loss and makes the final manifest explainable. Published events
remain closed to incremental ingestion during the pilot.
