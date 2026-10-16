#!/usr/bin/env python3
"""Kabbalah main orchestrator.

Strict pipeline:
    CODE V command log
        -> Recorder
        -> event_tree.json + state_library.json
        -> Compiler
        -> semantic_tree.json
        -> Visualizer
        -> trajectory.html

Main owns orchestration only. It does not reconstruct history, compile semantic
meaning, or render trajectory topology itself.

Interactive maintenance paths:
- unresolved initial-State inheritance -> explicit independent tree or manual
  historical-State selection;
- unresolved semantic Undo -> Recorder withholds commit, Main launches RulesManager,
  then replays the same log;
- trajectory.html "Add New Semantic" -> localhost bridge -> launch
  semantic_library_manager.py in a separate process;
- saved Semantic Library -> recompile the existing Recorder history without
  invoking Recorder again.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


APP_NAME = "Kabbalah"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765

EVENT_TREE_NAME = "event_tree.json"
STATE_LIBRARY_NAME = "state_library.json"
SEMANTIC_TREE_NAME = "semantic_tree.json"
TRAJECTORY_HTML_NAME = "trajectory.html"

RECORDER_FILE = "Recorder.py"
RULES_MANAGER_CANDIDATES = ("RulesManager.py", "rules_manager.py")
RECORDER_RULES_NAME = "recorder_rules.json"
SEMANTIC_LIBRARY_NAME = "semantic_library.json"
SEMANTIC_MANAGER_FILE = "semantic_library_manager.py"
VISUALIZER_FILE = "Visualizer.py"

# During development the current Type-2 compiler may still carry a versioned
# filename. Once the project is consolidated, compiler.py is the normal file.
COMPILER_CANDIDATES = ("compiler_v0.1.3_type2.py", "compiler.py")


class KabbalahError(RuntimeError):
    """Base error for orchestration failures."""


class ComponentMissingError(KabbalahError):
    """Raised when a required Kabbalah component cannot be found."""


class PipelineStageError(KabbalahError):
    """Raised when one strict pipeline stage fails."""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _banner() -> None:
    print("\nKABBALAH")
    print("Optical Design Trajectory Compiler")
    print("=" * 42)


def _print_stage(index: int, total: int, text: str) -> None:
    print(f"\n[{index}/{total}] {text}")


def _print_ok(text: str) -> None:
    print(f"[OK] {text}")


def _print_warning(text: str) -> None:
    print(f"[WARNING] {text}")


def _file_hash(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_module_from_path(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ComponentMissingError(f"Cannot import component: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _temporary_argv(argv: list[str]):
    old = sys.argv[:]
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = old


def _yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        ans = input(prompt + suffix).strip().lower()
        if not ans:
            return default
        if ans in {"y", "yes"}:
            return True
        if ans in {"n", "no"}:
            return False
        print("Please answer Y or N.")


def _choose_log_interactively() -> Path:
    """Use a native file picker when possible, otherwise fall back to console input."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.update()
        selected = filedialog.askopenfilename(
            title="Select CODE V command log",
            filetypes=[("CODE V command log", "*.txt"), ("Text files", "*.txt")],
        )
        root.destroy()
        if selected:
            return Path(selected).expanduser().resolve()
    except Exception:
        pass

    raw = input("CODE V command log (.txt): ").strip().strip('"')
    if not raw:
        raise KabbalahError("No command log selected.")
    return Path(raw).expanduser().resolve()


def _choose_project_dir_interactively(log_path: Path) -> Path:
    default = log_path.parent / f"{log_path.stem}_Kabbalah"

    print("\nSelect the Kabbalah project directory.")
    print("If several command logs belong to the SAME optical design, select the SAME project directory.")
    print("Kabbalah will extend the existing Event Tree / State Library and compile the logs as one trajectory.")
    print("If you select a DIFFERENT or NEW directory, Kabbalah will create a NEW tree instead.")
    print("This is not an error; it only changes how the design history is grouped and compiled.")
    print("A project directory must contain either BOTH event_tree.json and state_library.json, or neither.")

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.update()
        selected = filedialog.askdirectory(
            title="Select Kabbalah project directory",
            initialdir=str(log_path.parent),
            mustexist=False,
        )
        root.destroy()
        if selected:
            return Path(selected).expanduser().resolve()
    except Exception:
        pass

    raw = input(f"Project directory [Enter = {default}]: ").strip().strip('"')
    return Path(raw).expanduser().resolve() if raw else default.resolve()


# ---------------------------------------------------------------------------
# Component resolution
# ---------------------------------------------------------------------------


def _required_path(base: Path, name: str) -> Path:
    path = base / name
    if not path.is_file():
        raise ComponentMissingError(f"Missing required component: {path}")
    return path


def _find_rules_manager(base: Path) -> Path:
    for name in RULES_MANAGER_CANDIDATES:
        p = base / name
        if p.is_file():
            return p
    raise ComponentMissingError(
        "Rules Manager is required for unresolved semantic Undo handling. "
        f"Expected one of: {', '.join(RULES_MANAGER_CANDIDATES)}"
    )


def _find_compiler(base: Path) -> Path:
    for name in COMPILER_CANDIDATES:
        p = base / name
        if p.is_file():
            return p
    raise ComponentMissingError(
        "Compiler not found. Expected one of: " + ", ".join(COMPILER_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# Rules Manager child UI
# ---------------------------------------------------------------------------


def _read_multiline_until_end() -> str:
    print("Paste the raw CODE V log snippet for this semantic Undo.")
    print("Finish with END on its own line.")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines)


def _rules_manager_child(
    manager_path: Path,
    rules_path: Path,
    target: str,
    line_number: Optional[str],
) -> int:
    """Small interaction shell; all rule parsing/writing stays in RulesManager."""
    print("\nKabbalah - Semantic Undo Rules Manager")
    print("=" * 40)
    if target:
        print(f"Unresolved Undo target: {target}")
    if line_number:
        print(f"Raw log line: {line_number}")
    print(f"Rules: {rules_path}\n")

    module = _load_module_from_path("kabbalah_rules_manager_child", manager_path)

    # If a later RulesManager version provides its own interactive entry point,
    # prefer it and keep Main out of its UI details.
    if hasattr(module, "run_interactive"):
        fn = module.run_interactive
        try:
            fn(rules_path, target=target or None)
        except TypeError:
            try:
                fn(rules_path)
            except TypeError:
                fn()
        return 0

    manager_cls = getattr(module, "RulesManager", None)
    if manager_cls is None:
        print("RulesManager.py exposes neither run_interactive() nor RulesManager class.")
        return 2

    manager = manager_cls(rules_path)
    if hasattr(manager, "load"):
        manager.load()
    if hasattr(manager, "validate"):
        manager.validate()

    snippet = _read_multiline_until_end()
    if not snippet.strip():
        print("No snippet entered. Nothing was changed.")
        return 3

    try:
        preview = manager.parse_log_snippet(snippet)
    except Exception as exc:
        print(f"Could not parse snippet: {exc}")
        return 4

    print("\nParsed rule preview:")
    print(preview)
    if not _yes_no("Add this semantic Undo rule?", default=False):
        print("Cancelled. Nothing was changed.")
        return 3

    try:
        manager.add_from_log_snippet(snippet)
        manager.save()
    except Exception as exc:
        print(f"Could not save rule: {exc}")
        return 5

    print("Rule saved successfully. You may close this window.")
    return 0


def _self_python_command() -> list[str]:
    # Source mode now; this shape is also ready for a future single-file bundle.
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve())]

# ---------------------------------------------------------------------------
# Recorder stage
# ---------------------------------------------------------------------------


def _unresolved_semantic_undos(result: Any) -> list[Any]:
    diagnostics = getattr(result, "diagnostics", []) or []
    return [d for d in diagnostics if getattr(d, "code", None) == "UNRESOLVED_SEMANTIC_UNDO"]


def _run_recorder_once(
    recorder_module: Any,
    log_path: Path,
    rules_path: Path,
    event_tree_path: Path,
    state_library_path: Path,
    *,
    initial_state_override: Optional[str] = None,
    start_new_root: bool = False,
):
    storage = recorder_module.StorageContext(
        event_tree_path=event_tree_path,
        state_library_path=state_library_path,
    )
    recorder = recorder_module.Recorder(storage, rules_path)
    return recorder.process_log(
        log_path,
        initial_state_override=initial_state_override,
        start_new_root=start_new_root,
    )


def _choose_initial_state_resolution(
    resolution: Any,
    *,
    non_interactive: bool,
) -> tuple[Optional[str], bool]:
    """Ask for a continuation decision when Recorder found no exact RES match.

    Main displays Recorder-owned information and returns the user's choice.  It
    never reconstructs ancestry and never reads or writes Recorder persistence.
    """

    status = getattr(resolution, "status", None)
    if status == "MATCHED":
        return None, False
    if status not in {"NO_RES", "RES_UNMATCHED"}:
        raise PipelineStageError(f"Unexpected initial-State preflight status: {status}")

    if non_interactive:
        raise PipelineStageError(
            "Initial-State inheritance was not resolved automatically. "
            "An explicit continuation decision is required before Recorder can persist this log."
        )

    if status == "NO_RES":
        print("\nNo automatic initial-State inheritance was found.")
    else:
        print("\nThe initial lens State could not be resolved automatically.")

    while True:
        print("\nChoose how to continue:")
        print("[1] Continue from an existing State")
        print("[2] Start as an independent tree")
        choice = input("Select 1 or 2: ").strip()

        if choice == "1":
            return input("Enter the State ID to inherit from: ").strip(), False
        if choice == "2":
            return None, True
        print("Please select exactly 1 or 2.")


def _spawn_python_script(
    script: Path,
    args: list[str],
    *,
    wait: bool,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.Popen:
    cmd = [sys.executable, str(script), *args]
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    kwargs: dict[str, Any] = {"env": env}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    proc = subprocess.Popen(cmd, **kwargs)
    if wait:
        proc.wait()
    return proc


def _run_rules_manager_blocking(
    manager_path: Path,
    rules_path: Path,
    unresolved: Any,
) -> bool:
    """Launch Rules Manager as an independent process and wait for user completion."""
    target = str(getattr(unresolved, "target", "") or "")
    line_number = getattr(unresolved, "raw_line_number", None)

    print("\nRecorder requires a semantic Undo rule.")
    if target:
        print(f"Undo target: {target}")
    if line_number is not None:
        print(f"Raw log line: {line_number}")
    print("Opening Rules Manager. Add the missing rule, save it, then close the manager.")

    before = _file_hash(rules_path)
    env = {
        "KABBALAH_UNRESOLVED_UNDO_TARGET": target,
        "KABBALAH_UNRESOLVED_UNDO_LINE": "" if line_number is None else str(line_number),
        "KABBALAH_RECORDER_RULES": str(rules_path),
    }

    cmd = [
        *_self_python_command(),
        "--_rules-manager-child",
        "--rules-manager", str(manager_path),
        "--recorder-rules", str(rules_path),
        "--_undo-target", target,
        "--_undo-line", "" if line_number is None else str(line_number),
    ]
    kwargs: dict[str, Any] = {"env": env}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    proc = subprocess.Popen(cmd, **kwargs)
    proc.wait()
    if proc.returncode != 0:
        _print_warning(f"Rules Manager exited with code {proc.returncode}.")
        return False

    after = _file_hash(rules_path)
    if after == before:
        _print_warning("Recorder rules were not changed; replay cannot safely continue.")
        return False
    return True


def run_recorder_with_rule_recovery(
    recorder_module: Any,
    rules_manager_path: Optional[Path],
    log_path: Path,
    rules_path: Path,
    event_tree_path: Path,
    state_library_path: Path,
    *,
    non_interactive: bool = False,
) -> Any:
    """Resolve initial inheritance, then replay with semantic-Undo recovery.

    Main owns orchestration and user interaction only.  Recorder owns all Event
    Tree / State Library validation, topology construction, and persistence.
    """

    storage = recorder_module.StorageContext(
        event_tree_path=event_tree_path,
        state_library_path=state_library_path,
    )
    preflight_recorder = recorder_module.Recorder(storage, rules_path)
    resolution = preflight_recorder.inspect_initial_state(log_path)
    initial_state_override, start_new_root = _choose_initial_state_resolution(
        resolution,
        non_interactive=non_interactive,
    )

    attempt = 0
    while True:
        try:
            result = _run_recorder_once(
                recorder_module,
                log_path,
                rules_path,
                event_tree_path,
                state_library_path,
                initial_state_override=initial_state_override,
                start_new_root=start_new_root,
            )
        except recorder_module.InvalidInitialStateError as exc:
            if non_interactive:
                raise PipelineStageError(str(exc)) from exc
            _print_warning(str(exc))
            initial_state_override, start_new_root = _choose_initial_state_resolution(
                resolution,
                non_interactive=False,
            )
            continue

        attempt += 1

        unresolved = _unresolved_semantic_undos(result)
        if not unresolved:
            return result

        first = unresolved[0]
        _print_warning(
            f"Recorder found {len(unresolved)} unresolved semantic Undo diagnostic(s). "
            "Recorder did not commit the provisional replay."
        )

        if rules_manager_path is None:
            raise ComponentMissingError(
                "Recorder found an unresolved semantic Undo, but RulesManager.py is not available. "
                "Place RulesManager.py next to Kabbalah_main.py and rerun the same log."
            )

        if not _run_rules_manager_blocking(rules_manager_path, rules_path, first):
            raise PipelineStageError(
                "Semantic Undo rule was not added. Recorder history remains unchanged."
            )

        if attempt >= 50:
            raise PipelineStageError(
                "Recorder exceeded the semantic Undo recovery retry limit."
            )

        print("Rules updated. Replaying the original command log from unchanged Recorder history...")


# ---------------------------------------------------------------------------
# Compiler / Visualizer stages
# ---------------------------------------------------------------------------


def _run_compiler_imported(
    compiler_path: Path,
    event_tree_path: Path,
    state_library_path: Path,
    semantic_library_path: Path,
    semantic_tree_path: Path,
    *,
    delete_layout_views: bool,
) -> None:
    module = _load_module_from_path("kabbalah_compiler", compiler_path)
    if not hasattr(module, "main"):
        raise PipelineStageError(f"Compiler has no main() entry point: {compiler_path}")

    argv = [
        str(compiler_path),
        "--event-tree", str(event_tree_path),
        "--state-library", str(state_library_path),
        "--semantic-library", str(semantic_library_path),
        "--output", str(semantic_tree_path),
    ]
    if not delete_layout_views:
        argv.append("--keep-observations")

    with _temporary_argv(argv):
        module.main()

    if not semantic_tree_path.is_file():
        raise PipelineStageError("Compiler returned without creating semantic_tree.json.")


def _run_visualizer_imported(
    visualizer_path: Path,
    semantic_tree_path: Path,
    trajectory_html_path: Path,
) -> None:
    module = _load_module_from_path("kabbalah_visualizer", visualizer_path)
    if not hasattr(module, "main"):
        raise PipelineStageError(f"Visualizer has no main() entry point: {visualizer_path}")

    argv = [
        str(visualizer_path),
        "--input", str(semantic_tree_path),
        "--output", str(trajectory_html_path),
    ]
    with _temporary_argv(argv):
        module.main()

    if not trajectory_html_path.is_file():
        raise PipelineStageError("Visualizer returned without creating trajectory.html.")


# ---------------------------------------------------------------------------
# Semantic Library bridge
# ---------------------------------------------------------------------------


def _launch_semantic_manager(semantic_manager_path: Path, semantic_library_path: Path) -> None:
    _spawn_python_script(
        semantic_manager_path,
        ["--library", str(semantic_library_path)],
        wait=False,
    )


class _BridgeHandler(BaseHTTPRequestHandler):
    server_version = "KabbalahBridge/0.1"

    def _cors(self) -> None:
        # trajectory.html is normally opened from file:// and therefore needs
        # explicit CORS permission to POST to localhost.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/add-semantic":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        try:
            launcher = getattr(self.server, "semantic_launcher")
            launcher()
        except Exception as exc:
            self._reply(500, {"ok": False, "error": str(exc)})
            return
        self._reply(200, {"ok": True})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the Main console readable. Bridge requests are intentionally quiet.
        return


class SemanticBridge:
    def __init__(self, launcher, host: str = BRIDGE_HOST, port: int = BRIDGE_PORT):
        self.launcher = launcher
        self.host = host
        self.port = port
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        try:
            httpd = ThreadingHTTPServer((self.host, self.port), _BridgeHandler)
        except OSError as exc:
            raise KabbalahError(
                f"Cannot start local semantic bridge at http://{self.host}:{self.port}. "
                "Another Kabbalah instance may already be running."
            ) from exc
        setattr(httpd, "semantic_launcher", self.launcher)
        self.httpd = httpd
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
        if self.thread is not None:
            self.thread.join(timeout=2.0)
            self.thread = None


# ---------------------------------------------------------------------------
# Summary / project helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _semantic_summary(path: Path) -> dict[str, int]:
    try:
        tree = _read_json(path)
    except Exception:
        return {}
    nodes = tree.get("nodes", {}) or {}
    operations = [n for n in nodes.values() if n.get("type") == "operation"]
    states = [n for n in nodes.values() if n.get("type") == "state"]
    raw = sum(1 for n in operations if (n.get("canonical") or {}).get("operation_type") == "raw_command")
    side_edges = sum(
        1
        for n in nodes.values()
        for child in n.get("children", []) or []
        if child.get("role") == "side"
    )
    return {
        "operations": len(operations),
        "states": len(states),
        "raw": raw,
        "branches": side_edges,
    }


def _recorder_summary(result: Any) -> dict[str, int]:
    return {
        "events_created": len(getattr(result, "events_created", []) or []),
        "states_created": len(getattr(result, "states_created", []) or []),
        "raw_lines": int(getattr(result, "total_raw_lines", 0) or 0),
    }


def _validate_log_path(path: Path) -> None:
    if path.suffix.lower() != ".txt":
        raise KabbalahError(f"CODE V command log must be a .txt file: {path}")
    if not path.is_file():
        raise KabbalahError(f"CODE V command log not found: {path}")


def _project_paths(project_dir: Path) -> dict[str, Path]:
    return {
        "event_tree": project_dir / EVENT_TREE_NAME,
        "state_library": project_dir / STATE_LIBRARY_NAME,
        "semantic_tree": project_dir / SEMANTIC_TREE_NAME,
        "trajectory": project_dir / TRAJECTORY_HTML_NAME,
    }


def _validate_project_history_pair(project_dir: Path) -> bool:
    """Validate the canonical Recorder persistence pair for one Kabbalah project.

    Returns True when an existing history pair is present, False for a new project.
    Kabbalah never attempts to reconstruct from a half-present persistence pair.
    """

    event_tree = project_dir / EVENT_TREE_NAME
    state_library = project_dir / STATE_LIBRARY_NAME

    event_exists = event_tree.exists()
    state_exists = state_library.exists()

    if event_exists != state_exists:
        missing = state_library if event_exists else event_tree
        present = event_tree if event_exists else state_library
        raise KabbalahError(
            "Incomplete Kabbalah project history detected. "
            f"Found {present.name} but missing {missing.name}. "
            "Recorder was not started. Restore the matching persistence file, "
            "or choose a different/new project directory."
        )

    if event_exists:
        if not event_tree.is_file() or not state_library.is_file():
            raise KabbalahError(
                "Kabbalah project persistence paths must be normal files: "
                f"{event_tree} and {state_library}"
            )
        return True

    return False


def _recompile_existing_history(
    project_dir: Path,
    compiler_path: Path,
    semantic_library_path: Path,
    visualizer_path: Path,
    *,
    delete_layout_views: bool,
) -> None:
    """Call Compiler and Visualizer on an existing, unchanged Recorder history."""
    if not _validate_project_history_pair(project_dir):
        raise KabbalahError(
            "Recompilation requires existing event_tree.json and state_library.json "
            f"in the project directory: {project_dir}"
        )
    if not compiler_path.is_file():
        raise ComponentMissingError(f"Compiler not found: {compiler_path}")
    if not semantic_library_path.is_file():
        raise ComponentMissingError(f"Semantic Library not found: {semantic_library_path}")
    if not visualizer_path.is_file():
        raise ComponentMissingError(f"Visualizer not found: {visualizer_path}")

    paths = _project_paths(project_dir)
    _print_stage(1, 2, "Recompiling existing design history with the current Semantic Library...")
    _run_compiler_imported(
        compiler_path,
        paths["event_tree"],
        paths["state_library"],
        semantic_library_path,
        paths["semantic_tree"],
        delete_layout_views=delete_layout_views,
    )
    _print_ok("Compiler completed: semantic_tree.json updated.")

    _print_stage(2, 2, "Refreshing trajectory visualization...")
    _run_visualizer_imported(
        visualizer_path,
        paths["semantic_tree"],
        paths["trajectory"],
    )
    _print_ok("Visualizer completed: trajectory.html updated.")
    sem_stats = _semantic_summary(paths["semantic_tree"])
    if sem_stats:
        print(
            "Trajectory summary: "
            f"{sem_stats['operations']} operation(s), "
            f"{sem_stats['states']} State(s), "
            f"{sem_stats['branches']} side branch(es), "
            f"{sem_stats['raw']} raw/untranslated operation(s)."
        )


def run_recompile(args: argparse.Namespace) -> int:
    """Standalone Main entry point; never load or invoke Recorder."""
    if not args.project_dir:
        raise KabbalahError("--recompile-only requires --project-dir with an existing Kabbalah project.")
    if args.log:
        raise KabbalahError("--recompile-only does not accept --log; no raw log is replayed.")

    base = _script_dir()
    project_dir = Path(args.project_dir).expanduser().resolve()
    compiler_path = Path(args.compiler).expanduser().resolve() if args.compiler else _find_compiler(base)
    semantic_library_path = Path(args.semantic_library).expanduser().resolve() if args.semantic_library else base / SEMANTIC_LIBRARY_NAME
    visualizer_path = base / VISUALIZER_FILE

    print(f"\nExisting project: {project_dir}")
    print(f"Semantic Library: {semantic_library_path}")
    _recompile_existing_history(
        project_dir,
        compiler_path,
        semantic_library_path,
        visualizer_path,
        delete_layout_views=not args.keep_layout_views,
    )
    print("\nRecompilation completed. Recorder history was not replayed.")
    print(f"Semantic Tree: {project_dir / SEMANTIC_TREE_NAME}")
    print(f"Trajectory:    {project_dir / TRAJECTORY_HTML_NAME}")
    return 0


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_pipeline(args: argparse.Namespace) -> int:
    base = _script_dir()

    log_path = Path(args.log).expanduser().resolve() if args.log else _choose_log_interactively()
    _validate_log_path(log_path)

    if args.project_dir:
        project_dir = Path(args.project_dir).expanduser().resolve()
    else:
        project_dir = _choose_project_dir_interactively(log_path)
    project_dir.mkdir(parents=True, exist_ok=True)
    existing_history = _validate_project_history_pair(project_dir)

    delete_layout_views = not args.keep_layout_views
    if args.log is None and not args.non_interactive:
        delete_layout_views = _yes_no(
            "Delete repeated lens-layout views? (MTF, PSF, tolerancing, and other analysis operations are preserved.)",
            default=True,
        )

    recorder_path = _required_path(base, RECORDER_FILE)
    recorder_rules_path = Path(args.recorder_rules).expanduser().resolve() if args.recorder_rules else _required_path(base, RECORDER_RULES_NAME)
    semantic_library_path = Path(args.semantic_library).expanduser().resolve() if args.semantic_library else _required_path(base, SEMANTIC_LIBRARY_NAME)
    visualizer_path = _required_path(base, VISUALIZER_FILE)
    semantic_manager_path = _required_path(base, SEMANTIC_MANAGER_FILE)
    compiler_path = Path(args.compiler).expanduser().resolve() if args.compiler else _find_compiler(base)
    if args.rules_manager:
        rules_manager_path: Optional[Path] = Path(args.rules_manager).expanduser().resolve()
    else:
        rules_manager_path = None
        for candidate_name in RULES_MANAGER_CANDIDATES:
            candidate = base / candidate_name
            if candidate.is_file():
                rules_manager_path = candidate
                break

    if not recorder_rules_path.is_file():
        raise ComponentMissingError(f"Recorder rules not found: {recorder_rules_path}")
    if not semantic_library_path.is_file():
        raise ComponentMissingError(f"Semantic Library not found: {semantic_library_path}")
    if not compiler_path.is_file():
        raise ComponentMissingError(f"Compiler not found: {compiler_path}")
    if rules_manager_path is not None and not rules_manager_path.is_file():
        raise ComponentMissingError(f"Rules Manager not found: {rules_manager_path}")

    paths = _project_paths(project_dir)

    print(f"\nInput log:   {log_path}")
    print(f"Project dir: {project_dir}")
    if existing_history:
        print("Existing Recorder history detected: this log will extend the selected project history.")
    else:
        print("No Recorder history detected: this directory will start a new Kabbalah tree.")

    recorder_module = _load_module_from_path("kabbalah_recorder", recorder_path)

    _print_stage(1, 3, "Reconstructing design history...")
    result = run_recorder_with_rule_recovery(
        recorder_module,
        rules_manager_path,
        log_path,
        recorder_rules_path,
        paths["event_tree"],
        paths["state_library"],
        non_interactive=args.non_interactive,
    )
    rec_stats = _recorder_summary(result)
    _print_ok(
        f"Recorder completed: {rec_stats['events_created']} Event(s), "
        f"{rec_stats['states_created']} State(s) created from {rec_stats['raw_lines']} raw line(s)."
    )

    # Strict boundary: do not invoke Compiler until both Recorder artifacts exist.
    if not paths["event_tree"].is_file() or not paths["state_library"].is_file():
        raise PipelineStageError(
            "Recorder completed without both event_tree.json and state_library.json. Compiler was not started."
        )

    _print_stage(2, 3, "Compiling semantic trajectory...")
    _run_compiler_imported(
        compiler_path,
        paths["event_tree"],
        paths["state_library"],
        semantic_library_path,
        paths["semantic_tree"],
        delete_layout_views=delete_layout_views,
    )
    _print_ok("Compiler completed: semantic_tree.json created.")

    # Strict boundary: Visualizer is not invoked until Semantic Tree exists.
    if not paths["semantic_tree"].is_file():
        raise PipelineStageError(
            "Compiler did not create semantic_tree.json. Visualizer was not started."
        )

    _print_stage(3, 3, "Generating trajectory visualization...")
    _run_visualizer_imported(
        visualizer_path,
        paths["semantic_tree"],
        paths["trajectory"],
    )
    _print_ok("Visualizer completed: trajectory.html created.")

    sem_stats = _semantic_summary(paths["semantic_tree"])

    bridge: Optional[SemanticBridge] = None
    if not args.no_bridge:
        bridge = SemanticBridge(
            lambda: _launch_semantic_manager(semantic_manager_path, semantic_library_path)
        )
        bridge.start()
        _print_ok(f"Semantic bridge active at http://{BRIDGE_HOST}:{BRIDGE_PORT}/add-semantic")

    if not args.no_browser:
        try:
            webbrowser.open(paths["trajectory"].resolve().as_uri())
            _print_ok("Opened trajectory.html in the default browser.")
        except Exception as exc:
            _print_warning(f"Could not open the default browser automatically: {exc}")

    print("\nKabbalah completed successfully.")
    print(f"Event Tree:     {paths['event_tree']}")
    print(f"State Library:  {paths['state_library']}")
    print(f"Semantic Tree:  {paths['semantic_tree']}")
    print(f"Trajectory:     {paths['trajectory']}")
    if sem_stats:
        print(
            "Trajectory summary: "
            f"{sem_stats['operations']} operation(s), "
            f"{sem_stats['states']} State(s), "
            f"{sem_stats['branches']} side branch(es), "
            f"{sem_stats['raw']} raw/untranslated operation(s)."
        )

    if bridge is not None:
        if args.non_interactive:
            # Automated tests should not block forever.
            bridge.stop()
        else:
            print("\nKabbalah is active.")
            print('Use "Add New Semantic" in trajectory.html to open the Semantic Library Manager.')
            if sys.stdin.isatty():
                print("After saving a new semantic annotation, type R here to recompile without replaying the log.")
                print("Type Q or press Ctrl+C when you are finished.")
            else:
                print("Press Ctrl+C here when you are finished.")
            try:
                while True:
                    if not sys.stdin.isatty():
                        time.sleep(0.5)
                        continue
                    command = input("Kabbalah [R=recompile, Q=quit]: ").strip().lower()
                    if command in {"q", "quit", "exit"}:
                        break
                    if command not in {"r", "recompile"}:
                        if command:
                            print("Type R to recompile or Q to quit.")
                        continue
                    try:
                        _recompile_existing_history(
                            project_dir,
                            compiler_path,
                            semantic_library_path,
                            visualizer_path,
                            delete_layout_views=delete_layout_views,
                        )
                        print("Refresh the open trajectory.html page to see the updated semantics.")
                    except Exception as exc:
                        _print_warning(f"Recompilation failed; Kabbalah remains active: {exc}")
                        if os.environ.get("KABBALAH_DEBUG") == "1":
                            traceback.print_exc()
            except KeyboardInterrupt:
                print("\nClosing Kabbalah...")
            except EOFError:
                print("\nClosing Kabbalah...")
            finally:
                bridge.stop()

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kabbalah - Optical Design Trajectory Compiler"
    )
    parser.add_argument("--log", help="CODE V command log .txt path")
    parser.add_argument(
        "--recompile-only",
        action="store_true",
        help="Recompile an existing project's Event Tree with the current Semantic Library, without Recorder.",
    )
    parser.add_argument(
        "--project-dir",
        help=(
            "Kabbalah project directory. Reuse the same directory for logs that "
            "belong to the same persistent design history."
        ),
    )
    parser.add_argument(
        "--keep-layout-views",
        action="store_true",
        help="Keep repeated VIE/V3D layout-view transactions in the Semantic Tree.",
    )
    parser.add_argument("--recorder-rules", help="Override recorder_rules.json path")
    parser.add_argument("--semantic-library", help="Override semantic_library.json path")
    parser.add_argument("--compiler", help="Override compiler script path")
    parser.add_argument("--rules-manager", help="Override RulesManager.py path")
    parser.add_argument("--no-browser", action="store_true", help="Do not open trajectory.html automatically")
    parser.add_argument("--no-bridge", action="store_true", help="Do not start the Add New Semantic localhost bridge")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Testing mode: never wait for user input after the pipeline finishes.",
    )
    parser.add_argument("--_rules-manager-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_undo-target", default="", help=argparse.SUPPRESS)
    parser.add_argument("--_undo-line", default="", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    _banner()
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args._rules_manager_child:
            if not args.rules_manager or not args.recorder_rules:
                raise KabbalahError("Rules Manager child mode requires manager and rules paths.")
            return _rules_manager_child(
                Path(args.rules_manager).expanduser().resolve(),
                Path(args.recorder_rules).expanduser().resolve(),
                args._undo_target,
                args._undo_line or None,
            )
        if args.recompile_only:
            return run_recompile(args)
        return run_pipeline(args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except KabbalahError as exc:
        print(f"\nKabbalah stopped safely:\n{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nUnexpected Kabbalah error: {exc}", file=sys.stderr)
        if os.environ.get("KABBALAH_DEBUG") == "1":
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
