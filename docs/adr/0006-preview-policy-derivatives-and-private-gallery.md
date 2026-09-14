# ADR 0006: Event preview policy, local derivatives, and private gallery

- **Status:** Superseded in part by ADR 0008
- **Date:** 2026-09-12

## Context

Session 5 must turn verified originals into fast gallery media without changing the uploaded bytes
or widening access to the private bucket. The initial plan assumed that every preview was
watermarked, but the photographer needs watermarking to be optional and controlled from the
desktop. Multiple contributor installations also need one identical policy, otherwise an event can
contain inconsistent branding.

## Decision

### Canonical preview policy

- Watermarking is disabled by default. The event lead confirms one immutable preview policy in the
  desktop before derivative processing. Upload contributors can read it but cannot configure it.
- The server persists and versions the policy for all installations. An enabled policy requires a
  logo, one line of Unicode text, or both. The desktop offers the built-in OFTS wordmark and a
  custom transparent PNG while preserving logo color and alpha.
- The four fixed layouts are compact bottom-right, bottom-center, large centered brand, and repeated
  diagonal. Placement, scale, backing plate, and opacity belong to the template rather than a free
  form editor.
- The lead desktop rasterizes logo and text once into a canonical PNG. The server validates,
  normalizes, hashes, and stores that mark at an immutable private object key. Contributors fetch
  those exact bytes, so local fonts and SVG support cannot change an event after confirmation.
- The settings screen previews against a lead-selected local event photo when available and a
  synthetic sample otherwise. The sample is not uploaded or retained by OpenFotos.

### Derivatives and original integrity

- Every verified original receives a JPEG preview with a 2048-pixel maximum long edge at quality 85
  and a clean JPEG thumbnail with a 512-pixel maximum long edge at quality 78. Images are never
  upscaled. EXIF orientation is applied, colors are converted to sRGB, and all derivative EXIF,
  GPS, ICC, comment, and filename metadata is removed.
- Only previews receive the optional watermark. Thumbnails remain clean. Original objects are never
  decoded, rewritten, or watermarked; Session 8 downloads return their exact uploaded bytes and may
  therefore retain original metadata.
- The same desktop action uploads originals, renders derivatives immediately after original
  verification, registers exact size/checksum/dimension metadata, obtains exact-object PUT leases,
  and asks Django to verify each object with HEAD. Original, preview, and thumbnail checkpoints are
  independent and restart-safe.
- A contributor may obtain a five-minute exact GET URL only for one of its own verified originals
  when the local source is unavailable. The temporary read-back and rendered cache use private
  permissions and are deleted after verification or failure cleanup.
- A stable renderer/profile mismatch fails closed. After five reported processing attempts, the
  lead may audit-exclude an asset from the gallery while retaining its private original and manifest
  record. This decision is reversible in Processing or Review.

### Gallery delivery and publication

- Gallery pages are server rendered, use a 48-thumbnail masonry page, and provide a full-frame
  preview page with previous, next, and back navigation. Visitor HTML never shows uploaded
  filenames or internal object keys.
- Django checks the photographer membership or event PIN session before signing one exact preview
  or thumbnail GET for five minutes. Gallery HTML is private/no-store and noindex. Revocation and
  unpublishing invalidate application sessions immediately; already issued URLs can remain usable
  for up to five minutes.
- Ordering uses capture time when available, followed by natural filename order and asset UUID.
  Only the parsed capture timestamp is retained from EXIF.
- Publication requires a committed current-generation ingestion manifest, a confirmed preview
  policy (including the disabled policy), both verified derivatives for every non-excluded asset,
  a future expiry, and an event PIN. Finalization re-runs readiness because contributors may finish
  derivatives before intake closes.
- The photographer dashboard provides pre-publication review, failure/exclusion state,
  publish/unpublish, and an original-download policy with `disabled`, `authorized-visitors`, and
  `explicit-shares` modes. It defaults to disabled. Session 5 stores and audits the policy; Session 8
  implements original download and explicit-share delivery.

## Consequences

An optional watermark deters casual reuse but is not an access-control boundary. A visitor who can
display a 2048-pixel preview can save or screenshot it even when original downloads are disabled.
Short signed URLs also have a deliberate five-minute residual-access window after revocation.

The pilot trusts enrolled contributor software to follow the derivative contract, backed by strict
metadata validation, exact-object leases, server verification, lead review, and audit history. A
malicious enrolled contributor could still manufacture internally consistent derivative bytes;
server-side re-rendering is outside the pilot budget.
