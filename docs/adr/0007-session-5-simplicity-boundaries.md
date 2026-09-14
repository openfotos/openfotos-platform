# ADR 0007: Session 5 simplicity boundaries

- **Status:** Superseded in part by ADR 0008
- **Date:** 2026-09-13

## Context

A post-Session-5 review found that the implemented behavior was covered by tests, but several
concepts were represented or coordinated in more than one place. Media variants and derivative
profile values had multiple definitions, event lifecycle decisions were spread across commands,
the event JSON response was assembled and interpreted independently, mutating API views relied on
a check-command-remember ordering convention, and the desktop network service owned unrelated
transport and workflow mechanics.

These are structural risks rather than new product requirements. The Session 5 behavior, API
routes and successful response payloads, object keys, image settings, authorization rules, privacy
properties, retry limits, and SQLite schema must remain stable.

## Decision

### Canonical cross-process values

- `openfotos_contracts.AssetVariant` is the single original/preview/thumbnail value set. The
  storage package and the former `DerivativeVariant` name re-export that same type for
  compatibility rather than defining another enum.
- The `gallery-jpeg-v1` dimensions, byte limits, JPEG quality, progressive/optimization flags, and
  subsampling are one immutable derivative profile used by desktop rendering and server
  validation.
- A strict event snapshot contract owns the versioned server-to-desktop event and preview-policy
  representation. Django serializes it and the desktop validates it before updating its local
  cache.
- The unused asset-state graph is removed. Persisted upload-object state remains the asset-object
  lifecycle used by the product.

### Event lifecycle policy

The contracts package continues to own the legal event-state graph. One pure server lifecycle
module now owns command-specific decisions for contribution start, intake reopening, finalization,
derivative readiness, and manual transitions. Transactional services still own locking, database
updates, object-store work, and audit records. Existing transition order, guards, error messages,
and automatic Processing/Review behavior are unchanged.

### Idempotent mutation execution

Mutating desktop API views use one executor that claims an actor/key/request tuple before running
the command and stores the successful response afterward. The database uniqueness constraint and
row locking make the claim authoritative. A matching completed request replays its original
response, a changed request conflicts, and a matching request already in progress returns a
retryable `idempotency_in_progress` response without running the command. Normal command failures
release their claim so the same key can be retried; abandoned claims expire after at most 15
minutes. Completed responses retain the configured idempotency lifetime.

### Desktop network boundaries

`DesktopNetworkService` remains the UI-facing facade. It composes three independently testable
responsibilities:

- authenticated JSON control-plane transport and refresh-token rotation;
- exact-byte object-store upload/download mechanics and streaming digests; and
- restart-safe batch synchronization, retry policy, checkpoint transitions, and derivative
  rendering orchestration.

The facade constructor, public methods, progress callbacks, errors, injected HTTP clients, and
ownership/close behavior remain compatible.

## Consequences

The implementation has fewer places where a profile, media variant, event response, or state
decision can drift. Concurrent retries can no longer both pass an idempotency lookup before a
command starts. A client can now distinguish that short in-progress window from a completed
replay or a conflicting request.

No data migration, route change, object rewrite, or derivative regeneration is required. Historical
SQLite and Django migrations retain their literal values because migrations are immutable records
of the schemas they created.
