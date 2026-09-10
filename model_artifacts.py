"""Fail-closed preparation of immutable model artifacts for ComfyUI jobs."""

import hashlib
import json
import os
import re
from urllib.parse import urlsplit

import requests

SCHEMA_VERSION = "model-artifact-generation-v1"
MAX_ARTIFACTS = 12
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_BYTES = 4 * 1024 * 1024
ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FILENAME = re.compile(r"^[A-Za-z0-9_-]{8,128}-[0-9a-f]{16}\.safetensors$")


def prepare_model_artifacts(envelopes, target_dir, http_get=None):
    if not isinstance(envelopes, list) or len(envelopes) > MAX_ARTIFACTS:
        raise ValueError("model artifact envelope count is invalid")
    if not envelopes:
        return []
    validated = [_validate_envelope(item) for item in envelopes]
    identities = [item["artifactId"] for item in validated]
    filenames = [item["filename"] for item in validated]
    if len(set(identities)) != len(identities) or len(set(filenames)) != len(filenames):
        raise ValueError("duplicate model artifact envelope")
    fetch = http_get or requests.get

    os.makedirs(target_dir, exist_ok=True)
    target_root = os.path.realpath(target_dir)
    prepared = []
    try:
        for envelope in validated:
            final_path = os.path.realpath(os.path.join(target_root, envelope["filename"]))
            if os.path.dirname(final_path) != target_root:
                raise ValueError("model artifact filename escapes managed directory")
            partial_path = final_path + ".part"
            response = None
            try:
                response = fetch(
                    envelope["downloadUrl"],
                    stream=True,
                    allow_redirects=False,
                    timeout=(10, 60),
                )
                if getattr(response, "status_code", None) != 200:
                    raise ValueError(
                        "model artifact capability response is invalid "
                        f"(HTTP {getattr(response, 'status_code', 'unknown')})"
                    )
                expected_size = int(envelope["byteSize"])
                content_length = _content_length(getattr(response, "headers", {}))
                if content_length != expected_size:
                    raise ValueError("model artifact authoritative length mismatch")
                digest = hashlib.sha256()
                written = 0
                with open(partial_path, "xb") as output:
                    for chunk in response.iter_content(chunk_size=CHUNK_BYTES):
                        if not isinstance(chunk, bytes) or not chunk:
                            continue
                        written += len(chunk)
                        if written > expected_size:
                            raise ValueError("model artifact stream length mismatch")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if written != expected_size:
                    raise ValueError("model artifact stream length mismatch")
                if digest.hexdigest() != envelope["sha256"]:
                    raise ValueError("model artifact digest mismatch")
                os.replace(partial_path, final_path)
                prepared.append(final_path)
            finally:
                if response is not None and callable(getattr(response, "close", None)):
                    response.close()
                if os.path.exists(partial_path):
                    os.remove(partial_path)
        return prepared
    except Exception:
        cleanup_model_artifacts(prepared, target_root)
        raise


def cleanup_model_artifacts(paths, target_dir):
    target_root = os.path.realpath(target_dir)
    for path in paths or []:
        real_path = os.path.realpath(path)
        if os.path.dirname(real_path) != target_root:
            continue
        try:
            if os.path.isfile(real_path):
                os.remove(real_path)
        except OSError:
            pass


def verify_model_artifacts_visible(envelopes, prepared_paths, object_info_get, object_info_url):
    """Fail closed unless files exist and ComfyUI exposes their exact names."""
    if not isinstance(envelopes, list):
        raise ValueError("model artifact envelope list is invalid")
    if len(envelopes) != len(prepared_paths):
        raise ValueError("prepared model artifact count is invalid")

    filenames = []
    for envelope, path in zip(envelopes, prepared_paths):
        filename = envelope.get("filename") if isinstance(envelope, dict) else None
        if not isinstance(filename, str) or os.path.basename(path) != filename:
            raise ValueError("prepared model artifact filename is invalid")
        if not os.path.isfile(path):
            raise ValueError(f"prepared model artifact is missing: {filename}")
        filenames.append(filename)

    if not filenames:
        return

    response = object_info_get(object_info_url, timeout=30)
    try:
        if getattr(response, "status_code", None) != 200:
            raise ValueError("ComfyUI object_info preflight failed")
        try:
            object_info = response.json()
        except Exception as error:
            raise ValueError("ComfyUI object_info response is invalid") from error
    finally:
        if callable(getattr(response, "close", None)):
            response.close()

    catalog_text = json.dumps(object_info, ensure_ascii=False)
    missing = [filename for filename in filenames if filename not in catalog_text]
    if missing:
        raise ValueError(
            "ComfyUI object_info does not expose prepared LoRA(s): "
            + ", ".join(missing)
        )


def _content_length(headers):
    try:
        value = headers.get("Content-Length")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _validate_envelope(value):
    if not isinstance(value, dict) or set(value) != {
        "schemaVersion", "artifactId", "filename", "storageVersion",
        "sha256", "byteSize", "downloadUrl",
    }:
        raise ValueError("model artifact envelope shape is invalid")
    if value["schemaVersion"] != SCHEMA_VERSION or not ID.fullmatch(value["artifactId"]):
        raise ValueError("model artifact envelope identity is invalid")
    if not isinstance(value["filename"], str) or not FILENAME.fullmatch(value["filename"]):
        raise ValueError("model artifact filename is invalid")
    if not isinstance(value["sha256"], str) or not SHA256.fullmatch(value["sha256"]):
        raise ValueError("model artifact digest is invalid")
    expected_filename = f"{value['artifactId']}-{value['sha256'][:16]}.safetensors"
    if value["filename"] != expected_filename:
        raise ValueError("model artifact filename binding is invalid")
    if (not isinstance(value["storageVersion"], str) or not value["storageVersion"]
            or len(value["storageVersion"]) > 1024):
        raise ValueError("model artifact storage version is invalid")
    if not isinstance(value["downloadUrl"], str) or len(value["downloadUrl"]) > 8192:
        raise ValueError("model artifact capability URL is invalid")
    parsed = urlsplit(value["downloadUrl"])
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("model artifact capability URL is invalid")
    if not isinstance(value["byteSize"], str) or not value["byteSize"].isdigit():
        raise ValueError("model artifact byte length is invalid")
    size = int(value["byteSize"])
    if size < 1 or size > MAX_ARTIFACT_BYTES:
        raise ValueError("model artifact byte length is invalid")
    return value
