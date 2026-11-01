import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import asr_models as models
from common import UserError, read_json, write_json


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


class ModelManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.payloads = {"config.json": b'{"test":true}', "weights.safetensors": b"weight-data-for-testing", "README.md": b"Test resource license"}
        files = [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in self.payloads.items()]
        component = {"id": "asr", "name": "语音识别", "repository": "example/fixed", "revision": "a" * 40, "files": files}
        self.definitions = [{"id": mid, "name": mid, "subtitle": "test", "engine": "mlx-whisper", "format": "float16",
                             "license": "MIT", "legacy": mid == "small", "components": [copy.deepcopy(component)]}
                            for mid in ("turbo", "large-v3", "small")]
        qwen = {**copy.deepcopy(self.definitions[0]), "id": "qwen3-asr-1.7b", "engine": "mlx-audio", "format": "bfloat16"}
        qwen["components"].append({**copy.deepcopy(component), "id": "aligner", "name": "时间对齐"})
        self.definitions.insert(2, qwen)
        self.catalog_patch = patch.object(models, "catalog", return_value=self.definitions)
        self.catalog_patch.start()
        self.checkpoint_patch = patch("local_models.existing_checkpoint", return_value=None)
        self.checkpoint_patch.start()
        self.network_patch = patch.object(models, "_open_download", side_effect=self.open_download)
        self.network = self.network_patch.start()

    def tearDown(self):
        self.network_patch.stop(); self.checkpoint_patch.stop(); self.catalog_patch.stop()
        self.temp.cleanup()

    def open_download(self, url, offset):
        body = self.payloads[url.rsplit("/", 1)[-1]]
        return Response(body[offset:], 206 if offset else 200,
                        {"Content-Range": f"bytes {offset}-{len(body)-1}/{len(body)}"} if offset else {})

    def install(self, mid="turbo"):
        models.download_model(self.root, mid)
        return models._paths(self.root, mid)

    def test_empty_inventory_has_three_choices_and_does_not_download(self):
        value = models.inventory(self.root)
        self.assertEqual([m["id"] for m in value], ["turbo", "large-v3", "qwen3-asr-1.7b"])
        self.assertTrue(all(m["state"] == "uninstalled" for m in value))
        self.network.assert_not_called()
        with self.assertRaises(UserError), models.acquire_model(self.root, "turbo"):
            pass
        self.network.assert_not_called()

    def test_verified_shared_hub_cache_is_copied_and_never_removed(self):
        component = self.definitions[0]["components"][0]
        hub = self.root / "external-hub"
        snapshot = hub / "models--example--fixed" / "snapshots" / component["revision"]
        snapshot.mkdir(parents=True)
        for name, data in self.payloads.items():
            (snapshot / name).write_bytes(data)
        with patch.dict("os.environ", {"HF_HUB_CACHE": str(hub)}):
            paths = self.install()
        self.network.assert_not_called()
        self.assertNotEqual((snapshot / "weights.safetensors").stat().st_ino,
                            (paths["directory"] / "weights.safetensors").stat().st_ino)
        models.remove_model(self.root, "turbo")
        for name, data in self.payloads.items():
            self.assertEqual((snapshot / name).read_bytes(), data)

    def test_pinned_download_verifies_all_components_and_runs_offline(self):
        paths = self.install("qwen3-asr-1.7b")
        self.assertEqual(self.network.call_count, 6)
        for call in self.network.call_args_list:
            self.assertIn("/resolve/" + "a" * 40 + "/", call.args[0])
        receipt = read_json(paths["receipt"])
        self.assertEqual(len(receipt["components"]), 2)
        self.network.reset_mock()
        with models.acquire_model(self.root, "qwen3-asr-1.7b", "验收任务") as resource:
            self.assertEqual(set(resource["paths"]), {"asr", "aligner"})
            self.assertEqual(resource["snapshot"], receipt["snapshot"])
            row = next(x for x in models.inventory(self.root) if x["id"] == "qwen3-asr-1.7b")
            self.assertEqual(row["inUseTask"], "验收任务")
            self.assertEqual(row["state"], "ready")
        self.network.assert_not_called()

    def test_same_size_corruption_blocks_loading_and_is_repaired(self):
        paths = self.install()
        weight = paths["directory"] / "weights.safetensors"
        weight.write_bytes(b"X" * len(self.payloads[weight.name]))
        self.assertEqual(models.inventory(self.root)[0]["state"], "repair")
        with self.assertRaises(UserError), models.acquire_model(self.root, "turbo"):
            pass
        models.download_model(self.root, "turbo")
        self.assertEqual(weight.read_bytes(), self.payloads[weight.name])
        self.assertEqual(models.inventory(self.root)[0]["state"], "ready")

    def test_repair_reuses_intact_files_and_keeps_snapshot_identity(self):
        paths = self.install()
        before = read_json(paths["receipt"])["snapshot"]
        (paths["directory"] / "config.json").write_bytes(b"broken")
        self.network.reset_mock()
        models.download_model(self.root, "turbo")
        self.assertEqual([c.args[0].rsplit("/", 1)[-1] for c in self.network.call_args_list], ["config.json"])
        self.assertEqual(read_json(paths["receipt"])["snapshot"], before)
        self.assertEqual(models.inventory(self.root)[0]["state"], "ready")

    def test_documentation_does_not_change_resource_identity(self):
        paths = self.install()
        before = read_json(paths["receipt"])["snapshot"]
        components = copy.deepcopy(self.definitions[0]["components"])
        components[0]["files"] = [f for f in components[0]["files"] if f["path"] != "README.md"]
        after = models._make_receipt(self.definitions[0], paths["directory"], components)["snapshot"]
        self.assertEqual(before, after)

    def test_whisper_config_identity_ignores_layout_and_loader_defaults(self):
        paths = models._paths(self.root, "turbo")
        paths["directory"].mkdir()
        path = paths["directory"] / "config.json"
        write_json(path, {"n_mels": 128, "n_vocab": 51866})
        before = models._identity_sha(path, self.definitions[0])
        write_json(path, {"model_type": "whisper", "n_vocab": 51866, "n_mels": 128, "dtype": "float16"})
        self.assertEqual(before, models._identity_sha(path, self.definitions[0]))

    def test_expected_snapshot_rejects_another_version(self):
        paths = self.install()
        expected = copy.deepcopy(read_json(paths["receipt"])["snapshot"])
        expected["components"][0]["revision"] = "b" * 40
        with self.assertRaisesRegex(UserError, "版本"), models.acquire_model(self.root, "turbo", expected=expected):
            pass

    def test_pause_keeps_partial_and_continue_uses_range(self):
        paths = models._paths(self.root, "turbo")
        original_open = self.open_download
        class Interrupted(Response):
            def read(self, size=-1):
                if self.tell():
                    raise KeyboardInterrupt()
                return super().read(5)
        def fail_once(url, offset):
            if url.endswith("weights.safetensors"):
                return Interrupted(self.payloads["weights.safetensors"])
            return original_open(url, offset)
        with patch.object(models, "_open_download", side_effect=fail_once), self.assertRaises(KeyboardInterrupt):
            models.download_model(self.root, "turbo")
        partial = paths["staging"] / "weights.safetensors.part"
        self.assertEqual(partial.read_bytes(), self.payloads["weights.safetensors"][:5])
        self.assertFalse(paths["directory"].exists())
        self.assertFalse(paths["receipt"].exists())
        self.assertEqual(models.inventory(self.root)[0]["state"], "paused")
        self.network.reset_mock()
        models.download_model(self.root, "turbo")
        offsets = [(c.args[0].rsplit("/", 1)[-1], c.args[1]) for c in self.network.call_args_list]
        self.assertIn(("weights.safetensors", 5), offsets)
        self.assertNotIn(("config.json", 0), offsets)
        self.assertEqual(models.inventory(self.root)[0]["state"], "ready")

    def test_server_ignoring_range_restarts_instead_of_appending(self):
        paths = models._paths(self.root, "turbo")
        paths["staging"].mkdir()
        (paths["staging"] / "weights.safetensors.part").write_bytes(b"wrong")
        with patch.object(models, "_open_download", side_effect=lambda url, _offset: Response(self.payloads[url.rsplit("/", 1)[-1]])):
            models.download_model(self.root, "turbo")
        self.assertEqual((paths["directory"] / "weights.safetensors").read_bytes(), self.payloads["weights.safetensors"])

    def test_bad_range_or_checksum_never_publishes_model(self):
        paths = models._paths(self.root, "turbo")
        paths["staging"].mkdir()
        (paths["staging"] / "config.json.part").write_bytes(b"{")
        with patch.object(models, "_open_download", return_value=Response(b"x", 206, {"Content-Range": "bytes 0-1/2"})), self.assertRaises(UserError):
            models.download_model(self.root, "turbo")
        self.assertFalse(paths["receipt"].exists())
        models.remove_model(self.root, "turbo", partial_only=True)
        with patch.object(models, "_open_download", return_value=Response(b"x" * len(self.payloads["config.json"]))), self.assertRaises(UserError):
            models.download_model(self.root, "turbo")
        self.assertFalse(paths["receipt"].exists())
        self.assertFalse((paths["staging"] / "config.json.part").exists())

    def test_short_response_and_disk_full_preserve_incomplete_state(self):
        with patch.object(models, "_open_download", return_value=Response(b"{")), self.assertRaises(UserError):
            models.download_model(self.root, "turbo")
        self.assertEqual(models.inventory(self.root)[0]["state"], "failed")
        with patch.object(models.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()), self.assertRaisesRegex(UserError, "空间"):
            models.download_model(self.root, "turbo")
        self.assertFalse(models._paths(self.root, "turbo")["receipt"].exists())

    def test_in_use_model_cannot_be_deleted_or_repaired(self):
        paths = self.install()
        with models.acquire_model(self.root, "turbo", "进行中的任务"):
            for operation in (lambda: models.remove_model(self.root, "turbo"), lambda: models.download_model(self.root, "turbo")):
                with self.assertRaises(UserError):
                    operation()
            self.assertTrue(paths["directory"].is_dir())
        models.remove_model(self.root, "turbo")
        self.assertFalse(paths["directory"].exists())
        self.assertEqual(models.inventory(self.root)[0]["state"], "uninstalled")

    def test_inference_compute_lease_prevents_competing_gpu_load(self):
        self.install(); self.install("large-v3")
        with models.acquire_model(self.root, "turbo"):
            with self.assertRaises(UserError), models.acquire_model(self.root, "large-v3"):
                pass

    def test_remove_only_owned_resources_and_clear_keeps_installed_model(self):
        paths = self.install()
        unrelated = self.root / "external-cache"
        unrelated.mkdir(); (unrelated / "weights").write_bytes(b"keep")
        task = self.root / "recording-tasks" / "sample"
        task.mkdir(parents=True); (task / "transcript.json").write_text("[]")
        paths["staging"].mkdir(); (paths["staging"] / "temp.part").write_bytes(b"partial")
        models.remove_model(self.root, "turbo", partial_only=True)
        self.assertTrue(paths["directory"].is_dir())
        self.assertEqual(models.inventory(self.root)[0]["partialBytes"], 0)
        models.remove_model(self.root, "turbo")
        self.assertEqual((unrelated / "weights").read_bytes(), b"keep")
        self.assertTrue((task / "transcript.json").exists())

    def test_symlink_and_traversal_are_rejected(self):
        outside = self.root / "outside"; outside.mkdir()
        paths = models._paths(self.root, "turbo")
        paths["directory"].symlink_to(outside)
        with self.assertRaises(UserError):
            models.remove_model(self.root, "turbo")
        self.assertTrue(outside.is_dir())
        with self.assertRaises(UserError):
            models._paths(self.root, "../outside")
        with self.assertRaises(UserError):
            models._file(outside, "../preferences.json")
        paths["directory"].unlink()
        paths["staging"].mkdir()
        (paths["staging"] / "config.json.part").symlink_to(outside / "victim")
        with self.assertRaises(UserError):
            models.download_model(self.root, "turbo")
        self.assertFalse((outside / "victim").exists())

    def test_existing_canonical_cache_without_readme_is_adopted_offline(self):
        paths = models._paths(self.root, "turbo")
        paths["directory"].mkdir()
        for name, body in self.payloads.items():
            if name != "README.md":
                (paths["directory"] / name).write_bytes(body)
        self.assertEqual(models.inventory(self.root)[0]["state"], "ready")
        self.assertTrue(paths["receipt"].exists())
        self.network.assert_not_called()

    def test_missing_qwen_component_is_not_usable(self):
        paths = self.install("qwen3-asr-1.7b")
        (paths["directory"] / "aligner" / "config.json").unlink()
        with self.assertRaises(UserError), models.acquire_model(self.root, "qwen3-asr-1.7b"):
            pass
        self.assertEqual(next(x for x in models.inventory(self.root) if x["id"] == "qwen3-asr-1.7b")["state"], "repair")

    def test_legacy_small_shown_only_for_existing_cache_or_task(self):
        self.assertEqual(len(models.inventory(self.root)), 3)
        task = self.root / "recording-tasks" / "old-task"
        task.mkdir(parents=True)
        write_json(task / "request.json", {"whisperModel": "small"})
        row = models.inventory(self.root)[-1]
        self.assertEqual(row["id"], "small")
        self.assertTrue(row["legacy"])
        self.assertEqual(row["state"], "uninstalled")

    def test_crashed_activity_file_does_not_keep_model_busy(self):
        paths = self.install()
        write_json(paths["activities"] / "turbo.old.json", {"modelID": "turbo", "kind": "inference", "owner": "stale"})
        self.assertIsNone(models.inventory(self.root)[0]["inUseTask"])
        models.remove_model(self.root, "turbo")

    def test_cli_list_is_json_and_does_not_read_cloud_settings(self):
        (self.root / "preferences.json").write_text("invalid cloud settings")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(models.run_cli("list", None, self.root), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["stage"], "models")
        self.assertEqual(len(result["models"]), 3)
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
