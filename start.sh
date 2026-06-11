#!/bin/bash
set -e

# Путь к ComfyUI на вашем Network Volume
COMFYUI_DIR="/workspace/runpod-slim/ComfyUI"
VENV_DIR="$COMFYUI_DIR/.venv-cu128"

echo "=== Starting ComfyUI in Background ==="

# Активируем venv, если он есть
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
    echo "Activated venv: $VENV_DIR"
else
    echo "Warning: VENV_DIR not found, using system python"
fi

cd "$COMFYUI_DIR"
# Запускаем ComfyUI в фоне
FIXED_ARGS="--listen 0.0.0.0 --port 8188"
echo "Starting ComfyUI with args: $FIXED_ARGS"
python main.py $FIXED_ARGS &

echo "=== Starting RunPod Serverless Handler ==="
# Выходим из venv, чтобы запустить хендлер системным питоном (где мы установили runpod sdk)
deactivate 2>/dev/null || true

cd /
python3 /handler.py
