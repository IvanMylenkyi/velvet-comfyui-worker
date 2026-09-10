import hashlib
import os
import tempfile
import unittest

from model_artifacts import (
    cleanup_model_artifacts,
    prepare_model_artifacts,
    verify_model_artifacts_visible,
)


class FakeResponse:
    def __init__(self, data=b"verified-model-bytes", status_code=200, declared_length=None):
        self.data = data
        self.status_code = status_code
        self.headers = {"Content-Length": str(len(data) if declared_length is None else declared_length)}
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.data), max(1, min(chunk_size, 3))):
            yield self.data[offset:offset + max(1, min(chunk_size, 3))]

    def close(self):
        self.closed = True


class FakeGet:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class ObjectInfoResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class ModelArtifactWorkerEnvelopeTests(unittest.TestCase):
    def envelope(self, data):
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = "artifact_abcdefghijklmnopqrstuvwxyz123456"
        return {
            "schemaVersion": "model-artifact-generation-v1",
            "artifactId": artifact_id,
            "filename": f"{artifact_id}-{digest[:16]}.safetensors",
            "storageVersion": "primary-version-1",
            "sha256": digest,
            "byteSize": str(len(data)),
            "downloadUrl": "https://signed.example/exact-version?signature=opaque",
        }

    def test_downloads_capability_without_redirects_and_verifies_before_atomic_publish(self):
        data = b"verified-model-bytes"
        response = FakeResponse(data)
        fetch = FakeGet(response)
        with tempfile.TemporaryDirectory() as target:
            paths = prepare_model_artifacts([self.envelope(data)], target, http_get=fetch)
            self.assertEqual(len(paths), 1)
            with open(paths[0], "rb") as artifact:
                self.assertEqual(artifact.read(), data)
            self.assertEqual(fetch.calls, [(
                "https://signed.example/exact-version?signature=opaque",
                {"stream": True, "allow_redirects": False, "timeout": (10, 60)},
            )])
            self.assertTrue(response.closed)
            self.assertEqual([name for name in os.listdir(target) if name.endswith(".part")], [])
            cleanup_model_artifacts(paths, target)
            self.assertFalse(os.path.exists(paths[0]))

    def test_job_private_runtime_filename_survives_visibility_preflight(self):
        data = b"verified-model-bytes"
        artifact = self.envelope(data)
        runtime_filename = "vv_artifact_job123_0.safetensors"
        with tempfile.TemporaryDirectory() as target:
            paths = prepare_model_artifacts(
                [artifact],
                target,
                http_get=FakeGet(FakeResponse(data)),
                output_filenames={artifact["filename"]: runtime_filename},
            )
            self.assertEqual(os.path.basename(paths[0]), runtime_filename)
            response = ObjectInfoResponse({"CR LoRA Stack": {"input": {
                "required": {"lora_name_1": [runtime_filename]}
            }}})
            verify_model_artifacts_visible(
                [artifact], paths, FakeGet(response), "http://comfy/object_info",
                [runtime_filename],
            )
            cleanup_model_artifacts(paths, target)
            self.assertFalse(os.path.exists(paths[0]))

    def test_rejects_digest_mismatch_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as target:
            with self.assertRaisesRegex(ValueError, "digest"):
                prepare_model_artifacts(
                    [self.envelope(b"tampered")], target,
                    http_get=FakeGet(FakeResponse(b"expected")),
                )
            self.assertEqual(os.listdir(target), [])

    def test_rejects_short_oversized_or_lying_stream(self):
        expected = b"expected"
        cases = [
            FakeResponse(b"x", declared_length=len(expected)),
            FakeResponse(expected + b"extra", declared_length=len(expected)),
            FakeResponse(expected, declared_length=len(expected) + 1),
        ]
        for response in cases:
            with self.subTest(actual=len(response.data)), tempfile.TemporaryDirectory() as target:
                with self.assertRaisesRegex(ValueError, "length"):
                    prepare_model_artifacts(
                        [self.envelope(expected)], target, http_get=FakeGet(response),
                    )
                self.assertEqual(os.listdir(target), [])

    def test_rejects_redirect_or_non_success_response(self):
        for status in (301, 302, 307, 308, 403, 500):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as target:
                with self.assertRaisesRegex(ValueError, "response"):
                    prepare_model_artifacts(
                        [self.envelope(b"x")], target,
                        http_get=FakeGet(FakeResponse(b"x", status_code=status)),
                    )

    def test_rejects_malformed_or_duplicate_envelopes_before_http(self):
        base = self.envelope(b"x")
        cases = [
            [{**base, "schemaVersion": "legacy"}],
            [{**base, "filename": "../escape.safetensors"}],
            [{**base, "downloadUrl": "http://signed.example/model"}],
            [{**base, "downloadUrl": "https://user:pass@signed.example/model"}],
            [{**base, "downloadUrl": "https://signed.example/model#fragment"}],
            [{**base, "storageVersion": ""}],
            [{**base, "sha256": "x"}],
            [{**base, "byteSize": "0"}],
            [base, dict(base)],
        ]
        for envelopes in cases:
            with self.subTest(envelopes=envelopes), tempfile.TemporaryDirectory() as target:
                fetch = FakeGet(FakeResponse(b"x"))
                with self.assertRaises(ValueError):
                    prepare_model_artifacts(envelopes, target, http_get=fetch)
                self.assertEqual(fetch.calls, [])
                self.assertEqual(os.listdir(target), [])

    def test_empty_envelope_needs_no_capability(self):
        with tempfile.TemporaryDirectory() as target:
            self.assertEqual(prepare_model_artifacts([], target), [])

    def test_cleanup_refuses_paths_outside_managed_directory(self):
        with tempfile.TemporaryDirectory() as target, tempfile.NamedTemporaryFile() as external:
            cleanup_model_artifacts([external.name], target)
            self.assertTrue(os.path.exists(external.name))

    def test_visibility_preflight_requires_file_and_object_info_name(self):
        data = b"verified-model-bytes"
        artifact = self.envelope(data)
        with tempfile.TemporaryDirectory() as target:
            path = os.path.join(target, artifact["filename"])
            with open(path, "wb") as output:
                output.write(data)
            response = ObjectInfoResponse({"CR LoRA Stack": {"input": {"required": {"lora_name_1": [artifact["filename"]]}}}})
            fetch = FakeGet(response)

            verify_model_artifacts_visible([artifact], [path], fetch, "http://comfy/object_info")

            self.assertEqual(fetch.calls, [("http://comfy/object_info", {"timeout": 30})])
            self.assertTrue(response.closed)

            missing_response = ObjectInfoResponse({"CR LoRA Stack": {"input": {"required": {"lora_name_1": ["None"]}}}})
            with self.assertRaisesRegex(ValueError, "does not expose"):
                verify_model_artifacts_visible(
                    [artifact], [path], FakeGet(missing_response), "http://comfy/object_info"
                )

    def test_visibility_preflight_rejects_missing_file_before_http(self):
        data = b"verified-model-bytes"
        artifact = self.envelope(data)
        with tempfile.TemporaryDirectory() as target:
            fetch = FakeGet(ObjectInfoResponse({}))
            with self.assertRaisesRegex(ValueError, "missing"):
                verify_model_artifacts_visible(
                    [artifact], [os.path.join(target, artifact["filename"])],
                    fetch, "http://comfy/object_info"
                )
            self.assertEqual(fetch.calls, [])


if __name__ == "__main__":
    unittest.main()
