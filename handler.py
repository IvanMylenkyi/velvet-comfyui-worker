"""
RunPod Serverless Handler для ComfyUI.
Получает workflow JSON → запускает в ComfyUI → возвращает изображения.
Поддерживает streaming прогресса.
"""

import runpod
import json
import uuid
import time
import base64
import urllib.parse
import requests
import websocket as ws_lib

COMFY_URL = "http://127.0.0.1:8188"
MAX_WAIT = 300  # максимум 5 минут на генерацию


def wait_for_comfyui(timeout=600):
    """Ждём пока ComfyUI полностью запустится."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{COMFY_URL}/system_stats", timeout=2)
            if r.status_code == 200:
                print("[Handler] ComfyUI is ready!")
                return True
        except Exception:
            pass
        time.sleep(1)
        
    print("[Handler] Timeout reached. Dumping /comfyui.log to see why it failed:")
    try:
        with open("/comfyui.log", "r") as f:
            print("--- COMFYUI LOG START ---")
            print(f.read())
            print("--- COMFYUI LOG END ---")
    except Exception as e:
        print(f"Could not read /comfyui.log: {e}")
        
    raise RuntimeError(f"ComfyUI did not start within {timeout}s")


def get_images_from_history(prompt_id):
    """Получить все изображения из history после завершения генерации."""
    history = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10).json()
    images = []

    if prompt_id not in history:
        return images

    outputs = history[prompt_id].get("outputs", {})
    for node_id, output in outputs.items():
        for img in output.get("images", []):
            params = urllib.parse.urlencode({
                "filename": img["filename"],
                "type": img.get("type", "output"),
                "subfolder": img.get("subfolder", ""),
            })
            img_data = requests.get(
                f"{COMFY_URL}/view?{params}", timeout=30
            ).content
            images.append({
                "base64": base64.b64encode(img_data).decode("utf-8"),
                "filename": img["filename"],
            })

    return images


def handler(job):
    """
    Основной обработчик. Поддерживает действия:
    - generate: генерация изображений по workflow (default)
    - object_info: получить список нод/LoRA/моделей
    """
    job_input = job["input"]
    action = job_input.get("action", "generate")

    # --- Получить object_info (список LoRA и т.д.) ---
    if action == "object_info":
        wait_for_comfyui()
        resp = requests.get(f"{COMFY_URL}/object_info", timeout=30)
        return {"object_info": resp.json()}

    # --- Генерация ---
    workflow = job_input.get("workflow")
    if not workflow:
        return {"error": "No workflow provided"}

    wait_for_comfyui()

    client_id = str(uuid.uuid4())

    # 1. Подключиться к WS ComfyUI для отслеживания прогресса
    sock = ws_lib.WebSocket()
    sock.settimeout(MAX_WAIT)
    try:
        sock.connect(f"ws://127.0.0.1:8188/ws?clientId={client_id}")
    except Exception as e:
        return {"error": f"Failed to connect to ComfyUI WebSocket: {str(e)}"}

    # 2. Отправить workflow
    try:
        resp = requests.post(
            f"{COMFY_URL}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=10,
        )
    except Exception as e:
        sock.close()
        return {"error": f"Failed to submit workflow: {str(e)}"}

    if resp.status_code != 200:
        sock.close()
        return {"error": f"ComfyUI rejected workflow: {resp.text}"}

    prompt_id = resp.json().get("prompt_id")
    print(f"[Handler] Workflow submitted, prompt_id={prompt_id}")

    # 3. Слушать прогресс и стримить обновления
    try:
        while True:
            raw = sock.recv()
            if not isinstance(raw, str):
                continue

            msg = json.loads(raw)
            msg_type = msg.get("type")
            data = msg.get("data", {})

            if msg_type == "progress":
                # Стримим прогресс клиенту через RunPod streaming API
                yield {
                    "status": "progress",
                    "value": data.get("value", 0),
                    "max": data.get("max", 0),
                    "node": data.get("node", ""),
                    "prompt_id": prompt_id,
                }

            elif msg_type == "execution_cached":
                # Часть workflow закеширована
                yield {
                    "status": "cached",
                    "nodes": data.get("nodes", []),
                    "prompt_id": prompt_id,
                }

            elif msg_type == "executing":
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    # Генерация завершена
                    print(f"[Handler] Generation complete for {prompt_id}")
                    break
                else:
                    # Уведомляем о том, какая нода сейчас работает
                    yield {
                        "status": "executing",
                        "node": data.get("node"),
                        "prompt_id": prompt_id
                    }

            elif msg_type == "executed":
                # Перехватываем промежуточные результаты от каждой ноды
                yield {
                    "status": "executed",
                    "node": data.get("node"),
                    "output": data.get("output"),
                    "prompt_id": prompt_id
                }

            elif msg_type == "execution_error":
                sock.close()
                return {
                    "status": "error",
                    "error": f"ComfyUI execution error: {json.dumps(data)}",
                    "prompt_id": prompt_id,
                }

    except ws_lib.WebSocketTimeoutException:
        sock.close()
        return {
            "status": "error",
            "error": "Generation timeout — exceeded maximum wait time",
            "prompt_id": prompt_id,
        }
    except Exception as e:
        sock.close()
        return {
            "status": "error",
            "error": f"WebSocket error: {str(e)}",
            "prompt_id": prompt_id,
        }

    sock.close()

    # 4. Забрать результат — изображения в base64
    images = get_images_from_history(prompt_id)
    print(f"[Handler] Got {len(images)} images for {prompt_id}")

    return {
        "status": "completed",
        "prompt_id": prompt_id,
        "images": images,
    }


# Запуск serverless worker с поддержкой streaming
runpod.serverless.start({
    "handler": handler,
    "return_aggregate_stream": True,
})
