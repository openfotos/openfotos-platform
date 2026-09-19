# Third-party notices

OneNodeAI Studio relies on open-source Python packages, Qt libraries, and one open-license web font. Release builds generate
`THIRD_PARTY_LICENSES.txt` from the exact locked desktop environment and include it beside the
application, together with `LICENSE`, `NOTICE`, and the two model-license files. The generated file
is reviewed inside each draft release archive before publication.

The two optional face-model files are downloaded directly from OpenCV Zoo by the desktop client,
checked against frozen SHA-256 digests, and stored in the current user's private application-data
directory. They are not part of an installer or this repository:

- YuNet `face_detection_yunet_2023mar.onnx`: MIT License; see `licenses/YuNet-MIT.txt`.
- SFace `face_recognition_sface_2021dec.onnx`: Apache License 2.0; see
  `licenses/SFace-Apache-2.0.txt`.

The server self-hosts Cormorant Garamond (SIL Open Font License 1.1) for gallery and portfolio
typography instead of loading fonts from a third-party CDN:

- Cormorant Garamond web-font subsets: see `licenses/CormorantGaramond-OFL.txt`.

This notice is informational and does not replace the license texts shipped with a release.
