#!/bin/bash
set -e

# В Serverless сетевые диски всегда монтируются в /runpod-volume.
# Но так как твой Python venv был создан для /workspace, мы сделаем символическую ссылку, 
# чтобы не сломать пути (shebangs) внутри venv.
rm -rf /workspace || true
ln -s /runpod-volume /workspace

# ОПРЕДЕЛЯЕМ ПЕРЕМЕННЫЕ (вы их случайно пропустили!)
COMFYUI_DIR="/workspace/runpod-slim/ComfyUI"
VENV_DIR="$COMFYUI_DIR/.venv-cu128"

CONFIG_PATH="/workspace/runpod-slim/ComfyUI/custom_nodes/comfyui_tinyterranodes/config.ini"
LOCAL_TMP_CONFIG="/tmp/ttn_config.ini"

# 1. Сначала восстанавливаем для КАЖДОГО воркера локальный эталонный конфиг в /tmp, 
# чтобы плагину сразу было с чем работать (полный дефолтный конфиг ttN)
cat <<EOF > "$LOCAL_TMP_CONFIG"
[Versions]
tinyterranodes = 2.0.9

[Option Values]
auto_update = ('true', 'false')
enable_embed_autocomplete = ('true', 'false')
enable_interface = ('true', 'false')
enable_fullscreen = ('true', 'false')
enable_dynamic_widgets = ('true', 'false')
enable_dev_nodes = ('true', 'false')

[ttNodes]
auto_update = False
enable_interface = True
enable_fullscreen = True
enable_embed_autocomplete = True
enable_dynamic_widgets = True
enable_dev_nodes = False
EOF

# 2. Атомарная (неуязвимая) подмена проблемного файла на симлинк через Python
python3 -c '
import os
import uuid

config = "'"$CONFIG_PATH"'"
tmp_config = "'"$LOCAL_TMP_CONFIG"'"

try:
    if not os.path.islink(config):
        print("🛠 Устранение конфликта ttN: Атомарная подмена конфига...")
        # Создаем временный симлинк со случайным именем (никто с ним не пересечется)
        tmp_link = config + "." + str(uuid.uuid4())
        os.symlink(tmp_config, tmp_link)
        # Атомарно перезаписываем битый файл нашим симлинком (за 1 такт процессора)
        os.replace(tmp_link, config)
except Exception as e:
    print("Warning during symlink swap:", e)
'
# Активируем venv, если он есть
if [ -d "$VENV_DIR" ]; then
    source "$VENV_DIR/bin/activate"
    echo "Activated venv: $VENV_DIR"
else
    echo "Warning: VENV_DIR not found, using system python"
fi

cd "$COMFYUI_DIR"

# Жестко удаляем проблемную ноду, так как она крашит запуск, а ты её не используешь
echo "Removing problematic ComfyUI-SaveImageWithMetaData node..."
rm -rf "custom_nodes/comfyui-saveimagewithmetadata" || true
rm -rf "custom_nodes/ComfyUI-SaveImageWithMetaData" || true
rm -rf "custom_nodes/Comfyui-SaveImageWithMetaData" || true

# Запускаем ComfyUI в фоне и пишем логи в файл (в локальный /comfyui.log - это отлично!)
FIXED_ARGS="--listen 0.0.0.0 --port 8188"
echo "Starting ComfyUI with args: $FIXED_ARGS"
python -u main.py $FIXED_ARGS > /comfyui.log 2>&1 &

echo "=== Starting RunPod Serverless Handler ==="
# Выходим из venv, чтобы запустить хендлер системным питоном (где мы установили runpod sdk)
deactivate 2>/dev/null || true

cd /
python3 /handler.py
