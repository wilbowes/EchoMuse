# oww_forge — openWakeWord custom wake-word trainer.
#
# Build args:
#   GPU=1     → CUDA torch wheels (NVIDIA; large image, fast generation + training)
#   GPU=rocm  → ROCm 6.3 torch wheels (AMD). ROCm torch answers as torch.cuda,
#               so upstream runs unchanged; the host needs the amdgpu driver.
#   GPU=xpu   → XPU torch wheels (Intel Arc / Core Ultra), Intel's user-space
#               GPU driver, and patch_xpu.py over upstream's CUDA-only calls.
#   GPU=0     → CPU-only torch (default; ~3GB image, generation/training is slower)
#
# All pins are deliberate: openwakeword's train.py imports `generate_samples`
# from the piper-sample-generator repo ROOT, which only exists up to the
# v2.0.0 tag (the repo became a package afterwards). Don't bump that pin
# without re-checking train.py's import.

# Pinned by the multi-arch index digest; Dependabot moves it with the tag.
FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

ARG GPU=0

# build-essential: webrtcvad ships as sdist only and compiles a C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
        git wget ca-certificates libsndfile1 ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

# Intel's user-space GPU driver (Level Zero), for GPU=xpu only. Debian has no
# packages for it; these are Intel's release debs for compute-runtime
# 26.40.40196.8 and what its release notes pair it with, by hash (the
# compute-runtime three match Intel's ww40.sum).
RUN if [ "$GPU" = "xpu" ]; then \
        cd /tmp \
        && for u in \
            intel/intel-graphics-compiler/releases/download/v2.42.6/intel-igc-core-2_2.42.6+23070_amd64.deb \
            intel/intel-graphics-compiler/releases/download/v2.42.6/intel-igc-opencl-2_2.42.6+23070_amd64.deb \
            intel/compute-runtime/releases/download/26.40.40196.8/intel-opencl-icd_26.40.40196.8-0_amd64.deb \
            intel/compute-runtime/releases/download/26.40.40196.8/libigdgmm12_22.10.2_amd64.deb \
            intel/compute-runtime/releases/download/26.40.40196.8/libze-intel-gpu1_26.40.40196.8-0_amd64.deb \
            oneapi-src/level-zero/releases/download/v1.34.0/libze1_1.34.0+u24.04_amd64.deb; \
           do wget -q "https://github.com/$u"; done \
        && printf '%s\n' \
            'e20539632f2d99875d8f8b225d5da58b31f98baa54bb47c6328aa98ec473ab9d  intel-igc-core-2_2.42.6+23070_amd64.deb' \
            '7f5593cf828503ffb8450967768bde9209e22753bc576296e846b5a751011fa3  intel-igc-opencl-2_2.42.6+23070_amd64.deb' \
            '96046069e94680b761df6e83ddc64244721178b826e3e1f8af3ca104cfd0c7c2  intel-opencl-icd_26.40.40196.8-0_amd64.deb' \
            '053fceadb84a1d2f9a30a0d1bc9b16424b3f1a90cc49900fcd80aadcf25c8a4d  libigdgmm12_22.10.2_amd64.deb' \
            'd9c86b2bb195baea6936c2708846cd1fceeadc39bc5e167f728858a5ca6fb86e  libze-intel-gpu1_26.40.40196.8-0_amd64.deb' \
            '45210e4549cd965ad7b9f160eefe53cbcdba01af341bea7ac7ba0aedeeb613ba  libze1_1.34.0+u24.04_amd64.deb' \
           | sha256sum -c - \
        && apt-get update && apt-get install -y --no-install-recommends ./*.deb \
        && rm -rf /tmp/*.deb /var/lib/apt/lists/*; \
    fi

# torch first, so the CPU/GPU split doesn't invalidate the rest of the cache.
# cu128 is required for Blackwell cards (RTX 50xx = sm_120); every variant
# falls back to CPU at runtime when no GPU is visible. The checks fail the
# build if the wrong variant was installed, or a later install replaced it
# (the CPU index is not checked: its arm64 wheels carry no +cpu).
RUN case "$GPU" in \
        1)    index=cu128 ;; \
        rocm) index=rocm6.3 ;; \
        xpu)  index=xpu ;; \
        *)    index=cpu ;; \
    esac \
    && pip install --no-cache-dir torch==2.7.1 torchaudio==2.7.1 \
        --index-url "https://download.pytorch.org/whl/$index" \
    && python -c "import torch; v = torch.__version__; print('torch', v); assert '$index' == 'cpu' or v.endswith('+$index'), v"

COPY requirements.txt /opt/forge/requirements.txt
RUN pip install --no-cache-dir -r /opt/forge/requirements.txt \
    && python -c "import torch, sys; print('torch', torch.__version__)" \
    && if [ "$GPU" != "0" ]; then python -c "import torch; assert '+' in torch.__version__ and not torch.__version__.endswith('+cpu'), torch.__version__"; fi

# openWakeWord, pinned to a known-good main commit (train.py + example config
# verified against this SHA — see oww_forge/README.md)
RUN git clone https://github.com/dscripka/openWakeWord /opt/openwakeword \
    && git -C /opt/openwakeword checkout 368c03716d1e92591906a84949bc477f3a834455 \
    && pip install --no-cache-dir -e /opt/openwakeword \
    # Upstream bug: --convert_to_tflite defaults to the STRING "False", which
    # is truthy — without this patch every --train_model run ends by importing
    # tensorflow (not installed here; we ship ONNX only) and crashing.
    && sed -i 's/if args.convert_to_tflite:/if args.convert_to_tflite is True:/' \
         /opt/openwakeword/openwakeword/train.py

# piper-sample-generator, pinned to v2.0.0 (last flat-layout release: train.py
# does `from generate_samples import generate_samples` against the repo root).
# The models/ dir is symlinked into /data so the ~430MB LibriTTS-R checkpoint
# lives on the volume (downloaded by `forge.py assets`), not in the image.
RUN git clone https://github.com/rhasspy/piper-sample-generator /opt/piper-sample-generator \
    && git -C /opt/piper-sample-generator checkout 195e3bd967d54589c2137c9de2b22ad526ba6b6f \
    && rm -rf /opt/piper-sample-generator/models \
    && ln -s /data/assets/piper /opt/piper-sample-generator/models \
    # torch >= 2.6 defaults torch.load to weights_only=True; the LibriTTS-R
    # checkpoint is a full pickled model object, so loading needs the old mode.
    && sed -i 's/torch.load(model_path)/torch.load(model_path, weights_only=False)/' \
         /opt/piper-sample-generator/generate_samples.py \
    && grep -q "weights_only=False" /opt/piper-sample-generator/generate_samples.py

# Frozen feature-extraction models (melspectrogram + Google speech embedding).
# train.py expects them inside the openwakeword package resources.
RUN OWW_RES=/opt/openwakeword/openwakeword/resources/models \
    && mkdir -p $OWW_RES \
    && for f in melspectrogram.onnx embedding_model.onnx melspectrogram.tflite embedding_model.tflite; do \
         wget -q -O $OWW_RES/$f \
           https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f; \
       done

# deep-phonemizer's checkpoint load also predates torch 2.6's weights_only
# flip; it's only reached for out-of-dictionary words (e.g. phonetic spelling
# variants like "clarra"), so it survives dictionary-word test runs.
RUN sed -i 's/torch.load(checkpoint_path, map_location=device)/torch.load(checkpoint_path, map_location=device, weights_only=False)/' \
      /usr/local/lib/python3.11/site-packages/dp/model/model.py \
    && grep -q "weights_only=False" /usr/local/lib/python3.11/site-packages/dp/model/model.py

# ...and it is no longer reached at all. data.py downloaded DeepPhonemizer's
# en_us_cmudict_forward.pt on first use from an S3 bucket that answers 403
# (2026-09-28, no mirror found), saved the 278-byte error page AS the model,
# and so failed every later run too; and the download lived in the
# container, so recreating the container lost a copy that had worked.
# forge_g2p answers the same question from espeak-ng, already here for
# Piper. The patch asserts both edits landed, so an upstream change fails the
# build rather than quietly reviving the download.
COPY forge_g2p.py /opt/openwakeword/openwakeword/forge_g2p.py
COPY patch_oww_g2p.py /tmp/patch_oww_g2p.py
RUN python /tmp/patch_oww_g2p.py /opt/openwakeword/openwakeword/data.py && rm /tmp/patch_oww_g2p.py

# train.py builds adversarial negatives one target phrase at a time and only
# removes texts equal to that phrase, so "hey verona" emits "verona" as a
# negative even when "verona" is a target too. The bare name then trains as
# positive and negative at once and stops firing (2026-10-03). forge_negatives
# drops any negative containing a target phrase; the patch asserts both call
# sites were found.
COPY forge_negatives.py /opt/openwakeword/openwakeword/forge_negatives.py
COPY patch_oww_negatives.py /tmp/patch_oww_negatives.py
RUN python /tmp/patch_oww_negatives.py /opt/openwakeword/openwakeword/train.py && rm /tmp/patch_oww_negatives.py

# Intel GPUs are torch.xpu, and upstream asks only for torch.cuda; see
# patch_xpu.py. Every other image keeps upstream's device code as it is.
COPY patch_xpu.py /tmp/patch_xpu.py
RUN if [ "$GPU" = "xpu" ]; then \
        for f in /opt/openwakeword/openwakeword/train.py /opt/openwakeword/openwakeword/data.py \
                 /opt/piper-sample-generator/generate_samples.py; do \
            python /tmp/patch_xpu.py "$f" || exit 1; \
        done; \
    fi && rm /tmp/patch_xpu.py

COPY forge.py model_metadata.py google_tts.py forge_web.py forge_progress.py piper_voices.py config.template.yml /opt/forge/
COPY static/ /opt/forge/static/

WORKDIR /data
EXPOSE 8769
ENTRYPOINT ["python", "/opt/forge/forge.py"]
CMD ["--help"]
