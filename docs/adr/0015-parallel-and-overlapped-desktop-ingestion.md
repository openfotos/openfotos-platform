# ADR 0015: Parallel and overlapped desktop ingestion

- **Status:** Accepted
- **Date:** 2026-09-20
- **Amends:** ADR 0011's serial-processing constraint and ADR 0006's stage ordering

## Context

A wedding batch holds up to 10,000 photos. The Session 4–7 desktop pipeline ran three strictly
serial stages: every original transferred, then every preview/thumbnail rendered one photo at a
time, then every face embedding generated one photo at a time with one OpenCV thread (ADR 0011).
Wall-clock ingestion was the sum of network time and single-core CPU time, leaving most of a
modern workstation idle. The serial constraint was a pilot simplicity choice, not a correctness
requirement: per-asset checkpoints, exact-object leases, idempotency keys, and retry/reset
semantics are all asset-scoped and already safe under the concurrent original transfers that
Session 4 allowed.

## Decision

- Derivative rendering and face indexing each run in a thread pool of `min(4, cpu_count)` workers,
  configurable through `BatchSyncService(derivative_workers=..., face_workers=...)`. Pillow and
  OpenCV release the GIL inside their C operations, so threads parallelize the real work while
  sharing the existing lock-guarded SQLite checkpoint store.
- Face workers check out one YuNet/SFace engine each from a pool sized
  `min(face_workers, asset_count)`. OpenCV detector/recognizer instances are stateful and not
  thread-safe, so every worker owns its engine for the duration of one photo; each engine keeps
  the accepted single-OpenCV-thread configuration and hash-verified artifacts. The model contract,
  analysis document, and canonical contents are unchanged.
- The three stages run as overlapped runners instead of sequential phases. Derivative and face
  work for one asset starts as soon as its original is verified, while later originals still
  transfer. The originals runner signals verified assets; the other runners rescan their
  checkpoints with a one-second backstop wait.
- Preview-policy confirmation, the face-model compatibility check, and checkpoint creation move
  before the first transfer so every runner starts from validated, fail-closed inputs.
- A failure in any runner aborts the others at wave boundaries. Reported errors keep the
  deterministic originals → derivatives → faces priority of the serial order, and terminal
  progress is reported in the same stage order after all runners finish.
- Per-asset semantics are unchanged: five-attempt caps, stable failure codes, conflict handling,
  backoff with jitter, exact checksum verification, idempotency keys, and server reconciliation
  behave exactly as in the serial pipeline. Exhausted derivative/face items surface their
  review error once the originals runner has finished, so other uploads are not abandoned early.

## Consequences

Ingestion wall-clock approaches `max(transfer time, pooled CPU time)` instead of the serial sum;
face indexing and rendering each scale close to linearly with the worker count on multi-core
photographer workstations. Memory use grows by one loaded YuNet/SFace engine per face worker
(four by default).

Pause/resume, restart, and dashboard reset flows are unaffected because every stage still
persists asset-scoped checkpoints before and after each unit of work. A resumed batch recreates
the pools and continues from the same checkpoint boundaries as before.

## Verification

- Existing upload, derivative, face-index, cancellation, and reconciliation tests pass unchanged,
  including the deterministic final stage-progress order.
- A three-photo test with a barrier-protected engine proves pooled face workers embed
  concurrently and creates exactly one engine per worker.
- A two-photo test proves one asset's preview uploads while a later original is still
  transferring.
- A fatal derivative rejection aborts the pipeline with the stable error code and checkpoint
  failure state intact.

## Rejected alternatives

- **Process pools.** Engines and checkpoint handles cannot cross process boundaries cheaply, and
  the store is already designed for lock-guarded threads. Threads deliver the win without a new
  IPC boundary.
- **Browser/WebAssembly ingestion.** WASM inference is slower than native OpenCV, folder-scale
  resumability is weaker, and server-delivered code would weaken the pinned, hash-verified
  processing boundary that ADR 0011 established.
- **GPU inference.** No measured need after CPU pooling, and the pilot's photographer workstations
  are macOS-first; revisiting requires benchmark evidence under the Session 6 method.
