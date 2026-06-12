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
MAX_WAIT = 1200  # максимум 20 минут на генерацию


def wait_for_comfyui(timeout=1200):
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
    images = []
    try:
        history = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=10).json()

        if prompt_id not in history:
            return images

        outputs = history[prompt_id].get("outputs", {})
        for node_id, output in outputs.items():
            if not isinstance(output, dict):
                continue
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
    except Exception as e:
        import traceback
        print(f"[Handler] Error in get_images_from_history: {e}")
        traceback.print_exc()

    return images


def upload_to_s3(images, s3_config):
    try:
        import boto3
        import io

        endpoint = s3_config.get("endpoint")
        bucket = s3_config.get("bucket")
        access_key = s3_config.get("accessKey")
        secret_key = s3_config.get("secretKey")
        path = s3_config.get("path")

        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="fra1"
        )

        uploaded_keys = []
        for img in images:
            img_data = base64.b64decode(img["base64"])
            # Используем имя файла от ComfyUI, чтобы избежать перезаписи
            filename = img.get("filename", f"{uuid.uuid4().hex}.png")
            key = f"{path}/{filename}"
            
            s3.upload_fileobj(io.BytesIO(img_data), bucket, key, ExtraArgs={"ContentType": "image/png", "ACL": "public-read"})
            uploaded_keys.append(key)
            
        return uploaded_keys
    except Exception as e:
        print(f"[Handler] Failed to upload to S3: {e}")
        return []

def handler(job):
    """
    Основной обработчик. Поддерживает действия:
    - generate: генерация изображений по workflow (default)
    - object_info: получить список нод/LoRA/моделей
    """
    job_input = job["input"]
    action = job_input.get("action", "generate")
    s3_config = job_input.get("s3Config")

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

    # Добавляем переменные для хранения истории загрузок за сессию
    session_s3_keys = []
    session_images = []

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
                node_output = data.get("output", {})
                node_id = data.get("node")

                # --- НОВЫЙ БЛОК: СТРИМИНГ КАРТИНОК ---
                # Если нода вернула картинки (например, Save Image)
                if "images" in node_output:
                    node_images = []
                    for img_info in node_output["images"]:
                        params = urllib.parse.urlencode({
                            "filename": img_info["filename"],
                            "type": img_info.get("type", "output"),
                            "subfolder": img_info.get("subfolder", ""),
                        })
                        try:
                            # Скачиваем сгенерированную картинку
                            img_data = requests.get(f"{COMFY_URL}/view?{params}", timeout=30).content
                            node_images.append({
                                "base64": base64.b64encode(img_data).decode("utf-8"),
                                "filename": img_info["filename"]
                            })
                        except Exception as e:
                            print(f"[Handler] Error downloading image from node {node_id}: {e}")

                    # Сразу грузим в S3
                    step_s3_keys = []
                    if s3_config and node_images:
                        step_s3_keys = upload_to_s3(node_images, s3_config)
                        session_s3_keys.extend(step_s3_keys)
                    elif node_images:
                        session_images.extend(node_images)

                    # Отправляем клиенту стрим с готовой картинкой!
                    yield {
                        "status": "image_ready", # Новый статус для фронтенда
                        "node": node_id,
                        "prompt_id": prompt_id,
                        "s3_keys": step_s3_keys,
                        # Очищаем base64, если загрузили в S3, чтобы не рвать соединение тяжелым пейлоадом
                        "images": [] if step_s3_keys else node_images 
                    }
                # --- КОНЕЦ НОВОГО БЛОКА ---

                # Отправляем стандартное сообщение о завершении ноды
                yield {
                    "status": "executed",
                    "node": node_id,
                    "output": node_output,
                    "prompt_id": prompt_id
                }

            elif msg_type == "execution_error":
                sock.close()
                yield {
                    "status": "error",
                    "error": f"ComfyUI execution error: {json.dumps(data)}",
                    "prompt_id": prompt_id,
                }
                return

    except ws_lib.WebSocketTimeoutException:
        sock.close()
        yield {
            "status": "error",
            "error": "Generation timeout — exceeded maximum wait time",
            "prompt_id": prompt_id,
        }
        return
    except Exception as e:
        sock.close()
        yield {
            "status": "error",
            "error": f"WebSocket error: {str(e)}",
            "prompt_id": prompt_id,
        }
        return

    sock.close()
    yield {
        "status": "completed",
        "prompt_id": prompt_id,
        "images": session_images,  # <--- Вот здесь происходит обращение к переменной
        "s3_keys": session_s3_keys
    }
    # 4. Забрать результат — изображения в base64
    import sys
    images = get_images_from_history(prompt_id)
    print(f"[Handler] Got {len(images)} images for {prompt_id}")
    sys.stdout.flush()

    # Загрузить в S3 напрямую, если есть конфиг
    s3_keys = []
    if s3_config and images:
        print(f"[Handler] Uploading {len(images)} images to S3...")
        sys.stdout.flush()
        s3_keys = upload_to_s3(images, s3_config)
        print(f"[Handler] Uploaded to S3 keys: {s3_keys}")
        sys.stdout.flush()
        if s3_keys:
            # Очищаем images только если загрузка успешна, чтобы не превышать лимит Stream payload!
            images = []

    yield {
        "status": "completed",
        "prompt_id": prompt_id,
        "images": images,
        "s3_keys": s3_keys
    }


# Запуск serverless worker с поддержкой streaming
runpod.serverless.start({
    "handler": handler,
    "return_aggregate_stream": True,
})
