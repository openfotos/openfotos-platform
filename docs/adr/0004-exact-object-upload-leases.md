# ADR 0004: Exact-object upload leases

- **Status:** Accepted
- **Date:** 2026-09-11

## Context

The original pilot plan proposed temporary S3 credentials scoped to an event prefix. That makes
large direct uploads possible, but a credential broad enough to create arbitrary objects under a
prefix is harder to constrain consistently across Cloudflare R2 and local S3-compatible services.
It could also accidentally grant an upload-only contributor the ability to list or read private
objects. OpenFotos already knows every accepted asset's immutable key, byte length, content type,
MD5, and SHA-256 before transfer.

## Decision

- Parent object-store credentials exist only in the Django server environment. A desktop receives
  a presigned `PutObject` URL for one server-owned original key, never credentials or a prefix-wide
  capability.
- A lease expires after five minutes. A request names between one and eight assets, while each
  desktop runs one to four transfers concurrently. Pause stops requesting new leases and lets
  active requests drain.
- The signature binds the exact key, content length, base64 Content-MD5, JPEG content type,
  `If-None-Match: *`, and an `openfotos-sha256` metadata value. The desktop sends those headers
  unchanged and streams the source while recalculating SHA-256. A changed source fails locally.
- Successful objects are immutable. HTTP 412 is treated as possible lost-response recovery, not
  permission to overwrite: the desktop asks Django to verify the existing object.
- Completion is authoritative only after Django performs `HeadObject` and compares content length,
  content type, MD5-shaped ETag, and the SHA-256 metadata with the frozen reservation. A mismatch is
  deleted and recorded as a failed upload. Provider failure fails closed and remains retryable.
- Each object gets at most five transfer attempts using exponential backoff with full jitter. Only
  a verified object is skipped after restart; an interrupted in-flight object restarts from byte
  zero.
- Immutable ingestion manifests are written by Django with conditional `PutObject` and reconciled
  by content hash if a prior response was lost.

## Consequences

R2 Object Read & Write credentials are sufficient for the server; upload devices do not require a
separate Cloudflare token or account-level permissions. MinIO and R2 exercise the same S3 API
boundary. A copied URL remains usable for its one object until its short expiry, so revoking a
device cannot invalidate an already issued URL immediately; that five-minute residual window is an
accepted pilot risk.

The service can prove transport integrity against accidental corruption and source changes. A
malicious enrolled contributor who intentionally supplies matching false MD5 and SHA-256 values for
their own bytes cannot be detected by these checks. Enrollment, independent device revocation,
lead review, and audit records are the pilot controls for that actor.
