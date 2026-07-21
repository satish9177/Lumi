# Third-Party Notices

The MIT license in [LICENSE](LICENSE) applies to Lumi-authored source code and assets only. Third-party software and optional model assets remain subject to their own licenses.

## Optional local photo-search model packs

These files are not committed to this repository or included as source assets. Lumi downloads them only after the user enables the optional local photo-search feature; they stay on that device.

- **CLIP ViT-B/32** — by OpenAI; quantized ONNX export by Xenova (`Xenova/clip-vit-base-patch32`), MIT License.
- **Tesseract English trained data** — from the Tesseract OCR project (`tesseract-ocr/tessdata_fast`), Apache License 2.0.
- **YuNet face detector** — by Shiqi Yu and Yuantao Feng, MIT License. Lumi uses it to detect and count faces locally, including face selection for optional user-labelled matching; YuNet itself does not identify people.
- **SFace face recognition** — by Yaoyao Zhong, distributed by the OpenCV Zoo (`opencv/opencv_zoo`, `models/face_recognition_sface`) under the Apache License 2.0. That directory's own `LICENSE` and README state that all files in it, including the `.onnx` weights, are Apache 2.0 licensed; OpenCV Zoo licenses per model rather than repository-wide. Lumi uses it only to compare faces against people the user has explicitly labelled on their own device.

  Recorded because the licence does not reveal it: the SFace weights descend from models trained on the CASIA-WebFace, VGGFace2 and MS-Celeb-1M datasets, and the model card does not state which produced this export. MS-Celeb-1M was subsequently withdrawn by Microsoft over the provenance of its images. The Apache 2.0 grant covers use and redistribution of the model artifact; it makes no representation about how those training images were collected.

The application displays the relevant model-pack notices during the opt-in flow. See [docs/LOCAL-PHOTO-SEARCH.md](docs/LOCAL-PHOTO-SEARCH.md) for the pinned sources and local-storage behavior.

## Packaged dependencies

Electron, React, ONNX Runtime, Tesseract.js, Telegram, QRCode, and their transitive dependencies are separately licensed. Their license files are retained in their respective package distributions. This notice does not replace those upstream licenses.
