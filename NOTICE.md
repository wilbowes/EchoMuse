# Third-party components

EchoMuse is MIT licensed (see `LICENSE`). It vendors and links the components
below, each of which keeps its own licence. All are permissive and compatible
with redistribution under MIT, and each requires that its copyright notice
travels with the software — which is what this file is for.

The device binary published on the releases page is a **combined work**: it
links SpeexDSP and GoTinyAlsa. BSD-3-Clause asks that binary redistributions
reproduce the copyright notice "in the documentation or other materials
provided with the distribution", and this file is that material. If the binary
is ever distributed somewhere other than alongside this repository, this notice
needs to travel with it.

## Vendored into this repository

| Component | Where | Licence | Copyright |
|---|---|---|---|
| SpeexDSP (acoustic echo canceller) | `device/internal/aec/` | BSD-3-Clause | Xiph.Org Foundation, Jean-Marc Valin, Analog Devices, CSIRO |
| ONNX Runtime C API header | `device/internal/wakeword/ort/include/` | MIT | Microsoft |
| aioesphomeapi protocol buffers | `controller/esphome/vendor/` | MIT | Otto Winter |

Full licence texts sit beside the code, in `COPYING` or `*_LICENSE` files. Do
not remove them — they are the attribution the licences require.

## Linked as a submodule

| Component | Where | Licence | Copyright |
|---|---|---|---|
| GoTinyAlsa (`wilbowes/GoTinyAlsa` fork) | `GoTinyAlsa/` | BSD-3-Clause | binozoworks |

The fork exists to carry a `GetAudioStream` defer-in-loop leak fix; see
`device/CLAUDE.md` before repointing it upstream.

## Installed at build or run time

These are dependencies rather than vendored code — they are fetched by the
Dockerfiles and `requirements.txt`, and are not redistributed as source here.
Listed because they end up in the published container image:

- **ONNX Runtime** (MIT) — wake word inference, controller and device.
- **openWakeWord** (Apache-2.0) — wake word models and feature pipeline.
- **DTLN** (MIT, Nils L. Westhausen) — the two pretrained noise-suppression
  models the controller image downloads at build time, pinned to a commit in
  `controller/Dockerfile`. Baked into the published image, so they are
  redistributed with it.
- **ffmpeg** (LGPL-2.1+ as packaged by Debian) — invoked as a separate
  process, never linked.
- The Python dependencies in `controller/requirements.txt`, all permissive.

## Wake word models

The stock models shipped by openWakeWord keep their upstream licence. Models
built with `oww_forge/` are generated from synthetic speech; see
`oww_forge/README.md`, whose pinned upstreams carry their own terms.
