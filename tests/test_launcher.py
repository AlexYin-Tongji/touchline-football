"""Content-based setup plans and refusal to mix a running old backend with new UI."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import launch
from server import build_info


class SetupPlanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        for name in ("src/App.tsx", "public/assets/catalog.json", "scripts/build-club-index.mjs",
                     "vite.config.ts", "tsconfig.json", "index.html", "package.json", "package-lock.json",
                     "requirements.txt", "requirements.lock.txt", "server/main.py", "agent_skills/team_maintenance/SKILL.md"):
            self.write(name, "initial")

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def prepared(self):
        (self.root / "node_modules").mkdir()
        self.write("dist/index.html", "compiled")
        for part in ("python", "node", "frontend"):
            build_info.record_success(part, self.root)

    def test_first_run_installs_both_dependency_sets_and_builds(self):
        plan = build_info.setup_plan(self.root)
        self.assertTrue(plan["python"])
        self.assertTrue(plan["node"])
        self.assertTrue(plan["frontend"])
        self.prepared()
        self.assertEqual({key: build_info.setup_plan(self.root)[key] for key in ("python", "node", "frontend")},
                         {"python": False, "node": False, "frontend": False})

    def test_content_change_with_unchanged_timestamp_and_asset_deletion_rebuild(self):
        self.prepared()
        source = self.root / "src/App.tsx"
        before = source.stat()
        source.write_text("changed", encoding="utf-8")
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertTrue(build_info.setup_plan(self.root)["frontend"])
        build_info.record_success("frontend", self.root)
        (self.root / "public/assets/catalog.json").unlink()
        self.assertTrue(build_info.setup_plan(self.root)["frontend"])

    def test_changed_lockfile_reinstalls_node_and_setup_forces_all_stages(self):
        self.prepared()
        self.write("package-lock.json", "new dependency")
        self.assertTrue(build_info.setup_plan(self.root)["node"])
        self.assertTrue(build_info.setup_plan(self.root)["frontend"])
        self.assertFalse(build_info.setup_plan(self.root)["python"])
        self.assertTrue(all(build_info.setup_plan(self.root, force=True)[key] for key in ("python", "node", "frontend")))

    def test_build_script_config_and_requirements_changes_are_inputs(self):
        self.prepared()
        for name in ("scripts/build-club-index.mjs", "vite.config.ts", "tsconfig.json"):
            self.write(name, "updated")
            self.assertTrue(build_info.setup_plan(self.root)["frontend"], name)
            build_info.record_success("frontend", self.root)
        self.write("requirements.txt", "new requirement")
        self.assertTrue(build_info.setup_plan(self.root)["python"])
        build_info.record_success("python", self.root)
        self.write("requirements.lock.txt", "new resolved version")
        self.assertTrue(build_info.setup_plan(self.root)["python"])

    def test_generated_files_and_user_data_do_not_change_application_identity(self):
        self.prepared()
        before = build_info.application_id(self.root)
        for name in ("src/generated/club-index.json", "data/touchline.sqlite3", "dist/assets/a.js", "output/test.log", "server/__pycache__/main.pyc"):
            self.write(name, "generated or private")
        self.assertEqual(build_info.application_id(self.root), before)
        self.assertFalse(build_info.setup_plan(self.root)["frontend"])
        self.write("server/main.py", "new backend")
        self.assertNotEqual(build_info.application_id(self.root), before)
        self.assertFalse(build_info.setup_plan(self.root)["frontend"])

    def test_interrupted_reinstall_is_not_marked_as_successful_on_next_start(self):
        self.prepared()
        build_info.invalidate_stage("node", self.root)
        self.assertTrue(build_info.setup_plan(self.root)["node"])
        self.assertTrue(build_info.setup_plan(self.root)["frontend"])
        build_info.record_success("node", self.root)
        build_info.invalidate_stage("frontend", self.root)
        self.assertTrue(build_info.setup_plan(self.root)["frontend"])

    def test_runtime_tree_without_frontend_sources_still_has_a_stable_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_root = Path(directory)
            (runtime_root / "server").mkdir()
            (runtime_root / "server/main.py").write_text("backend", encoding="utf-8")
            identity = build_info.application_id(runtime_root)
            self.assertEqual(len(identity), 64)
            self.assertEqual(identity, build_info.application_id(runtime_root))


class LauncherCompatibilityTests(unittest.TestCase):
    def test_old_or_unversioned_service_is_not_reused_or_replaced(self):
        for build_id in (None, "older-build"):
            ready = {"workspace_id": launch.WORKSPACE_ID, "pid": 1234, "build_id": build_id}
            with patch.object(launch, "status", return_value=ready), patch("sys.argv", ["launch.py", "--no-browser"]), \
                 patch.object(launch.subprocess, "Popen") as spawn, patch.object(launch.webbrowser, "open") as browser:
                with self.assertRaisesRegex(RuntimeError, "旧|不一致"):
                    launch.main()
                spawn.assert_not_called()
                browser.assert_not_called()

    def test_preflight_allows_same_build_but_blocks_mutation_while_it_runs(self):
        ready = {"workspace_id": launch.WORKSPACE_ID, "pid": 1234, "build_id": build_info.BUILD_ID}
        with patch.object(launch, "status", return_value=ready), patch("sys.argv", ["launch.py", "--preflight"]):
            self.assertEqual(launch.main(), 0)
        with patch.object(launch, "status", return_value=ready), patch("sys.argv", ["launch.py", "--preflight", "--require-stopped"]):
            with self.assertRaisesRegex(RuntimeError, "停止|关闭"):
                launch.main()

    def test_check_exposes_incompatibility_without_claiming_the_new_service_is_ready(self):
        output = io.StringIO()
        with patch.object(launch, "status", return_value={"workspace_id": launch.WORKSPACE_ID, "build_id": "old"}), \
             patch("sys.argv", ["launch.py", "--check"]), contextlib.redirect_stdout(output):
            self.assertEqual(launch.main(), 0)
        status = json.loads(output.getvalue())
        self.assertTrue(status["running"])
        self.assertFalse(status["compatible"])
        self.assertTrue(status["needs_restart"])


if __name__ == "__main__":
    unittest.main()
