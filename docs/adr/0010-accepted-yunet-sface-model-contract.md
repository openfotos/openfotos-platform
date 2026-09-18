# ADR 0010: Accepted YuNet and SFace model contract

- **Status:** Accepted
- **Date:** 2026-09-16
- **Depends on:** ADR 0009

## Context

Session 6 had to select a replaceable CPU face detector/recognizer only after matching, dense-group,
performance, memory, privacy, and model-rights evidence passed. Face search is a relevance filter
over a gallery the guest may already browse, so the selection optimizes recall subject to a fixed
false-accept guardrail rather than using similarity as authorization.

The benchmark compared OpenCV YuNet plus SFace with InsightFace Buffalo-M. It then evaluated the
leading candidate on 1,782 consented full-resolution wedding photos totalling 31.51 GB on the
i5-6200U/8 GB performance floor. Reports are redacted aggregates; source media, filenames, paths,
identities, crops, and embeddings remain untracked.

## Decision

Accept the following indivisible model contract for Session 7:

- model ID: `opencv-yunet-2023mar-sface-2021dec`;
- detector: YuNet `face_detection_yunet_2023mar.onnx`, SHA-256
  `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`, MIT;
- recognizer: SFace `face_recognition_sface_2021dec.onnx`, SHA-256
  `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`, Apache-2.0;
- preprocessing: `exif-srgb-letterbox-opencv-sface-v1`;
- detector input: 640×640 with execution floor `0.5`;
- usable face: detector confidence at least `0.8` and minimum side at least 40 pixels;
- maximum usable faces per photo: 100;
- embedding: 128 dimensions, L2-normalized within `0.001` tolerance;
- metric: cosine distance;
- accepted maximum distance: `0.55514365`.

Model ID, ordered artifact hashes, preprocessing, detector geometry/floor, vector dimension,
normalization, quality rules, metric, and distance threshold are one compatibility boundary. A
change to any member requires a new model ID, benchmark decision, ADR, and explicit index rebuild;
it must never silently mix vectors.

## Evidence

The LFW holdout produced 98.204712% same-person recall and a 0.00318334% cross-identity
false-accept rate, passing the respective 98% minimum and 0.01% maximum.

The complete representative run produced:

- 1,782/1,782 photos and zero processing failures;
- 8,558 detected and 6,604 usable face instances;
- 119 photos with at least ten usable faces and a maximum of 28 usable faces in one photo;
- 54 minutes 16 seconds wall time and 797.8 MiB peak RSS;
- a 6.022-hour projection for 10,000 photos and 100,000 usable faces.

The final report decision is `go: true` with no failing reason. The raw benchmark reports retain
their generated `preliminary` label; this ADR is the explicit human acceptance record.

## Consequences

Session 7 may implement event-scoped per-photo face analysis and persistence against this exact
contract. Server validation must reject mismatched hashes, dimension, non-finite values,
normalization, model ID, asset scope, or content identity before writing vectors.

The representative dataset has no identity labels. Its 186 photos without a usable face may include
decor/detail photos as well as difficult faces, so the UI and publication workflow retain explicit
`no_usable_face` behavior and face search remains best effort. Guests can always return to the full
authorized gallery.

Buffalo-M is not accepted. Under the same quality rules it achieved only 67.457352% end-to-end LFW
holdout recall and was materially slower and larger in memory, despite strong conditional matching
for faces that survived filtering.

Model weights remain external, hash-verified inputs and must not be committed with source code.
