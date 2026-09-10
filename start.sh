#!/bin/bash
set -Eeuo pipefail

STARTUP_PHASE="bootstrap"

startup_error() {
    local status=$?
    local command_name="${BASH_COMMAND%% *}"
    printf 'worker_startup_failed startup_phase=%s exit_code=%s line=%s command=%s\n' \
        "$STARTUP_PHASE" "$status" "${BASH_LINENO[0]:-unknown}" "$command_name" >&2
    exit "$status"
}
trap startup_error ERR

printf 'worker_entrypoint_started\n'
printf 'worker_build=%s\n' "${WORKER_BUILD_SHA:-local}"
STARTUP_PHASE="preflight"
printf 'startup_phase=%s\n' "$STARTUP_PHASE"

if [[ "${WORKER_STARTUP_SELF_TEST:-0}" == "1" ]]; then
    [[ -x /bin/bash ]]
    PYTHON_BIN="$(command -v python3)"
    [[ -x "$PYTHON_BIN" ]]
    [[ -f /handler.py ]]
    [[ -f /model_artifacts.py ]]
    COMFYUI_DIR=/workspace/runpod-slim/ComfyUI \
    LORA_DIR=/workspace/runpod-slim/ComfyUI/models/loras \
    "$PYTHON_BIN" -c "import sys; sys.path.insert(0, '/'); import boto3, handler, model_artifacts, requests, runpod, websocket"
    printf 'startup_self_test_passed\n'
    exit 0
fi

STARTUP_PHASE="volume"
cd /
[[ -d /runpod-volume ]]
MOUNTPOINT_BIN="$(command -v mountpoint)"
[[ -x "$MOUNTPOINT_BIN" ]]
"$MOUNTPOINT_BIN" -q /runpod-volume

# The network volume is the only supported workspace. Never recursively delete
# an ambiguous /workspace path: an unexpected non-empty path is a hard error.
if [[ -L /workspace ]]; then
    [[ "$(readlink -f /workspace)" == "/runpod-volume" ]]
elif [[ -e /workspace ]]; then
    [[ -d /workspace ]]
    [[ -z "$(find /workspace -mindepth 1 -maxdepth 1 -print -quit)" ]]
    rmdir /workspace
fi
[[ ! -e /workspace && ! -L /workspace ]]
ln -s /runpod-volume /workspace

# One explicit path shared with handler.py; no alternative-path probing.
export COMFYUI_DIR="/workspace/runpod-slim/ComfyUI"
export LORA_DIR="$COMFYUI_DIR/models/loras"
VENV_DIR="$COMFYUI_DIR/.venv-cu128"
[[ -f "$COMFYUI_DIR/main.py" ]]
[[ -x "$VENV_DIR/bin/python" ]]

CONFIG_PATH="$COMFYUI_DIR/custom_nodes/comfyui_tinyterranodes/config.ini"
LOCAL_TMP_CONFIG="/tmp/ttn_config.ini"

# Restore a per-worker local reference config before ComfyUI starts.
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

STARTUP_PHASE="config"
CONFIG_PATH="$CONFIG_PATH" LOCAL_TMP_CONFIG="$LOCAL_TMP_CONFIG" python3 -c '
import os
import uuid

config = os.environ["CONFIG_PATH"]
tmp_config = os.environ["LOCAL_TMP_CONFIG"]

try:
    if not os.path.islink(config):
        print("Replacing ttN config with an atomic symlink")
        tmp_link = config + "." + str(uuid.uuid4())
        os.symlink(tmp_config, tmp_link)
        os.replace(tmp_link, config)
except Exception as error:
    print("Warning during symlink swap:", error)
'

STARTUP_PHASE="python"
source "$VENV_DIR/bin/activate"
echo "Activated venv: $VENV_DIR"

cd "$COMFYUI_DIR"

# This custom node is incompatible with the worker contract and is not used.
echo "Removing problematic ComfyUI-SaveImageWithMetaData node..."
rm -rf "custom_nodes/comfyui-saveimagewithmetadata"
rm -rf "custom_nodes/ComfyUI-SaveImageWithMetaData"
rm -rf "custom_nodes/Comfyui-SaveImageWithMetaData"

STARTUP_PHASE="comfy"
FIXED_ARGS="--listen 0.0.0.0 --port 8188"
echo "Starting ComfyUI with args: $FIXED_ARGS"
python -u main.py $FIXED_ARGS > /comfyui.log 2>&1 &
COMFY_PID=$!
sleep 2
kill -0 "$COMFY_PID"

echo "=== Starting RunPod Serverless Handler ==="
STARTUP_PHASE="handler"
deactivate 2>/dev/null || true

cd /
exec python3 /handler.py
