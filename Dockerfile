# Pin the mutable RunPod tag to the digest resolved from Docker Hub.
# Base-image updates must be promoted in a separate canary change.
ARG COMFYUI_BASE_DIGEST=sha256:2cb4015beb6e16b0bbc05ed5d1e39288545b7f4ce8fed9534f4ef0fa88aa2e4d
FROM runpod/comfyui:cuda12.8@${COMFYUI_BASE_DIGEST}

# Install dependencies with the exact runtime used by the handler.
RUN python3 -m pip install --no-cache-dir runpod websocket-client requests boto3

COPY handler.py /handler.py
COPY model_artifacts.py /model_artifacts.py
COPY start.sh /start.sh

RUN chmod +x /start.sh

# Fail the build before publication if the startup contract or runtime imports
# are broken. The handler is import-safe and does not start RunPod here.
RUN test -x /bin/bash \
    && test -x "$(command -v python3)" \
    && test -f /start.sh \
    && python3 -c "from pathlib import Path; data = Path('/start.sh').read_bytes(); assert data.startswith(b'#!/bin/bash\\n'), 'start.sh must begin with LF shebang'; assert b'\\r' not in data, 'start.sh contains CRLF'; assert not data.startswith(b'\\xef\\xbb\\xbf'), 'start.sh contains BOM'" \
    && /bin/bash -n /start.sh
RUN python3 -c "import sys; sys.path.insert(0, '/'); import model_artifacts"
RUN COMFYUI_DIR=/workspace/runpod-slim/ComfyUI \
    LORA_DIR=/workspace/runpod-slim/ComfyUI/models/loras \
    python3 -c "import sys; sys.path.insert(0, '/'); import boto3, handler, model_artifacts, requests, runpod, websocket"

ARG WORKER_BUILD_SHA=local
ENV WORKER_BUILD_SHA=${WORKER_BUILD_SHA}
LABEL org.opencontainers.image.revision=${WORKER_BUILD_SHA}

# Execute the no-GPU filesystem/interpreter/import self-test in CI.
RUN WORKER_STARTUP_SELF_TEST=1 /bin/bash /start.sh

# One unambiguous startup contract. RunPod must not add a second start command.
ENTRYPOINT ["/bin/bash", "/start.sh"]
CMD []
