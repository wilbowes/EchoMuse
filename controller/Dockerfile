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
ARG GPU=0
RUN if [ "$GPU" = "1" ]; then \
      pip uninstall -y onnxruntime && \
      pip install --no-cache-dir onnxruntime-gpu==1.20.1; \
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
COPY em_scenes.py .
COPY em_ns.py .
COPY em_pki.py .
COPY em_esphome.py .
COPY em_ble_proxy.py .
COPY version.py .
# ESPHome native API subpackage (frame protocol, message registry, vendored protobuf)
COPY esphome/ ./esphome/
# Device payloads (start_server.sh etc.) — canonical copies served to devices
COPY device_payloads/ ./device_payloads/

# DTLN noise-suppression models (em_ns.py) — vendored at build time like the
# JS deps above; pinned to a commit so builds are reproducible. MIT licensed.
RUN mkdir -p /app/models/dtln && \
    curl -sfL -o /app/models/dtln/model_1.onnx \
      https://raw.githubusercontent.com/breizhn/DTLN/1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc/pretrained_model/model_1.onnx && \
    curl -sfL -o /app/models/dtln/model_2.onnx \
      https://raw.githubusercontent.com/breizhn/DTLN/1de1f15a8b5b7e1c44905618ff2ef70ca8277fbc/pretrained_model/model_2.onnx
# Dashboard SPA — placeholder is index.html, full dashboard is dashboard.html
COPY static/ ./static/
# Compile JSX dashboard with esbuild
RUN curl -sfL -o /tmp/esbuild.tar.gz       https://registry.npmjs.org/@esbuild/linux-x64/-/linux-x64-0.20.2.tgz &&     tar -xz -C /tmp -f /tmp/esbuild.tar.gz &&     install -m755 /tmp/package/bin/esbuild /usr/local/bin/esbuild &&     esbuild /app/static/dashboard.jsx       --bundle=false       --jsx=transform       --jsx-factory=React.createElement       --jsx-fragment=React.Fragment       --outfile=/app/static/dashboard.js &&     rm /tmp/esbuild.tar.gz
# Database persisted via volume mount
# docker run -v ~/EchoMuse/controller/data:/app/data ...
# Set DB_PATH=/app/data/echomuse.db in environment
CMD ["python", "-u", "em_controller.py"]
