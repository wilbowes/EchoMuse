"""Point upstream's CUDA-only device code at Intel's XPU, for the GPU=xpu image
only. openWakeWord and piper-sample-generator test torch.cuda and call
.cuda(); an Intel GPU is torch.xpu, so without this both run on the CPU
beside an idle GPU. AMD needs nothing: ROCm torch answers as torch.cuda.

Left alone on purpose: train.py's feature extraction asks for ONNX Runtime's
CUDA provider when torch.cuda is available. The image's onnxruntime is the
CPU build, so on XPU it takes the CPU branch, with more threads.

Each edit must match exactly as often as the pinned source has it, or the
build fails: an upstream move should stop the build, not leave half the work
on the GPU. Usage: patch_xpu.py <file> (train.py, data.py, generate_samples.py).
"""
import sys
from pathlib import Path

CUDA_DEVICE = "torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')"
XPU_DEVICE = "torch.device('xpu' if torch.xpu.is_available() else 'cpu')"

EDITS = {
    "train.py": [
        (CUDA_DEVICE, XPU_DEVICE, 1),
        ("torch.cuda.empty_cache()", "torch.xpu.empty_cache()", 4),
    ],
    "data.py": [
        (CUDA_DEVICE, XPU_DEVICE, 1),
    ],
    "generate_samples.py": [
        ("if torch.cuda.is_available():", "if torch.xpu.is_available():", 2),
        ("model.cuda()", "model.to('xpu')", 1),
        ("except torch.cuda.OutOfMemoryError:", "except torch.OutOfMemoryError:", 1),
        ("torch.cuda.empty_cache()", "torch.xpu.empty_cache()", 1),
        ("speaker_1.cuda()", "speaker_1.to('xpu')", 1),
        ("speaker_2.cuda()", "speaker_2.to('xpu')", 1),
        ("x = x.cuda()", "x = x.to('xpu')", 1),
        ("x_lengths.cuda()", "x_lengths.to('xpu')", 1),
    ],
}


def patch(name: str, src: str) -> str:
    for old, new, count in EDITS[name]:
        found = src.count(old)
        if found != count:
            sys.exit(f"patch_xpu: {name}: expected {count} of {old!r}, found {found}")
        src = src.replace(old, new)
    for left in ("torch.cuda.empty_cache", ".cuda()"):
        if left in src:
            sys.exit(f"patch_xpu: {name}: {left!r} still present")
    return src


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_bytes(patch(path.name, path.read_bytes().decode()).encode())
