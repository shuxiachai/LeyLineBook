"""Review-gated official-old/native/legacy-coordinator smoke; no downloads.

Without --execute: verify supplied frozen inputs and extract pinned batch helpers.
--execute: one isolated old-native seed -> original legacy batch -> new-native
attempt. Review this script and fixtures/native_ready_observer.cjs before use.
No retry, rebuild, synthetic ready, request interception, timeout/flag changes,
browser installation, user-data access, or global process-name cleanup.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import traceback

import psutil

from real_upgrade_smoke import (
    Api, OLD_NAME, OLD_SHA256, OUTPUT, ROOT, archive_application_log,
    checkpoint, contained, copy_new, digest, logical_database, logical_summary,
    require, seed_old, verify_materials,
)

OLD_COMMIT = "505ff2d1c1d0dd2eb5af843b217f2df3dc8d7a34"
NEW_NAME = "LeyLineBook-v3.0.6-Windows-x64.exe"
HISTORICAL_LOCAL_HASH = "b5a7a9aa744a6f7e6a6a0d3272ff27ed45c6af35c2dd8c4d06adc5eaaec16aed"
OBSERVER = ROOT / "scripts/fixtures/native_ready_observer.cjs"
FORBIDDEN_PORTS = {8765, 18765, 9222}


def legacy_helpers(source: str):
    tree = ast.parse(source)
    names = {"_batch_quote", "_build_update_batch"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    require(len(nodes) == 2 and {node.name for node in nodes} == names, "Pinned legacy helpers missing")
    text = "\n\n".join(ast.get_source_segment(source, node) for node in nodes) + "\n"
    namespace = {"Path": Path}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "pinned-v3.0.5-helpers", "exec"), namespace)
    return namespace["_build_update_batch"], text


def frozen_inputs(manifest: Path) -> list[dict]:
    require(manifest.is_absolute() and manifest.resolve() == manifest, "Frozen manifest must not redirect")
    rows = json.loads(manifest.read_text(encoding="utf-8-sig"))
    require(len(rows) == 13, "Frozen manifest must contain exactly 13 inputs")
    require(len({row["path"] for row in rows}) == 13, "Frozen manifest has duplicates")
    for row in rows:
        path = contained(ROOT / row["path"], ROOT)
        require(digest(path) == row["sha256"], f"Frozen source changed: {row['path']}")
    return rows


def child_environment(data: Path, temporary: Path, profile: Path, token: str, api: int, cdp: int) -> dict:
    require(api != cdp and not ({api, cdp} & FORBIDDEN_PORTS), "Unsafe/reused port selection")
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {"PYTHONHOME", "PYTHONPATH", "TASK_RECORDER_PORT", "BROWSER"}
           and not key.upper().startswith("WEBVIEW2_")}
    env.update({"TEMP": str(temporary), "TMP": str(temporary), "LEYLINEBOOK_DATA_DIR": str(data),
                "LEYLINEBOOK_SESSION_TOKEN": token, "TASK_RECORDER_PORT": str(api),
                "PYTHONNOUSERSITE": "1", "PYTHONTZPATH": "",
                "WEBVIEW2_USER_DATA_FOLDER": str(profile),
                "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={cdp}",
                "WEBVIEW2_WAIT_FOR_SCRIPT_DEBUGGER": "1"})
    return env


def reserve_ports() -> tuple[int, int]:
    reservations = []
    try:
        for _ in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservations.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", 0))
        ports = tuple(sock.getsockname()[1] for sock in reservations)
        require(len(set(ports)) == 2 and not (set(ports) & FORBIDDEN_PORTS), "Unsafe allocated ports")
        return ports
    finally:
        for sock in reservations:
            sock.close()


def listener(port: int) -> int | None:
    rows = [row for row in psutil.net_connections(kind="tcp")
            if row.status == psutil.CONN_LISTEN and row.laddr.port == port]
    if not rows:
        return None
    require(all(row.laddr.ip in {"127.0.0.1", "::1"} for row in rows), "Non-loopback listener")
    pids = {row.pid for row in rows}
    require(len(pids) == 1 and None not in pids, "Ambiguous listener ownership")
    return pids.pop()


class OwnedTree:
    """Retain OS handles so PID reuse cannot redirect cleanup to another process."""

    def __init__(self, evidence: dict, executable: Path, profile: Path, environment: dict):
        self.evidence, self.items = evidence, {}
        self.executable, self.profile = executable, profile
        self.environment = {key: environment[key] for key in
                            ("LEYLINEBOOK_DATA_DIR", "TEMP", "LEYLINEBOOK_SESSION_TOKEN", "TASK_RECORDER_PORT")}
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result

    def role(self, executable: Path, process, parent: int | None, requested: str | None) -> str:
        parent_role = self.items[parent]["role"] if parent is not None else None
        if executable == self.executable and parent_role in {None, "native", "coordinator"}:
            actual = process.environ()
            require(all(actual.get(key) == value for key, value in self.environment.items()),
                    "Native child environment differs from this fixture")
            return "native"
        system = Path(os.environ["WINDIR"]) / "System32"
        if parent is None and requested == "coordinator" and executable == system / "cmd.exe":
            return "coordinator"
        if parent is None and requested == "observer" and executable == Path(shutil.which("node")).resolve():
            return "observer"
        if parent_role == "coordinator" and executable == system / "timeout.exe":
            return "legacy-timeout"
        if executable.name.lower() == "msedgewebview2.exe" and parent_role in {"native", "webview"}:
            args = process.cmdline()
            profiles = [arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")]
            require(all(Path(value).resolve() == self.profile for value in profiles), "Foreign WebView2 profile")
            if parent_role == "native":
                require(len(profiles) == 1, "WebView2 root lacks this fixture profile")
            else:
                require(executable == self.items[parent]["executable"], "WebView2 child executable changed")
            return "webview"
        raise ValueError("Process is outside the fixture executable/profile allowlist")

    def add(self, pid: int, parent: int | None = None, *, requested: str | None = None) -> None:
        if pid in self.items:
            require(self.same(pid), "PID was reused within owned process tree")
            return
        process = psutil.Process(pid)
        born = process.create_time()
        if parent is not None:
            require(self.same(parent) and process.ppid() == parent
                    and born >= self.items[parent]["born"], "Child ancestry changed during discovery")
        executable = Path(process.exe()).resolve()
        try:
            role = self.role(executable, process, parent, requested)
        except (ValueError, AssertionError):
            self.evidence.setdefault("excludedProcesses", []).append(
                {"pid": pid, "created": born, "parent": parent, "executable": str(executable)})
            raise
        handle = self.kernel.OpenProcess(0x00101001, False, pid)
        require(bool(handle), f"Cannot retain owned process handle: {pid}")
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            require(self.kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)),
                    "Cannot verify process creation time")
            stamp = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            require(abs(stamp / 10000000 - 11644473600 - born) < 0.001,
                    "Process handle creation time differs from discovered PID")
            self.evidence.setdefault("ownedProcesses", []).append(
                {"pid": pid, "created": born, "createdFiletime": stamp, "parent": parent,
                 "executable": str(executable), "role": role})
            # Commit only after all fallible metadata reads and handle checks.
            self.items[pid] = {"process": process, "born": born, "handle": handle, "parent": parent,
                               "executable": executable, "role": role}
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def same(self, pid: int) -> bool:
        item = self.items.get(pid)
        return bool(item and self.kernel.WaitForSingleObject(item["handle"], 0) == 258
                    and item["process"].is_running() and item["process"].create_time() == item["born"])

    def refresh(self, *, strict: bool = True) -> None:
        pending = list(self.items)
        while pending:
            pid = pending.pop()
            if not self.same(pid):
                continue
            try:
                children = self.items[pid]["process"].children()
            except psutil.NoSuchProcess:
                continue
            for child in children:
                if child.pid not in self.items:
                    try:
                        self.add(child.pid, pid)
                    except psutil.NoSuchProcess:
                        continue
                    except (ValueError, AssertionError):
                        if strict:
                            raise
                        continue
                    pending.append(child.pid)

    def verify_port(self, port: int, *, executable: Path | None = None, profile: Path | None = None) -> int | None:
        pid = listener(port)
        if pid is None:
            return None
        require(self.same(pid), f"Port {port} is not owned by the recorded child tree")
        process = self.items[pid]["process"]
        if executable is not None:
            require(Path(process.exe()).resolve() == executable, "API listener is not the isolated EXE")
        if profile is not None:
            args = process.cmdline()
            values = [arg.split("=", 1)[1] for arg in args if arg.startswith("--user-data-dir=")]
            require(len(values) == 1 and Path(values[0]).resolve() == profile, "CDP profile is not isolated")
            require(f"--remote-debugging-port={port}" in args, "CDP command-line port differs")
        self.evidence.setdefault("portOwners", {})[str(port)] = {"pid": pid, "created": self.items[pid]["born"]}
        return pid

    def cleanup(self, *, challenge_authorized: bool) -> list[int]:
        forced = []
        try:
            try:
                self.refresh(strict=False)
            except Exception as error:
                # Discovery failure must not prevent cleanup of retained, verified handles.
                self.evidence["cleanupDiscoveryError"] = repr(error)
            if not challenge_authorized:
                self.evidence["unresolvedProcesses"] = [
                    {"pid": pid, "created": item["born"], "role": item["role"],
                     "reason": "No authenticated application challenge authorizes forced cleanup"}
                    for pid, item in self.items.items()
                    if self.kernel.WaitForSingleObject(item["handle"], 0) == 258]
                return forced
            # Child-first, handle-directed termination, never taskkill /T or /IM.
            for pid, item in reversed(list(self.items.items())):
                if self.kernel.WaitForSingleObject(item["handle"], 0) == 258:
                    require(self.same(pid), "Cleanup identity changed; refusing termination")
                    require(self.kernel.TerminateProcess(item["handle"], 91), f"Cannot stop owned PID {pid}")
                    forced.append(pid)
                    require(self.kernel.WaitForSingleObject(item["handle"], 10000) == 0,
                            f"Owned PID {pid} did not exit")
        finally:
            self.evidence["forcedCleanupPids"] = forced
            for item in self.items.values():
                self.kernel.CloseHandle(item["handle"])
        return forced


def visible_windows(tree: OwnedTree, executable: Path) -> list[dict]:
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    windows = []

    @callback_type
    def visit(hwnd, _):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in tree.items and tree.same(pid.value) and user.IsWindowVisible(hwnd):
            if Path(tree.items[pid.value]["process"].exe()).resolve() == executable:
                rect = wintypes.RECT()
                if user.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right > rect.left and rect.bottom > rect.top:
                    windows.append({"hwnd": int(hwnd), "pid": pid.value, "created": tree.items[pid.value]["born"],
                                    "rect": [rect.left, rect.top, rect.right, rect.bottom]})
        return True

    require(user.EnumWindows(visit, 0), "Unable to enumerate owned native windows")
    return windows


def run_native(directory: Path, data: Path, executable: Path, result: dict, action, *, batch_builder=None,
               staged: Path | None = None, old: Path | None = None, new_hash: str | None = None) -> None:
    directory.mkdir()
    temporary, profile = directory / "temp", directory / "webview-profile"
    temporary.mkdir()
    profile.mkdir()
    api_port, cdp_port = reserve_ports()
    token = secrets.token_hex(32)
    env = child_environment(data, temporary, profile, token, api_port, cdp_port)
    health = temporary / ("LeyLineBook-update-health-" + secrets.token_hex(8) + ".ok")
    batch = temporary / "legacy-coordinator.bat"
    legacy = batch_builder is not None
    evidence = {"stage": directory.name, "responses": [], "passed": False, "legacy": legacy,
                "apiPort": api_port, "cdpPort": cdp_port, "profile": str(profile),
                "data": str(data), "temp": str(temporary), "health": str(health), "healthEvents": []}
    result["stages"].append(evidence)
    api = Api(api_port, token, evidence)
    tree = OwnedTree(evidence, executable, profile, env)
    launcher = observer = None
    stop_watch = threading.Event()
    started = time.monotonic()
    healthy = threading.Event()
    watcher_error = []

    def watch_health():
        previous = None
        try:
            while not stop_watch.is_set():
                try:
                    content = health.read_bytes()
                except FileNotFoundError:
                    content = None
                if content != previous:
                    evidence["healthEvents"].append({"seconds": round(time.monotonic() - started, 6),
                                                     "contents": None if content is None else content.decode("ascii")})
                    previous = content
                    if content == b"ready\n":
                        (directory / "observed-health.txt").write_bytes(content)
                        healthy.set()
                stop_watch.wait(0.005)
        except Exception as error:
            watcher_error.append(repr(error))

    watcher = threading.Thread(target=watch_health, daemon=True)

    def guard():
        tree.refresh()
        require(not watcher_error, f"Health observer failed: {watcher_error}")
        markers = list(temporary.glob("LeyLineBook-session-*.txt"))
        require(all(path.name == f"LeyLineBook-session-{api_port}.txt" for path in markers), "Fallback API port used")
        log = data / "task_recorder.log"
        if log.exists():
            text = log.read_text(encoding="utf-8", errors="replace")
            require("selecting another local port" not in text and "\u56de\u9000\u5230\u6d4f\u89c8\u5668" not in text,
                    "Native startup fallback detected; stop without changing environment")
        if launcher and launcher.poll() is not None:
            evidence.setdefault("launcherExit", {"code": launcher.returncode,
                                                   "seconds": round(time.monotonic() - started, 6)})
            require(legacy and healthy.is_set(), "Launcher exited before observed natural health")
        tree.verify_port(api_port, executable=executable)
        tree.verify_port(cdp_port, profile=profile)
        require(time.monotonic() - started < 45, "45-second outer observation bound expired (no coordinator extension)")

    try:
        require(listener(api_port) is None and listener(cdp_port) is None, "Port occupied immediately before launch")
        watcher.start()
        if legacy:
            batch.write_text(batch_builder(staged, executable, old, health), encoding="mbcs")
            (directory / "original-coordinator.bat.txt").write_bytes(batch.read_bytes())
            evidence["batchSha256"] = digest(batch)
            evidence["launchArgs"] = ["cmd.exe", "/c", str(batch)]
            evidence["creationflags"] = 0x00000008
            # Exact v3.0.5 launch arguments, flags and close_fds; no /d or timeout replacement.
            launcher = subprocess.Popen(evidence["launchArgs"], creationflags=0x00000008,
                                        close_fds=True, cwd=executable.parent, env=env)
        else:
            evidence["launchArgs"] = [str(executable)]
            launcher = subprocess.Popen(evidence["launchArgs"], cwd=executable.parent, env=env,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        tree.add(launcher.pid, requested="coordinator" if legacy else "native")
        evidence["launcherPid"] = launcher.pid
        while True:
            guard()
            api_pid = tree.verify_port(api_port, executable=executable)
            cdp_pid = tree.verify_port(cdp_port, profile=profile)
            if api_pid is not None and not api.verified:
                actual_env = tree.items[api_pid]["process"].environ()
                require(all(actual_env.get(key) == env[key] for key in
                            ("LEYLINEBOOK_DATA_DIR", "TEMP", "LEYLINEBOOK_SESSION_TOKEN", "TASK_RECORDER_PORT")),
                        "App did not inherit the isolated environment")
                api.verify_identity()
                evidence["identityVerified"] = True
                runtimes = [path for path in temporary.glob("_MEI*/python3*.dll")
                            if re.fullmatch(r"python3[0-9]{1,2}\.dll", path.name)]
                require(len(runtimes) == 1, "Cannot identify the actual bundled Python runtime")
                evidence["bundledPython"] = {"path": str(runtimes[0]), "file": runtimes[0].name,
                                             "sha256": digest(runtimes[0])}
            if api.verified and cdp_pid is not None:
                break
            time.sleep(0.05)
        with (directory / "observer.log").open("xb") as log:
            observer = subprocess.Popen([shutil.which("node"), str(OBSERVER), str(cdp_port), str(api_port),
                                         str(directory), str(int((time.time() + 45 - (time.monotonic() - started)) * 1000))],
                                        cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW, env=env)
            tree.add(observer.pid, requested="observer")
            while observer.poll() is None:
                guard()
                time.sleep(0.05)
        require(observer.returncode == 0, "Native CDP observer failed; inspect observer.json/log")
        observed = json.loads((directory / "observer.json").read_text(encoding="utf-8"))
        require(observed.get("passed") is True, "Observer did not prove natural ready/native DOM")
        evidence["observer"] = observed
        evidence["windows"] = visible_windows(tree, executable)
        require(evidence["windows"], "No visible window belonging to isolated native EXE")
        if legacy:
            while launcher.poll() is None:
                guard()
                time.sleep(0.05)
            require(launcher.returncode == 0 and healthy.is_set() and not health.exists()
                    and not old.exists() and not staged.exists() and not batch.exists(),
                    "Original coordinator did not complete its healthy/delete-old path")
            require(digest(executable) == new_hash, "Moved candidate differs from supplied frozen input")
            evidence["legacyHealthyPathVerified"] = True
        else:
            require(not healthy.is_set() and not health.exists(), "Old seed unexpectedly created update health")
        action(api, evidence)
        tree.refresh()
        tree.verify_port(api_port, executable=executable)
        tree.verify_port(cdp_port, profile=profile)
        api.verify_identity()
        evidence["shutdownChallengeVerified"] = True
        api.success("/api/shutdown", {})
        deadline = time.monotonic() + 10
        while any(tree.same(pid) for pid in tree.items) and time.monotonic() < deadline:
            tree.refresh()
            time.sleep(0.05)
        require(not any(tree.same(pid) for pid in tree.items), "Owned processes did not exit after authenticated shutdown")
        launcher.wait(timeout=1)
        require(launcher.returncode == 0, "Application/coordinator exit was not clean")
        evidence["passed"] = True
    except Exception as error:
        evidence["error"] = repr(error)
        evidence["traceback"] = traceback.format_exc()
        raise
    finally:
        stop_watch.set()
        if watcher.ident is not None:
            watcher.join(timeout=2)
        try:
            authorized = evidence.get("shutdownChallengeVerified", False)
            if not authorized:
                try:
                    tree.refresh(strict=False)
                    if tree.verify_port(api_port, executable=executable) is not None:
                        api.verify_identity()
                        evidence["cleanupChallengeVerified"] = True
                        authorized = True
                        api.success("/api/shutdown", {})
                        time.sleep(0.3)
                except Exception as error:
                    evidence["cleanupIdentityError"] = repr(error)
            forced = tree.cleanup(challenge_authorized=authorized)
            if forced or evidence.get("unresolvedProcesses") or evidence.get("cleanupDiscoveryError"):
                evidence["passed"] = False
        except Exception as error:
            evidence["cleanupError"] = repr(error)
            evidence["passed"] = False
        finally:
            for process in (observer, launcher):
                if process is not None:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        evidence.setdefault("unresolvedPids", []).append(process.pid)
            evidence["seconds"] = round(time.monotonic() - started, 3)
            evidence["exitCode"] = None if launcher is None else launcher.poll()
            evidence["healthPresentAtEnd"] = health.exists()
            if evidence.get("unresolvedProcesses") or evidence.get("unresolvedPids") or evidence.get("cleanupError"):
                archive = data / "task_recorder.log"
            else:
                archive = archive_application_log(data, directory)
            evidence["applicationLog"] = str(archive) if archive else None
            (directory / "stage.json").write_text(json.dumps(evidence, ensure_ascii=True, indent=2), encoding="utf-8")
        require(evidence["passed"] and not evidence.get("cleanupDiscoveryError"),
                "Native stage failed or cleanup could not verify all descendants")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run once only after independent script review")
    parser.add_argument("--new-exe", type=Path, required=True)
    parser.add_argument("--new-sha256", required=True)
    parser.add_argument("--build-source", choices=("local-observer-validation", "ci", "release"),
                        required=True)
    parser.add_argument("--build-reference", required=True,
                        help="CI run/artifact or release asset reference; provenance supplied by parent")
    parser.add_argument("--source-inputs", type=Path, required=True,
                        help="Parent-supplied frozen source manifest for this exact build")
    args = parser.parse_args()
    require(os.name == "nt", "Windows is required")
    require(OUTPUT.resolve() == OUTPUT, "Output directory must not redirect")
    require(re.fullmatch(r"[0-9a-fA-F]{64}", args.new_sha256) is not None, "Candidate SHA-256 required")
    new_hash = args.new_sha256.lower()
    require(not args.execute or new_hash != HISTORICAL_LOCAL_HASH,
            "Historical b5a7 local candidate is paused; await parent's post-fix artifact")
    old_input, new_input = ROOT / OLD_NAME, args.new_exe.absolute()
    require(new_input != old_input and new_input.suffix.lower() == ".exe", "Distinct candidate EXE required")
    require(args.build_source == "local-observer-validation" or not args.build_reference.startswith("local-"),
            "CI/Release provenance requires the parent's actual run or asset reference")
    manifest = args.source_inputs.absolute()
    rows = frozen_inputs(manifest)
    manifest_hash = digest(manifest)
    require(old_input.resolve() == old_input and digest(old_input) == OLD_SHA256
            and old_input.stat().st_size == 16154324, "Official old input identity mismatch")
    require(new_input.resolve() == new_input and digest(new_input) == new_hash, "Supplied frozen candidate mismatch")
    require(shutil.which("node") is not None and OBSERVER.is_file(), "Existing Node observer runtime missing")
    version = subprocess.run([shutil.which("node"), "-e",
                              "console.log(JSON.stringify({node:process.version,playwright:require('playwright/package.json').version,webSocket:typeof WebSocket}))"],
                             cwd=ROOT, capture_output=True, text=True, check=True, timeout=10)
    runtime = json.loads(version.stdout)
    require(runtime["webSocket"] == "function", "Existing Node runtime lacks WebSocket")
    pinned = subprocess.run(["git", "show", OLD_COMMIT + ":app.py"], cwd=ROOT, capture_output=True,
                            check=True, timeout=10).stdout.decode("utf-8")
    build_batch, helper_text = legacy_helpers(pinned)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="real-native-upgrade-" if args.execute else "native-upgrade-preflight-", dir=OUTPUT))
    report = {"passed": False, "preflightOnly": not args.execute, "nativeAttempts": 0, "sameProblemFailures": 0,
              "priorOtherFailures": {"syntheticUnknownInterruption": 1, "missingChromiumResolvedWithEdge": 1},
              "githubDiscoveryDownload": False, "completeEndToEndUpdate": False, "fakeReady": False,
              "backgroundUpdateCheck": "Natural frontend may check latest; not suppressed or counted as download validation",
              "runtime": runtime, "oldCommit": OLD_COMMIT, "oldInput": str(old_input), "oldSha256": OLD_SHA256,
              "newInput": str(new_input), "newSha256": new_hash, "frozenInputs": rows, "output": str(output),
              "buildSource": args.build_source, "buildReference": args.build_reference,
              "provenanceSuppliedByParent": True, "releaseGatePass": False,
              "sourceInputsManifest": str(manifest), "sourceInputsManifestSha256": manifest_hash,
              "scripts": {str(path.relative_to(ROOT)): digest(path) for path in (Path(__file__), OBSERVER)}, "stages": []}
    (output / "pinned-legacy-helpers.py.txt").write_text(helper_text, encoding="utf-8")
    report["legacyHelperSha256"] = hashlib.sha256(helper_text.encode("utf-8")).hexdigest()
    print(f"Native upgrade evidence: {output}", flush=True)
    try:
        if args.execute:
            report["nativeAttempts"] = 1
            data, program = output / "data", output / "bin"
            data.mkdir()
            program.mkdir()
            old, new, staged = program / OLD_NAME, program / NEW_NAME, output / "candidate.part"
            copy_new(old_input, old, OLD_SHA256)
            copy_new(new_input, staged, new_hash)
            seed = {}

            def seed_action(api, _):
                seed["data"], seed["credentials"] = seed_old(api)

            run_native(output / "official-old-native", data, old, report, seed_action)
            database = data / "task_records.db"
            original = logical_database(database)
            require(checkpoint(database) is None, "Old seed already has candidate checkpoint")
            report["oldDatabase"] = logical_summary(original)

            def candidate_action(api, evidence):
                exported = api.success("/api/export")
                require(exported["appVersion"] == "3.0.6" and exported["schemaVersion"] == 4,
                        "Candidate version/schema mismatch")
                require(api.success("/api/state")["timeContractVersion"] == 2, "Wrong candidate time contract")
                for table in ("accounts", "tasks", "records"):
                    require({row["id"] for row in seed["data"]["export"][table]}
                            <= {row["id"] for row in exported["data"][table]}, f"Candidate lost old {table}")
                require(api.success(f"/api/accounts/{seed['data']['accountId']}/credentials") == seed["credentials"],
                        "Candidate lost synthetic old DPAPI credentials")
                recovery, snapshot, saved, marker = verify_materials(database, new, output, original)
                evidence.update({"recovery": str(recovery), "snapshot": str(snapshot), "savedOldExe": str(saved),
                                 "snapshotSha256": digest(snapshot), "checkpoint": marker, "fullSnapshotExact": True,
                                 "syntheticCredentialsPreserved": True, "savedOldSha256": digest(saved)})

            run_native(output / "legacy-to-candidate-native", data, new, report, candidate_action,
                       batch_builder=build_batch, staged=staged, old=old, new_hash=new_hash)
            report["nativeReadyVerified"] = True
            report["oldCoordinatorExecuted"] = True
        report["passed"] = True
    except Exception as error:
        report["error"], report["traceback"] = repr(error), traceback.format_exc()
        report["sameProblemFailures"] = int(args.execute)
    finally:
        try:
            require(frozen_inputs(manifest) == rows and digest(manifest) == manifest_hash
                    and digest(old_input) == OLD_SHA256 and digest(new_input) == new_hash,
                    "Frozen inputs changed during smoke")
            report["frozenInputsUnchanged"] = True
        except Exception as error:
            report["passed"] = False
            report["inputVerificationError"] = repr(error)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"stages", "frozenInputs"}}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
