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
import re
import requests
import websocket as ws_lib
from model_artifacts import (
    cleanup_model_artifacts,
    prepare_model_artifacts,
    verify_model_artifacts_visible,
)

COMFY_URL = "http://127.0.0.1:8188"
MAX_WAIT = 1200  # максимум 20 минут на генерацию
WORKER_BUILD_SHA = os.environ.get("WORKER_BUILD_SHA", "local")
COMFYUI_DIR = os.environ.get("COMFYUI_DIR")
LORA_DIR = os.environ.get("LORA_DIR")
if not COMFYUI_DIR or not LORA_DIR:
    raise RuntimeError("COMFYUI_DIR and LORA_DIR must be provided by start.sh")


def _safe_error_text(error):
    """Keep query-string capabilities and credentials out of worker logs."""
    text = str(error)
    text = re.sub(r"(?i)(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", text)
    text = re.sub(
        r"(?i)([?&](?:token|sig|signature|key|secret|access_token)=[^&\s]+)",
        "?<redacted>",
        text,
    )
    return text


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
        print(f"Could not read /comfyui.log: {_safe_error_text(e)}")

    raise RuntimeError(f"ComfyUI did not start within {timeout}s")


def _image_descriptor_key(image):
    """Stable identity for one Comfy output image within a prompt."""
    node_id = image.get("nodeId") or image.get("node_id") or ""
    object_index = image.get("objectIndex")
    if not isinstance(object_index, int):
        object_index = -1
    return str(node_id), object_index, str(image.get("filename") or "")


def get_images_from_history(prompt_id, exclude_descriptors=None, exclude_filenames=None):
    """Получить все изображения из history после завершения генерации.

    ``exclude_filenames`` is retained for old callers, while new callers use
    the full output descriptor so equal filenames from different nodes do not
    accidentally deduplicate one another.
    """
    if exclude_descriptors is None:
        exclude_descriptors = set()
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
            for object_index, img in enumerate(output.get("images", [])):
                if not isinstance(img, dict) or not img.get("filename"):
                    continue
                descriptor = {
                    "nodeId": str(node_id) if node_id is not None else None,
                    "objectIndex": object_index,
                    "filename": img["filename"],
                }
                if _image_descriptor_key(descriptor) in exclude_descriptors:
                    continue
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
                        "nodeId": str(node_id) if node_id is not None else None,
                        "objectIndex": object_index,
                    })
                else:
                    print(f"[Handler] Failed to get image {img['filename']}: HTTP {response.status_code}")
    except Exception as e:
        print(f"[Handler] Error in get_images_from_history: {_safe_error_text(e)}")

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
            uploaded_keys.append({
                "storageKey": key,
                "filename": filename,
                "nodeId": img.get("nodeId") or img.get("node_id"),
                "objectIndex": img.get("objectIndex") if isinstance(img.get("objectIndex"), int) else None,
            })
            
        return uploaded_keys
    except Exception as e:
        print(f"[Handler] Failed to upload to S3: {_safe_error_text(e)}")
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
        print(f"[Handler] Failed to upload {filepath} to S3: {_safe_error_text(e)}")
        return None

def _run_job(job, prepared_artifact_paths=None):
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
            yield yield_error(f"Failed to get object_info: {_safe_error_text(e)}")
            return

    # --- Генерация ---
    workflow = job_input.get("workflow")
    if not workflow:
        yield yield_error("No workflow provided")
        return

    try:
        wait_for_comfyui()
    except Exception as e:
        yield yield_error(f"ComfyUI failed to start: {_safe_error_text(e)}")
        return

    try:
        verify_model_artifacts_visible(
            job_input.get("model_artifacts", []),
            prepared_artifact_paths or [],
            requests.get,
            f"{COMFY_URL}/object_info",
        )
    except Exception as e:
        yield yield_error(f"ComfyUI model-artifact preflight failed: {_safe_error_text(e)}")
        return


    # --- Подготовка input_images (если есть) ---
    input_images = job_input.get("input_images", {})
    if input_images:
        try:
            input_dir = os.path.join(COMFYUI_DIR, "input")
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
            yield yield_error(f"Error saving input images: {_safe_error_text(e)}")
            return

    client_id = str(uuid.uuid4())

    # 1. Подключиться к WS ComfyUI
    sock = ws_lib.WebSocket()
    sock.settimeout(MAX_WAIT)
    try:
        sock.connect(f"ws://127.0.0.1:8188/ws?clientId={client_id}")
    except Exception as e:
        yield yield_error(f"Failed to connect to ComfyUI WebSocket: {_safe_error_text(e)}")
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
        yield yield_error(f"Failed to submit workflow: {_safe_error_text(e)}")
        return

    if resp.status_code != 200:
        sock.close()
        yield yield_error(f"ComfyUI rejected workflow: {resp.text}")
        return

    prompt_id = resp.json().get("prompt_id")
    print(f"[Handler] Workflow submitted, prompt_id={prompt_id}")

    session_images = []
    session_s3_keys = []
    session_descriptor_keys = set()

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
                    for object_index, img_info in enumerate(node_output["images"]):
                        if not isinstance(img_info, dict) or not img_info.get("filename"):
                            continue
                        descriptor = {
                            "nodeId": str(node_id) if node_id is not None else None,
                            "objectIndex": object_index,
                            "filename": img_info["filename"],
                        }
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
                                    "filename": img_info["filename"],
                                    "nodeId": str(node_id) if node_id is not None else None,
                                    "objectIndex": object_index,
                                })
                            else:
                                print(f"[Handler] Failed to stream image {img_info['filename']}: HTTP {response.status_code}")
                        except Exception as e:
                            print(f"[Handler] Error downloading image from node {node_id}: {_safe_error_text(e)}")

                    # A worker can observe the same executed event more than
                    # once during reconnect/replay. Keep one descriptor per
                    # node/object in the aggregate result.
                    fresh_node_images = []
                    for image in node_images:
                        descriptor_key = _image_descriptor_key(image)
                        if descriptor_key not in session_descriptor_keys:
                            session_descriptor_keys.add(descriptor_key)
                            fresh_node_images.append(image)
                    node_images = fresh_node_images

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
        yield yield_error(f"WebSocket error: {_safe_error_text(e)}", prompt_id)
        return

    sock.close()

    try:
        import sys
        images = get_images_from_history(prompt_id, exclude_descriptors=session_descriptor_keys)
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
        pass


def handler(job):
    job_input = job.get("input", {}) if isinstance(job, dict) else {}
    model_artifacts = job_input.get("model_artifacts", [])
    model_artifacts_count = len(model_artifacts) if isinstance(model_artifacts, list) else 0
    print(f"worker_build={WORKER_BUILD_SHA}")
    print(f"model_artifacts_count={model_artifacts_count}")
    print(f"lora_directory={LORA_DIR}")
    if job_input.get("action", "generate") != "generate":
        yield from _run_job(job)
        return
    loras_dir = LORA_DIR
    prepared = []
    try:
        prepared = prepare_model_artifacts(
            model_artifacts,
            loras_dir,
        )
        for path in prepared:
            print(f"prepared_artifact={os.path.basename(path)}")
        yield from _run_job(job, prepared)
    except Exception as error:
        yield {
            "status": "error",
            "comfy_error": f"Failed to prepare verified model artifacts: {_safe_error_text(error)}",
            "log_s3_key": None,
        }
    finally:
        cleanup_model_artifacts(prepared, loras_dir)

if __name__ == "__main__":
    runpod.serverless.start({
        "handler": handler,
        "return_aggregate_stream": True,
    })
