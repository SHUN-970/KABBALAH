#!/usr/bin/env python3
"""
Semantic Library Manager
========================
A deliberately lightweight editor for semantic_library.json.

Supported:
    Type 1: add one atomic translation rule
    Type 3: add one exact contiguous command-sequence annotation

Not supported:
    Type 2: protected Compiler behavior; never edited here
    deleting/editing/reordering rules
    semantic inference or automatic generalization

Usage:
    python semantic_library_manager.py --library semantic_library.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable


PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def load_library(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        library = json.load(f)

    if not isinstance(library, dict):
        raise ValueError("Semantic Library root must be a JSON object.")
    if not isinstance(library.get("type1_rules"), list):
        raise ValueError("Semantic Library must contain a type1_rules list.")
    if not isinstance(library.get("type3_rules"), list):
        raise ValueError("Semantic Library must contain a type3_rules list.")
    if "type2_rules" in library:
        raise ValueError(
            "type2_rules must not exist in Semantic Library; Type 2 is Compiler-protected."
        )
    return library


def atomic_save(path: Path, library: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(library, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def existing_rule_ids(library: dict) -> set[str]:
    ids = set()
    for group in ("type1_rules", "type3_rules"):
        for rule in library.get(group, []):
            rule_id = rule.get("rule_id")
            if isinstance(rule_id, str):
                ids.add(rule_id)
    return ids


def next_auto_id(library: dict, prefix: str) -> str:
    used = existing_rule_ids(library)
    index = 1
    while True:
        candidate = f"{prefix}_CUSTOM_{index:03d}"
        if candidate not in used:
            return candidate
        index += 1


def placeholders_in_object(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for v in value.values():
            found.update(placeholders_in_object(v))
    elif isinstance(value, list):
        for v in value:
            found.update(placeholders_in_object(v))
    elif isinstance(value, str):
        found.update(PLACEHOLDER_RE.findall(value))
    return found


def validate_new_type1(
    library: dict,
    rule_id: str,
    pattern: str,
    canonical: dict,
    text_template: str,
) -> None:
    if not rule_id:
        raise ValueError("rule_id cannot be empty.")
    if rule_id in existing_rule_ids(library):
        raise ValueError(f"Duplicate rule_id: {rule_id}")

    pattern = pattern.strip()
    if not pattern:
        raise ValueError("Type 1 pattern cannot be empty.")

    for rule in library["type1_rules"]:
        if rule.get("pattern") == pattern:
            raise ValueError(
                "An identical Type 1 pattern already exists: "
                f"{rule.get('rule_id', '<unknown>')}"
            )

    if not isinstance(canonical, dict):
        raise ValueError("canonical must be a JSON object.")
    if not isinstance(canonical.get("operation_type"), str) or not canonical["operation_type"]:
        raise ValueError("canonical must contain a non-empty string operation_type.")

    if not text_template.strip():
        raise ValueError("text_template cannot be empty.")

    captures = set(PLACEHOLDER_RE.findall(pattern))
    used = placeholders_in_object(canonical)
    used.update(PLACEHOLDER_RE.findall(text_template))
    unknown = used - captures
    if unknown:
        raise ValueError(
            "canonical/text_template uses placeholder(s) not captured by pattern: "
            + ", ".join(sorted(unknown))
        )


def validate_new_type3(
    library: dict,
    rule_id: str,
    commands: list[str],
    text: str,
) -> None:
    if not rule_id:
        raise ValueError("rule_id cannot be empty.")
    if rule_id in existing_rule_ids(library):
        raise ValueError(f"Duplicate rule_id: {rule_id}")
    if not commands:
        raise ValueError("Type 3 command sequence cannot be empty.")
    if any(not command.strip() for command in commands):
        raise ValueError("Type 3 command sequence cannot contain blank commands.")
    if not text.strip():
        raise ValueError("Type 3 annotation cannot be empty.")

    for rule in library["type3_rules"]:
        if rule.get("exact_commands") == commands:
            raise ValueError(
                "An identical Type 3 exact sequence already exists: "
                f"{rule.get('rule_id', '<unknown>')}"
            )


def prompt_rule_id(library: dict, prefix: str) -> str:
    suggested = next_auto_id(library, prefix)
    value = input(f"Rule ID [{suggested}]: ").strip()
    return value or suggested


def _slugify_placeholder(text: str, fallback: str) -> str:
    """Convert a short semantic label into a legal placeholder name."""
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", text.strip().lower()).strip("_")
    if not slug:
        slug = fallback
    if not re.match(r"[A-Za-z_]", slug):
        slug = "n_" + slug
    return slug


def _unique_placeholder(base: str, used: set[str]) -> str:
    candidate = base
    i = 2
    while candidate in used:
        candidate = f"{base}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _prefix_number(text: str) -> tuple[str, str] | None:
    """Recognize compact forms such as S5, C4, F12, W2."""
    match = re.fullmatch(r"([A-Za-z_]+)([-+]?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    return match.group(1), match.group(2)


def _yes_no(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    answer = input(prompt + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _ask_semantic(label: str) -> str:
    while True:
        value = input(label).strip()
        if value:
            return value
        print("Semantic description cannot be empty.")


def _build_type1_from_raw_command(raw_command: str) -> tuple[str, dict, list[str]]:
    """
    Interactively convert one raw command into a pattern and generic canonical form.

    Philosophy:
        - the program identifies syntax;
        - the user supplies semantics;
        - canonical structure is generated internally and is never authored by hand.
    """
    tokens = raw_command.split()
    if not tokens:
        raise ValueError("Raw command cannot be empty.")

    print("\nOperation type")
    print("Examples: set, vary, freeze, constrain, configure, enable, run, view, cancel")
    operation_type = _ask_semantic("Operation type: ")
    operation_type = _slugify_placeholder(operation_type, "operation")

    pattern_tokens: list[str] = []
    components: list[dict] = []
    used_placeholders: set[str] = set()
    available_placeholders: list[str] = []

    print("\nCommand semantics")
    print("For each token, Kabbalah identifies its syntax and asks only for missing meaning.")

    for index, token in enumerate(tokens, start=1):
        print(f"\nToken {index}: {token}")

        # Standalone number, e.g. 0.3 or 100.
        if _is_number(token):
            print(f'  Detected numeric token: "{token}"')
            generalize = _yes_no("  Should this number be generalized to match other numeric values?", True)
            if generalize:
                placeholder = _unique_placeholder("number", used_placeholders)
                pattern_tokens.append("{" + placeholder + "}")
                available_placeholders.append(placeholder)
                components.append({
                    "source": "{" + placeholder + "}",
                    "syntax": "number",
                    "semantic": "number",
                    "value": "{" + placeholder + "}",
                })
                print(f"  -> generalized as {{{placeholder}}}")
            else:
                meaning = _ask_semantic(f'  What does the fixed value "{token}" mean? ')
                pattern_tokens.append(token)
                components.append({
                    "source": token,
                    "syntax": "fixed_number",
                    "semantic": meaning,
                })
            continue

        # Prefix + number, e.g. S5.
        split = _prefix_number(token)
        if split:
            prefix, number = split
            print(f'  Detected prefix + number: "{prefix}" + "{number}"')
            prefix_meaning = _ask_semantic(f'  What does "{prefix}" mean? ')
            generalize = _yes_no(
                f'  Should the numeric part "{number}" be generalized to match other numbers?',
                True,
            )
            if generalize:
                base = _slugify_placeholder(prefix_meaning, "number")
                if not base.endswith("_number") and not base.endswith("_index"):
                    base += "_number"
                placeholder = _unique_placeholder(base, used_placeholders)
                pattern_tokens.append(prefix + "{" + placeholder + "}")
                available_placeholders.append(placeholder)
                components.append({
                    "source": prefix + "{" + placeholder + "}",
                    "syntax": "prefix_number",
                    "semantic": prefix_meaning,
                    "prefix": prefix,
                    "number": "{" + placeholder + "}",
                })
                print(f"  -> generalized as {prefix}{{{placeholder}}}")
            else:
                fixed_meaning = input(
                    f'  Optional meaning of fixed token "{token}" '
                    f'[{prefix_meaning} {number}]: '
                ).strip() or f"{prefix_meaning} {number}"
                pattern_tokens.append(token)
                components.append({
                    "source": token,
                    "syntax": "fixed_token",
                    "semantic": fixed_meaning,
                })
            continue

        # General literal token, e.g. ABC, AUT, CAN, QCN.
        meaning = _ask_semantic(f'  What does "{token}" mean? ')
        pattern_tokens.append(token)
        components.append({
            "source": token,
            "syntax": "literal",
            "semantic": meaning,
        })

    pattern = " ".join(pattern_tokens)
    canonical = {
        "operation_type": operation_type,
        "semantic_components": components,
    }

    return pattern, canonical, available_placeholders


def add_type1_interactive(library: dict) -> bool:
    print("\nAdd Type 1 atomic semantic rule")
    print("--------------------------------")
    print("Paste one real raw command. Kabbalah will identify simple syntax")
    print("and ask you what each part means. Canonical JSON is generated internally.")
    print("")

    rule_id = prompt_rule_id(library, "T1")
    raw_command = input("Raw command: ").strip()
    if not raw_command:
        raise ValueError("Raw command cannot be empty.")

    pattern, canonical, placeholders = _build_type1_from_raw_command(raw_command)

    print("\nGenerated pattern:")
    print(f"  {pattern}")
    if placeholders:
        print("Available placeholders for the final translation:")
        print("  " + ", ".join("{" + p + "}" for p in placeholders))
    else:
        print("No variable placeholders were generated for this command.")

    print("\nHow should the whole command be translated into natural optical-design language?")
    if placeholders:
        print("Use the placeholders shown above where the number should remain variable.")
    text_template = _ask_semantic("Semantic translation: ")

    validate_new_type1(library, rule_id, pattern, canonical, text_template)

    rule = {
        "rule_id": rule_id,
        "pattern": pattern,
        "canonical": canonical,
        "text_template": text_template,
    }

    print("\nInterpretation preview")
    print("----------------------")
    print(f"Raw example : {raw_command}")
    print(f"Pattern     : {pattern}")
    print(f"Translation : {text_template}")
    print(f"Operation   : {canonical['operation_type']}")
    print("Components:")
    for component in canonical["semantic_components"]:
        print(f"  - {component['source']} -> {component['semantic']}")

    if input("\nAdd this Type 1 rule? [y/N]: ").strip().lower() != "y":
        print("Cancelled.")
        return False

    library["type1_rules"].append(rule)
    return True

def add_type3_interactive(library: dict) -> bool:
    print("\nAdd Type 3 exact-sequence annotation")
    print("------------------------------------")
    print("Paste exact CODE V commands, one command per line.")
    print("Enter END on its own line when finished.")
    print("")

    rule_id = prompt_rule_id(library, "T3")
    text = input("Annotation text: ").strip()

    commands: list[str] = []
    print("Exact commands:")
    while True:
        line = input()
        if line == "END":
            break
        if line.strip():
            commands.append(line.rstrip())

    validate_new_type3(library, rule_id, commands, text)

    rule = {
        "rule_id": rule_id,
        "exact_commands": commands,
        "text": text,
    }

    print("\nRule to add:")
    print(json.dumps(rule, ensure_ascii=False, indent=2))
    if input("Add this Type 3 rule? [y/N]: ").strip().lower() != "y":
        print("Cancelled.")
        return False

    library["type3_rules"].append(rule)
    return True


def run_interactive(path: Path) -> None:
    while True:
        library = load_library(path)

        print("\nSemantic Library Manager")
        print("========================")
        print(f"Library: {path}")
        print(f"Type 1 rules: {len(library['type1_rules'])}")
        print(f"Type 3 rules: {len(library['type3_rules'])}")
        print("")
        print("1. Add Type 1 rule")
        print("2. Add Type 3 rule")
        print("3. Exit")

        choice = input("\nSelect: ").strip()

        try:
            if choice == "1":
                changed = add_type1_interactive(library)
            elif choice == "2":
                changed = add_type3_interactive(library)
            elif choice == "3":
                return
            else:
                print("Unknown selection.")
                continue

            if changed:
                atomic_save(path, library)
                print(f"Saved: {path}")

        except (ValueError, OSError) as exc:
            print(f"\nERROR: {exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lightweight Semantic Library Manager for Type 1 and Type 3."
    )
    parser.add_argument(
        "--library",
        default="semantic_library.json",
        help="Path to semantic_library.json (default: semantic_library.json)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    path = Path(args.library)

    if not path.exists():
        raise SystemExit(f"Semantic Library not found: {path}")

    run_interactive(path)


if __name__ == "__main__":
    main()
