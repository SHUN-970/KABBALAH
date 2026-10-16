# KABBALAH: Local Usage Guide for AI Agents

KABBALAH processes raw CODE V command logs into a design history, a semantic trajectory, and an HTML visualization. This guide describes the **current local directory**. Before running it, check `Kabbalah_main.py --help` and the relevant source files in this directory. Do not substitute an older archive, a library copy, or scripts recalled from a previous conversation.

## Components and data boundaries

| Component | Responsibility | Write boundary |
| --- | --- | --- |
| `Kabbalah_main.py` | Orchestrates the stages, requests inheritance decisions, and launches managers | Does not directly reconstruct or modify Recorder history topology |
| `Recorder.py` | Parses logs, handles Undo/Redo, and builds Events and States | `event_tree.json` and `state_library.json` in the project directory |
| `RulesManager.py` | Maintains semantic Undo mappings | `recorder_rules.json`; Recorder only reads this file |
| `compiler.py` | Compiles Recorder history with the Semantic Library | `semantic_tree.json` in the project directory |
| `semantic_library_manager.py` | Interactively maintains Type 1 and Type 3 semantic rules | `semantic_library.json` |
| `Visualizer.py` | Renders the Semantic Tree | `trajectory.html` in the project directory |

`event_tree.json` and `state_library.json` are a paired factual history: either both must exist or neither may exist. `semantic_tree.json` and `trajectory.html` are derived outputs that can be regenerated. A Semantic Operation's `source_event_id` points back to its Recorder Event; semantic translation does not alter the raw command or Event topology.

## Importing a new log

1. Use the **original CODE V `.txt` log** and project directory specified by the user. If multiple logs belong to one project, import them into the same directory, one at a time, in the user's specified order. Do not import concurrently, infer inheritance from filenames alone, or import a log already listed in `command_logs` again.
2. Before importing, inspect `command_logs` in the existing `event_tree.json` and State IDs in `state_library.json`. Check the expected next LOG/Event/State numbers. Save hashes or backups of the factual history files; do not manually clear, replace, or rebuild existing project history.
3. Preflight the initial State with the local `Recorder.inspect_initial_state(log_path)`. `MATCHED` means the log's `RES` path exactly matched the latest historical SAV State with that path. `NO_RES` and `RES_UNMATCHED` require an explicit choice of an existing State or a new independent tree.
4. If the user requests a State **different from** an already matched `RES` State, **stop and identify the exact RES line**. The current program does not allow overriding a successful RES match. Let the user decide whether to edit the original log. Do not silently remove or change RES, tamper with the State Library, reparent generated Events, or add an override interface.
5. Once inheritance is settled, run the main entry point in this directory. Pass `--log` and `--project-dir` explicitly to avoid selecting the wrong directory. If there is no automatic match, Main asks whether to inherit an existing State or start an independent tree; `--non-interactive` **cannot** make that decision for the user.

PowerShell example (run from this directory and replace the sample paths with the actual task paths):

```powershell
Set-Location 'D:\optical_design\Kabbalah'
$logPath = 'D:\optical_design\command log\example.txt'
$projectDir = 'D:\optical_design\example_project'
python .\Kabbalah_main.py --log $logPath --project-dir $projectDir --no-browser --no-bridge
```

`--no-browser --no-bridge` only disables automatic browser opening and the local semantic-button service; Recorder, Compiler, and Visualizer still run. Use `--non-interactive` only when the initial State is already resolved and no interaction is expected; it does not automatically resolve a new semantic Undo. By default, semantic compilation folds repeated VIE/V3D layout-view transactions. Use `--keep-layout-views` if they must be retained.

## Handling semantic Undo errors

For an Undo, Recorder first looks for the exact raw command on the active path. It then uses `recorder_rules.json` to resolve a semantic target to its source command or GUI transaction opener. On `UNRESOLVED_SEMANTIC_UNDO`, Recorder returns provisional diagnostics and **does not commit that log to the Event Tree or State Library**. Main may launch RulesManager; after a rule is saved, it replays the original log.

First compare the Undo with preceding commands and indentation in the raw log. Check whether a rule is genuinely missing or a parser-boundary issue has caused a cascade of missing-command diagnostics. Add only a mapping supported by the raw evidence. Do not disguise an ordinary command, report output, or cascading error as a new semantic Undo rule. Verify the target's capitalization, the source-command format, and the meaning of parameters such as `{index}` and `{value}`. `RulesManager.py` can confirm a rule interactively from a raw snippet and is responsible for validation and saving; Recorder must not guess or write rules.

## Updating semantics and visualization only

After changing `semantic_library.json`, **do not re-import the raw log**. Run this on the existing project:

```powershell
python .\Kabbalah_main.py --recompile-only --project-dir $projectDir
```

This mode calls only Compiler and Visualizer, updating `semantic_tree.json` and `trajectory.html`. Verify that the before/after hashes of `event_tree.json` and `state_library.json` are identical. `--recompile-only` requires the existing history pair and cannot be combined with `--log`. The Semantic Library accepts only the top-level fields in its current schema. Type 2 rules are internal to Compiler: **do not** add `type2_rules` to `semantic_library.json`. The interactive Semantic Library Manager currently supports Type 1 and Type 3 only.

## Verification after a run

- All three main stages report success. The output directory contains three parseable JSON files and one HTML file.
- The new `command_logs` entries have the requested order, source paths, and `initial_state_id` values. For an inherited log, its first new Event's parent is the chosen State's `created_by_event_id`; for a new independent tree, that Event has no parent.
- New Event and State IDs advance in import order. Do not equate the number of displayed HTML nodes with the actual number of States in the State Library.
- If semantic Undo diagnostics occurred, confirm they were resolved and the log was committed. Do not present provisional replay statistics as saved results.
- After code changes, run `python -m unittest discover -s .\tests -q`. After a semantics-only recompile, also verify that the factual-history file hashes are unchanged.

Raw logs are evidence; preserve their provenance and contents. Before any replay that would change an existing inheritance relationship, historical numbering, or compiled facts, explain the impact and obtain an explicit user decision.
