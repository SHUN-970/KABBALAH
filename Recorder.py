"""Deterministic recorder for raw CODE V command logs.

The recorder replays one ``.txt`` command log at a time and persists two
independent data sets: an Event Tree and a State Library.  It intentionally
does not call CODE V, inspect lens files, calculate optical quantities, or
modify the semantic Undo rule library.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Optional


PERSISTENCE_VERSION = 1
RULES_VERSION = 1


class RecorderError(Exception):
    """Base exception for Recorder failures."""


class RecorderConfigurationError(RecorderError):
    """Raised when runtime configuration is invalid."""


class RulesFileError(RecorderConfigurationError):
    """Raised when the read-only semantic Undo rules cannot be loaded."""


class PersistenceError(RecorderError):
    """Raised when persistent trajectory data is invalid or cannot be saved."""


class LogInputError(RecorderError):
    """Raised when the design-data input is not a readable ``.txt`` log."""


class InitialStateResolutionRequired(RecorderError):
    """Raised when initial State inheritance requires an explicit decision."""

    def __init__(self, resolution: "InitialStateResolution") -> None:
        self.resolution = resolution
        super().__init__(
            "Initial State inheritance was not resolved automatically. "
            "Choose an existing State or explicitly start an independent tree."
        )


class InvalidInitialStateError(RecorderConfigurationError):
    """Raised when a manually selected State ID is absent or invalid."""


@dataclass(frozen=True)
class StorageContext:
    """Locations of the two logical Recorder persistence files."""

    event_tree_path: Path
    state_library_path: Path

    def __post_init__(self) -> None:
        """Normalize explicit path-like fields while keeping the context immutable."""

        try:
            event_path = Path(os.fspath(self.event_tree_path))
            state_path = Path(os.fspath(self.state_library_path))
        except TypeError as exc:
            raise RecorderConfigurationError(
                "StorageContext paths must be valid filesystem path values."
            ) from exc
        object.__setattr__(self, "event_tree_path", event_path)
        object.__setattr__(self, "state_library_path", state_path)

    @classmethod
    def from_value(
        cls,
        value: "StorageContext | str | os.PathLike[str] | Mapping[str, Any]",
    ) -> "StorageContext":
        """Build a context from a directory, mapping, or existing context."""

        if isinstance(value, cls):
            context = value
        elif isinstance(value, Mapping):
            try:
                event_path = Path(os.fspath(value["event_tree_path"]))
                state_path = Path(os.fspath(value["state_library_path"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise RecorderConfigurationError(
                    "storage_context mapping must contain valid "
                    "'event_tree_path' and 'state_library_path' values."
                ) from exc
            context = cls(event_path, state_path)
        else:
            try:
                root = Path(os.fspath(value))
            except TypeError as exc:
                raise RecorderConfigurationError(
                    "storage_context must be a directory path, StorageContext, "
                    "or a mapping of persistence paths."
                ) from exc
            context = cls(
                event_tree_path=root / "event_tree.json",
                state_library_path=root / "state_library.json",
            )

        if _same_path(context.event_tree_path, context.state_library_path):
            raise RecorderConfigurationError(
                "Event Tree and State Library must use different files."
            )
        return context


@dataclass(frozen=True)
class EventRecord:
    """One retained CODE V command in the persistent Event Tree."""

    event_id: str
    parent_event_id: Optional[str]
    log_id: str
    raw_command: str
    raw_line_number: int


@dataclass(frozen=True)
class CommandLogRecord:
    """Metadata for one command log accepted by the Recorder."""

    log_id: str
    initial_state_id: Optional[str]
    log_path: str


@dataclass(frozen=True)
class StateRecord:
    """A captured AUT State or path-only SAV State."""

    state_id: str
    created_by_event_id: str
    log_id: str
    file_path: Optional[str] = None
    cycle_number: Optional[int] = None
    error_function_value: Optional[float] = None
    error_function_construction: Optional[list[str]] = None
    lens_data: Optional[list[str]] = None
    first_order_header: Optional[str] = None
    first_order_values: Optional[str] = None


@dataclass(frozen=True)
class InitialStateResolution:
    """Result of resolving the initial State of a new command log."""

    status: str
    res_line_number: Optional[int]
    res_file_path: Optional[str]
    initial_state_id: Optional[str]


@dataclass(frozen=True)
class DiagnosticRecord:
    """A structured, non-fatal replay diagnostic."""

    level: str
    code: str
    message: str
    raw_line_number: Optional[int] = None
    target: Optional[str] = None


@dataclass(frozen=True)
class ProcessResult:
    """Result returned after a command log is replayed and persisted."""

    command_log: CommandLogRecord
    initial_state: InitialStateResolution
    events_created: list[EventRecord]
    states_created: list[StateRecord]
    active_path: list[str]
    diagnostics: list[DiagnosticRecord]
    total_raw_lines: int
    auto_output_line_count: int
    auto_output_event_leak_count: int
    auto_output_active_after_replay: bool
    pending_aut_go_event_id: Optional[str]
    redo_depth_after_replay: int

    @property
    def active_head(self) -> Optional[str]:
        """Return the tail of the reconstructed active history."""

        return self.active_path[-1] if self.active_path else None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the result."""

        result = asdict(self)
        result["active_head"] = self.active_head
        return result


class LineType(str, Enum):
    """Frozen command-log classifier result types."""

    SKIP = "SKIP"
    COMMAND = "COMMAND"
    UNDO = "UNDO"
    REDO = "REDO"


def _same_path(left: Path, right: Path) -> bool:
    """Compare configuration paths without requiring them to exist."""

    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def is_separator_line(line: str) -> bool:
    """Return whether a line is made only from visual separator characters."""

    stripped = line.strip()
    return len(stripped) >= 3 and all(ch in "-=#*_" for ch in stripped)


def starts_with_timestamp(line: str) -> bool:
    """Return whether a line starts with a CODE V-style clock value."""

    return bool(re.match(r"^\d{1,2}:\d{2}:\d{2}\b", line))


def _is_duration_clock_line(line: str) -> bool:
    """Return whether a line is a PDF-wrapped CODE V duration clock."""

    return bool(re.fullmatch(r"[0-9]+H[ \t]+[0-9]+M[ \t]+[0-9]+S[ \t]*", line))


def strip_inline_comment(line: str) -> str:
    """Remove an inline ``!`` comment from an already classified command."""

    if "!" not in line:
        return line.strip()
    return line.split("!", 1)[0].strip()


def _parse_history_target(line: str, marker: str) -> tuple[bool, Optional[str]]:
    """Parse a CODE V history target without erasing target indentation.

    CODE V inserts two spaces between the ``! Undo:``/``! Redo:`` marker and
    the original command.  Any whitespace remaining after that fixed display
    separator belonged to the original command, so the normal classifier
    would have skipped it as indented output.
    """

    candidate = line.lstrip()
    if not candidate.startswith(marker):
        return False, None

    target = candidate[len(marker) :].rstrip()
    if target.startswith("  "):
        target = target[2:]
    elif target[:1].isspace():
        # Tolerate a one-character separator in manually assembled snippets.
        target = target[1:]

    if not target or target[0].isspace():
        return True, None
    return True, target


def classify_line(line: str) -> tuple[LineType, Optional[str]]:
    """Classify one normal-region command-log line."""

    is_undo, undo_target = _parse_history_target(line, "! Undo:")
    if is_undo:
        return (
            (LineType.UNDO, undo_target)
            if undo_target is not None
            else (LineType.SKIP, None)
        )
    is_redo, redo_target = _parse_history_target(line, "! Redo:")
    if is_redo:
        return (
            (LineType.REDO, redo_target)
            if redo_target is not None
            else (LineType.SKIP, None)
        )

    stripped = line.strip()
    if stripped == "":
        return LineType.SKIP, None
    if is_separator_line(line):
        return LineType.SKIP, None
    if line.startswith("CODE V"):
        return LineType.SKIP, None
    if starts_with_timestamp(line):
        return LineType.SKIP, None
    if _is_duration_clock_line(line):
        return LineType.SKIP, None
    if line and line[0].isspace():
        return LineType.SKIP, None
    if line.startswith("!"):
        return LineType.COMMAND, line.strip()
    if line and line[0].islower():
        command = strip_inline_comment(line)
        return (LineType.COMMAND, command) if command else (LineType.SKIP, None)
    if re.match(r"^[A-Z][a-z]", line):
        return LineType.SKIP, None

    command = strip_inline_comment(line)
    return (LineType.COMMAND, command) if command else (LineType.SKIP, None)


def get_save_file_path(command: str) -> Optional[str]:
    """Extract the exact quoted path from an uppercase SAV command."""

    match = re.match(r'^SAV\s+"([^"]+)"\s*;?\s*$', command.strip())
    return match.group(1) if match else None


def get_res_file_path(line: str) -> Optional[str]:
    """Extract an exact path from RES or a ``CODE V> RES`` echo only."""

    text = line.strip()
    if text.startswith("CODE V>"):
        text = text[len("CODE V>") :].strip()
    match = re.match(
        r'^RES\s+"([^"]+)"\s*;?\s*$',
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def find_initial_res(raw_lines: Sequence[str]) -> Optional[dict[str, Any]]:
    """Return metadata for the first valid RES command, if one exists."""

    for line_number, line in enumerate(raw_lines, start=1):
        file_path = get_res_file_path(line)
        if file_path is not None:
            return {
                "line_number": line_number,
                "file_path": file_path,
                "raw_line": line,
            }
    return None


def _get_cycle_number(line: str) -> Optional[int]:
    match = re.match(r"^\s*CYCLE NUMBER\s+(\d+):", line)
    return int(match.group(1)) if match else None


def _get_error_function_value(line: str) -> Optional[float]:
    match = re.search(
        r"ERR\.\s*F\.\s*=\s*"
        r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)",
        line,
    )
    return float(match.group(1)) if match else None


def _is_obj_start(line: str) -> bool:
    return bool(re.match(r"^>?\s*OBJ:", line.lstrip()))


def _is_img_end(line: str) -> bool:
    return line.lstrip().startswith("IMG:")


def _is_auto_completion(line: str) -> bool:
    """Return whether an output line contains the stable completion marker."""

    return re.search(r"\bAUTO\s+Completion\b", line) is not None


_CANONICAL_UNDO_PLACEHOLDER_RE = re.compile(
    r"\{(?P<kind>index|value)(?:_(?P<number>\d+))?\}"
)


def _compile_semantic_matcher(matcher: str) -> Optional[re.Pattern[str]]:
    """Compile a canonical numeric semantic Undo matcher.

    Legacy exact ``XXX; CAN[;]`` rules are not compiled and continue to use exact
    string equality.  Canonical rules may contain ``{index}``, ``{index_2}``,
    ``{value}``, etc.
    """

    matches = list(_CANONICAL_UNDO_PLACEHOLDER_RE.finditer(matcher))
    if not matches:
        return None
    pieces: list[str] = []
    pos = 0
    for match in matches:
        pieces.append(
            re.escape(matcher[pos:match.start()]).replace(r"\ ", r"\s+")
        )
        if match.group("kind") == "index":
            pieces.append(r"\d+")
        else:
            pieces.append(
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
            )
        pos = match.end()
    pieces.append(re.escape(matcher[pos:]).replace(r"\ ", r"\s+"))
    try:
        return re.compile(r"^" + "".join(pieces) + r"$")
    except re.error:
        return None


def _is_gui_opener(opener: str) -> bool:
    """Return whether an opener has the legacy ``XXX; CAN[;]`` shape."""

    return re.fullmatch(r"\S[^;]*;\s*CAN;?", opener) is not None


def _is_semantic_matcher(matcher: str) -> bool:
    """Accept either a legacy GUI opener or a canonical numeric command matcher."""

    if _is_gui_opener(matcher):
        return True
    compiled = _compile_semantic_matcher(matcher)
    if compiled is None:
        return False
    stripped = _CANONICAL_UNDO_PLACEHOLDER_RE.sub("", matcher)
    return "{" not in stripped and "}" not in stripped


def _semantic_match(matcher: str, raw_command: str) -> bool:
    """Match one semantic Undo rule against a recorded raw command."""

    compiled = _compile_semantic_matcher(matcher)
    if compiled is None:
        return raw_command == matcher
    return compiled.fullmatch(raw_command) is not None


def _is_first_order_header(line: str) -> bool:
    text = re.sub(r"\s+", " ", line.strip())
    return (
        text.startswith("EFL ")
        and "REDU" in text
        and "PIM" in text
        and "OAL" in text
        and "EN PUP" in text
        and "EX PUP" in text
    )


class _AutoStateParser:
    """Capture the frozen minimum State from output owned by one AUT run."""

    def __init__(self, diagnostics: list[DiagnosticRecord]) -> None:
        self.diagnostics = diagnostics
        self.aut_block_active = False
        self.output_active = False
        self._reset_capture()

    def _reset_capture(self) -> None:
        self.current_cycle: Optional[int] = None
        self.current_cycle_data: Optional[dict[str, Any]] = None
        self.previous_nonblank_line: Optional[str] = None
        self.error_function_construction_active = False
        self.error_function_construction_lines: list[str] = []
        self.collecting_lens_data = False
        self.waiting_first_order_header = False
        self.waiting_first_order_values = False
        self.latest_complete_state: Optional[dict[str, Any]] = None

    def consume_command(self, line_number: int, command: str) -> None:
        """Observe commands only to detect exact ``AUT ... GO`` boundaries."""

        stripped = command.strip()
        if stripped == "AUT":
            self.aut_block_active = True
            return
        if stripped == "CAN" and self.aut_block_active:
            self.aut_block_active = False
            return
        if self.aut_block_active and stripped == "GO":
            self.aut_block_active = False
            self._reset_capture()
            self.output_active = True

    def _start_cycle(self, cycle_number: int) -> None:
        self.current_cycle = cycle_number
        self.current_cycle_data = {
            "cycle_number": cycle_number,
            "error_function_value": None,
            "error_function_construction": deepcopy(
                self.error_function_construction_lines
            ),
            "lens_data": [],
            "first_order_header": None,
            "first_order_values": None,
        }
        self.collecting_lens_data = False
        self.waiting_first_order_header = False
        self.waiting_first_order_values = False

    def _invalidate_current_cycle_capture(self) -> None:
        """Quarantine data after an unexplained cycle jump without guessing."""

        self.current_cycle_data = None
        self.collecting_lens_data = False
        self.waiting_first_order_header = False
        self.waiting_first_order_values = False

    def _commit_current_cycle(self, line_number: int) -> None:
        data = self.current_cycle_data
        if data is None:
            return
        complete = (
            data["error_function_value"] is not None
            and bool(data["lens_data"])
            and data["first_order_header"] is not None
            and data["first_order_values"] is not None
        )
        if complete:
            self.latest_complete_state = deepcopy(data)
        else:
            self.diagnostics.append(
                DiagnosticRecord(
                    level="warning",
                    code="INCOMPLETE_AUT_CYCLE",
                    message=(
                        f"AUT cycle {data['cycle_number']} did not contain all "
                        "required State fields and was not committed."
                    ),
                    raw_line_number=line_number,
                )
            )

    def consume_output(self, line_number: int, line: str) -> bool:
        """Consume one owned output line; return True when AUT completes."""

        completed = False
        try:
            if _is_auto_completion(line):
                self.output_active = False
                completed = True
                return completed

            if line.strip() == "ERROR FUNCTION CONSTRUCTION":
                self.error_function_construction_active = True
                self.error_function_construction_lines = []
                return completed

            if self.error_function_construction_active:
                if line.strip() == "CONVERGENCE CONTROLS":
                    self.error_function_construction_active = False
                elif line.strip():
                    self.error_function_construction_lines.append(line.rstrip())
                return completed

            cycle_number = _get_cycle_number(line)
            if cycle_number is not None:
                if self.current_cycle is None:
                    self._start_cycle(cycle_number)
                elif cycle_number == self.current_cycle:
                    pass
                elif cycle_number == self.current_cycle + 1:
                    self._start_cycle(cycle_number)
                else:
                    self.diagnostics.append(
                        DiagnosticRecord(
                            level="warning",
                            code="UNEXPECTED_AUT_CYCLE",
                            message=(
                                "Unexpected AUT cycle transition: "
                                f"current={self.current_cycle}, seen={cycle_number}."
                            ),
                            raw_line_number=line_number,
                        )
                    )
                    self._invalidate_current_cycle_capture()
                return completed

            data = self.current_cycle_data
            if data is None:
                return completed

            error_value = _get_error_function_value(line)
            if error_value is not None:
                data["error_function_value"] = error_value
                return completed

            if (
                not self.collecting_lens_data
                and not self.waiting_first_order_header
                and not self.waiting_first_order_values
                and _is_obj_start(line)
            ):
                self.collecting_lens_data = True
                data["lens_data"] = []
                if self.previous_nonblank_line is not None:
                    data["lens_data"].append(self.previous_nonblank_line)
                data["lens_data"].append(line.rstrip())
                return completed

            if self.collecting_lens_data:
                data["lens_data"].append(line.rstrip())
                if _is_img_end(line):
                    self.collecting_lens_data = False
                    self.waiting_first_order_header = True
                return completed

            if self.waiting_first_order_header:
                if line.strip() and _is_first_order_header(line):
                    data["first_order_header"] = line.rstrip()
                    self.waiting_first_order_header = False
                    self.waiting_first_order_values = True
                return completed

            if self.waiting_first_order_values:
                if line.strip():
                    data["first_order_values"] = line.rstrip()
                    self.waiting_first_order_values = False
                    self._commit_current_cycle(line_number)
                return completed

            return completed
        finally:
            if line.strip():
                self.previous_nonblank_line = line.rstrip()


class Recorder:
    """Replay raw CODE V logs into a persistent Event Tree and State Library.

    Parameters
    ----------
    storage_context:
        A directory path (using ``event_tree.json`` and ``state_library.json``),
        a :class:`StorageContext`, or a mapping containing both explicit paths.
    rules_path:
        Path to the existing, read-only ``recorder_rules.json`` file.
    """

    def __init__(
        self,
        storage_context: StorageContext
        | str
        | os.PathLike[str]
        | Mapping[str, Any],
        rules_path: str | os.PathLike[str],
    ) -> None:
        self.storage_context = StorageContext.from_value(storage_context)
        try:
            self.rules_path = Path(os.fspath(rules_path))
        except TypeError as exc:
            raise RecorderConfigurationError("rules_path must be a file path.") from exc

        if any(
            _same_path(self.rules_path, persistence_path)
            for persistence_path in (
                self.storage_context.event_tree_path,
                self.storage_context.state_library_path,
            )
        ):
            raise RecorderConfigurationError(
                "The read-only rules file cannot also be a persistence file."
            )

        self._semantic_undo_map: dict[str, tuple[str, ...]] = {}
        self.events: list[EventRecord] = []
        self.command_logs: list[CommandLogRecord] = []
        self.states: list[StateRecord] = []
        self._event_data: dict[str, Any] = {}
        self._state_data: dict[str, Any] = {}
        self._load_rules()
        self._load_storage()

    @property
    def semantic_undo_map(self) -> dict[str, tuple[str, ...]]:
        """Return an isolated copy of the loaded read-only semantic map."""

        return dict(self._semantic_undo_map)

    def reload(self) -> None:
        """Reload rules and both persistence files from disk."""

        self._load_rules()
        self._load_storage()

    def _load_rules(self) -> None:
        path = self.rules_path
        if not path.is_file():
            raise RulesFileError(f"Semantic Undo rules file does not exist: {path}")
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RulesFileError(
                f"Cannot load semantic Undo rules from {path}: {exc}"
            ) from exc

        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") != RULES_VERSION
        ):
            raise RulesFileError(
                f"Rules file must be a JSON object with version={RULES_VERSION}."
            )
        semantic_map = data.get("semantic_undo_map")
        if not isinstance(semantic_map, dict):
            raise RulesFileError("Rules file 'semantic_undo_map' must be an object.")

        validated: dict[str, tuple[str, ...]] = {}
        for target, openers in semantic_map.items():
            if not isinstance(target, str) or not target.strip():
                raise RulesFileError("Every semantic Undo target must be non-empty text.")
            if target != target.strip() or "\n" in target or "\r" in target:
                raise RulesFileError(
                    f"Semantic Undo target {target!r} must be trimmed single-line text."
                )
            if not isinstance(openers, list) or not openers:
                raise RulesFileError(
                    f"Semantic Undo target {target!r} must contain an opener list."
                )
            clean_openers: list[str] = []
            for opener in openers:
                if not isinstance(opener, str) or not opener.strip():
                    raise RulesFileError(
                        f"Semantic Undo target {target!r} contains an invalid opener."
                    )
                if opener != opener.strip() or "\n" in opener or "\r" in opener:
                    raise RulesFileError(
                        f"Semantic Undo opener {opener!r} for target {target!r} "
                        "must be trimmed single-line text."
                    )
                if not _is_semantic_matcher(opener):
                    raise RulesFileError(
                        f"Semantic Undo matcher {opener!r} for target {target!r} "
                        "must be either a legacy 'XXX; CAN[;]' opener or a "
                        "supported canonical numeric matcher using {index}/{value}."
                    )
                if opener in clean_openers:
                    raise RulesFileError(
                        f"Semantic Undo target {target!r} contains duplicate opener "
                        f"{opener!r}."
                    )
                clean_openers.append(opener)
            validated[target] = tuple(clean_openers)
        self._semantic_undo_map = validated

    @staticmethod
    def _empty_event_data() -> dict[str, Any]:
        return {
            "version": PERSISTENCE_VERSION,
            "next_log_number": 1,
            "next_event_number": 1,
            "command_logs": [],
            "events": [],
        }

    @staticmethod
    def _empty_state_data() -> dict[str, Any]:
        return {
            "version": PERSISTENCE_VERSION,
            "next_state_number": 1,
            "states": [],
        }

    @staticmethod
    def _read_json_or_default(
        path: Path,
        default_factory: Any,
        label: str,
    ) -> dict[str, Any]:
        if not path.exists():
            return default_factory()
        if not path.is_file():
            raise PersistenceError(f"{label} path is not a file: {path}")
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PersistenceError(f"Cannot load {label} from {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PersistenceError(f"{label} must contain a JSON object: {path}")
        if (
            type(data.get("version")) is not int
            or data.get("version") != PERSISTENCE_VERSION
        ):
            raise PersistenceError(
                f"{label} must use version={PERSISTENCE_VERSION}: {path}"
            )
        return data

    @staticmethod
    def _records_from_data(
        data: dict[str, Any],
        key: str,
        record_type: Any,
        label: str,
    ) -> list[Any]:
        raw_records = data.get(key)
        if not isinstance(raw_records, list):
            raise PersistenceError(f"{label} field {key!r} must be a JSON array.")
        records = []
        for index, item in enumerate(raw_records):
            if not isinstance(item, dict):
                raise PersistenceError(
                    f"{label} {key}[{index}] must be a JSON object."
                )
            try:
                records.append(record_type(**item))
            except (TypeError, ValueError) as exc:
                raise PersistenceError(
                    f"Invalid {label} record at {key}[{index}]: {exc}"
                ) from exc
        return records

    @staticmethod
    def _id_number(identifier: str, prefix: str, label: str) -> int:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)", identifier)
        if not match:
            raise PersistenceError(f"Invalid {label} ID: {identifier!r}")
        return int(match.group(1))

    @classmethod
    def _next_counter(
        cls,
        data: dict[str, Any],
        counter_key: str,
        identifiers: Sequence[str],
        prefix: str,
        label: str,
    ) -> int:
        stored = data.get(counter_key, 1)
        if isinstance(stored, bool) or not isinstance(stored, int) or stored < 1:
            raise PersistenceError(f"{counter_key!r} must be a positive integer.")
        largest = max(
            (cls._id_number(value, prefix, label) for value in identifiers),
            default=0,
        )
        return max(stored, largest + 1)

    def _load_storage(self) -> None:
        event_exists = self.storage_context.event_tree_path.exists()
        state_exists = self.storage_context.state_library_path.exists()
        if event_exists != state_exists:
            missing = (
                self.storage_context.state_library_path
                if event_exists
                else self.storage_context.event_tree_path
            )
            raise PersistenceError(
                "Recorder persistence is incomplete; both logical data files "
                f"must exist together. Missing: {missing}"
            )
        event_data = self._read_json_or_default(
            self.storage_context.event_tree_path,
            self._empty_event_data,
            "Event Tree",
        )
        state_data = self._read_json_or_default(
            self.storage_context.state_library_path,
            self._empty_state_data,
            "State Library",
        )
        events = self._records_from_data(
            event_data, "events", EventRecord, "Event Tree"
        )
        command_logs = self._records_from_data(
            event_data, "command_logs", CommandLogRecord, "Event Tree"
        )
        states = self._records_from_data(
            state_data, "states", StateRecord, "State Library"
        )
        self._validate_integrity(events, command_logs, states)

        event_data["next_event_number"] = self._next_counter(
            event_data,
            "next_event_number",
            [event.event_id for event in events],
            "E",
            "Event",
        )
        event_data["next_log_number"] = self._next_counter(
            event_data,
            "next_log_number",
            [record.log_id for record in command_logs],
            "LOG",
            "Command Log",
        )
        state_data["next_state_number"] = self._next_counter(
            state_data,
            "next_state_number",
            [state.state_id for state in states],
            "ST",
            "State",
        )
        self._event_data = event_data
        self._state_data = state_data
        self.events = events
        self.command_logs = command_logs
        self.states = states

    @classmethod
    def _validate_integrity(
        cls,
        events: Sequence[EventRecord],
        command_logs: Sequence[CommandLogRecord],
        states: Sequence[StateRecord],
    ) -> None:
        event_ids = [event.event_id for event in events]
        log_ids = [record.log_id for record in command_logs]
        state_ids = [state.state_id for state in states]
        for identifier in event_ids:
            cls._id_number(identifier, "E", "Event")
        for identifier in log_ids:
            cls._id_number(identifier, "LOG", "Command Log")
        for identifier in state_ids:
            cls._id_number(identifier, "ST", "State")
        if len(set(event_ids)) != len(event_ids):
            raise PersistenceError("Event Tree contains duplicate Event IDs.")
        if len(set(log_ids)) != len(log_ids):
            raise PersistenceError("Event Tree contains duplicate Command Log IDs.")
        if len(set(state_ids)) != len(state_ids):
            raise PersistenceError("State Library contains duplicate State IDs.")

        event_id_set = set(event_ids)
        log_id_set = set(log_ids)
        state_id_set = set(state_ids)
        for event in events:
            if event.parent_event_id is not None and event.parent_event_id not in event_id_set:
                raise PersistenceError(
                    f"{event.event_id} references missing parent "
                    f"{event.parent_event_id}."
                )
            if (
                event.parent_event_id is not None
                and cls._id_number(event.parent_event_id, "E", "Event")
                >= cls._id_number(event.event_id, "E", "Event")
            ):
                raise PersistenceError(
                    f"{event.event_id} has non-historical parent "
                    f"{event.parent_event_id}; parent Event IDs must be older."
                )
            if event.log_id not in log_id_set:
                raise PersistenceError(
                    f"{event.event_id} references missing Command Log {event.log_id}."
                )
            if not isinstance(event.raw_command, str) or not event.raw_command:
                raise PersistenceError(f"{event.event_id} has an invalid raw command.")
            if (
                isinstance(event.raw_line_number, bool)
                or not isinstance(event.raw_line_number, int)
                or event.raw_line_number < 1
            ):
                raise PersistenceError(
                    f"{event.event_id} has an invalid raw line number."
                )
        for state in states:
            if state.created_by_event_id not in event_id_set:
                raise PersistenceError(
                    f"{state.state_id} references missing creator Event "
                    f"{state.created_by_event_id}."
                )
            if state.log_id not in log_id_set:
                raise PersistenceError(
                    f"{state.state_id} references missing Command Log {state.log_id}."
                )
            creator = next(
                event
                for event in events
                if event.event_id == state.created_by_event_id
            )
            if state.log_id != creator.log_id:
                raise PersistenceError(
                    f"{state.state_id} and creator {creator.event_id} reference "
                    "different Command Logs."
                )
            if state.file_path is not None:
                if not state.file_path or get_save_file_path(creator.raw_command) != state.file_path:
                    raise PersistenceError(
                        f"SAV State {state.state_id} does not exactly match its "
                        f"creator command {creator.raw_command!r}."
                    )
                optical_fields = (
                    state.cycle_number,
                    state.error_function_value,
                    state.error_function_construction,
                    state.lens_data,
                    state.first_order_header,
                    state.first_order_values,
                )
                if any(value is not None for value in optical_fields):
                    raise PersistenceError(
                        f"Path-only SAV State {state.state_id} contains AUT data."
                    )
            else:
                if creator.raw_command != "GO":
                    raise PersistenceError(
                        f"AUT State {state.state_id} was not created by a GO Event."
                    )
                if (
                    state.cycle_number is None
                    or state.error_function_value is None
                    or not isinstance(state.error_function_construction, list)
                    or not isinstance(state.lens_data, list)
                    or not state.lens_data
                    or state.first_order_header is None
                    or state.first_order_values is None
                ):
                    raise PersistenceError(
                        f"AUT State {state.state_id} is missing required captured data."
                    )
        for record in command_logs:
            if not isinstance(record.log_path, str) or not record.log_path:
                raise PersistenceError(f"{record.log_id} has an invalid log path.")
            if record.initial_state_id is not None and record.initial_state_id not in state_id_set:
                raise PersistenceError(
                    f"{record.log_id} references missing initial State "
                    f"{record.initial_state_id}."
                )

        events_by_id = {event.event_id: event for event in events}
        for event_id in event_ids:
            seen: set[str] = set()
            cursor: Optional[str] = event_id
            while cursor is not None:
                if cursor in seen:
                    raise PersistenceError(
                        f"Event Tree contains a parent cycle involving {cursor}."
                    )
                seen.add(cursor)
                cursor = events_by_id[cursor].parent_event_id

    @classmethod
    def _find_latest_state_for_path(
        cls,
        file_path: str,
        states: Sequence[StateRecord],
    ) -> Optional[StateRecord]:
        matching = [state for state in states if state.file_path == file_path]
        if not matching:
            return None
        return max(
            matching,
            key=lambda state: cls._id_number(state.state_id, "ST", "State"),
        )

    @classmethod
    def _resolve_initial_state(
        cls,
        raw_lines: Sequence[str],
        states: Sequence[StateRecord],
    ) -> InitialStateResolution:
        res_info = find_initial_res(raw_lines)
        if res_info is None:
            return InitialStateResolution("NO_RES", None, None, None)
        matched = cls._find_latest_state_for_path(res_info["file_path"], states)
        if matched is None:
            return InitialStateResolution(
                "RES_UNMATCHED",
                res_info["line_number"],
                res_info["file_path"],
                None,
            )
        return InitialStateResolution(
            "MATCHED",
            res_info["line_number"],
            res_info["file_path"],
            matched.state_id,
        )

    def inspect_initial_state(
        self,
        log_path: str | os.PathLike[str],
    ) -> InitialStateResolution:
        """Read-only preflight resolution for one command log.

        This method never writes Event Tree or State Library data.  It exists so
        the orchestration layer can ask the user what to do when no exact RES
        match establishes the initial State.
        """

        path, raw_lines = self._read_log_lines(log_path)
        del path  # only validation and raw lines are needed for preflight
        self._load_storage()
        return self._resolve_initial_state(raw_lines, self.states)

    def list_sav_states(self) -> list[StateRecord]:
        """Return historical SAV States, newest first, without modifying storage."""

        self._load_storage()
        sav_states = [state for state in self.states if state.file_path is not None]
        return sorted(
            sav_states,
            key=lambda state: self._id_number(state.state_id, "ST", "State"),
            reverse=True,
        )

    @staticmethod
    def _validate_manual_initial_state(
        state_id: str,
        states_by_id: Mapping[str, StateRecord],
    ) -> StateRecord:
        if not isinstance(state_id, str) or not state_id.strip():
            raise InvalidInitialStateError(
                "Manual initial State ID must be non-empty text."
            )
        state_id = state_id.strip()
        try:
            state = states_by_id[state_id]
        except KeyError as exc:
            raise InvalidInitialStateError(
                f"Manual initial State does not exist: {state_id}"
            ) from exc
        return state

    @classmethod
    def _apply_initial_state_decision(
        cls,
        automatic: InitialStateResolution,
        states_by_id: Mapping[str, StateRecord],
        *,
        initial_state_override: Optional[str],
        start_new_root: bool,
    ) -> InitialStateResolution:
        """Apply a human decision only when exact RES matching did not succeed."""

        if initial_state_override is not None and start_new_root:
            raise RecorderConfigurationError(
                "Choose either a manual initial State or a new root, not both."
            )

        if automatic.status == "MATCHED":
            if initial_state_override is not None or start_new_root:
                raise RecorderConfigurationError(
                    "An exact RES match cannot be overridden by a manual decision."
                )
            return automatic
        if automatic.status not in {"NO_RES", "RES_UNMATCHED"}:
            raise RecorderConfigurationError(
                f"Unknown initial-State resolution status: {automatic.status}"
            )

        if initial_state_override is not None:
            state = cls._validate_manual_initial_state(
                initial_state_override, states_by_id
            )
            return InitialStateResolution(
                "MANUAL_MATCH",
                automatic.res_line_number,
                automatic.res_file_path,
                state.state_id,
            )

        if start_new_root:
            return InitialStateResolution(
                "NEW_ROOT_CONFIRMED",
                automatic.res_line_number,
                automatic.res_file_path,
                None,
            )

        raise InitialStateResolutionRequired(automatic)

    @staticmethod
    def _ancestor_path(
        event_id: str,
        events_by_id: Mapping[str, EventRecord],
    ) -> list[str]:
        reverse_path: list[str] = []
        seen: set[str] = set()
        cursor: Optional[str] = event_id
        while cursor is not None:
            if cursor in seen:
                raise PersistenceError(
                    f"Event Tree contains a parent cycle involving {cursor}."
                )
            seen.add(cursor)
            try:
                event = events_by_id[cursor]
            except KeyError as exc:
                raise PersistenceError(
                    f"Cannot reconstruct history; Event {cursor} is missing."
                ) from exc
            reverse_path.append(cursor)
            cursor = event.parent_event_id
        reverse_path.reverse()
        return reverse_path

    @staticmethod
    def _find_target_index(
        target: str,
        active_path: Sequence[str],
        events_by_id: Mapping[str, EventRecord],
        semantic_undo_map: Mapping[str, Sequence[str]],
    ) -> Optional[int]:
        for index in range(len(active_path) - 1, -1, -1):
            if events_by_id[active_path[index]].raw_command == target:
                return index
        allowed_openers = semantic_undo_map.get(target)
        if allowed_openers is not None:
            for index in range(len(active_path) - 1, -1, -1):
                raw_command = events_by_id[active_path[index]].raw_command
                if any(
                    _semantic_match(matcher, raw_command)
                    for matcher in allowed_openers
                ):
                    return index
        return None

    @staticmethod
    def _stage_json(path: Path, data: dict[str, Any]) -> Path:
        temporary_path: Optional[Path] = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            return temporary_path
        except (OSError, TypeError, ValueError) as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise PersistenceError(f"Cannot stage persistence file {path}: {exc}") from exc

    def _persist(self, event_data: dict[str, Any], state_data: dict[str, Any]) -> None:
        destinations = (
            self.storage_context.event_tree_path,
            self.storage_context.state_library_path,
        )
        staged: list[Optional[Path]] = [None, None]
        try:
            staged[0] = self._stage_json(destinations[0], event_data)
            staged[1] = self._stage_json(destinations[1], state_data)
            os.replace(staged[0], destinations[0])
            staged[0] = None
            os.replace(staged[1], destinations[1])
            staged[1] = None
        except OSError as exc:
            raise PersistenceError(f"Cannot commit Recorder persistence: {exc}") from exc
        finally:
            for temporary_path in staged:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink(missing_ok=True)
                    except OSError:
                        pass

    @staticmethod
    def _read_log_lines(
        log_path: str | os.PathLike[str],
    ) -> tuple[Path, list[str]]:
        try:
            path = Path(os.fspath(log_path))
        except TypeError as exc:
            raise LogInputError("log_path must be an explicit .txt file path.") from exc
        if path.suffix.lower() != ".txt":
            raise LogInputError(f"Recorder accepts only .txt command logs: {path}")
        if not path.is_file():
            raise LogInputError(f"Command log does not exist or is not a file: {path}")
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            raise LogInputError(f"Cannot read command log {path}: {exc}") from exc
        return path, text.splitlines()

    def process_log(
        self,
        log_path: str | os.PathLike[str],
        *,
        initial_state_override: Optional[str] = None,
        start_new_root: bool = False,
    ) -> ProcessResult:
        """Replay and persist one explicit raw CODE V command-log ``.txt`` file.

        Exact RES-path matching remains the only automatic route.  Without a
        match, manual inheritance or a new-root decision must be explicit and
        is validated by Recorder before any Event is created.
        """

        path, raw_lines = self._read_log_lines(log_path)

        # Refresh external runtime state before allocating project-global IDs.
        self._load_rules()
        self._load_storage()
        semantic_map = self._semantic_undo_map
        events = list(self.events)
        command_logs = list(self.command_logs)
        states = list(self.states)
        events_by_id = {event.event_id: event for event in events}
        states_by_id = {state.state_id: state for state in states}

        automatic_resolution = self._resolve_initial_state(raw_lines, states)
        resolution = self._apply_initial_state_decision(
            automatic_resolution,
            states_by_id,
            initial_state_override=initial_state_override,
            start_new_root=start_new_root,
        )
        diagnostics: list[DiagnosticRecord] = []
        if resolution.status == "MANUAL_MATCH":
            diagnostics.append(
                DiagnosticRecord(
                    level="info",
                    code="MANUAL_INITIAL_STATE",
                    message=(
                        f"The log was explicitly linked to historical State "
                        f"{resolution.initial_state_id}."
                    ),
                    raw_line_number=resolution.res_line_number,
                )
            )
        elif resolution.status == "NEW_ROOT_CONFIRMED":
            diagnostics.append(
                DiagnosticRecord(
                    level="info",
                    code="NEW_ROOT_CONFIRMED",
                    message="The log was explicitly confirmed as an independent tree.",
                    raw_line_number=resolution.res_line_number,
                )
            )

        if resolution.initial_state_id is None:
            active_path: list[str] = []
        else:
            initial_state = states_by_id[resolution.initial_state_id]
            active_path = self._ancestor_path(
                initial_state.created_by_event_id, events_by_id
            )

        event_counter = self._event_data["next_event_number"]
        state_counter = self._state_data["next_state_number"]
        log_counter = self._event_data["next_log_number"]
        log_id = f"LOG{log_counter:06d}"
        log_counter += 1
        command_log = CommandLogRecord(
            log_id=log_id,
            initial_state_id=resolution.initial_state_id,
            log_path=str(path.absolute()),
        )
        command_logs.append(command_log)

        new_events: list[EventRecord] = []
        new_states: list[StateRecord] = []
        redo_stack: list[dict[str, Any]] = []
        auto_output_line_numbers: set[int] = set()
        auto_parser = _AutoStateParser(diagnostics)
        pending_aut_go_event_id: Optional[str] = None

        for line_number, line in enumerate(raw_lines, start=1):
            if auto_parser.output_active:
                auto_output_line_numbers.add(line_number)
                completed = auto_parser.consume_output(line_number, line)
                if completed:
                    latest = auto_parser.latest_complete_state
                    if pending_aut_go_event_id is not None and latest is not None:
                        state_id = f"ST{state_counter:06d}"
                        state_counter += 1
                        aut_state = StateRecord(
                            state_id=state_id,
                            created_by_event_id=pending_aut_go_event_id,
                            log_id=log_id,
                            file_path=None,
                            cycle_number=latest["cycle_number"],
                            error_function_value=latest["error_function_value"],
                            error_function_construction=deepcopy(
                                latest["error_function_construction"]
                            ),
                            lens_data=deepcopy(latest["lens_data"]),
                            first_order_header=latest["first_order_header"],
                            first_order_values=latest["first_order_values"],
                        )
                        states.append(aut_state)
                        states_by_id[state_id] = aut_state
                        new_states.append(aut_state)
                    else:
                        diagnostics.append(
                            DiagnosticRecord(
                                level="warning",
                                code="AUT_STATE_NOT_CREATED",
                                message=(
                                    "AUT completed without both a bound GO Event and "
                                    "a complete captured cycle; no State was created."
                                ),
                                raw_line_number=line_number,
                            )
                        )
                    pending_aut_go_event_id = None
                continue

            line_type, payload = classify_line(line)
            if line_type == LineType.SKIP:
                continue
            if payload is None:
                continue

            if line_type == LineType.UNDO:
                target_index = self._find_target_index(
                    payload, active_path, events_by_id, semantic_map
                )
                if target_index is None:
                    guidance = (
                        "No matching semantic command/opener was found in the active path. "
                        "Copy the relevant raw command or transaction opener together "
                        "with this Undo line into RulesManager; Recorder did not guess "
                        "or modify the rules file."
                    )
                    diagnostics.append(
                        DiagnosticRecord(
                            level="warning",
                            code="UNRESOLVED_SEMANTIC_UNDO",
                            message=f"Undo target {payload!r} was not resolved. {guidance}",
                            raw_line_number=line_number,
                            target=payload,
                        )
                    )
                else:
                    skipped_span = active_path[target_index:].copy()
                    del active_path[target_index:]
                    redo_stack.append(
                        {"target": payload, "skipped_span": skipped_span}
                    )
                continue

            if line_type == LineType.REDO:
                if not redo_stack:
                    diagnostics.append(
                        DiagnosticRecord(
                            level="warning",
                            code="UNRESOLVED_REDO",
                            message="Redo stack is empty.",
                            raw_line_number=line_number,
                            target=payload,
                        )
                    )
                    continue
                record = redo_stack[-1]
                if record["target"] != payload:
                    diagnostics.append(
                        DiagnosticRecord(
                            level="warning",
                            code="REDO_TARGET_MISMATCH",
                            message=(
                                f"Redo target mismatch: expected {record['target']!r}, "
                                f"seen {payload!r}."
                            ),
                            raw_line_number=line_number,
                            target=payload,
                        )
                    )
                    continue
                skipped_span = record["skipped_span"]
                current_head = active_path[-1] if active_path else None
                if skipped_span:
                    expected_parent = events_by_id[
                        skipped_span[0]
                    ].parent_event_id
                    if expected_parent != current_head:
                        diagnostics.append(
                            DiagnosticRecord(
                                level="warning",
                                code="REDO_PARENT_MISMATCH",
                                message=(
                                    "Redo span no longer connects to the active head: "
                                    f"expected parent={expected_parent!r}, "
                                    f"active head={current_head!r}."
                                ),
                                raw_line_number=line_number,
                                target=payload,
                            )
                        )
                        continue
                redo_stack.pop()
                active_path.extend(skipped_span)
                continue

            # Any retained normal command invalidates replay-local Redo history.
            if redo_stack:
                cleared = len(redo_stack)
                redo_stack.clear()
                diagnostics.append(
                    DiagnosticRecord(
                        level="info",
                        code="REDO_CLEARED",
                        message=f"A new command cleared {cleared} Redo record(s).",
                        raw_line_number=line_number,
                    )
                )

            event_id = f"E{event_counter:06d}"
            event_counter += 1
            event = EventRecord(
                event_id=event_id,
                parent_event_id=active_path[-1] if active_path else None,
                log_id=log_id,
                raw_command=payload,
                raw_line_number=line_number,
            )
            events.append(event)
            events_by_id[event_id] = event
            new_events.append(event)
            active_path.append(event_id)

            save_path = get_save_file_path(payload)
            if save_path is not None:
                state_id = f"ST{state_counter:06d}"
                state_counter += 1
                save_state = StateRecord(
                    state_id=state_id,
                    created_by_event_id=event_id,
                    log_id=log_id,
                    file_path=save_path,
                )
                states.append(save_state)
                states_by_id[state_id] = save_state
                new_states.append(save_state)

            is_aut_go = payload.strip() == "GO" and auto_parser.aut_block_active
            auto_parser.consume_command(line_number, payload)
            if is_aut_go and auto_parser.output_active:
                pending_aut_go_event_id = event_id

        if auto_parser.output_active:
            diagnostics.append(
                DiagnosticRecord(
                    level="warning",
                    code="INCOMPLETE_AUT_OUTPUT",
                    message=(
                        "The log ended before an 'AUTO Completion' boundary; no State "
                        "was created for the unfinished AUT run."
                    ),
                    raw_line_number=len(raw_lines) if raw_lines else None,
                )
            )

        auto_output_event_leaks = sum(
            1
            for event in new_events
            if event.raw_line_number in auto_output_line_numbers
        )
        self._validate_integrity(events, command_logs, states)

        result = ProcessResult(
            command_log=command_log,
            initial_state=resolution,
            events_created=new_events,
            states_created=new_states,
            active_path=active_path.copy(),
            diagnostics=diagnostics.copy(),
            total_raw_lines=len(raw_lines),
            auto_output_line_count=len(auto_output_line_numbers),
            auto_output_event_leak_count=auto_output_event_leaks,
            auto_output_active_after_replay=auto_parser.output_active,
            pending_aut_go_event_id=pending_aut_go_event_id,
            redo_depth_after_replay=len(redo_stack),
        )

        # A replay containing unresolved semantic Undo is provisional history.
        # Recorder returns it for rule maintenance but does not commit it.  This
        # keeps Main completely outside Event Tree / State Library mutation.
        if any(d.code == "UNRESOLVED_SEMANTIC_UNDO" for d in diagnostics):
            return result

        event_data = {
            "version": PERSISTENCE_VERSION,
            "next_log_number": log_counter,
            "next_event_number": event_counter,
            "command_logs": [asdict(record) for record in command_logs],
            "events": [asdict(event) for event in events],
        }
        state_data = {
            "version": PERSISTENCE_VERSION,
            "next_state_number": state_counter,
            "states": [asdict(state) for state in states],
        }
        self._persist(event_data, state_data)

        self._event_data = event_data
        self._state_data = state_data
        self.events = events
        self.command_logs = command_logs
        self.states = states
        return result



__all__ = [
    "CommandLogRecord",
    "DiagnosticRecord",
    "EventRecord",
    "InitialStateResolution",
    "InitialStateResolutionRequired",
    "InvalidInitialStateError",
    "LineType",
    "LogInputError",
    "PersistenceError",
    "ProcessResult",
    "Recorder",
    "RecorderConfigurationError",
    "RecorderError",
    "RulesFileError",
    "StateRecord",
    "StorageContext",
    "classify_line",
    "find_initial_res",
    "get_res_file_path",
    "get_save_file_path",
]
