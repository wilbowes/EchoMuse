FROM python:3.12-slim
# Links the GHCR package page back to the repo (and lets the package inherit
# the repo's public visibility once connected).
LABEL org.opencontainers.image.source="https://github.com/wilbowes/EchoMuse" \
      org.opencontainers.image.description="EchoMuse controller — Echo Dot Gen 2 open-source voice assistant satellite"
WORKDIR /app
# Install curl for vendoring JS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends curl ffmpeg && rm -rf /var/lib/apt/lists/*
# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# GPU=1 swaps the CPU onnxruntime from requirements.txt for the CUDA build
# (both provide the same `onnxruntime` import, so they can't coexist).
# Local docker-compose.yml sets it; the published GHCR image is CPU-only.
#
# The two packages DO NOT share a version line, so this pin cannot be kept
# in lockstep with requirements.txt by matching the string. onnxruntime
# publishes 1.20.1 and no 1.20.2; onnxruntime-gpu publishes 1.20.0 and
# 1.20.2 and no longer publishes 1.20.1 — it was there when this was
# written and has since gone. The failure only ever surfaces on a fresh
# local GPU build (the published image is CPU-only and never runs this
# line), which is why it sat here unnoticed until 2026-08-10.
ARG GPU=0
RUN if [ "$GPU" = "1" ]; then \
      pip uninstall -y onnxruntime && \
      pip install --no-cache-dir onnxruntime-gpu==1.20.2; \
    fi
# Install openwakeword without deps — tflite-runtime has no Python 3.12
# x86_64 build; onnxruntime handles inference instead
RUN pip install --no-cache-dir --no-deps openwakeword==0.6.0 && \
    python3 -c "import openwakeword; openwakeword.utils.download_models()"
# Vendor JS dependencies — served locally, no CDN needed at runtime
RUN mkdir -p /app/static/vendor/fonts && \
    curl -sfL -o /app/static/vendor/react.min.js \
      https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js && \
    curl -sfL -o /app/static/vendor/react-dom.min.js \
      https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js && \
    curl -sfL -o /app/static/vendor/xterm.js \
      https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js && \
    curl -sfL -o /app/static/vendor/xterm.css \
      https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css && \
    curl -sfL -o /app/static/vendor/addon-fit.js \
      https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js && \
    curl -sfL -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
      -o /app/static/vendor/fonts/dm-sans.woff2 \
      "https://fonts.gstatic.com/s/dmsans/v17/rP2Yp2ywxg089UriI5-g4vlH9VoD8Cmcqbu0-K4.woff2" && \
    curl -sfL -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
      -o /app/static/vendor/fonts/dm-mono-400.woff2 \
      "https://fonts.gstatic.com/s/dmmono/v16/aFTR7PB1QTsUX8KYvumzEYOtbQ.woff2" && \
    curl -sfL -A "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
      -o /app/static/vendor/fonts/dm-mono-500.woff2 \
      "https://fonts.gstatic.com/s/dmmono/v16/aFTU7PB1QTsUX8KYthqQBA.woff2"
# Controller version — baked in by the controller-release workflow from the
# controller-v* tag; "dev" for local builds (version.py falls back to git
# describe only outside a container).
ARG EM_CONTROLLER_VERSION=dev
ENV EM_CONTROLLER_VERSION=$EM_CONTROLLER_VERSION
# Controller source
COPY em_controller.py .
COPY em_api.py .
COPY em_db.py .
COPY em_auth.py .
COPY em_eq.py .
COPY em_limiter.py .
COPY em_mbc.py .
COPY em_scenes.py .
COPY em_support.py .
COPY em_shadow.py .
COPY em_barge.py .
COPY em_ns.py .
COPY em_pki.py .
COPY em_hostip.py .
COPY em_ingressauth.py .
COPY em_linkauth.py .
COPY em_esphome.py .
COPY em_ble_proxy.py .
COPY em_oww_assets.py  ./
COPY em_oww_models.py .
COPY em_oww_warmup.py .
COPY em_arbiter.py .
COPY em_button.py .
COPY em_tap_burst.py .
COPY em_player.py .
COPY em_config_sections.py .
COPY em_recordings.py .
COPY em_turnclock.py .
COPY em_firmware.py .
COPY em_announce.py .
COPY em_runbarrier.py .
COPY em_volume.py .
COPY em_timers.py .
COPY version.py .
# ESPHome native API subpackage (frame protocol, message registry, vendored protobuf)
COPY esphome/ ./esphome/
# Device payloads (start_server.sh etc.) — canonical copies served to devices
COPY device_payloads/ ./device_payloads/
# Timer alarm sound (HA Voice PE, CC BY 4.0 — attribution in sounds/LICENSE.md,
# which ships with the audio rather than only in the repo).
COPY sounds/ ./sounds/

# DTLN noise-suppression models (em_ns.py) — vendored at build time like the
# JS deps above; pinned to a commit so builds are reproducible. MIT licensed.
RUN mkdir -p /app/models/dtln && \
    curl -sfL -o /app/models/dtln/model_1.onnx \
      https://raw.githubusercontent.com/breizhn/DTLN/1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc/pretrained_model/model_1.onnx && \
    curl -sfL -o /app/models/dtln/model_2.onnx \
      https://raw.githubusercontent.com/breizhn/DTLN/1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc/pretrained_model/model_2.onnx

# ONNX Runtime for the DEVICE (armeabi-v7a) — on-device wake word assets.
#
# NOT a controller dependency: it is a 12.3MB ARM shared library the
# controller only ever *distributes*, because it is deliberately absent from
# the firmware (12.3MB would double the OTA payload and both A/B slots).
# Vendored so devices never need internet, and pinned by AAR sha256 so a
# rebuild cannot silently change what gets installed on a device — a
# byte-for-byte match against what is already fielded is how the provenance
# of the hand-installed copy was confirmed. MIT licensed.
#
# Extracted with Python's zipfile because unzip is not in the slim base.
RUN mkdir -p /app/models/oww_runtime && \
    curl -sfL -o /tmp/ort.aar \
      https://repo1.maven.org/maven2/com/microsoft/onnxruntime/onnxruntime-android/1.19.2/onnxruntime-android-1.19.2.aar && \
    echo "84737dd1fdb715f0ee695c0a9864f6949b66ecb4c46ca733bc9211bf0c2a7b5f  /tmp/ort.aar" | sha256sum -c - && \
    python -c "import zipfile, shutil; z = zipfile.ZipFile('/tmp/ort.aar'); shutil.copyfileobj(z.open('jni/armeabi-v7a/libonnxruntime.so'), open('/app/models/oww_runtime/libonnxruntime.so', 'wb'))" && \
    rm /tmp/ort.aar
# Dashboard SPA — placeholder is index.html, full dashboard is dashboard.html
COPY static/ ./static/
# Compile JSX dashboard with esbuild
RUN EARCH="$(dpkg --print-architecture)" && case "$EARCH" in amd64) EPKG=linux-x64 ;; arm64) EPKG=linux-arm64 ;; *) echo "unsupported arch: $EARCH" >&2; exit 1 ;; esac && curl -sfL -o /tmp/esbuild.tar.gz "https://registry.npmjs.org/@esbuild/${EPKG}/-/${EPKG}-0.20.2.tgz" &&     tar -xz -C /tmp -f /tmp/esbuild.tar.gz &&     install -m755 /tmp/package/bin/esbuild /usr/local/bin/esbuild &&     esbuild /app/static/dashboard.jsx       --bundle=false       --jsx=transform       --jsx-factory=React.createElement       --jsx-fragment=React.Fragment       --outfile=/app/static/dashboard.js &&     rm -rf /tmp/esbuild.tar.gz /tmp/package /usr/local/bin/esbuild
# Database persisted via volume mount
# docker run -v ~/EchoMuse/controller/data:/app/data ...
# Set DB_PATH=/app/data/echomuse.db in environment
COPY em_cacerts.py .
COPY em_start.py .
# em_start.py bridges /data/options.json (Home Assistant add-on config) into
# the env vars em_controller.py already reads, then execs into it — a no-op
# outside the add-on, where /data/options.json doesn't exist.
CMD ["python", "-u", "em_start.py"]
