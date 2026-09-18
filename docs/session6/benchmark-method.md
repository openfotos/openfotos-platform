# Session 6 face-engine benchmark method

This document freezes the benchmark method used before OpenFotos accepts a production face model.
The committed reports contain only aggregate measurements and reproducibility metadata. They must
not contain images, identity labels, filenames, paths, crops, or embeddings.

## Decision gates

The matching threshold is calibrated on the LFW development-training identities and evaluated,
unchanged, on the disjoint development-test identities. The selected threshold is the widest
cosine-distance threshold that stays at or below a 0.01% cross-identity false-accept rate on the
calibration split. Detection failures and faces rejected by the fixed quality rules count as
same-person misses.

A candidate passes the preliminary matching gate only when the holdout has at least 98% recall and
at most a 0.01% false-accept rate. Among candidates that pass every final gate, OpenFotos selects
the highest-recall candidate. The fixed threshold is not a guest or photographer setting.

The final decision additionally requires 500–1,000 consented representative wedding photos,
including dense groups, on the i5-6200U/8 GiB performance floor. Processing 10,000 photos and up
to 100,000 usable faces must project below 12 hours and 2 GiB peak RSS. LFW is aligned and
single-person-heavy, so its timing and detection yield cannot satisfy those final gates.

The representative benchmark requires at least ten photos that each produce ten or more usable
faces. It fits aggregate elapsed time as a fixed per-photo cost plus a per-detected-face cost, then
projects 10,000 photos and enough detections to yield 100,000 usable faces. The report records the
largest observed face count so reviewers can see how far the projection extrapolates.

## Frozen image and model contract

- Decode with Pillow, apply EXIF orientation, convert an embedded ICC profile to sRGB when present,
  and pass BGR pixels to the engines.
- Use a 640×640 detector input, a 0.5 detector execution floor, a 0.8 usable-face confidence
  threshold, and a 40-pixel minimum face side.
- Reject a photo result with more than 100 usable faces.
- L2-normalize every embedding and use cosine distance. A future result document is accepted only
  when its model ID, complete artifact-hash list, dimension, finite values, and L2 norm match the
  accepted contract.
- Run CPU inference with four threads. Timing uses a deterministic 1,000-image sample with seed
  `20260914`; quality is evaluated over every image in both LFW development splits.

## Candidates and pinned artifacts

| Candidate | Artifact | SHA-256 | Rights basis |
| --- | --- | --- | --- |
| OpenCV YuNet + SFace | `face_detection_yunet_2023mar.onnx` | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` | YuNet MIT |
| OpenCV YuNet + SFace | `face_recognition_sface_2021dec.onnx` | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` | SFace Apache-2.0 |
| InsightFace Buffalo-M | `det_2.5g.onnx` | `041f73f47371333d1d17a6fee6c8ab4e6aecabefe398ff32cca4e2d5eaee0af9` | Separate written OpenFotos use/distribution permission |
| InsightFace Buffalo-M | `w600k_r50.onnx` | `4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43` | Separate written OpenFotos use/distribution permission |

The OpenCV model terms are published with [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
and [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface).
InsightFace's public pretrained-model terms require separate licensing for commercial use; the
permission evidence confirmed for OpenFotos is private and must not be committed. Model files are
external inputs and remain ignored by Git even when redistribution permission exists.

## Reproduction

Install the vision dependencies and acquire the exact pinned artifacts outside Git. Use the LFW
deep-funneled images with `peopleDevTrain.csv` and `peopleDevTest.csv`, each containing the header
`name,images` and the standard View 1 identity/count rows.

```bash
uv sync --extra vision

XDG_CONFIG_HOME=/tmp/openfotos-session6-config \
XDG_CACHE_HOME=/tmp/openfotos-session6-cache \
MPLCONFIGDIR=/tmp/openfotos-session6-matplotlib \
NO_ALBUMENTATIONS_UPDATE=1 \
uv run python -m openfotos_vision.benchmark \
  --lfw-metadata ./lfw_dataset \
  --lfw-images ./lfw_dataset/lfw-deepfunneled/lfw-deepfunneled \
  --engine sface \
  --detector-model ./models/session6/opencv/face_detection_yunet_2023mar.onnx \
  --recognizer-model ./models/session6/opencv/face_recognition_sface_2021dec.onnx \
  --output ./docs/session6/reports/lfw-sface.json

XDG_CONFIG_HOME=/tmp/openfotos-session6-config \
XDG_CACHE_HOME=/tmp/openfotos-session6-cache \
MPLCONFIGDIR=/tmp/openfotos-session6-matplotlib \
NO_ALBUMENTATIONS_UPDATE=1 \
uv run python -m openfotos_vision.benchmark \
  --lfw-metadata ./lfw_dataset \
  --lfw-images ./lfw_dataset/lfw-deepfunneled/lfw-deepfunneled \
  --engine insightface \
  --detector-model ./models/session6/insightface/buffalo_m/det_2.5g.onnx \
  --recognizer-model ./models/session6/insightface/buffalo_m/w600k_r50.onnx \
  --output ./docs/session6/reports/lfw-buffalo-m.json
```

Each command prints its candidate and single process ID at startup, then aggregate progress every
500 images. Candidate processes run sequentially. The report is written atomically only after the
full evaluation completes.

For consented representative media, place the folder under the Git-ignored
`private_benchmark_media/` directory. The command requires an explicit consent attestation and the
matching report for the exact same model artifacts. It prints progress every ten photos and never
prints or reports a source path or filename.

```bash
XDG_CONFIG_HOME=/tmp/openfotos-session6-config \
XDG_CACHE_HOME=/tmp/openfotos-session6-cache \
MPLCONFIGDIR=/tmp/openfotos-session6-matplotlib \
NO_ALBUMENTATIONS_UPDATE=1 \
uv run python -m openfotos_vision.benchmark \
  --representative-images ./private_benchmark_media/consented-wedding-folder \
  --matching-report ./docs/session6/reports/lfw-sface.json \
  --consent-confirmed \
  --sample-size 1782 \
  --engine sface \
  --detector-model ./models/session6/opencv/face_detection_yunet_2023mar.onnx \
  --recognizer-model ./models/session6/opencv/face_recognition_sface_2021dec.onnx \
  --output ./docs/session6/reports/representative-expansion-sface.json
```

This unlabelled benchmark measures face instances, not unique people. It cannot independently
measure recall or false accepts, so its output embeds the hash and aggregate metrics of the
validated LFW evidence instead of treating wedding detections as identity ground truth.

## Preliminary LFW results

Both reports were produced on the i5-6200U/8 GiB performance floor with the frozen method above.

| Candidate | Holdout recall | Holdout FAR | Images/s | Projected 10k one-face time | Peak RSS | Preliminary matching gate |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| YuNet + SFace | 98.2047% | 0.003183% | 18.0204 | 0.154 h | 242.8 MiB | Pass |
| Buffalo-M | 67.4574% | 0.001836% | 2.6892 | 1.033 h | 695.9 MiB | Fail |

YuNet + SFace is the preliminary lead because it is the only candidate that passes both matching
guardrails. Its fixed candidate cosine-distance threshold is `0.55514365`.

Buffalo-M's low end-to-end recall is primarily a usable-face-yield failure under the fixed quality
rules: 16,687 of 24,620 holdout positive pairs had two usable faces, and 16,608 of those pairs
matched. It also left 3,183 of 13,233 images without a usable face. This does not support accepting
the Buffalo-M detector/recognizer pair even though its conditional recognition and FAR are strong.

These LFW decisions were preliminary because neither report measured dense groups, realistic
wedding crops, or 100,000 usable faces. The representative evidence below completed those gates.

## Representative wedding result

The consented dataset supplied on 2026-09-16 contained 361 full-resolution JPEGs totalling 9.93 GB.
The report intentionally contains no event name, source path, filename, image, face crop, identity,
or embedding.

| Measurement | Result | Gate |
| --- | ---: | --- |
| Evaluated photos | 361 | Fail: agreed minimum is 500 |
| Detected face instances | 2,686 | Informational |
| Usable face instances | 2,343 | Informational; not unique people |
| Photos with at least 10 usable faces | 74 | Pass: minimum is 10 |
| Maximum usable faces in one photo | 36 | Dense behavior exercised |
| Processing failures | 0 | Pass |
| Throughput | 0.4074 photos/s | Informational |
| Projected 10,000 photos/100,000 usable faces | 7.183 h | Pass: maximum is 12 h |
| Peak RSS | 781.4 MiB | Pass: maximum is 2,048 MiB |

The projection fits `2.210662` seconds per photo and `0.032742` seconds per detected face. Producing
100,000 usable faces is projected to require 114,639 detections; that is an average of 11.46
detections per target photo, within the observed maximum of 62.

YuNet + SFace therefore passes the matching, dense-group, performance, memory, and processing
reliability gates. The redacted report remains a no-go solely because 361 photos do not satisfy the
literal 500-photo representative-data minimum. Face instances cannot substitute for photo count
without explicitly revising that acceptance criterion.

## Final representative result and acceptance

A subsequent complete run evaluated 1,782 additional consented full-resolution JPEGs totalling
31.51 GB. The expanded report contains no event/folder name or per-photo data.

| Measurement | Result | Gate |
| --- | ---: | --- |
| Evaluated photos | 1,782 | Pass: minimum is 500 |
| Detected face instances | 8,558 | Informational |
| Usable face instances | 6,604 | Informational; not unique people |
| Photos with at least 10 usable faces | 119 | Pass: minimum is 10 |
| Maximum usable faces in one photo | 28 | Dense behavior exercised |
| Processing failures | 0 | Pass |
| Wall time | 54 min 16 sec | Informational |
| Projected 10,000 photos/100,000 usable faces | 6.022 h | Pass: maximum is 12 h |
| Peak RSS | 797.8 MiB | Pass: maximum is 2,048 MiB |

The expanded report decision is `go: true` with no failing reason. ADR 0010 formally accepts
YuNet/SFace and maximum cosine distance `0.55514365` for Session 7. The raw generated reports retain
their `preliminary` label so measurement output is not rewritten after review.
