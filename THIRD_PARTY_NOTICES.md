# Third-party notices

The project code, documentation and original generated artwork are distributed
under GPL-3.0-or-later; the full GPL version 3 text is in `LICENSE`.

## Vendored PWA helpers

`home_monitor/static/pwa-update.js` and `sw-update.js` are adapted from
[pwa-kit](https://github.com/zandaulion/pwa-kit), distributed under
GPL-3.0-or-later. The worker and cache-reset flow also adapt its reference
implementation. Home Monitor restricts caching to an explicit shell allowlist,
preserves unfinished forms, and avoids navigating clients that already reloaded.
Retain these source and license notices when redistributing them.

## Separately downloaded models

Model weights are not included in this repository or its source ZIP. The model
downloader pins SHA-256 digests and records sources in an ignored runtime manifest.
Downloading or using them does not change their upstream terms.

| Model | Upstream notice |
| --- | --- |
| YOLOX Nano | [Apache-2.0](https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE) |
| YuNet | [MIT](https://github.com/opencv/opencv_zoo/blob/main/models/face_detection_yunet/LICENSE) |
| SFace | [Apache-2.0](https://github.com/opencv/opencv_zoo/blob/main/models/face_recognition_sface/LICENSE) |

Python dependencies are installed separately and retain their own licenses and
notices. No dependency code, model weights, external photos, camera recordings,
or real face samples are bundled with the source release.
