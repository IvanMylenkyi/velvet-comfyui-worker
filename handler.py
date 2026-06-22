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
import os
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


def get_images_from_history(prompt_id, exclude_filenames=None):
    """Получить все изображения из history после завершения генерации."""
    if exclude_filenames is None:
        exclude_filenames = set()
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
                if img["filename"] in exclude_filenames:
                    continue
                params = urllib.parse.urlencode({
                    "filename": img["filename"],
                    "type": img.get("type", "output"),
                    "subfolder": img.get("subfolder", ""),
                })
                response = requests.get(
                    f"{COMFY_URL}/view?{params}", timeout=30
                )
                if response.status_code == 200:
                    images.append({
                        "base64": base64.b64encode(response.content).decode("utf-8"),
                        "filename": img["filename"],
                    })
                else:
                    print(f"[Handler] Failed to get image {img['filename']}: HTTP {response.status_code}")
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
        path = s3_config.get("path", "").strip("/")

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
            filename = img.get("filename", f"{uuid.uuid4().hex}.png")
            key = f"{path}/{filename}" if path else filename
            
            s3.upload_fileobj(io.BytesIO(img_data), bucket, key, ExtraArgs={"ContentType": "image/png", "ACL": "public-read"})
            uploaded_keys.append(key)
            
        return uploaded_keys
    except Exception as e:
        print(f"[Handler] Failed to upload to S3: {e}")
        return []

def upload_file_to_s3(filepath, s3_config, content_type="text/plain"):
    try:
        if not os.path.exists(filepath):
            return None
        import boto3
        endpoint = s3_config.get("endpoint")
        bucket = s3_config.get("bucket")
        access_key = s3_config.get("accessKey")
        secret_key = s3_config.get("secretKey")
        path = s3_config.get("path", "").strip("/")
        
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="fra1"
        )
        
        filename = f"{uuid.uuid4().hex}_worker_log.txt"
        key = f"{path}/{filename}" if path else filename
        
        with open(filepath, "rb") as f:
            s3.upload_fileobj(f, bucket, key, ExtraArgs={"ContentType": content_type, "ACL": "public-read"})
        return key
    except Exception as e:
        print(f"[Handler] Failed to upload {filepath} to S3: {e}")
        return None

def handler(job):
    """
    Основной обработчик. Поддерживает действия:
    - generate: генерация изображений по workflow (default)
    - object_info: получить список нод/LoRA/моделей
    """
    job_input = job.get("input", {})
    action = job_input.get("action", "generate")
    s3_config = job_input.get("s3Config")

    # Вспомогательная функция: гарантирует загрузку лога при любой ошибке (и обходит фильтр RunPod)
    def yield_error(error_msg, prompt_id=None):
        log_key = upload_file_to_s3("/comfyui.log", s3_config) if s3_config else None
        res = {
            "status": "error", 
            "comfy_error": error_msg, # Убегаем от обрезания RunPod
            "log_s3_key": log_key
        }
        if prompt_id:
            res["prompt_id"] = prompt_id
        return res

    # --- Получить object_info (список LoRA и т.д.) ---
    if action == "object_info":
        try:
            wait_for_comfyui()
            resp = requests.get(f"{COMFY_URL}/object_info", timeout=30)
            yield {"object_info": resp.json()}
            return
        except Exception as e:
            yield yield_error(f"Failed to get object_info: {str(e)}")
            return

    # --- Генерация ---
    workflow = job_input.get("workflow")
    if not workflow:
        yield yield_error("No workflow provided")
        return

    try:
        wait_for_comfyui()
    except Exception as e:
        yield yield_error(f"ComfyUI failed to start: {str(e)}")
        return

    # --- Подготовка Custom LoRAs ---
    custom_loras = job_input.get("custom_loras", [])
    downloaded_loras = []
    if custom_loras:
        try:
            possible_dirs = [
                "/runpod-volume/runpod-slim/ComfyUI/models/loras",
                "/workspace/runpod-slim/ComfyUI/models/loras",
                "/workspace/ComfyUI/models/loras"
            ]
            loras_dir = possible_dirs[0]
            for d in possible_dirs:
                if os.path.exists(os.path.dirname(d)):
                    loras_dir = d
                    break
                    
            os.makedirs(loras_dir, exist_ok=True)
            
            for lora in custom_loras:
                name = lora.get("name")
                url = lora.get("url")
                if name and url:
                    filepath = os.path.join(loras_dir, name)
                    if not os.path.exists(filepath):
                        print(f"[Handler] Downloading Custom LoRA {name} from {url}...")
                        r = requests.get(url, stream=True, timeout=60)
                        r.raise_for_status()
                        with open(filepath, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                    downloaded_loras.append(filepath)
        except Exception as e:
            yield yield_error(f"Failed to process custom LoRAs: {str(e)}")
            return

    # --- Подготовка input_images (если есть) ---
    input_images = job_input.get("input_images", {})
    if input_images:
        try:
            input_dir = "/workspace/runpod-slim/ComfyUI/input"
            os.makedirs(input_dir, exist_ok=True)
            for filename, b64_str in input_images.items():
                if "," in b64_str:
                    b64_str = b64_str.split(",", 1)[1]
                img_data = base64.b64decode(b64_str)
                filepath = os.path.join(input_dir, filename)
                with open(filepath, "wb") as f:
                    f.write(img_data)
                print(f"[Handler] Saved input image: {filename}")
        except Exception as e:
            yield yield_error(f"Error saving input images: {str(e)}")
            return

    client_id = str(uuid.uuid4())

    # 1. Подключиться к WS ComfyUI
    sock = ws_lib.WebSocket()
    sock.settimeout(MAX_WAIT)
    try:
        sock.connect(f"ws://127.0.0.1:8188/ws?clientId={client_id}")
    except Exception as e:
        yield yield_error(f"Failed to connect to ComfyUI WebSocket: {str(e)}")
        return

    # 2. Отправить workflow
    try:
        resp = requests.post(
            f"{COMFY_URL}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=10,
        )
    except Exception as e:
        sock.close()
        yield yield_error(f"Failed to submit workflow: {str(e)}")
        return

    if resp.status_code != 200:
        sock.close()
        yield yield_error(f"ComfyUI rejected workflow: {resp.text}")
        return

    prompt_id = resp.json().get("prompt_id")
    print(f"[Handler] Workflow submitted, prompt_id={prompt_id}")

    session_images = []
    session_s3_keys = []
    uploaded_filenames = set()

    # 3. Слушать прогресс и стримить обновления
    try:
        while True:
            raw = sock.recv()
            if not isinstance(raw, str):
                continue

            msg = json.loads(raw)
            msg_type = msg.get("type")
            data = msg.get("data") or {}

            if msg_type == "progress":
                yield {
                    "status": "progress",
                    "value": data.get("value", 0),
                    "max": data.get("max", 0),
                    "node": data.get("node", ""),
                    "prompt_id": prompt_id,
                }

            elif msg_type == "execution_cached":
                yield {
                    "status": "cached",
                    "nodes": data.get("nodes", []),
                    "prompt_id": prompt_id,
                }

            elif msg_type == "executing":
                if data.get("node") is None and data.get("prompt_id") == prompt_id:
                    print(f"[Handler] Generation complete for {prompt_id}")
                    break
                else:
                    yield {
                        "status": "executing",
                        "node": data.get("node"),
                        "prompt_id": prompt_id
                    }

            elif msg_type == "executed":
                node_output = data.get("output") or {}
                node_id = data.get("node")

                if isinstance(node_output, dict) and "images" in node_output:
                    node_images = []
                    for img_info in node_output["images"]:
                        uploaded_filenames.add(img_info["filename"])
                        params = urllib.parse.urlencode({
                            "filename": img_info["filename"],
                            "type": img_info.get("type", "output"),
                            "subfolder": img_info.get("subfolder", ""),
                        })
                        try:
                            response = requests.get(f"{COMFY_URL}/view?{params}", timeout=30)
                            if response.status_code == 200:
                                node_images.append({
                                    "base64": base64.b64encode(response.content).decode("utf-8"),
                                    "filename": img_info["filename"]
                                })
                            else:
                                print(f"[Handler] Failed to stream image {img_info['filename']}: HTTP {response.status_code}")
                        except Exception as e:
                            print(f"[Handler] Error downloading image from node {node_id}: {e}")

                    step_s3_keys = []
                    if s3_config and node_images:
                        step_s3_keys = upload_to_s3(node_images, s3_config)
                        if step_s3_keys:
                            session_s3_keys.extend(step_s3_keys)
                        else:
                            session_images.extend(node_images)
                    elif node_images:
                        session_images.extend(node_images)

                    yield {
                        "status": "image_ready",
                        "node": node_id,
                        "prompt_id": prompt_id,
                        "s3_keys": step_s3_keys,
                        "images": [] if step_s3_keys else node_images 
                    }

                yield {
                    "status": "executed",
                    "node": node_id,
                    "output": node_output,
                    "prompt_id": prompt_id
                }

            elif msg_type == "execution_error":
                sock.close()
                yield yield_error(f"ComfyUI execution error: {json.dumps(data)}", prompt_id)
                return

    except ws_lib.WebSocketTimeoutException:
        sock.close()
        yield yield_error("Generation timeout — exceeded maximum wait time", prompt_id)
        return
    except Exception as e:
        sock.close()
        yield yield_error(f"WebSocket error: {str(e)}", prompt_id)
        return

    sock.close()

    try:
        import sys
        images = get_images_from_history(prompt_id, exclude_filenames=uploaded_filenames)
        print(f"[Handler] Got {len(images)} cached/unstreamed images for {prompt_id}")
        sys.stdout.flush()

        s3_keys = []
        if s3_config and images:
            print(f"[Handler] Uploading {len(images)} cached images to S3...")
            sys.stdout.flush()
            s3_keys = upload_to_s3(images, s3_config)
            print(f"[Handler] Uploaded to S3 keys: {s3_keys}")
            sys.stdout.flush()
            if s3_keys:
                images = []

        final_images = session_images + images
        final_s3_keys = session_s3_keys + s3_keys

        # --- ЗАЩИТА ОТ ТИХИХ КРАШЕЙ ---
        if not final_images and not final_s3_keys:
            yield yield_error("Workflow finished, but no images were generated! Silent node crash detected.", prompt_id)
            return

        log_key = upload_file_to_s3("/comfyui.log", s3_config) if s3_config else None
        yield {
            "status": "completed",
            "prompt_id": prompt_id,
            "images": final_images,
            "s3_keys": final_s3_keys,
            "log_s3_key": log_key
        }
    finally:
        for filepath in downloaded_loras:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    print(f"[Handler] Deleted custom LoRA {filepath} for isolation.")
            except Exception as e:
                print(f"[Handler] Error deleting LoRA {filepath}: {e}")

runpod.serverless.start({
    "handler": handler,
    "return_aggregate_stream": True,
})
