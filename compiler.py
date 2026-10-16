"""Standalone Optical Design Trajectory Compiler v0.1.3 Type-2 protocol update.

Faithfully consolidated from the v0.1.1 topology, semantics, compiler, I/O,
and CLI modules. Recorder JSON files and the Semantic Library are the only
inputs; the Semantic Tree JSON is the formal output.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple


# --- Event topology (v0.1.1 topology.py) ---

def event_number(event_id: str) -> int:
    return int(event_id[1:])


@dataclass
class Branch:
    """Compiler-only branch descriptor.

    A branch stores only its exclusive Events plus the original Event anchor.
    Shared ancestry is compiled once in the parent branch.
    """

    branch_id: str
    anchor_event_id: Optional[str]
    events: List[str]
    role: str
    child_branches: List["Branch"] = field(default_factory=list)


class EventTopology:
    """Read-only topology view over Recorder Event Tree facts."""

    def __init__(self, event_tree: dict, state_library: dict):
        self.event_tree = event_tree
        self.events = {e["event_id"]: e for e in event_tree.get("events", [])}
        self.children: Dict[str, List[str]] = defaultdict(list)
        self.roots: List[str] = []

        for event in event_tree.get("events", []):
            parent = event.get("parent_event_id")
            if parent is None:
                self.roots.append(event["event_id"])
            else:
                self.children[parent].append(event["event_id"])

        # Recorder creation order is historical order. The earliest child is the
        # original continuation; later children are later side attempts.
        self.roots.sort(key=event_number)
        for parent in self.children:
            self.children[parent].sort(key=event_number)

        self.states_by_creator = {
            state["created_by_event_id"]: state
            for state in state_library.get("states", [])
        }
        self._branch_counter = 1

    def _new_branch_id(self) -> str:
        branch_id = f"BR{self._branch_counter:06d}"
        self._branch_counter += 1
        return branch_id

    def build_branch(self, start_event_id: str, anchor_event_id: Optional[str], role: str) -> Branch:
        branch = Branch(
            branch_id=self._new_branch_id(),
            anchor_event_id=anchor_event_id,
            events=[],
            role=role,
        )
        current = start_event_id
        while current is not None:
            branch.events.append(current)
            children = self.children.get(current, [])
            if not children:
                break

            primary = children[0]
            for side in children[1:]:
                branch.child_branches.append(
                    self.build_branch(side, current, "side")
                )
            current = primary

        return branch

    def build_branch_forest(self) -> List[Branch]:
        return [self.build_branch(root, None, "primary") for root in self.roots]

    def split_sections(self, event_ids: List[str]) -> List[List[str]]:
        """Split a branch after every State-creator Event."""
        sections: List[List[str]] = []
        current: List[str] = []
        for event_id in event_ids:
            current.append(event_id)
            if event_id in self.states_by_creator:
                sections.append(current)
                current = []
        if current:
            sections.append(current)
        return sections


# --- Semantic translation and matching (v0.1.1 semantics.py) ---

# Type 2 is protected Compiler protocol. The Semantic Library may document
# this registry for humans/LLMs, but these tables remain Compiler-internal.
# Priority within Type 2: C GUI-opener cleanup > A semantic module >
# B repeated layout-view cleanup.
GUI_OPENERS = {
    "AUT; CAN;", "TES; CAN;", "CAM; CAN;", "VIE; CAN;", "ENV; CAN;",
    "V3D; CAN;", "VAP; CAN;", "FAB2; CAN;", "ELE; CAN;", "BEA; CAN;",
    "ANA; CAN;", "RIM; CAN;", "FIE; CAN;", "PMA; CAN;", "FMA; CAN;",
    "FOO; CAN;", "CAT; CAN;", "FOV; CAN;", "SPO; CAN;", "QUA; CAN;",
    "RAD; CAN;", "GDE; CAN;", "GLS; CAN;", "MTF; CAN;", "BIO; CAN;",
    "PSF; CAN;", "LSF; CAN;", "WAV; CAN;", "PAR; CAN;", "BPR; CAN;",
    "BSP; CAN;", "CEF; CAN;", "IMS; CAN;", "FAB1; CAN;", "COS; CAN;",
    "WEI; CAN;", "ALI; CAN;", "SPE; CAN;", "TRA; CAN;", "GHO; CAN;",
    "NAR; CAN;", "TOR; CAN;", "TOD; CAN;", "LUM; CAN;",
    "!TOLFDIF; CAN;", "!TOLMONTE; CAN;", "LIB; CAN;", "UGR; CAN;",
}

TYPE2_CLASS_A_MODULES = {
    "AUT": "Set and run optimization",
    "TES": "Run Test Plate Fitting",
    "CAM": "Run Zoom Cam Design",
    "ENV": "Run Environmental Change",
    "ALI": "Run Alignment Optimization",
    "VAP": "View Apertures",
    "FAB2": "View Interferogram",
    "ELE": "View CODE V Drawing",
    "BEA": "Run Gaussian Beam Trace",
    "ANA": "Run First/Third Order Analysis",
    "RIM": "View Aberration Curves",
    "FIE": "View Field Curves",
    "PMA": "View Pupil Map",
    "FMA": "View Field Map",
    "FOO": "View Footprint Plot",
    "CAT": "View Catseye Plot",
    "FOV": "View Biocular FOV Plot",
    "SPO": "View Spot Diagram",
    "QUA": "Run Quadrant Analysis",
    "RAD": "Run Radial Energy Analysis",
    "GDE": "Run Detector Analysis",
    "GLS": "View Line Spread Function",
    "MTF": "View MTF",
    "BIO": "Run Biocular Analysis",
    "PSF": "View PSF",
    "LSF": "View LSF",
    "WAV": "View Wavefront Analysis",
    "PAR": "Run Partial Coherence Analysis",
    "BPR": "Run FFT Beam Propagation",
    "BSP": "Run Beam Synthesis Propagation",
    "CEF": "Run Fiber Coupling Analysis",
    "IMS": "Run 2D Image Simulation",
    "FAB1": "Generate Fabrication Data Tables",
    "COS": "Run Cost Analysis",
    "WEI": "Run Weight Analysis",
    "SPE": "Run Spectral Analysis",
    "TRA": "Run Transmission Analysis",
    "GHO": "Run Ghost Image Analysis",
    "NAR": "Run Narcissus Analysis",
    "TOR": "Run Tolerancing Analysis",
    "TOD": "View Distortion",
    "LUM": "Run Illumination Analysis",
}

TYPE2_CLASS_B_MODULES = {
    "VIE": "View Lens",
    "V3D": "3D Viewing",
}

# A and B share the same bounded matcher. Their only difference is the
# post-match action: A is kept+annotated; B is deleted when cleanup is enabled.
TYPE2_MODULE_HEADS = {
    *TYPE2_CLASS_A_MODULES.keys(),
    *TYPE2_CLASS_B_MODULES.keys(),
}


def normalize_ws(text: str) -> str:
    return " ".join(text.strip().split())


def compile_pattern(pattern: str) -> re.Pattern:
    """Compile a parameterized Type-1 pattern with whitespace flexibility."""
    pieces: List[str] = []
    pos = 0
    for match in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", pattern):
        literal = re.escape(pattern[pos:match.start()]).replace(r"\ ", r"\s+")
        pieces.append(literal)
        name = match.group(1)
        pieces.append(fr"(?P<{name}>\S+)")
        pos = match.end()
    pieces.append(re.escape(pattern[pos:]).replace(r"\ ", r"\s+"))
    return re.compile(r"^\s*" + "".join(pieces) + r"\s*$", re.IGNORECASE)


def coerce_value(value: str) -> Any:
    try:
        if re.fullmatch(r"[-+]?\d+", value):
            return int(value)
        return float(value)
    except ValueError:
        return value


def render_canonical(template: Any, captures: Dict[str, str]) -> Any:
    if isinstance(template, dict):
        return {key: render_canonical(value, captures) for key, value in template.items()}
    if isinstance(template, list):
        return [render_canonical(value, captures) for value in template]
    if isinstance(template, str):
        exact_capture = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template)
        if exact_capture:
            return coerce_value(captures[exact_capture.group(1)])
        return template.format(**captures)
    return template


class AtomicTranslator:
    """Type 1 canonical translation with raw-command fallback."""

    def __init__(self, semantic_library: dict):
        self.rules = [
            (rule, compile_pattern(rule["pattern"]))
            for rule in semantic_library.get("type1_rules", [])
        ]

    def translate(self, raw_command: str) -> Tuple[dict, str, Optional[str]]:
        for rule, regex in self.rules:
            match = regex.match(raw_command)
            if not match:
                continue
            captures = match.groupdict()
            canonical = render_canonical(rule["canonical"], captures)
            text = rule["text_template"].format(**captures)
            return canonical, text, rule["rule_id"]

        return {
            "operation_type": "raw_command",
            "command": raw_command,
        }, raw_command, None


class Type3Resolver:
    """Resolve exact high-level semantic candidates into non-overlapping masks."""

    def __init__(self, events: Dict[str, dict], semantic_library: dict, trace: dict):
        self.events = events
        self.rules = semantic_library.get("type3_rules", [])
        self.trace = trace

    @staticmethod
    def _overlaps(a: dict, b: dict) -> bool:
        return not (a["end"] < b["start"] or b["end"] < a["start"])

    @staticmethod
    def _strictly_contains(a: dict, b: dict) -> bool:
        return (
            a["start"] <= b["start"]
            and a["end"] >= b["end"]
            and (a["start"], a["end"]) != (b["start"], b["end"])
        )

    @classmethod
    def _span_distance(cls, a: dict, b: dict) -> int:
        if cls._overlaps(a, b):
            return -1
        if a["end"] < b["start"]:
            return b["start"] - a["end"] - 1
        return a["start"] - b["end"] - 1

    def resolve(self, section: List[str]) -> Tuple[Set[str], List[Tuple[dict, List[str]]]]:
        raw = [self.events[event_id]["raw_command"] for event_id in section]
        candidates: List[dict] = []

        for rule in self.rules:
            sequence = rule["exact_commands"]
            length = len(sequence)
            for start in range(0, len(section) - length + 1):
                if raw[start:start + length] != sequence:
                    continue
                candidate = {
                    "rule": rule,
                    "start": start,
                    "end": start + length - 1,
                    "span": section[start:start + length],
                }
                candidates.append(candidate)
                self.trace["type3_candidates"].append({
                    "rule_id": rule["rule_id"],
                    "events": candidate["span"],
                })

        # Duplicate exact spans should normally be rejected by Library Manager.
        # Compiler remains permissive/deterministic if they nevertheless appear.
        deduplicated: Dict[Tuple[int, int], dict] = {}
        for candidate in candidates:
            key = (candidate["start"], candidate["end"])
            previous = deduplicated.get(key)
            if previous is None or candidate["rule"]["rule_id"] < previous["rule"]["rule_id"]:
                if previous is not None:
                    self.trace["type3_skipped"].append({
                        "rule_id": previous["rule"]["rule_id"],
                        "events": previous["span"],
                        "reason": "duplicate_span",
                    })
                deduplicated[key] = candidate
            else:
                self.trace["type3_skipped"].append({
                    "rule_id": candidate["rule"]["rule_id"],
                    "events": candidate["span"],
                    "reason": "duplicate_span",
                })

        selected: List[dict] = []
        remaining = list(deduplicated.values())
        while remaining:
            undominated = [
                candidate
                for candidate in remaining
                if not any(
                    self._strictly_contains(other, candidate)
                    for other in remaining
                    if other is not candidate
                )
            ]
            if not undominated:
                break

            def rank(candidate: dict) -> tuple:
                min_distance = (
                    10**9
                    if not selected
                    else min(self._span_distance(candidate, prior) for prior in selected)
                )
                length = candidate["end"] - candidate["start"] + 1
                return (
                    -min_distance,                 # farther from resolved semantics first
                    -length,                       # then larger semantic
                    candidate["start"],            # then earlier span
                    candidate["rule"]["rule_id"], # deterministic lexical tie-break
                )

            chosen = min(undominated, key=rank)
            selected.append(chosen)

            next_remaining: List[dict] = []
            for candidate in remaining:
                if candidate is chosen:
                    continue
                if self._overlaps(candidate, chosen):
                    reason = (
                        "contained"
                        if self._strictly_contains(chosen, candidate)
                        else "crossing_overlap"
                    )
                    self.trace["type3_skipped"].append({
                        "rule_id": candidate["rule"]["rule_id"],
                        "events": candidate["span"],
                        "reason": reason,
                        "winner_rule_id": chosen["rule"]["rule_id"],
                    })
                else:
                    next_remaining.append(candidate)
            remaining = next_remaining

        selected.sort(key=lambda candidate: candidate["start"])
        protected: Set[str] = set()
        matches: List[Tuple[dict, List[str]]] = []
        for candidate in selected:
            protected.update(candidate["span"])
            matches.append((candidate["rule"], candidate["span"]))
            self.trace["type3_matches"].append({
                "rule_id": candidate["rule"]["rule_id"],
                "events": candidate["span"],
            })
        return protected, matches


def is_gui_opener(raw_command: str) -> bool:
    return normalize_ws(raw_command).upper() in GUI_OPENERS


def find_bounded_modules(
    section: List[str],
    events: Dict[str, dict],
    protected: Set[str],
    deleted_gui: Set[str],
) -> Tuple[List[Tuple[str, List[str]]], List[Tuple[str, List[str]]]]:
    """Type 2 bounded-module pass after Type 3 and Class-C cleanup.

    Class A and Class B share one deterministic matcher:
    - same HEAD before GO -> shrink candidate start to the later HEAD;
    - different known Type-2 HEAD before GO -> abandon current candidate;
    - GO closes only a still-valid candidate.

    Returns (class_a_modules, class_b_modules).
    """
    class_a_modules: List[Tuple[str, List[str]]] = []
    class_b_modules: List[Tuple[str, List[str]]] = []
    occupied: Set[str] = set(protected) | set(deleted_gui)

    def scan(start_token: str) -> List[List[str]]:
        found: List[List[str]] = []
        index = 0
        while index < len(section):
            event_id = section[index]
            command = normalize_ws(events[event_id]["raw_command"]).upper()
            if event_id in occupied or command != start_token:
                index += 1
                continue

            candidate_start = index
            tail = index + 1
            found_go = False
            invalidated = False

            while tail < len(section):
                tail_event = section[tail]
                if tail_event in occupied:
                    invalidated = True
                    break

                tail_command = normalize_ws(events[tail_event]["raw_command"]).upper()

                if tail_command == "GO":
                    found_go = True
                    break

                if tail_command in TYPE2_MODULE_HEADS:
                    if tail_command == start_token:
                        candidate_start = tail
                    else:
                        invalidated = True
                        break

                tail += 1

            if found_go and not invalidated:
                span = section[candidate_start:tail + 1]
                found.append(span)
                occupied.update(span)
                index = tail + 1
            else:
                index += 1

        return found

    # Class A has higher priority than Class B. The matcher itself is identical.
    for head in TYPE2_CLASS_A_MODULES:
        for span in scan(head):
            class_a_modules.append((head, span))

    for head in TYPE2_CLASS_B_MODULES:
        for span in scan(head):
            class_b_modules.append((head, span))

    return class_a_modules, class_b_modules


def extract_epd(state: dict) -> Optional[float]:
    """Read EPD directly from recorded EN PUP; do not derive F-number."""
    header = state.get("first_order_header")
    values = state.get("first_order_values")
    if not header or not values or "EN PUP" not in header.upper():
        return None

    numbers = re.findall(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?",
        values,
    )
    if len(numbers) < 5:
        return None
    return float(numbers[4])


def make_state_node(state: dict) -> dict:
    if state.get("file_path"):
        return {
            "type": "state",
            "state_kind": "SAV",
            "summary": {"file_path": state["file_path"]},
            "children": [],
        }

    return {
        "type": "state",
        "state_kind": "AUT",
        "summary": {
            "error_function_value": state.get("error_function_value"),
            "epd": extract_epd(state),
        },
        "children": [],
    }


# --- Semantic Tree assembly (v0.1.1 compiler.py) ---

class OpticalTrajectoryCompiler:
    """Deterministic Compiler from Recorder facts to Semantic Tree."""

    def __init__(
        self,
        event_tree: dict,
        state_library: dict,
        semantic_library: dict,
        *,
        delete_observations: bool = True,
        compile_id: Optional[str] = None,
    ):
        self.event_tree = event_tree
        self.state_library = state_library
        self.semantic_library = semantic_library
        self.delete_observations = delete_observations
        self.compile_id = compile_id or self._generate_compile_id()

        self.topology = EventTopology(event_tree, state_library)
        self.events = self.topology.events
        self.states_by_creator = self.topology.states_by_creator
        self.atomic_translator = AtomicTranslator(semantic_library)

        self.nodes: Dict[str, dict] = {}
        self.annotations: Dict[str, dict] = {}
        self.event_to_operation: Dict[str, str] = {}
        self.event_to_continuation_anchor: Dict[str, Optional[str]] = {}
        self.root_node_id: Optional[str] = None
        self._op_counter = 1
        self._annotation_counter = 1

        # Detailed compilation diagnostics remain in the optional trace output.
        # Each surviving Operation also carries its factual source Event ID.
        self.trace = {
            "compile_id": self.compile_id,
            "delete_observations": self.delete_observations,
            "deleted_gui_events": [],
            "deleted_observation_events": [],
            "empty_branches": [],
            "branch_first_survivors": [],
            "state_promotions": [],
            "type3_candidates": [],
            "type3_matches": [],
            "type3_skipped": [],
            "aut_modules": [],
            "observation_modules": [],
            "type2_class_a_modules": [],
            "type2_class_b_modules": [],
        }
        self.type3_resolver = Type3Resolver(self.events, semantic_library, self.trace)

    @staticmethod
    def _generate_compile_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"CT{stamp}"

    def _new_operation_id(self) -> str:
        node_id = f"OP{self._op_counter:06d}"
        self._op_counter += 1
        return node_id

    def _new_annotation_id(self) -> str:
        annotation_id = f"AN{self._annotation_counter:06d}"
        self._annotation_counter += 1
        return annotation_id

    def _attach_child(self, parent_id: str, child_id: str, role: str) -> None:
        children = self.nodes[parent_id].setdefault("children", [])
        if not any(child["node_id"] == child_id for child in children):
            children.append({"node_id": child_id, "role": role})

    def _resolve_anchor(self, event_id: Optional[str]) -> Optional[str]:
        """Resolve a branch anchor through State promotion or deleted-node contraction."""
        current = event_id
        while current is not None:
            if current in self.event_to_continuation_anchor:
                return self.event_to_continuation_anchor[current]
            current = self.events[current].get("parent_event_id")
        return None

    def _compile_section(
        self,
        section: List[str],
        cursor: Optional[str],
        first_edge_role: str,
    ) -> Tuple[Optional[str], int]:
        # Pass 1: Type 3 exact semantic masks.
        protected, type3_matches = self.type3_resolver.resolve(section)

        # Pass 2: remove standalone GUI openers only outside Type 3 masks.
        deleted_gui = {
            event_id
            for event_id in section
            if event_id not in protected
            and is_gui_opener(self.events[event_id]["raw_command"])
        }
        self.trace["deleted_gui_events"].extend(
            sorted(deleted_gui, key=event_number)
        )

        # Pass 3/4: Class-A semantic modules, then Class-B repeated layout views.
        class_a_modules, class_b_modules = find_bounded_modules(
            section,
            self.events,
            protected,
            deleted_gui,
        )
        self.trace["type2_class_a_modules"].extend(
            {"head": head, "events": span} for head, span in class_a_modules
        )
        self.trace["type2_class_b_modules"].extend(
            {"head": head, "events": span} for head, span in class_b_modules
        )
        # Legacy trace aliases retained for downstream compatibility.
        self.trace["aut_modules"].extend(
            span for head, span in class_a_modules if head == "AUT"
        )
        self.trace["observation_modules"].extend(
            {"head": head, "events": span} for head, span in class_b_modules
        )

        deleted_observations: Set[str] = set()
        if self.delete_observations:
            for _, span in class_b_modules:
                deleted_observations.update(span)
        self.trace["deleted_observation_events"].extend(
            sorted(deleted_observations, key=event_number)
        )

        deleted = deleted_gui | deleted_observations
        operation_for_event: Dict[str, str] = {}
        created_count = 0

        # Pass 5: atomic Type 1 translation on every surviving command.
        for event_id in section:
            if event_id in deleted:
                # Tree contraction: deleted Events inherit current continuation anchor.
                self.event_to_continuation_anchor[event_id] = cursor
                continue

            raw_command = self.events[event_id]["raw_command"]
            canonical, text, rule_id = self.atomic_translator.translate(raw_command)
            operation_id = self._new_operation_id()
            operation_node = {
                "type": "operation",
                "source_event_id": event_id,
                "canonical": canonical,
                "text": text,
                "children": [],
            }
            if rule_id is not None:
                operation_node["semantic_rule_id"] = rule_id

            self.nodes[operation_id] = operation_node
            self.event_to_operation[event_id] = operation_id
            operation_for_event[event_id] = operation_id

            if cursor is None:
                if self.root_node_id is None:
                    self.root_node_id = operation_id
            else:
                role = first_edge_role if created_count == 0 else "primary"
                self._attach_child(cursor, operation_id, role)

            if created_count == 0 and first_edge_role == "side":
                self.trace["branch_first_survivors"].append({
                    "event_id": event_id,
                    "node_id": operation_id,
                    "parent_node_id": cursor,
                })

            cursor = operation_id
            created_count += 1
            self.event_to_continuation_anchor[event_id] = cursor

            # State continuation promotion is universal for AUT and SAV State.
            if event_id in self.states_by_creator:
                state = self.states_by_creator[event_id]
                state_id = state["state_id"]
                self.nodes[state_id] = make_state_node(state)
                self._attach_child(operation_id, state_id, "primary")
                cursor = state_id
                self.event_to_continuation_anchor[event_id] = state_id
                self.trace["state_promotions"].append({
                    "event_id": event_id,
                    "operation_node_id": operation_id,
                    "state_id": state_id,
                })

        # Pass 6: annotations are attached after atomic nodes exist.
        for rule, span in type3_matches:
            members = [operation_for_event[event_id] for event_id in span if event_id in operation_for_event]
            if members:
                annotation_id = self._new_annotation_id()
                self.annotations[annotation_id] = {
                    "annotation_type": "type3",
                    "text": rule["text"],
                    "members": members,
                    "semantic_rule_id": rule["rule_id"],
                }

        for head, span in class_a_modules:
            members = [operation_for_event[event_id] for event_id in span if event_id in operation_for_event]
            if members:
                annotation_id = self._new_annotation_id()
                self.annotations[annotation_id] = {
                    "annotation_type": "type2_module",
                    "text": TYPE2_CLASS_A_MODULES[head],
                    "members": members,
                }

        if not self.delete_observations:
            for head, span in class_b_modules:
                members = [operation_for_event[event_id] for event_id in span if event_id in operation_for_event]
                if members:
                    annotation_id = self._new_annotation_id()
                    self.annotations[annotation_id] = {
                        "annotation_type": "type2_layout_view",
                        "text": TYPE2_CLASS_B_MODULES[head],
                        "members": members,
                    }

        return cursor, created_count

    def _compile_branch(self, branch: Branch) -> None:
        # A side branch starts from its resolved semantic anchor. Deleted leading
        # observations do not move this cursor; the first surviving operation
        # therefore attaches directly to the original anchor.
        cursor = self._resolve_anchor(branch.anchor_event_id)
        initial_cursor = cursor
        total_created = 0

        for section in self.topology.split_sections(branch.events):
            cursor, created = self._compile_section(
                section,
                cursor,
                branch.role if total_created == 0 else "primary",
            )
            total_created += created

        # No empty branch object/reference is ever emitted into Semantic Tree.
        if total_created == 0 and branch.role == "side":
            self.trace["empty_branches"].append({
                "branch_id": branch.branch_id,
                "anchor_event_id": branch.anchor_event_id,
                "event_ids": branch.events,
                "resolved_anchor_node_id": initial_cursor,
            })

        # Parent-first compilation guarantees anchor mappings exist for descendants.
        # If this branch was fully contracted, descendants resolve through deleted
        # Events to the nearest surviving continuation anchor and are promoted there.
        for child_branch in branch.child_branches:
            self._compile_branch(child_branch)

    def compile(self) -> Tuple[dict, dict]:
        for branch in self.topology.build_branch_forest():
            self._compile_branch(branch)

        semantic_tree = {
            "schema_version": 1,
            "compile_id": self.compile_id,
            "compiled_at": datetime.now(timezone.utc).isoformat(),
            "source_logs": [
                item["log_path"]
                for item in self.event_tree.get("command_logs", [])
            ],
            "root_node_id": self.root_node_id,
            "nodes": self.nodes,
            "annotations": self.annotations,
        }
        return semantic_tree, self.trace


# --- File I/O (v0.1.1 io.py) ---

def load_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: str | Path, payload: Any) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def validate_semantic_library_shape(library: dict) -> None:
    """Small stdlib-only validator for the frozen public Library boundary.

    The JSON Schema in schemas/semantic_library.schema.json is authoritative.
    This runtime check intentionally stays small and dependency-free.
    """
    allowed_top = {"version", "documentation", "type1_rules", "type3_rules"}
    unknown = set(library) - allowed_top
    if unknown:
        raise ValueError(
            "Semantic Library contains unsupported top-level field(s): "
            + ", ".join(sorted(unknown))
        )
    if "type2_rules" in library:
        raise ValueError("Type 2 rules are Compiler-internal and cannot exist in Semantic Library.")
    if not isinstance(library.get("type1_rules", []), list):
        raise ValueError("type1_rules must be a list")
    if not isinstance(library.get("type3_rules", []), list):
        raise ValueError("type3_rules must be a list")


# --- Command-line entry point (v0.1.1 cli.py) ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compile Recorder facts into a Semantic Tree.")
    parser.add_argument("--event-tree", required=True)
    parser.add_argument("--state-library", required=True)
    parser.add_argument("--semantic-library", required=True)
    parser.add_argument("--output", required=True, help="Semantic Tree JSON output")
    parser.add_argument("--trace-output", help="Optional compiler trace JSON")
    parser.add_argument("--compile-id", help="Optional explicit compile ID")
    parser.add_argument(
        "--keep-observations",
        action="store_true",
        help="Keep observation modules instead of deleting them",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    event_tree = load_json(args.event_tree)
    state_library = load_json(args.state_library)
    semantic_library = load_json(args.semantic_library)
    validate_semantic_library_shape(semantic_library)

    compiler = OpticalTrajectoryCompiler(
        event_tree,
        state_library,
        semantic_library,
        delete_observations=not args.keep_observations,
        compile_id=args.compile_id,
    )
    semantic_tree, trace = compiler.compile()
    save_json(args.output, semantic_tree)
    if args.trace_output:
        save_json(args.trace_output, trace)


if __name__ == "__main__":
    main()
