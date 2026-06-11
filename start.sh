#!/bin/bash
set -e

# В Serverless сетевые диски всегда монтируются в /runpod-volume.
# Но так как твой Python venv был создан для /workspace, мы сделаем символическую ссылку, 
# чтобы не сломать пути (shebangs) внутри venv.
rm -rf /workspace || true
ln -s /runpod-volume /workspace

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

# Фикс: при копировании файлов с Windows папка могла поменять регистр (стать маленькими буквами), 
# из-за чего Python падает в Linux (где регистр важен). Возвращаем правильное имя:
if [ -d "custom_nodes/comfyui-saveimagewithmetadata" ]; then
    echo "Fixing lowercase directory name for ComfyUI-SaveImageWithMetaData..."
    mv custom_nodes/comfyui-saveimagewithmetadata custom_nodes/ComfyUI-SaveImageWithMetaData || true
fi

# Запускаем ComfyUI в фоне и пишем логи в файл
FIXED_ARGS="--listen 0.0.0.0 --port 8188"
echo "Starting ComfyUI with args: $FIXED_ARGS"
python -u main.py $FIXED_ARGS > /comfyui.log 2>&1 &

echo "=== Starting RunPod Serverless Handler ==="
# Выходим из venv, чтобы запустить хендлер системным питоном (где мы установили runpod sdk)
deactivate 2>/dev/null || true

cd /
python3 /handler.py
