FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir \
    httpx \
    "websockets==12.0" \
    zeroconf \
    numpy \
    onnxruntime-gpu \
    openwakeword
COPY em_controller.py .
CMD ["python", "-u", "em_controller.py"]
