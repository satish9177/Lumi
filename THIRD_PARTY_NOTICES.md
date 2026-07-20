# Third-Party Notices

The MIT license in [LICENSE](LICENSE) applies to Lumi-authored source code and assets only. Third-party software and optional model assets remain subject to their own licenses.

## Optional local photo-search model packs

These files are not committed to this repository or included as source assets. Lumi downloads them only after the user enables the optional local photo-search feature; they stay on that device.

- **CLIP ViT-B/32** — by OpenAI; quantized ONNX export by Xenova (`Xenova/clip-vit-base-patch32`), MIT License.
- **Tesseract English trained data** — from the Tesseract OCR project (`tesseract-ocr/tessdata_fast`), Apache License 2.0.
- **YuNet face detector** — by Shiqi Yu and Yuantao Feng, MIT License. Lumi uses it only for local face counts; it does not identify people.

The application displays the relevant model-pack notices during the opt-in flow. See [docs/LOCAL-PHOTO-SEARCH.md](docs/LOCAL-PHOTO-SEARCH.md) for the pinned sources and local-storage behavior.

## Packaged dependencies

Electron, React, ONNX Runtime, Tesseract.js, Telegram, QRCode, and their transitive dependencies are separately licensed. Their license files are retained in their respective package distributions. This notice does not replace those upstream licenses.
