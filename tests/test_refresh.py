import importlib.util
import os
import queue
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import folder_paths


module_path = Path(__file__).resolve().parents[1] / "refresh.py"
server_module = types.ModuleType("server")
server_module.PromptServer = type("PromptServer", (), {})
sys.modules["server"] = server_module
spec = importlib.util.spec_from_file_location("magik_auto_nodes_refresh_refresh", module_path)
refresh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresh
spec.loader.exec_module(refresh)


tracker = None


def node_info(node_class):
    tracker.record("loras")


class CategoryWatchTests(unittest.TestCase):
    def test_changes_only_supported_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested" / "first.safetensors").touch()
            folder_paths.folder_names_and_paths["test_models"] = ([directory], {".safetensors"})
            watch = refresh.CategoryWatch("test_models", [directory], {".safetensors"})
            self.assertEqual(watch.files, {os.path.join("nested", "first.safetensors")})

            (root / "other.txt").touch()
            self.assertIsNone(watch.refresh({watch.roots[0]: {directory}}))

            (root / "nested" / "second.safetensors").touch()
            self.assertEqual(watch.refresh({watch.roots[0]: {str(root / "nested")}})["added"], [os.path.join("nested", "second.safetensors")])

            (root / "nested" / "second.safetensors").rename(root / "second.safetensors")
            change = watch.refresh({watch.roots[0]: {directory, str(root / "nested")}})
            self.assertEqual(change["added"], ["second.safetensors"])
            self.assertEqual(change["removed"], [os.path.join("nested", "second.safetensors")])

            (root / "nested" / "first.safetensors").unlink()
            (root / "nested").rmdir()
            self.assertEqual(watch.refresh({watch.roots[0]: {directory}})["removed"], [os.path.join("nested", "first.safetensors")])
            self.assertEqual(folder_paths.filename_list_cache["test_models"][0], ["second.safetensors"])
            self.assertEqual(folder_paths.get_filename_list("test_models"), ["second.safetensors"])
            folder_paths.filename_list_cache.pop("test_models", None)
            folder_paths.folder_names_and_paths.pop("test_models", None)

    def test_duplicate_names_across_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / "one", Path(directory) / "two"]
            for root in roots:
                root.mkdir()
                (root / "same.pt").touch()
            watch = refresh.CategoryWatch("test_duplicates", [str(root) for root in roots], {".pt"})
            (roots[0] / "same.pt").unlink()
            self.assertIsNone(watch.refresh({watch.roots[0]: {str(roots[0])}}))
            (roots[1] / "same.pt").unlink()
            self.assertEqual(watch.refresh({watch.roots[1]: {str(roots[1])}})["removed"], ["same.pt"])
            folder_paths.filename_list_cache.pop("test_duplicates", None)


class NativeObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.models = self.base / "models"
        self.models.mkdir()
        self.input = self.base / "input"
        self.input.mkdir()
        self.events = queue.Queue()
        server = types.SimpleNamespace(send_sync=lambda name, event: self.events.put((name, event)))
        self.tracker = refresh.FileRefresh(server)
        self.tracker.dependencies["test_models"] = {"Loader"}
        self.paths_patch = patch.dict(folder_paths.folder_names_and_paths, {"test_models": ([str(self.models)], {".pt"})}, clear=True)
        self.input_patch = patch.object(folder_paths, "get_input_directory", return_value=str(self.input))
        self.models_patch = patch.object(folder_paths, "models_dir", str(self.models))
        self.paths_patch.start()
        self.input_patch.start()
        self.models_patch.start()

    def tearDown(self):
        self.tracker.stop()
        self.models_patch.stop()
        self.input_patch.stop()
        self.paths_patch.stop()
        folder_paths.filename_list_cache.pop("test_models", None)
        self.temp.cleanup()

    def event(self):
        name, event = self.events.get(timeout=5)
        self.assertEqual(name, "magik_auto_nodes_refresh")
        self.assertEqual(event["nodes"], ["Loader"])
        return event["categories"]["test_models"]

    def test_native_create_move_delete_and_idle(self):
        self.tracker.start()
        root = self.tracker.watches["test_models"].roots[0]
        with patch.object(root, "_read", wraps=root._read) as read:
            time.sleep(0.4)
            read.assert_not_called()
        (self.models / "nested").mkdir()
        for name in ("one.pt", "two.pt", "three.pt"):
            (self.models / "nested" / name).touch()
        self.assertEqual(self.event()["added"], 3)

        (self.models / "nested" / "one.pt").rename(self.models / "renamed.pt")
        change = self.event()
        self.assertEqual(change["added"], 1)
        self.assertEqual(change["removed"], 1)
        shutil.rmtree(self.models / "nested")
        self.assertEqual(self.event()["removed"], 2)
        (self.models / "ignored.txt").touch()
        (self.models / "renamed.pt").write_text("changed contents")
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=0.4)

    def test_missing_root_and_recreation(self):
        self.models.rmdir()
        self.tracker.start()
        self.models.mkdir()
        (self.models / "one.pt").touch()
        self.assertEqual(self.event()["added"], 1)
        shutil.rmtree(self.models)
        self.assertEqual(self.event()["removed"], 1)
        self.models.mkdir()
        (self.models / "two.pt").touch()
        self.assertEqual(self.event()["added"], 1)
        (self.models / "three.pt").touch()
        self.assertEqual(self.event()["added"], 1)

    def test_does_not_scan_other_categories(self):
        first = self.models / "first"
        second = self.models / "second"
        first.mkdir()
        second.mkdir()
        folder_paths.folder_names_and_paths["test_models"] = ([str(first)], {".pt"})
        folder_paths.folder_names_and_paths["test_other"] = ([str(second)], {".pt"})
        self.tracker.start()
        other_root = self.tracker.watches["test_other"].roots[0]
        with patch.object(other_root, "_read", wraps=other_root._read) as read:
            (first / "one.pt").touch()
            self.assertEqual(self.event()["added"], 1)
            read.assert_not_called()
        folder_paths.filename_list_cache.pop("test_other", None)

    def test_rapid_root_replacement(self):
        (self.models / "old.pt").touch()
        self.tracker.start()
        shutil.rmtree(self.models)
        self.models.mkdir()
        (self.models / "one.pt").touch()
        self.assertEqual(self.event(), {"added": 1, "removed": 1})
        (self.models / "two.pt").touch()
        self.assertEqual(self.event()["added"], 1)

    def test_symlink_directory_target(self):
        target = self.base / "external"
        target.mkdir()
        (self.models / "linked").symlink_to(target, target_is_directory=True)
        self.tracker.start()
        (target / "one.pt").touch()
        self.assertEqual(self.event()["added"], 1)

    def test_new_symlink_and_retarget(self):
        first = self.base / "first"
        second = self.base / "second"
        first.mkdir()
        second.mkdir()
        (first / "one.pt").touch()
        (second / "two.pt").touch()
        self.tracker.start()
        (self.models / "linked").symlink_to(first, target_is_directory=True)
        self.assertEqual(self.event()["added"], 1)
        (first / "three.pt").touch()
        self.assertEqual(self.event()["added"], 1)
        replacement = self.models / "replacement"
        replacement.symlink_to(second, target_is_directory=True)
        replacement.replace(self.models / "linked")
        change = self.event()
        self.assertEqual(change["added"], 1)
        self.assertEqual(change["removed"], 2)
        (second / "four.pt").touch()
        self.assertEqual(self.event()["added"], 1)


class DependencyTests(unittest.TestCase):
    def test_records_node_info_caller(self):
        tracker = refresh.FileRefresh(None)
        server_module.tracker = tracker
        server_node_info = types.FunctionType(node_info.__code__, server_module.__dict__)
        server_node_info("LoraLoader")
        self.assertEqual(tracker.dependencies["loras"], {"LoraLoader"})

    def test_old_revision_requests_full_refresh(self):
        tracker = refresh.FileRefresh(None)
        tracker.revision = 102
        tracker.history.extend({"revision": n} for n in range(3, 103))
        self.assertTrue(tracker.state(0)["full_refresh"])
        self.assertEqual(len(tracker.state(100)["events"]), 2)


class RegressionTests(unittest.TestCase):
    def test_new_category_and_changed_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "a.pt").touch()
            (base / "b.bin").touch()
            tracker = refresh.FileRefresh(None)
            with patch.dict(folder_paths.folder_names_and_paths,
                            {"new": ([directory], {".pt"})}, clear=True):
                self.assertEqual(tracker.sync_roots()["new"]["added"], ["a.pt"])
                folder_paths.folder_names_and_paths["new"] = ([directory], {".bin"})
                self.assertEqual(tracker.sync_roots()["new"],
                                 {"added": ["b.bin"], "removed": ["a.pt"]})
            folder_paths.filename_list_cache.pop("new", None)

    def test_poll_child_survives_native_parent_and_failed_watch_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            child = base / "network"
            child.mkdir()
            tracker = refresh.FileRefresh(None)
            tracker.poll_paths = [str(child)]
            tracker.watches["models"] = refresh.CategoryWatch("models", [str(child)], {".pt"})
            tracker.schedule = lambda *args: None
            with patch.object(folder_paths, "models_dir", directory):
                tracker.update_subscriptions()
                self.assertIn((str(child), True), tracker.failed)
                scheduled = []
                observer = types.SimpleNamespace(is_alive=lambda: False)
                tracker.schedule = lambda *key: (scheduled.append(key) or (observer, object()))
                tracker.failed = {key: 0 for key in tracker.failed}
                tracker.update_subscriptions()
                self.assertIn((str(child), True), scheduled)
                self.assertFalse(tracker.failed)


if __name__ == "__main__":
    unittest.main()
