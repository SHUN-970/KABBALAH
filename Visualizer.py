#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

# Visualizer v0.2 constants
LANE_GAP = 430
LEFT_MARGIN = 150
TOP_MARGIN = 105
STATE_RADIUS = 58
END_W = 68
END_H = 36
HUMAN_LEN = 108
OPT_LEN = 50
EMPTY_GAP = 42
LINE_WIDTH = 12
PANEL_WIDTH = 340
OPT_TEXT = "Set and run optimization"
BRANCH_COLOR = "#8a8a8a"
SEMANTIC_MANAGER_ENDPOINT = "http://127.0.0.1:8765/add-semantic"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_float(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "N/A"
    if abs(v) >= 100:
        return f"{v:.3f}"
    if abs(v) >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.6f}".rstrip("0").rstrip(".")


def filename_only(p: str) -> str:
    try:
        return PureWindowsPath(p).name or p
    except Exception:
        return p


def esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


class Projector:
    """
    Semantic Tree -> presentation graph.

    Important v0.2 behavior:
    - operation-level branches do NOT split the primary visual edge;
    - the primary edge continues as one black/red sequence;
    - side branches attach through a branch anchor recorded on that edge;
    - branch anchors are presentation-only and never become visible nodes.
    """

    def __init__(self, tree: dict):
        self.tree = tree
        self.nodes = tree.get("nodes", {})
        self.annotations = tree.get("annotations", {})
        self.root_semantic = tree.get("root_node_id")
        self.semantic_roots = self._find_semantic_roots()

        self.display_nodes: Dict[str, dict] = {}
        self.edges: List[dict] = []
        self.anchors: Dict[str, dict] = {}

        self.edge_i = 1
        self.anchor_i = 1
        self.end_i = 1
        self.segment_i = 1
        self.next_side_lane = 1

        self.opt_member_to_ann: Dict[str, str] = {}
        for aid, ann in self.annotations.items():
            if ann.get("text") == OPT_TEXT:
                for mid in ann.get("members", []):
                    self.opt_member_to_ann[mid] = aid

    def _find_semantic_roots(self) -> List[str]:
        """Return every disconnected Semantic Tree root in stable order.

        The formal schema exposes one ``root_node_id``, but Recorder storage can
        legitimately contain several independent Event roots (for example,
        separate logs without a matching RES State).  Compiler preserves all
        of those components in the flat node table.  Presentation must discover
        and render every zero-parent component instead of silently showing only
        the component named by ``root_node_id``.
        """

        referenced = {
            child.get("node_id")
            for node in self.nodes.values()
            for child in node.get("children", [])
            if child.get("node_id") in self.nodes
        }
        roots = sorted(node_id for node_id in self.nodes if node_id not in referenced)
        if self.root_semantic in roots:
            roots.remove(self.root_semantic)
            roots.insert(0, self.root_semantic)
        elif not roots and self.root_semantic in self.nodes:
            # Defensive fallback for malformed/cyclic input: preserve the
            # previous single-root behavior rather than emitting an empty view.
            roots = [self.root_semantic]
        return roots

    def _new_end(self) -> str:
        nid = f"END_{self.end_i:04d}"
        self.end_i += 1
        self.display_nodes[nid] = {"id": nid, "kind": "end", "visible": True}
        return nid

    def _new_anchor(self, parent_edge_id: str, after_operation_id: str) -> str:
        aid = f"ANCHOR_{self.anchor_i:04d}"
        self.anchor_i += 1
        self.anchors[aid] = {
            "id": aid,
            "parent_edge_id": parent_edge_id,
            "after_operation_id": after_operation_id,
            "side_edge_ids": [],
        }
        return aid

    def _state_node(self, sid: str, state: dict) -> dict:
        summary = state.get("summary", {}) or {}
        file_path = summary.get("file_path")
        if file_path:
            return {
                "id": sid,
                "kind": "file_state",
                "visible": True,
                "file_path": str(file_path),
                "short_file": filename_only(str(file_path)),
                "state_kind": state.get("state_kind"),
            }
        if state.get("state_kind") == "AUT":
            return {
                "id": sid,
                "kind": "aut_state",
                "visible": True,
                "errf": safe_float(summary.get("error_function_value")),
                "epd": safe_float(summary.get("epd")),
                "state_kind": "AUT",
            }
        return {
            "id": sid,
            "kind": "generic_state",
            "visible": True,
            "state_kind": state.get("state_kind"),
            "summary": summary,
        }

    def _segmentize(self, op_ids: List[str]) -> List[dict]:
        groups: List[dict] = []
        for oid in op_ids:
            opt_aid = self.opt_member_to_ann.get(oid)
            kind = "optimization" if opt_aid else "human"
            key = opt_aid if opt_aid else None
            if groups and groups[-1]["kind"] == kind and groups[-1].get("annotation_id") == key:
                groups[-1]["operations"].append(oid)
            else:
                sid = f"SEG_{self.segment_i:05d}"
                self.segment_i += 1
                groups.append({
                    "id": sid,
                    "kind": kind,
                    "annotation_id": key,
                    "operations": [oid],
                })
        return groups

    @staticmethod
    def _primary_and_sides(children: List[dict]) -> Tuple[dict, List[dict]]:
        if not children:
            raise ValueError("children cannot be empty")
        primary = next((c for c in children if c.get("role") == "primary"), children[0])
        sides = [c for c in children if c is not primary]
        return primary, sides

    def _alloc_side_lane(self) -> int:
        lane = self.next_side_lane
        self.next_side_lane += 1
        return lane

    def build(self) -> Tuple[Dict[str, dict], List[dict], Dict[str, dict], str]:
        root = "VIRTUAL_ROOT"
        self.display_nodes[root] = {"id": root, "kind": "virtual", "visible": True}

        if not self.semantic_roots:
            end = self._new_end()
            self._build_edge("node", root, None, end, lane=0, role="primary")
            return self.display_nodes, self.edges, self.anchors, root

        # Give each independent Semantic Tree one contiguous lane block.  Build
        # a complete tree (primary path plus all of its side branches) before
        # allocating the first lane of the next tree.  Reserving every root
        # lane up front would interleave the components: side branches from the
        # leftmost tree would be pushed to the far right of later roots.
        self.next_side_lane = 0
        for index, semantic_root in enumerate(self.semantic_roots):
            root_lane = self.next_side_lane
            self.next_side_lane += 1
            self._build_edge_from_semantic(
                source_type="node",
                source_id=root,
                semantic_start=semantic_root,
                lane=root_lane,
                role="primary" if index == 0 else "side",
            )
        return self.display_nodes, self.edges, self.anchors, root

    def _build_edge(self, source_type: str, source_id: str, semantic_start: Optional[str], target_id: str,
                    lane: int, role: str, operations: Optional[List[str]] = None) -> dict:
        eid = f"EDGE_{self.edge_i:05d}"
        self.edge_i += 1
        edge = {
            "id": eid,
            "source_type": source_type,
            "source_id": source_id,
            "target": target_id,
            "lane": lane,
            "role": role,
            "operations": list(operations or []),
            "segments": [],
            "anchor_ids": [],
        }
        self.edges.append(edge)
        return edge

    def _build_edge_from_semantic(self, source_type: str, source_id: str, semantic_start: str,
                                  lane: int, role: str) -> None:
        eid = f"EDGE_{self.edge_i:05d}"
        self.edge_i += 1
        edge = {
            "id": eid,
            "source_type": source_type,
            "source_id": source_id,
            "target": None,
            "lane": lane,
            "role": role,
            "operations": [],
            "segments": [],
            "anchor_ids": [],
        }
        self.edges.append(edge)

        current = semantic_start
        while True:
            node = self.nodes[current]
            ntype = node.get("type")

            if ntype == "state":
                if current not in self.display_nodes:
                    self.display_nodes[current] = self._state_node(current, node)
                edge["target"] = current
                edge["segments"] = self._segmentize(edge["operations"])

                children = node.get("children", [])
                if not children:
                    return
                primary, sides = self._primary_and_sides(children)

                # Primary continuation stays in the same lane.
                self._build_edge_from_semantic(
                    source_type="node",
                    source_id=current,
                    semantic_start=primary["node_id"],
                    lane=lane,
                    role="primary",
                )
                # Side continuations receive new lanes to the right.
                for child in sides:
                    self._build_edge_from_semantic(
                        source_type="node",
                        source_id=current,
                        semantic_start=child["node_id"],
                        lane=self._alloc_side_lane(),
                        role="side",
                    )
                return

            if ntype == "operation":
                edge["operations"].append(current)

            children = node.get("children", [])
            if not children:
                end = self._new_end()
                edge["target"] = end
                edge["segments"] = self._segmentize(edge["operations"])
                return

            if len(children) == 1:
                current = children[0]["node_id"]
                continue

            # Branch inside the operation interval.
            # The primary visual edge remains intact; only side branches start from an anchor.
            primary, sides = self._primary_and_sides(children)
            anchor_id = self._new_anchor(edge["id"], current)
            edge["anchor_ids"].append(anchor_id)

            for child in sides:
                side_lane = self._alloc_side_lane()
                before_count = len(self.edges)
                self._build_edge_from_semantic(
                    source_type="anchor",
                    source_id=anchor_id,
                    semantic_start=child["node_id"],
                    lane=side_lane,
                    role="side",
                )
                # The recursive call appends at least one edge; record the first edge as anchor child.
                if len(self.edges) > before_count:
                    self.anchors[anchor_id]["side_edge_ids"].append(self.edges[before_count]["id"])

            current = primary["node_id"]


class Layout:
    def __init__(self, nodes: Dict[str, dict], edges: List[dict], anchors: Dict[str, dict], root: str):
        self.nodes = nodes
        self.edges = edges
        self.anchors = anchors
        self.root = root

        self.edge_by_id = {e["id"]: e for e in edges}
        self.node_out_edges: Dict[str, List[dict]] = {}
        self.anchor_out_edges: Dict[str, List[dict]] = {}
        for e in edges:
            if e["source_type"] == "node":
                self.node_out_edges.setdefault(e["source_id"], []).append(e)
            else:
                self.anchor_out_edges.setdefault(e["source_id"], []).append(e)

        self.positions: Dict[str, Tuple[float, float]] = {root: (LEFT_MARGIN, TOP_MARGIN)}
        self.edge_geom: Dict[str, dict] = {}
        self.anchor_pos: Dict[str, Tuple[float, float]] = {}

    def half_h(self, nid: str) -> float:
        kind = self.nodes[nid]["kind"]
        if kind in {"virtual", "aut_state", "file_state", "generic_state"}:
            return STATE_RADIUS
        if kind == "end":
            return END_H / 2
        return 0

    @staticmethod
    def segment_total(edge: dict) -> float:
        if not edge.get("segments"):
            return 0
        return sum(HUMAN_LEN if s["kind"] == "human" else OPT_LEN for s in edge["segments"])

    def build(self) -> Tuple[Dict[str, Tuple[float, float]], Dict[str, dict], Dict[str, Tuple[float, float]], int, int]:
        self._layout_node(self.root)
        max_x = max([x for x, _ in self.positions.values()] + [LEFT_MARGIN])
        max_y = max([y for _, y in self.positions.values()] + [TOP_MARGIN])
        return self.positions, self.edge_geom, self.anchor_pos, int(max_x + LANE_GAP), int(max_y + 220)

    def _source_start(self, edge: dict) -> Tuple[float, float, Optional[Tuple[float, float, float, float]]]:
        """Return vertical segment start (x,y) and optional horizontal branch connector."""
        lane_x = LEFT_MARGIN + edge["lane"] * LANE_GAP

        if edge["source_type"] == "anchor":
            ax, ay = self.anchor_pos[edge["source_id"]]
            # Gray is horizontal-only and means "new branch".
            return lane_x, ay, (ax, ay, lane_x, ay)

        sx, sy = self.positions[edge["source_id"]]
        source_kind = self.nodes[edge["source_id"]]["kind"]
        source_lane_x = sx

        if lane_x == source_lane_x:
            return lane_x, sy + self.half_h(edge["source_id"]), None

        # Side branch directly from a State: horizontal branch connector only.
        start_x = sx + (STATE_RADIUS if source_kind != "end" else END_W / 2)
        return lane_x, sy, (start_x, sy, lane_x, sy)

    def _layout_node(self, node_id: str) -> None:
        for edge in self.node_out_edges.get(node_id, []):
            self._layout_edge(edge)

    def _layout_edge(self, edge: dict) -> None:
        x, y_start, branch_connector = self._source_start(edge)
        segments = edge.get("segments", [])
        seg_geoms = []
        cy = y_start

        for seg in segments:
            length = HUMAN_LEN if seg["kind"] == "human" else OPT_LEN
            y1, y2 = cy, cy + length
            seg_geoms.append({
                "segment_id": seg["id"],
                "kind": seg["kind"],
                "x": x,
                "y1": y1,
                "y2": y2,
            })
            cy = y2

        if not segments:
            cy += EMPTY_GAP

        target = edge["target"]
        target_y = cy + self.half_h(target)
        self.positions[target] = (x, target_y)

        self.edge_geom[edge["id"]] = {
            "branch_connector": branch_connector,
            "segments": seg_geoms,
        }

        # Resolve branch anchors AFTER segment geometry is known.
        for anchor_id in edge.get("anchor_ids", []):
            anchor = self.anchors[anchor_id]
            op_id = anchor["after_operation_id"]
            ay = self._anchor_y(edge, seg_geoms, op_id)
            self.anchor_pos[anchor_id] = (x, ay)

        # Lay out side branches attached to these anchors.
        for anchor_id in edge.get("anchor_ids", []):
            for child_edge in self.anchor_out_edges.get(anchor_id, []):
                self._layout_edge(child_edge)

        # Continue from the visible target node.
        self._layout_node(target)

    def _anchor_y(self, edge: dict, seg_geoms: List[dict], op_id: str) -> float:
        for seg, geom in zip(edge.get("segments", []), seg_geoms):
            if op_id in seg.get("operations", []):
                ops = seg["operations"]
                idx = ops.index(op_id)
                # Approximate visual position only; exact command-level topology remains in Semantic Tree.
                frac = (idx + 1) / max(1, len(ops))
                frac = max(0.18, min(0.82, frac))
                return geom["y1"] + frac * (geom["y2"] - geom["y1"])
        # Defensive fallback: attach to middle of first available segment.
        if seg_geoms:
            g = seg_geoms[0]
            return (g["y1"] + g["y2"]) / 2
        # Edge with no visual segment is extremely unusual; attach at source level.
        _, sy, _ = self._source_start(edge)
        return sy


def annotation_maps(tree: dict) -> Tuple[Dict[str, str], Dict[str, str]]:
    first = {}
    member = {}
    for aid, ann in tree.get("annotations", {}).items():
        members = ann.get("members", [])
        if members:
            first[members[0]] = aid
        for mid in members:
            member[mid] = aid
    return first, member


def branch_dot_html(marked: bool) -> str:
    return '<span class="branch-op-dot" title="A side branch starts after this operation"></span>' if marked else '<span class="branch-op-dot spacer"></span>'


def operation_label_html(operation_id: str, node: dict) -> str:
    return (
        f'<span><span class="operation-id">{esc(operation_id)}</span>'
        f'{esc(node.get("text", ""))}</span>'
    )


def human_panel(tree: dict, op_ids: List[str], branch_ops: set[str]) -> str:
    nodes = tree["nodes"]
    anns = tree.get("annotations", {})
    first, _ = annotation_maps(tree)
    op_set = set(op_ids)
    out = ['<div class="panel-title">Human operations</div>']
    i = 0

    while i < len(op_ids):
        oid = op_ids[i]
        aid = first.get(oid)
        if aid:
            ann = anns[aid]
            members = ann.get("members", [])
            if ann.get("text") != OPT_TEXT and members and all(m in op_set for m in members):
                rows = "".join(
                    f'<div class="atomic-op branch-row">{branch_dot_html(m in branch_ops)}{operation_label_html(m, nodes[m])}</div>'
                    for m in members if m in nodes
                )
                out.append(
                    '<details class="annotation-detail">'
                    f'<summary>{esc(ann.get("text", ""))}</summary>'
                    f'<div class="annotation-body">{rows}</div>'
                    '</details>'
                )
                i += len(members)
                continue

        out.append(
            f'<div class="operation-row branch-row">{branch_dot_html(oid in branch_ops)}{operation_label_html(oid, nodes[oid])}</div>'
        )
        i += 1
    return "".join(out)


def optimization_panel(tree: dict, aid: Optional[str], op_ids: List[str], branch_ops: set[str]) -> str:
    nodes = tree["nodes"]
    anns = tree.get("annotations", {})
    title = anns.get(aid, {}).get("text", OPT_TEXT) if aid else OPT_TEXT
    rows = "".join(
        f'<div class="operation-row branch-row">{branch_dot_html(oid in branch_ops)}{operation_label_html(oid, nodes[oid])}</div>'
        for oid in op_ids if oid in nodes
    )
    return f'<div class="panel-title optimization-title">{esc(title)}</div>{rows}'


def fill_for(node: dict) -> str:
    if node["kind"] == "virtual":
        return "#ffffff"
    if node["kind"] == "file_state":
        return "#8ec5ff"
    if node["kind"] == "aut_state":
        errf = node.get("errf")
        if errf is None:
            return "#d9d9d9"
        if errf < 1:
            return "#8fd694"
        if errf < 100:
            return "#f4d76b"
        return "#ef8b83"
    return "#d9d9d9"


def svg_node(nid: str, node: dict, x: float, y: float) -> str:
    kind = node["kind"]
    if kind == "end":
        return (
            f'<g id="NODE_{esc(nid)}" class="end-node">'
            f'<rect x="{x-END_W/2}" y="{y-END_H/2}" width="{END_W}" height="{END_H}" rx="4" />'
            f'<text x="{x}" y="{y+4}" text-anchor="middle">END</text></g>'
        )

    attrs = ' data-virtual-root="true"' if kind == "virtual" else ""
    title = f'<title>{esc(node.get("file_path", ""))}</title>' if kind == "file_state" else ""
    parts = [f'<g id="NODE_{esc(nid)}">{title}']
    parts.append(f'<circle cx="{x}" cy="{y}" r="{STATE_RADIUS}" fill="{fill_for(node)}" class="state-node"{attrs}/>')

    if kind == "virtual":
        lines = [(y-6, "DESIGN"), (y+15, "TARGET")]
    elif kind == "aut_state":
        parts.append(f'<text x="{x}" y="{y-28}" text-anchor="middle" class="state-id">{esc(nid)}</text>')
        lines = [(y-5, f"ERR.F. = {fmt_num(node.get('errf'))}"), (y+17, f"EPD = {fmt_num(node.get('epd'))}")]
    elif kind == "file_state":
        parts.append(f'<text x="{x}" y="{y-28}" text-anchor="middle" class="state-id">{esc(nid)}</text>')
        name = node.get("short_file", "")
        if len(name) > 18:
            name = name[:15] + "..."
        lines = [(y-5, "SAV"), (y+17, name)]
    else:
        parts.append(f'<text x="{x}" y="{y-28}" text-anchor="middle" class="state-id">{esc(nid)}</text>')
        lines = [(y-5, str(node.get("state_kind") or "STATE")), (y+17, "STATE")]

    for ty, txt in lines:
        parts.append(f'<text x="{x}" y="{ty}" text-anchor="middle" class="state-text">{esc(txt)}</text>')
    if kind == "virtual":
        parts.append(f'<text id="design-target-label" x="{x}" y="{y+STATE_RADIUS+28}" text-anchor="middle" class="target-label">Click to define design target</text>')
    parts.append('</g>')
    return "".join(parts)


def generate_html(tree: dict) -> str:
    projector = Projector(tree)
    dnodes, dedges, anchors, droot = projector.build()
    layout = Layout(dnodes, dedges, anchors, droot)
    positions, geom, anchor_pos, width, height = layout.build()

    edge_by_id = {e["id"]: e for e in dedges}
    branch_ops_by_edge: Dict[str, set[str]] = {e["id"]: set() for e in dedges}
    for anchor in anchors.values():
        branch_ops_by_edge[anchor["parent_edge_id"]].add(anchor["after_operation_id"])

    svg: List[str] = []
    panel_data: Dict[str, dict] = {}
    state_data: Dict[str, dict] = {}

    # Horizontal branch connectors only. No gray vertical continuation exists in v0.2.
    for edge in dedges:
        conn = geom[edge["id"]].get("branch_connector")
        if conn:
            x1, y1, x2, y2 = conn
            svg.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="branch-connector"/>')

    # Continuous black/red vertical segments.
    for edge in dedges:
        seg_by_id = {s["id"]: s for s in edge.get("segments", [])}
        branch_ops = branch_ops_by_edge.get(edge["id"], set())
        for sg in geom[edge["id"]]["segments"]:
            s = seg_by_id[sg["segment_id"]]
            cls = "human-segment" if s["kind"] == "human" else "optimization-segment"
            svg.append(
                f'<line id="{s["id"]}" x1="{sg["x"]}" y1="{sg["y1"]}" x2="{sg["x"]}" y2="{sg["y2"]}" '
                f'class="trajectory-segment {cls}" data-segment-id="{s["id"]}"/>'
            )
            seg_branch_ops = set(s["operations"]) & branch_ops
            if s["kind"] == "human":
                content = human_panel(tree, s["operations"], seg_branch_ops)
            else:
                content = optimization_panel(tree, s.get("annotation_id"), s["operations"], seg_branch_ops)
            panel_data[s["id"]] = {"x": sg["x"] + 28, "y": sg["y1"], "html": content}

    # Black dots mark operation-level branch origins on the uninterrupted parent segment.
    for anchor_id, (ax, ay) in anchor_pos.items():
        svg.append(f'<circle cx="{ax}" cy="{ay}" r="{LINE_WIDTH/2 + 2}" class="branch-origin-dot"><title>Side branch starts here</title></circle>')

    # Visible nodes.
    for nid, node in dnodes.items():
        x, y = positions[nid]
        svg.append(svg_node(nid, node, x, y))
        if node["kind"] == "file_state":
            state_data[nid] = {
                "x": x + 74,
                "y": y - 20,
                "html": '<div class="panel-title">Saved state</div>'
                        + f'<div class="operation-row"><strong>File:</strong><br>{esc(node.get("file_path", ""))}</div>'
            }

    compile_id = str(tree.get("compile_id", "trajectory"))
    pdata = json.dumps(panel_data, ensure_ascii=False)
    sdata = json.dumps(state_data, ensure_ascii=False)

    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Optical Design Trajectory - {esc(compile_id)}</title>
<style>
:root{{--bg:#f7f7f5;--ink:#171717;--muted:#666;--panel:rgba(255,255,255,.97);--red:#c93832;--branch:{BRANCH_COLOR};}}
*{{box-sizing:border-box}} html,body{{margin:0;padding:0;background:var(--bg);color:var(--ink);font-family:Inter,"Segoe UI",Arial,sans-serif}}
.topbar{{position:sticky;top:0;z-index:20;height:54px;padding:0 18px;display:flex;align-items:center;gap:18px;background:rgba(247,247,245,.94);border-bottom:1px solid #ddd;backdrop-filter:blur(8px)}}
.title{{font-weight:700}} .meta{{font-size:13px;color:var(--muted)}} .semantic-button{{margin-left:auto;padding:7px 11px;border:1px solid #b8b8b8;border-radius:6px;background:#fff;color:#222;font-size:12px;font-weight:700;cursor:pointer}} .semantic-button:hover{{background:#f0f0ee}} .semantic-button:disabled{{opacity:.55;cursor:default}} .legend{{display:flex;gap:14px;font-size:12px;color:#555}}
.legend-item{{display:inline-flex;align-items:center;gap:5px}} .legend-line{{width:22px;border-top:{LINE_WIDTH/2}px solid #111;border-radius:4px}} .legend-line.red{{border-top-color:var(--red)}} .legend-line.gray{{border-top-color:var(--branch)}}
.workspace{{position:relative;min-width:{width}px;min-height:{height}px;width:{width}px;height:{height}px}}
svg{{position:absolute;left:0;top:0;overflow:visible}}
.state-node{{stroke:#171717;stroke-width:2.5}} .state-id{{font-size:11px;font-weight:800;pointer-events:none}} .state-text{{font-size:12px;font-weight:650;pointer-events:none}} .target-label{{font-size:12px;fill:#555;pointer-events:none}}
.end-node rect{{fill:#fff;stroke:#444;stroke-width:1.8}} .end-node text{{font-size:12px;font-weight:700}}
.branch-connector{{stroke:var(--branch);stroke-width:{LINE_WIDTH};stroke-linecap:round;fill:none}}
.trajectory-segment{{stroke-linecap:butt;stroke-width:{LINE_WIDTH};cursor:pointer}} .human-segment{{stroke:#161616}} .optimization-segment{{stroke:var(--red)}} .trajectory-segment:hover{{stroke-width:{LINE_WIDTH+4}}}
.branch-origin-dot{{fill:#111;stroke:#111;stroke-width:1}}
.detail-panel{{position:absolute;z-index:15;width:{PANEL_WIDTH}px;max-height:70vh;overflow:auto;padding:13px 14px 14px;background:var(--panel);border:1px solid #cfcfcf;border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.13);display:none}}
.panel-title{{font-size:14px;font-weight:750;margin-bottom:9px;padding-bottom:8px;border-bottom:1px solid #ddd}} .optimization-title{{color:#9e2925}}
.operation-row,.atomic-op{{padding:5px 0;font-family:"Cascadia Mono",Consolas,monospace;font-size:12px;line-height:1.45;overflow-wrap:anywhere;word-break:break-word}}
.operation-id{{font-weight:700;color:#555;white-space:nowrap;margin-right:6px}}
.branch-row{{display:grid;grid-template-columns:14px 1fr;column-gap:4px;align-items:start}}
.branch-op-dot{{width:7px;height:7px;border-radius:50%;background:#111;margin-top:5px;display:inline-block}} .branch-op-dot.spacer{{background:transparent}}
.annotation-detail{{margin:6px 0;border-left:3px solid #8b8b8b;padding-left:8px}} .annotation-detail summary{{cursor:pointer;font-size:12px;font-weight:700;line-height:1.4;overflow-wrap:anywhere}} .annotation-body{{margin-top:5px;padding-left:6px}}
.close-panel{{float:right;border:none;background:transparent;cursor:pointer;font-size:17px;line-height:1;color:#666}} .target-editor textarea{{width:100%;min-height:105px;resize:vertical;font:inherit;padding:8px}} .target-editor button{{margin-top:8px;padding:6px 11px;border:1px solid #999;border-radius:5px;background:#fff;cursor:pointer}}
</style>
</head>
<body>
<div class="topbar"><div class="title">Optical Design Trajectory</div><div class="meta">Compile ID: {esc(compile_id)}</div><button class="semantic-button" id="add-semantic-button" type="button">Add New Semantic</button><div class="legend"><span class="legend-item"><span class="legend-line"></span>Human operations</span><span class="legend-item"><span class="legend-line red"></span>Optimization</span><span class="legend-item"><span class="legend-line gray"></span>New branch</span></div></div>
<div class="workspace" id="workspace">
<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" aria-label="Optical design trajectory">{''.join(svg)}</svg>
<div class="detail-panel" id="detail-panel"><button class="close-panel" id="close-panel" title="Close">x</button><div id="detail-content"></div></div>
</div>
<script>
const panelData = {pdata};
const stateData = {sdata};
const panel = document.getElementById('detail-panel');
const content = document.getElementById('detail-content');
function openPanel(x,y,htmlContent){{content.innerHTML=htmlContent;panel.style.left=x+'px';panel.style.top=y+'px';panel.style.display='block';}}
function hidePanel(){{panel.style.display='none';}}
const addSemanticButton=document.getElementById('add-semantic-button');
if(addSemanticButton){{addSemanticButton.addEventListener('click',async()=>{{
  const original=addSemanticButton.textContent;
  addSemanticButton.disabled=true;
  addSemanticButton.textContent='Opening...';
  try{{
    const response=await fetch('http://127.0.0.1:8765/add-semantic',{{method:'POST'}});
    if(!response.ok) throw new Error('bridge unavailable');
    addSemanticButton.textContent='Opened';
    setTimeout(()=>{{addSemanticButton.textContent=original;addSemanticButton.disabled=false;}},900);
  }}catch(err){{
    addSemanticButton.textContent=original;
    addSemanticButton.disabled=false;
    alert('Kabbalah local host is not running. Start Kabbalah through main.py, then use this button again.');
  }}
}});}}
document.getElementById('close-panel').addEventListener('click',hidePanel);
document.querySelectorAll('[data-segment-id]').forEach(el=>{{el.addEventListener('click',ev=>{{ev.stopPropagation();const d=panelData[el.dataset.segmentId];if(d)openPanel(d.x,d.y,d.html);}});}});
Object.entries(stateData).forEach(([nid,d])=>{{const g=document.getElementById('NODE_'+nid);if(!g)return;g.style.cursor='pointer';g.addEventListener('click',ev=>{{ev.stopPropagation();openPanel(d.x,d.y,d.html);}});}});
const virtualCircle=document.querySelector('[data-virtual-root="true"]');
const targetLabel=document.getElementById('design-target-label');
const storageKey='optical-design-target::{esc(compile_id)}';
function refreshTargetLabel(){{const v=localStorage.getItem(storageKey)||'';if(targetLabel)targetLabel.textContent=v?(v.length>54?v.slice(0,51)+'...':v):'Click to define design target';}}
if(virtualCircle){{virtualCircle.style.cursor='pointer';virtualCircle.addEventListener('click',ev=>{{ev.stopPropagation();const existing=localStorage.getItem(storageKey)||'';const safe=existing.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');openPanel({LEFT_MARGIN+74},{TOP_MARGIN-20},'<div class="panel-title">Design target</div><div class="target-editor"><textarea id="target-input" placeholder="Describe the optical design target...">'+safe+'</textarea><button id="save-target">Save target</button></div>');document.getElementById('save-target').addEventListener('click',()=>{{const v=document.getElementById('target-input').value.trim();localStorage.setItem(storageKey,v);refreshTargetLabel();hidePanel();}});}});}}
refreshTargetLabel();
</script>
</body>
</html>'''


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate self-contained optical trajectory HTML from semantic_tree.json")
    ap.add_argument("--input", required=True, help="semantic_tree.json")
    ap.add_argument("--output", default="trajectory.html", help="output HTML path")
    args = ap.parse_args()

    inp = Path(args.input)
    out = Path(args.output)
    tree = load_json(inp)
    out.write_text(generate_html(tree), encoding="utf-8")

    p = Projector(tree)
    dnodes, dedges, anchors, _ = p.build()
    segs = sum(len(e.get("segments", [])) for e in dedges)
    print(f"Generated: {out}")
    print(f"Semantic roots: {len(p.semantic_roots)}")
    print(f"Display nodes: {len(dnodes)}")
    print(f"Display edges: {len(dedges)}")
    print(f"Operation-branch anchors: {len(anchors)}")
    print(f"Visual segments: {segs}")


if __name__ == "__main__":
    main()
