FROM python:3.12-slim

WORKDIR /app

# Install dependencies first — layer cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install openwakeword without deps — tflite-runtime has no Python 3.12
# x86_64 build; onnxruntime handles inference instead
RUN pip install --no-cache-dir --no-deps openwakeword==0.6.0

# Controller source
COPY em_controller.py .
COPY em_api.py .
COPY em_db.py .
COPY em_auth.py .

# Dashboard SPA
COPY static/ ./static/

# Database persisted via volume mount
# docker run -v ~/EchoMuse/controller/data:/app/data ...
# Set DB_PATH=/app/data/echomuse.db in environment

CMD ["python", "-u", "em_controller.py"]
