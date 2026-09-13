"""Pure native-harness checks: no EXEs, sockets, browsers or user data."""

import ast
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from real_native_upgrade_smoke import OwnedTree, child_environment, legacy_helpers, run_native


class NativeHelpersTest(unittest.TestCase):
    def test_legacy_extraction_does_not_run_module_top_level(self):
        source = '''raise RuntimeError("must not run")
def _batch_quote(value: Path | str) -> str:
    return '"' + str(value).replace('%', '%%') + '"'
def _build_update_batch(temp_exe: Path, new_exe: Path, current_exe: Path, health_file: Path) -> str:
    return _batch_quote(new_exe) + " --update-health-file " + _batch_quote(health_file)
'''
        builder, text = legacy_helpers(source)
        self.assertNotIn("must not run", text)
        self.assertEqual(builder(Path("stage"), Path("new%.exe"), Path("old"), Path("health")),
                         '"new%%.exe" --update-health-file "health"')

    def test_missing_or_duplicate_legacy_function_rejected(self):
        for source in ("pass", "def _batch_quote(): pass\ndef _batch_quote(): pass"):
            with self.assertRaises(AssertionError):
                legacy_helpers(source)

    def test_child_environment_does_not_mutate_parent_or_reuse_profile(self):
        parent = {"WEBVIEW2_USER_DATA_FOLDER": "user-profile", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER": "custom",
                  "BROWSER": "user-default", "PYTHONPATH": "user-path", "PATH": "runtime"}
        with patch.dict("os.environ", parent, clear=True):
            env = child_environment(Path("data"), Path("temp"), Path("isolated"), "a" * 64, 51001, 51002)
            import os
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(env["WEBVIEW2_USER_DATA_FOLDER"], "isolated")
        self.assertEqual(env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"], "--remote-debugging-port=51002")
        self.assertEqual(env["WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER"], "1")
        self.assertEqual(env["TASK_RECORDER_PORT"], "51001")
        for key in ("BROWSER", "PYTHONPATH", "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"):
            self.assertNotIn(key, env)

    def test_conventional_or_equal_ports_are_rejected(self):
        for api, cdp in ((8765, 51002), (51001, 9222), (51001, 51001)):
            with self.assertRaises(AssertionError):
                child_environment(Path("data"), Path("temp"), Path("profile"), "a" * 64, api, cdp)

    def test_observer_launch_receives_stage_temp_and_session_environment(self):
        assignments = [node for node in ast.walk(ast.parse(inspect.getsource(run_native)))
                       if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                       and any(isinstance(target, ast.Name) and target.id == "observer" for target in node.targets)]
        self.assertEqual(len(assignments), 1)
        environment = child_environment(Path("case/data"), Path("case/temp"), Path("case/profile"),
                                        "a" * 64, 51001, 51002)
        launch, clock, paths = Mock(), Mock(), Mock()
        clock.time.return_value, clock.monotonic.return_value = 100.0, 2.0
        paths.which.return_value = "node.exe"
        namespace = {**run_native.__globals__, "subprocess": launch, "time": clock, "shutil": paths,
                     "env": environment, "directory": Path("case/stage"), "cdp_port": 51002,
                     "api_port": 51001, "started": 0.0, "log": object()}
        # Execute just the real launch expression with Popen mocked, never the native harness.
        eval(compile(ast.Expression(assignments[0].value), "observer-launch-fixture", "eval"), namespace)
        launch.Popen.assert_called_once()
        received = launch.Popen.call_args.kwargs["env"]
        self.assertIs(received, environment)
        self.assertEqual(received["TEMP"], str(Path("case/temp")))
        self.assertEqual(received["TMP"], str(Path("case/temp")))
        self.assertEqual(received["LEYLINEBOOK_DATA_DIR"], str(Path("case/data")))
        self.assertEqual(received["LEYLINEBOOK_SESSION_TOKEN"], "a" * 64)

    def test_metadata_failure_never_registers_or_closes_a_process_handle(self):
        for error in (psutil.NoSuchProcess(123), psutil.AccessDenied(123)):
            tree = OwnedTree.__new__(OwnedTree)
            tree.items, tree.evidence, tree.kernel = {}, {}, Mock()
            process = Mock()
            process.create_time.return_value = 100.0
            process.exe.side_effect = error
            with patch("real_native_upgrade_smoke.psutil.Process", return_value=process):
                with self.assertRaises(type(error)):
                    tree.add(123)
            self.assertEqual(tree.items, {})
            tree.kernel.OpenProcess.assert_not_called()
            tree.kernel.CloseHandle.assert_not_called()

    def test_handle_validation_failure_closes_once_without_registration(self):
        tree = OwnedTree.__new__(OwnedTree)
        tree.items, tree.evidence, tree.kernel = {}, {}, Mock()
        tree.role = Mock(return_value="native")
        tree.kernel.OpenProcess.return_value = 999
        tree.kernel.GetProcessTimes.return_value = False
        process = Mock()
        process.create_time.return_value = 100.0
        process.exe.return_value = sys.executable
        with patch("real_native_upgrade_smoke.psutil.Process", return_value=process):
            with self.assertRaisesRegex(AssertionError, "creation time"):
                tree.add(123)
        self.assertEqual(tree.items, {})
        tree.kernel.CloseHandle.assert_called_once_with(999)

    def test_default_browser_descendant_is_excluded_without_opening_handle(self):
        tree = OwnedTree.__new__(OwnedTree)
        tree.evidence, tree.kernel, tree.environment = {}, Mock(), {}
        tree.executable = Path(sys.executable).resolve()
        tree.items = {1: {"born": 99.0, "role": "native"}}
        tree.same = Mock(return_value=True)
        process = Mock()
        process.create_time.return_value = 100.0
        process.ppid.return_value = 1
        process.exe.return_value = str(Path(sys.executable).parent / "msedge.exe")
        with patch("real_native_upgrade_smoke.psutil.Process", return_value=process):
            with self.assertRaisesRegex(ValueError, "allowlist"):
                tree.add(123, 1)
        self.assertNotIn(123, tree.items)
        self.assertEqual(tree.evidence["excludedProcesses"][0]["pid"], 123)
        tree.kernel.OpenProcess.assert_not_called()

    def test_cleanup_without_challenge_retains_unresolved_processes(self):
        tree = OwnedTree.__new__(OwnedTree)
        tree.evidence, tree.kernel = {}, Mock()
        tree.refresh = Mock()
        tree.items = {123: {"born": 100.0, "role": "native", "handle": 999}}
        tree.kernel.WaitForSingleObject.return_value = 258
        self.assertEqual(tree.cleanup(challenge_authorized=False), [])
        tree.kernel.TerminateProcess.assert_not_called()
        tree.kernel.CloseHandle.assert_called_once_with(999)
        self.assertEqual(tree.evidence["unresolvedProcesses"][0]["pid"], 123)


if __name__ == "__main__":
    unittest.main()
