"""Maintain the semantic Undo compatibility rules used by ``Recorder``.

This module deliberately has no dependency on ``Recorder.py``.  It validates,
edits, previews, and explicitly saves the version-1 ``recorder_rules.json``
document.  Mutating methods only change the in-memory document; :meth:`save`
is the sole persistence operation.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Final


SCHEMA_VERSION: Final[int] = 1
_TOP_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    {"version", "semantic_undo_map"}
)

# CODE V history lines observed by the Recorder use ``! Undo: TARGET``.  Be
# tolerant of spacing around ``!`` and ``:``, but retain case-sensitive target
# matching so a caller can select an exact Undo label.
_UNDO_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*!\s*Undo\s*:\s*(?P<target>.*?)\s*$"
)
_HISTORY_CONTROL_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*!\s*(?:Undo|Redo)\s*:"
)

# A legacy GUI transaction opener is an unindented command whose last command is CAN.
# The final semicolon is optional because both variants exist in the rule file.
# The first command may begin with punctuation, notably ``!TOLFDIF; CAN;``.
_OPENER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<opener>[^\s;][^;]*;\s*CAN;?)\s*$"
)

# Recorder-rule canonical numeric placeholders.  ``index`` is deliberately
# integer-only; ``value`` accepts integer, decimal, and scientific notation.
# Optional numeric suffixes allow one command to contain several parameters.
_CANONICAL_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(
    r"\{(?P<kind>index|value)(?:_(?P<number>\d+))?\}"
)
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
)
_PREFIX_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<prefix>[A-Za-z_]+)(?P<number>[-+]?\d+(?:\.\d+)?)"
)


class RulesManagerError(Exception):
    """Base class for all RulesManager-specific errors."""


class RulesFileError(RulesManagerError):
    """Raised when the rules file cannot be read or written safely."""


class RulesValidationError(RulesManagerError):
    """Raised when a rules document does not match the strict v1 schema."""


class RuleNotFoundError(RulesManagerError):
    """Raised when a requested target or opener is absent."""


class RuleConflictError(RulesManagerError):
    """Raised when an edit would overwrite or duplicate an existing rule."""


class SnippetParseError(RulesManagerError):
    """Raised when a raw CODE V snippet cannot yield an unambiguous rule."""


class _StrictJSONError(ValueError):
    """Internal marker for non-standard JSON and duplicate object keys."""


@dataclass(frozen=True, slots=True)
class RulePreview:
    """A non-persistent semantic Undo target/matcher pair extracted from a log snippet.

    Line numbers are one-based within the supplied snippet so the caller can
    show the exact extraction to a user before calling :meth:`RulesManager.save`.
    """

    target: str
    opener: str
    undo_line_number: int
    opener_line_number: int
    undo_line: str
    opener_line: str

    def to_dict(self) -> dict[str, str | int]:
        """Return a JSON-friendly representation of the preview."""

        return {
            "target": self.target,
            "opener": self.opener,
            "undo_line_number": self.undo_line_number,
            "opener_line_number": self.opener_line_number,
            "undo_line": self.undo_line,
            "opener_line": self.opener_line,
        }


@dataclass(frozen=True, slots=True)
class RuleUpdateResult:
    """Result of applying a snippet preview to the in-memory rule library."""

    preview: RulePreview
    added: bool

    @property
    def requires_save(self) -> bool:
        """Whether the in-memory update introduced an unsaved rule."""

        return self.added

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation of the result."""

        return {
            "preview": self.preview.to_dict(),
            "added": self.added,
            "requires_save": self.requires_save,
        }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting duplicate keys."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _StrictJSONError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_nonstandard_constant(token: str) -> None:
    """Reject NaN and Infinity, which are not part of standard JSON."""

    raise _StrictJSONError(f"non-standard JSON numeric constant: {token}")


def _clean_nonempty_string(value: object, *, field: str) -> str:
    """Validate a non-empty, already-trimmed single-line string."""

    if not isinstance(value, str):
        raise RulesValidationError(
            f"{field} must be a string, got {type(value).__name__}."
        )
    if not value:
        raise RulesValidationError(f"{field} must not be empty.")
    if value != value.strip():
        raise RulesValidationError(
            f"{field} must not contain leading or trailing whitespace: {value!r}."
        )
    if "\n" in value or "\r" in value:
        raise RulesValidationError(f"{field} must be a single line.")
    return value


def _canonical_pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one numeric canonical matcher used by Recorder rules."""

    pieces: list[str] = []
    pos = 0
    matches = list(_CANONICAL_PLACEHOLDER_RE.finditer(pattern))
    if not matches:
        raise ValueError("canonical matcher contains no {index}/{value} placeholder")
    for match in matches:
        literal = re.escape(pattern[pos:match.start()]).replace(r"\ ", r"\s+")
        pieces.append(literal)
        kind = match.group("kind")
        if kind == "index":
            pieces.append(r"\d+")
        else:
            pieces.append(
                r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
            )
        pos = match.end()
    pieces.append(re.escape(pattern[pos:]).replace(r"\ ", r"\s+"))
    return re.compile(r"^" + "".join(pieces) + r"$")


def _is_valid_canonical_matcher(matcher: str) -> bool:
    """Return whether *matcher* is a supported parameterized command pattern."""

    if not matcher or matcher != matcher.strip() or "\n" in matcher or "\r" in matcher:
        return False
    if matcher[0].isspace():
        return False
    try:
        _canonical_pattern_to_regex(matcher)
    except (ValueError, re.error):
        return False
    # Reject stray braces so malformed placeholders do not silently become literals.
    without_placeholders = _CANONICAL_PLACEHOLDER_RE.sub("", matcher)
    return "{" not in without_placeholders and "}" not in without_placeholders


def _is_valid_opener(opener: str) -> bool:
    """Accept a legacy GUI opener or a numeric canonical command matcher."""

    return (
        _OPENER_RE.fullmatch(opener) is not None
        or _is_valid_canonical_matcher(opener)
    )


def _yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _ask_numeric_role(fragment: str) -> str:
    """Ask whether one numeric parameter represents an index or a value."""

    while True:
        answer = input(
            f'Numeric part {fragment!r}: is this an [i]ndex or [v]alue? '
        ).strip().lower()
        if answer in {"i", "index"}:
            return "index"
        if answer in {"v", "value"}:
            return "value"
        print("Please enter i/index or v/value.")


def _next_placeholder(kind: str, counts: dict[str, int]) -> str:
    counts[kind] = counts.get(kind, 0) + 1
    number = counts[kind]
    return kind if number == 1 else f"{kind}_{number}"


def _canonicalize_command_interactive(command: str) -> str:
    """Convert numeric command arguments into {index}/{value} placeholders.

    The first whitespace-delimited token is treated as the CODE V command name
    and is never generalized, preventing mnemonics such as V3D or FAB2 from
    being mistaken for numeric parameters.
    """

    command = command.strip()
    if not command:
        raise SnippetParseError("Source command is empty.")
    tokens = command.split()
    if len(tokens) == 1:
        return command

    counts: dict[str, int] = {}
    result = [tokens[0]]
    for token in tokens[1:]:
        # Standalone numeric argument.
        if _NUMBER_RE.fullmatch(token):
            kind = _ask_numeric_role(token)
            placeholder = _next_placeholder(kind, counts)
            result.append("{" + placeholder + "}")
            continue

        # Common compact indexed/value form, e.g. S4, C6, Z1.
        prefix_match = _PREFIX_NUMBER_RE.fullmatch(token)
        if prefix_match is not None:
            prefix = prefix_match.group("prefix")
            number = prefix_match.group("number")
            kind = _ask_numeric_role(token)
            placeholder = _next_placeholder(kind, counts)
            result.append(prefix + "{" + placeholder + "}")
            continue

        # More complicated tokens are intentionally retained literally.  The
        # manager does not guess semantics inside glass names, paths, ranges, etc.
        result.append(token)

    return " ".join(result)


class RulesManager:
    """Load and maintain a strict version-1 semantic Undo rule library.

    Parameters
    ----------
    rules_path:
        Path to ``recorder_rules.json``.  Construction does not read or write
        the file; call :meth:`load` explicitly before viewing or editing it.

    Notes
    -----
    ``add_rule``, ``edit_rule``, ``remove_rule``, and
    ``add_from_log_snippet`` only mutate memory.  The caller must invoke
    :meth:`save` explicitly after inspecting the result.
    """

    def __init__(self, rules_path: str | os.PathLike[str]) -> None:
        self.rules_path = Path(rules_path)
        self._data: dict[str, Any] | None = None
        self._baseline_data: dict[str, Any] | None = None
        self._dirty = False

    @property
    def is_loaded(self) -> bool:
        """Return whether a validated document is currently loaded."""

        return self._data is not None

    @property
    def is_dirty(self) -> bool:
        """Return whether the in-memory rules differ from the last load/save."""

        return self._dirty

    def load(self) -> dict[str, object]:
        """Read and validate the configured rules file.

        The currently loaded document is replaced only after the new file has
        parsed and validated successfully.  UTF-8 with an optional BOM is
        accepted.  Duplicate object keys and non-standard JSON constants are
        rejected.

        Returns
        -------
        dict
            A defensive copy of the complete validated document.

        Raises
        ------
        RulesFileError
            If the path is missing, unreadable, or contains malformed JSON.
        RulesValidationError
            If the parsed JSON does not conform to schema version 1.
        """

        try:
            with self.rules_path.open("r", encoding="utf-8-sig") as stream:
                candidate = json.load(
                    stream,
                    object_pairs_hook=_strict_object,
                    parse_constant=_reject_nonstandard_constant,
                )
        except FileNotFoundError as exc:
            raise RulesFileError(
                f"Rules file does not exist: {self.rules_path}"
            ) from exc
        except IsADirectoryError as exc:
            raise RulesFileError(
                f"Rules path is a directory, not a JSON file: {self.rules_path}"
            ) from exc
        except PermissionError as exc:
            raise RulesFileError(
                f"Rules file is not readable: {self.rules_path}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise RulesFileError(
                f"Rules file is not valid UTF-8: {self.rules_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise RulesFileError(
                "Malformed rules JSON at "
                f"{self.rules_path}:{exc.lineno}:{exc.colno}: {exc.msg}"
            ) from exc
        except _StrictJSONError as exc:
            raise RulesFileError(
                f"Malformed rules JSON in {self.rules_path}: {exc}"
            ) from exc
        except OSError as exc:
            raise RulesFileError(
                f"Could not read rules file {self.rules_path}: {exc}"
            ) from exc

        self.validate(candidate)
        # ``candidate`` is now known to be a plain JSON object graph.
        self._data = candidate
        self._baseline_data = deepcopy(candidate)
        self._dirty = False
        return self.view()

    def validate(self, data: object | None = None) -> bool:
        """Validate a document against the strict version-1 schema.

        If *data* is omitted, the currently loaded in-memory document is
        checked.  Validation never changes manager state.  Success returns
        ``True``; failure raises :class:`RulesValidationError`.
        """

        candidate = self._require_loaded() if data is None else data

        if not isinstance(candidate, dict):
            raise RulesValidationError(
                "Rules document must be a JSON object at the top level."
            )

        actual_keys = frozenset(candidate.keys())
        if actual_keys != _TOP_LEVEL_KEYS:
            missing = sorted(_TOP_LEVEL_KEYS - actual_keys, key=repr)
            extra = sorted(actual_keys - _TOP_LEVEL_KEYS, key=repr)
            details: list[str] = []
            if missing:
                details.append(f"missing keys: {missing}")
            if extra:
                details.append(f"unexpected keys: {extra}")
            raise RulesValidationError(
                "Rules document must contain exactly 'version' and "
                f"'semantic_undo_map' ({'; '.join(details)})."
            )

        version = candidate["version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise RulesValidationError(
                f"Unsupported rules schema version {version!r}; expected "
                f"integer {SCHEMA_VERSION}."
            )

        undo_map = candidate["semantic_undo_map"]
        if not isinstance(undo_map, dict):
            raise RulesValidationError(
                "'semantic_undo_map' must be a JSON object."
            )

        for raw_target, raw_openers in undo_map.items():
            target = _clean_nonempty_string(raw_target, field="Undo target")
            if not isinstance(raw_openers, list):
                raise RulesValidationError(
                    f"Openers for target {target!r} must be a JSON array."
                )
            if not raw_openers:
                raise RulesValidationError(
                    f"Target {target!r} must contain at least one opener."
                )

            seen: set[str] = set()
            for index, raw_opener in enumerate(raw_openers):
                opener = _clean_nonempty_string(
                    raw_opener,
                    field=f"Opener {index} for target {target!r}",
                )
                if not _is_valid_opener(opener):
                    raise RulesValidationError(
                        f"Invalid opener {opener!r} for target {target!r}; "
                        "expected either an unindented 'XXX; CAN[;]' opener or a supported canonical numeric matcher."
                    )
                if opener in seen:
                    raise RulesValidationError(
                        f"Duplicate opener {opener!r} for target {target!r}."
                    )
                seen.add(opener)

        return True

    def view(self) -> dict[str, object]:
        """Return a defensive copy of the complete in-memory document."""

        return deepcopy(self._require_loaded())

    def view_rules(self) -> dict[str, list[str]]:
        """Return a defensive copy of only ``semantic_undo_map``."""

        data = self._require_loaded()
        return deepcopy(data["semantic_undo_map"])

    def get_openers(self, target: str) -> tuple[str, ...]:
        """Return the opener list for *target* as an immutable tuple."""

        target = _clean_nonempty_string(target, field="Undo target")
        rules = self._rules()
        try:
            return tuple(rules[target])
        except KeyError as exc:
            raise RuleNotFoundError(f"Undo target not found: {target!r}") from exc

    def add_rule(self, target: str, opener: str) -> bool:
        """Add *opener* to *target* in memory.

        Existing opener lists are never overwritten.  Returns ``True`` when a
        new pair was inserted and ``False`` when the exact pair already exists.
        No file is written.
        """

        target = _clean_nonempty_string(target, field="Undo target")
        opener = self._validate_opener(opener)
        rules = self._rules()

        existing = rules.get(target)
        if existing is None:
            rules[target] = [opener]
            self._refresh_dirty()
            return True
        if opener in existing:
            return False

        existing.append(opener)
        self._refresh_dirty()
        return True

    def edit_rule(
        self,
        target: str,
        old_opener: str | None = None,
        new_opener: str | None = None,
        *,
        new_target: str | None = None,
    ) -> bool:
        """Edit an opener, rename a target, or perform both in memory.

        ``old_opener`` and ``new_opener`` must either both be supplied or both
        omitted.  ``new_target`` optionally renames the mapping key.  Conflicts
        are reported rather than merged or overwritten.  Returns whether the
        document changed.  No file is written.
        """

        target = _clean_nonempty_string(target, field="Undo target")
        if (old_opener is None) != (new_opener is None):
            raise RulesValidationError(
                "old_opener and new_opener must be supplied together."
            )
        if old_opener is None and new_target is None:
            raise RulesValidationError(
                "edit_rule requires an opener replacement, new_target, or both."
            )

        replacement_target = (
            _clean_nonempty_string(new_target, field="New Undo target")
            if new_target is not None
            else target
        )
        validated_old = (
            self._validate_opener(old_opener) if old_opener is not None else None
        )
        validated_new = (
            self._validate_opener(new_opener) if new_opener is not None else None
        )

        current = self._require_loaded()
        staged = deepcopy(current)
        staged_rules: dict[str, list[str]] = staged["semantic_undo_map"]
        if target not in staged_rules:
            raise RuleNotFoundError(f"Undo target not found: {target!r}")
        if replacement_target != target and replacement_target in staged_rules:
            raise RuleConflictError(
                f"Cannot rename {target!r} to existing target "
                f"{replacement_target!r}."
            )

        openers = staged_rules[target]
        if validated_old is not None and validated_new is not None:
            try:
                opener_index = openers.index(validated_old)
            except ValueError as exc:
                raise RuleNotFoundError(
                    f"Opener {validated_old!r} not found for target {target!r}."
                ) from exc
            if validated_new != validated_old and validated_new in openers:
                raise RuleConflictError(
                    f"Opener {validated_new!r} already exists for target "
                    f"{target!r}."
                )
            openers[opener_index] = validated_new

        if replacement_target != target:
            rebuilt: dict[str, list[str]] = {}
            for existing_target, existing_openers in staged_rules.items():
                key = (
                    replacement_target
                    if existing_target == target
                    else existing_target
                )
                rebuilt[key] = existing_openers
            staged["semantic_undo_map"] = rebuilt

        if staged == current:
            return False
        self.validate(staged)
        self._data = staged
        self._refresh_dirty()
        return True

    def remove_rule(self, target: str, opener: str | None = None) -> bool:
        """Remove a target or one opener from memory.

        Omitting *opener* removes the complete target.  Removing its final
        opener also removes the target, preserving the strict non-empty-list
        schema.  Missing targets/openers raise :class:`RuleNotFoundError`.
        No file is written.
        """

        target = _clean_nonempty_string(target, field="Undo target")
        rules = self._rules()
        if target not in rules:
            raise RuleNotFoundError(f"Undo target not found: {target!r}")

        if opener is None:
            del rules[target]
            self._refresh_dirty()
            return True

        opener = self._validate_opener(opener)
        try:
            rules[target].remove(opener)
        except ValueError as exc:
            raise RuleNotFoundError(
                f"Opener {opener!r} not found for target {target!r}."
            ) from exc
        if not rules[target]:
            del rules[target]
        self._refresh_dirty()
        return True

    def delete_rule(self, target: str, opener: str | None = None) -> bool:
        """Alias for :meth:`remove_rule`, provided for explicit delete APIs."""

        return self.remove_rule(target, opener)

    @staticmethod
    def _select_undo_line(
        lines: list[str],
        *,
        undo_target: str | None = None,
    ) -> tuple[int, str]:
        """Return the selected Undo line index and parsed target."""

        selected_target = undo_target.strip() if isinstance(undo_target, str) else None
        for index in range(len(lines) - 1, -1, -1):
            match = _UNDO_RE.fullmatch(lines[index])
            if match is None:
                continue
            target = match.group("target").strip()
            if not target:
                continue
            if selected_target is None or target == selected_target:
                return index, target
        if selected_target:
            raise SnippetParseError(
                f"No '! Undo:' line with target {selected_target!r} was found."
            )
        raise SnippetParseError("No non-empty '! Undo:' line was found in the log snippet.")

    @staticmethod
    def _candidate_source_lines(lines: list[str], undo_index: int) -> list[tuple[int, str]]:
        """Return conservative unindented command candidates before an Undo line."""

        candidates: list[tuple[int, str]] = []
        for index in range(undo_index - 1, -1, -1):
            raw = lines[index].removeprefix("\ufeff") if index == 0 else lines[index]
            if not raw.strip() or _HISTORY_CONTROL_RE.match(raw):
                continue
            if raw[:1].isspace():
                continue
            # RulesManager does not try to classify CODE V output.  Candidate
            # selection is human-confirmed in run_interactive().
            candidates.append((index, raw.rstrip()))
        return candidates

    @staticmethod
    def _preview_for_explicit_source(
        lines: list[str],
        undo_index: int,
        target: str,
        source_index: int,
        matcher: str,
    ) -> RulePreview:
        return RulePreview(
            target=target,
            opener=matcher,
            undo_line_number=undo_index + 1,
            opener_line_number=source_index + 1,
            undo_line=lines[undo_index],
            opener_line=lines[source_index],
        )

    def parse_log_snippet(
        self,
        text: str,
        *,
        undo_target: str | None = None,
        undo_line_number: int | None = None,
    ) -> RulePreview:
        """Extract a semantic Undo rule from pasted raw CODE V text.

        By default, the last ``! Undo:`` line is selected.  A caller may select
        the last Undo with an exact *undo_target*, a one-based
        *undo_line_number*, or both.  The nearest preceding unindented
        ``XXX; CAN``/``XXX; CAN;`` line is returned as the opener.  Search stops
        at an earlier Undo/Redo boundary so an opener from a previous history
        transaction is never guessed.  Intermediate commands and output are
        intentionally not interpreted.

        This method neither requires a loaded library nor mutates any state.
        """

        if not isinstance(text, str):
            raise SnippetParseError(
                f"Log snippet must be a string, got {type(text).__name__}."
            )
        lines = text.splitlines()
        if not lines:
            raise SnippetParseError("Log snippet is empty.")

        selected_target = None
        if undo_target is not None:
            try:
                selected_target = _clean_nonempty_string(
                    undo_target, field="Selected Undo target"
                )
            except RulesValidationError as exc:
                raise SnippetParseError(str(exc)) from exc

        selected_index: int | None = None
        parsed_target: str | None = None

        if undo_line_number is not None:
            if type(undo_line_number) is not int or not (
                1 <= undo_line_number <= len(lines)
            ):
                raise SnippetParseError(
                    "undo_line_number must be a valid one-based line number "
                    f"between 1 and {len(lines)}."
                )
            selected_index = undo_line_number - 1
            undo_match = _UNDO_RE.fullmatch(lines[selected_index])
            if undo_match is None:
                raise SnippetParseError(
                    f"Line {undo_line_number} is not a '! Undo:' line."
                )
            parsed_target = undo_match.group("target").strip()
            if not parsed_target:
                raise SnippetParseError(
                    f"Undo line {undo_line_number} has an empty target."
                )
            if selected_target is not None and parsed_target != selected_target:
                raise SnippetParseError(
                    f"Undo line {undo_line_number} has target {parsed_target!r}, "
                    f"not requested target {selected_target!r}."
                )
        else:
            for index in range(len(lines) - 1, -1, -1):
                undo_match = _UNDO_RE.fullmatch(lines[index])
                if undo_match is None:
                    continue
                candidate_target = undo_match.group("target").strip()
                if selected_target is None:
                    if not candidate_target:
                        raise SnippetParseError(
                            f"The last Undo line ({index + 1}) has an empty "
                            "target; no earlier Undo was guessed."
                        )
                    selected_index = index
                    parsed_target = candidate_target
                    break
                if candidate_target == selected_target:
                    selected_index = index
                    parsed_target = candidate_target
                    break

        if selected_index is None or parsed_target is None:
            if selected_target is None:
                raise SnippetParseError(
                    "No non-empty '! Undo:' line was found in the log snippet."
                )
            raise SnippetParseError(
                f"No '! Undo:' line with target {selected_target!r} was found."
            )

        opener_index: int | None = None
        opener: str | None = None
        for index in range(selected_index - 1, -1, -1):
            # A UTF-8 BOM may survive when text was copied from the start of a
            # file.  It is not part of the CODE V command.
            candidate = lines[index]
            if index == 0:
                candidate = candidate.removeprefix("\ufeff")
            if _HISTORY_CONTROL_RE.match(candidate):
                break
            opener_match = _OPENER_RE.fullmatch(candidate)
            if opener_match is not None:
                opener_index = index
                opener = opener_match.group("opener")
                break

        if opener_index is None or opener is None:
            raise SnippetParseError(
                "No valid unindented 'XXX; CAN' or 'XXX; CAN;' opener was "
                f"found before Undo target {parsed_target!r}; no rule was added."
            )

        return RulePreview(
            target=parsed_target,
            opener=opener,
            undo_line_number=selected_index + 1,
            opener_line_number=opener_index + 1,
            undo_line=lines[selected_index],
            opener_line=lines[opener_index],
        )

    def add_from_log_snippet(
        self,
        text: str,
        *,
        undo_target: str | None = None,
        undo_line_number: int | None = None,
    ) -> RuleUpdateResult:
        """Parse a snippet and add its pair to the in-memory document.

        The returned preview is confirmation data for the caller.  This method
        never calls :meth:`save`; persistence always requires a separate,
        explicit call.
        """

        preview = self.parse_log_snippet(
            text,
            undo_target=undo_target,
            undo_line_number=undo_line_number,
        )
        added = self.add_rule(preview.target, preview.opener)
        return RuleUpdateResult(preview=preview, added=added)

    def save(self) -> Path:
        """Validate and atomically persist the in-memory rules document.

        A temporary file is written, flushed, and then atomically replaced into
        ``rules_path``.  On failure, the original rules file is left untouched
        and the temporary file is removed when possible.
        """

        data = self._require_loaded()
        self.validate(data)

        parent = self.rules_path.parent
        if not parent.exists():
            raise RulesFileError(
                f"Rules directory does not exist: {parent}"
            )
        if not parent.is_dir():
            raise RulesFileError(
                f"Rules file parent is not a directory: {parent}"
            )

        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.rules_path.name}.",
                suffix=".tmp",
                dir=parent,
                text=True,
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(
                descriptor, "w", encoding="utf-8", newline="\n"
            ) as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.rules_path)
        except OSError as exc:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RulesFileError(
                f"Could not atomically save rules file {self.rules_path}: {exc}"
            ) from exc

        self._baseline_data = deepcopy(data)
        self._dirty = False
        return self.rules_path

    def _require_loaded(self) -> dict[str, Any]:
        """Return the in-memory document or raise a clear lifecycle error."""

        if self._data is None:
            raise RulesManagerError(
                "No rules document is loaded; call load() before this operation."
            )
        return self._data

    def _rules(self) -> dict[str, list[str]]:
        """Return the validated in-memory semantic Undo map."""

        data = self._require_loaded()
        return data["semantic_undo_map"]

    def _refresh_dirty(self) -> None:
        """Recompute whether the loaded document differs from its baseline."""

        data = self._require_loaded()
        self._dirty = self._baseline_data is None or data != self._baseline_data

    @staticmethod
    def _validate_opener(opener: object) -> str:
        """Validate one public-API opener argument."""

        opener = _clean_nonempty_string(opener, field="Semantic Undo opener")
        if not _is_valid_opener(opener):
            raise RulesValidationError(
                f"Invalid matcher {opener!r}; expected either an unindented "
                "'XXX; CAN[;]' opener or a supported canonical numeric matcher."
            )
        return opener


def _read_snippet_until_end() -> str:
    print("Paste the complete raw CODE V example, including the original command")
    print("or transaction opener and the '! Undo:' line.")
    print("You may include intervening lines. Type END on its own line when finished.\n")
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


def run_interactive(
    rules_path: str | os.PathLike[str] | None = None,
    *,
    target: str | None = None,
) -> None:
    """Interactive semantic Undo rule capture used by Kabbalah Main.

    Legacy GUI transactions are still recognized automatically from ``XXX; CAN``.
    For semantic Undo of an ordinary command (for example ``DEL S4`` followed by
    ``! Undo:  Delete surface``), the user confirms the source command and classifies
    each numeric parameter as an index or a value.  The stored matcher is canonical,
    e.g. ``DEL S{index}``.
    """

    if rules_path is None:
        env_path = os.environ.get("KABBALAH_RECORDER_RULES")
        rules_path = env_path or "recorder_rules.json"

    manager = RulesManager(rules_path)
    manager.load()
    manager.validate()

    print("\nSemantic Undo Rules Manager")
    print("===========================")
    print(f"Rules: {manager.rules_path}")
    if target:
        print(f"Unresolved Undo target: {target}")

    snippet = _read_snippet_until_end()
    if not snippet.strip():
        print("No snippet entered. Nothing was changed.")
        return

    lines = snippet.splitlines()
    undo_index, parsed_target = manager._select_undo_line(lines, undo_target=target)

    # First preserve the old and safest path: a GUI transaction opener.
    opener_index: int | None = None
    opener: str | None = None
    for index in range(undo_index - 1, -1, -1):
        candidate = lines[index].removeprefix("\ufeff") if index == 0 else lines[index]
        if _HISTORY_CONTROL_RE.match(candidate):
            break
        match = _OPENER_RE.fullmatch(candidate)
        if match is not None:
            opener_index = index
            opener = match.group("opener")
            break

    if opener_index is not None and opener is not None:
        preview = manager._preview_for_explicit_source(
            lines, undo_index, parsed_target, opener_index, opener
        )
        print("\nDetected legacy GUI transaction opener.")
    else:
        candidates = manager._candidate_source_lines(lines, undo_index)
        if not candidates:
            raise SnippetParseError(
                "No unindented source-command candidate was found before the Undo line."
            )

        print("\nNo XXX; CAN opener was found. Select the original command being undone:")
        shown = candidates[:20]
        for number, (line_index, command) in enumerate(shown, start=1):
            print(f"  {number:>2}. line {line_index + 1}: {command}")

        while True:
            answer = input(f"Select 1-{len(shown)} [1]: ").strip()
            if not answer:
                selected = 1
                break
            try:
                selected = int(answer)
            except ValueError:
                print("Please enter a number from the list.")
                continue
            if 1 <= selected <= len(shown):
                break
            print("Selection out of range.")

        source_index, source_command = shown[selected - 1]
        print(f"\nSelected source command: {source_command}")
        matcher = _canonicalize_command_interactive(source_command)
        preview = manager._preview_for_explicit_source(
            lines, undo_index, parsed_target, source_index, matcher
        )

    print("\nRule preview")
    print("------------")
    print(f"Undo target       : {preview.target}")
    print(f"Raw source command: {preview.opener_line}")
    print(f"Canonical matcher : {preview.opener}")
    print(f"Undo line         : {preview.undo_line}")
    print("\nComplete raw example:")
    print(preview.opener_line)
    print(preview.undo_line)

    if not _yes_no("Add this semantic Undo rule?", default=False):
        print("Cancelled. Nothing was changed.")
        return

    added = manager.add_rule(preview.target, preview.opener)
    if not added:
        print("The same rule already exists. Nothing was changed.")
        return
    manager.save()
    print(f"Rule saved successfully: {manager.rules_path}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Semantic Undo Rules Manager")
    parser.add_argument("--rules", default="recorder_rules.json")
    parser.add_argument("--target", default=None)
    args = parser.parse_args()
    run_interactive(args.rules, target=args.target)


__all__ = [
    "RuleConflictError",
    "RuleNotFoundError",
    "RulePreview",
    "RuleUpdateResult",
    "RulesFileError",
    "RulesManager",
    "RulesManagerError",
    "RulesValidationError",
    "SCHEMA_VERSION",
    "SnippetParseError",
    "run_interactive",
]


if __name__ == "__main__":
    main()
