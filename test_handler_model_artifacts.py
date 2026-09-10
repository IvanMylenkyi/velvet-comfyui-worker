import pathlib
import unittest


class HandlerModelArtifactIntegrationTests(unittest.TestCase):
    def test_docker_image_contains_verified_artifact_module_and_smoke_test(self):
        dockerfile = pathlib.Path(__file__).with_name("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY model_artifacts.py /model_artifacts.py", dockerfile)
        self.assertIn(
            'RUN python3 -c "import sys; sys.path.insert(0, \'/\'); import model_artifacts"',
            dockerfile,
        )

    def test_handler_uses_verified_envelope_and_has_no_legacy_url_downloader(self):
        source = pathlib.Path(__file__).with_name("handler.py").read_text(encoding="utf-8")
        self.assertIn("prepare_model_artifacts", source)
        self.assertIn('job_input.get("model_artifacts", [])', source)
        self.assertNotIn("modelArtifactStorage", source)
        self.assertNotIn("custom_loras", source)
        self.assertNotIn("requests.get(url", source)
        self.assertIn("cleanup_model_artifacts(prepared, loras_dir)", source)
        self.assertNotIn("Downloading Custom LoRA", source)
        self.assertIn("worker_build=", source)
        self.assertIn("model_artifacts_count=", source)
        self.assertIn("lora_directory=", source)
        self.assertIn("prepared_artifact=", source)
        self.assertIn("verify_model_artifacts_visible", source)
        self.assertIn('"COMFYUI_DIR"', source)
        self.assertIn('"nodeId"', source)
        self.assertIn('"objectIndex"', source)
        self.assertIn('"storageKey"', source)
        self.assertIn("exclude_descriptors", source)


if __name__ == "__main__":
    unittest.main()
