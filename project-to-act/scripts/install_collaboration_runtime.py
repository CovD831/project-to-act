#!/usr/bin/env python3
"""Install the Project-to-Act runtime and tool-neutral adapters without overwriting files."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime_transaction import FileTransaction, RuntimeFailure  # noqa: E402


RUNTIME_FILES = (
    "task_view.py",
    "runtime_transaction.py",
    "task_runtime.py",
    "hook_adapter.py",
)
AGENTS_START = "<!-- project-to-act-runtime:start -->"
AGENTS_END = "<!-- project-to-act-runtime:end -->"
DEFAULT_CONFIG = {"schemaVersion": 1, "experimentalHandoffWrites": False}
AGENTS_BLOCK = f"""{AGENTS_START}
## Project-to-Act repository runtime

- Project facts live in `.project-to-act/PROJECT_*.md`; canonical task facts live in `.project-to-act/tasks/<ID>/`.
- At session start run `python .project-to-act/bin/hook_adapter.py --project-root . --event session-start`.
- Before commit run `python .project-to-act/bin/hook_adapter.py --project-root . --event pre-commit`; CI runs the same canonical validator.
- If task discovery is ambiguous, pass `--task-id`; never guess from chat history or agent memory.
- Hooks do not infer role, handoff type, verification verdict, or Gate decisions. Use explicit semantic commands for those changes.
- `COLLABORATION_CONFIG.json` keeps experimental handoff writes disabled unless an approved pilot explicitly enables them.
{AGENTS_END}
"""
PRE_COMMIT = """#!/bin/sh
set -eu
root=$(git rev-parse --show-toplevel)
exec python "$root/.project-to-act/bin/hook_adapter.py" --project-root "$root" --event pre-commit
"""
WORKFLOW = """name: Project-to-Act protocol

on:
  pull_request:

permissions:
  contents: read

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: python .project-to-act/bin/hook_adapter.py --project-root . --event ci
"""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeFailure("INSTALL_CONFLICT", f"{label} must be a regular JSON file", "Repair the existing project configuration.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeFailure("INSTALL_CONFLICT", f"{label} is not valid UTF-8 JSON", "Repair the existing project configuration.") from error
    if not isinstance(value, dict):
        raise RuntimeFailure("INSTALL_CONFLICT", f"{label} must contain an object", "Repair the existing project configuration.")
    return value


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)


def install_runtime(
    project_root: Path,
    *,
    dry_run: bool = False,
    activate_git_hook: bool = False,
    install_agents: bool = True,
) -> dict[str, Any]:
    root = project_root.expanduser().resolve()
    management = root / ".project-to-act"
    project_config = management / "PROJECT_CONFIG.json"
    if not root.is_dir() or management.is_symlink() or not management.is_dir() or not project_config.is_file():
        raise RuntimeFailure("PROVIDER_NOT_FOUND", "Project-to-Act management is not initialized", "Initialize or adopt Project-to-Act before installing collaboration runtime.", 3)
    config = _read_json(project_config, "PROJECT_CONFIG.json")
    if config.get("schema_version") != 1 or config.get("mode") not in {"managed", "external-ledger"}:
        raise RuntimeFailure("INSTALL_CONFLICT", "Unsupported PROJECT_CONFIG.json", "Migrate the project management configuration first.")

    mappings: list[tuple[Path, bytes, str]] = []
    for filename in RUNTIME_FILES:
        source = SCRIPT_DIR / filename
        if source.is_symlink() or not source.is_file():
            raise RuntimeFailure("INSTALL_CONFLICT", f"Runtime source is missing: {source}", "Repair the Skill installation.")
        mappings.append((management / "bin" / filename, source.read_bytes(), "created"))
    mappings.extend(
        [
            (management / "hooks" / "pre-commit", PRE_COMMIT.encode("utf-8"), "created"),
            (root / ".github" / "workflows" / "project-to-act.yml", WORKFLOW.encode("utf-8"), "created"),
        ]
    )

    collaboration_config = management / "COLLABORATION_CONFIG.json"
    config_created = not collaboration_config.exists()
    if config_created:
        mappings.append((collaboration_config, (json.dumps(DEFAULT_CONFIG, indent=2) + "\n").encode("utf-8"), "created"))
    else:
        existing = _read_json(collaboration_config, "COLLABORATION_CONFIG.json")
        if existing.get("schemaVersion") != 1 or not isinstance(existing.get("experimentalHandoffWrites"), bool):
            raise RuntimeFailure("INSTALL_CONFLICT", "Existing collaboration config is incompatible", "Migrate it explicitly; the installer will not overwrite it.")

    agents_path = root / "AGENTS.md"
    agents_action = "skipped"
    agents_content: bytes | None = None
    if install_agents:
        if agents_path.exists():
            if agents_path.is_symlink() or not agents_path.is_file():
                raise RuntimeFailure("INSTALL_CONFLICT", "AGENTS.md is not a regular file", "Repair AGENTS.md before installing the fallback block.")
            existing_agents = agents_path.read_text(encoding="utf-8")
            has_start = AGENTS_START in existing_agents
            has_end = AGENTS_END in existing_agents
            if has_start != has_end or existing_agents.count(AGENTS_START) != existing_agents.count(AGENTS_END):
                raise RuntimeFailure("INSTALL_CONFLICT", "AGENTS.md has an incomplete Project-to-Act marker block", "Repair the marker block manually.")
            if has_start:
                start = existing_agents.index(AGENTS_START)
                end = existing_agents.index(AGENTS_END, start) + len(AGENTS_END)
                if existing_agents[start:end] != AGENTS_BLOCK.rstrip():
                    raise RuntimeFailure("INSTALL_CONFLICT", "AGENTS.md Project-to-Act marker block differs from this runtime", "Review and migrate the marker block explicitly; the installer will not overwrite it.")
                agents_action = "unchanged"
            else:
                agents_action = "appended"
                agents_content = f"{existing_agents.rstrip()}\n\n{AGENTS_BLOCK}".encode("utf-8")
        else:
            agents_action = "created"
            agents_content = AGENTS_BLOCK.encode("utf-8")
        if agents_content is not None:
            mappings.append((agents_path, agents_content, agents_action))

    conflicts = []
    unchanged = []
    writes = []
    for target, content, action in mappings:
        relative = target.relative_to(root).as_posix()
        if target.exists():
            if target.is_symlink() or not target.is_file():
                conflicts.append(relative)
            elif target.read_bytes() == content:
                unchanged.append(relative)
            elif target == agents_path and agents_action == "appended":
                writes.append((target, content))
            else:
                conflicts.append(relative)
        else:
            writes.append((target, content))
    if conflicts:
        raise RuntimeFailure("INSTALL_CONFLICT", f"Installation would overwrite existing files: {conflicts}", "Review or migrate the listed files; the installer made no changes.")

    if not dry_run:
        transaction = FileTransaction(root)
        for target, content in writes:
            transaction.add_text(target, content.decode("utf-8"))
        transaction.commit()
        hook_path = management / "hooks" / "pre-commit"
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    git_hook_active = False
    if activate_git_hook and not dry_run:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeFailure("GIT_FACT_UNAVAILABLE", "Cannot activate hook outside a Git worktree", "Initialize Git or omit --activate-git-hook.")
        configured = _git(root, "config", "core.hooksPath", ".project-to-act/hooks")
        if configured.returncode != 0:
            detail = configured.stderr.strip() or "Unable to configure core.hooksPath"
            raise RuntimeFailure("GIT_FACT_UNAVAILABLE", f"Runtime files were installed, but Git hook activation failed: {detail}", "The installed runtime remains usable; activate the versioned hook manually.")
        git_hook_active = True
    elif not dry_run:
        current = _git(root, "config", "--get", "core.hooksPath")
        git_hook_active = current.returncode == 0 and current.stdout.strip() == ".project-to-act/hooks"

    created_or_updated = [target.relative_to(root).as_posix() for target, _ in writes]
    return {
        "schemaVersion": 1,
        "valid": True,
        "dryRun": dry_run,
        "createdOrUpdated": created_or_updated,
        "unchanged": sorted(set(unchanged)),
        "agentsAction": agents_action,
        "configCreated": config_created,
        "gitHookActive": git_hook_active,
        "gitHookActivation": None if git_hook_active else "git config core.hooksPath .project-to-act/hooks",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--activate-git-hook", action="store_true")
    parser.add_argument("--skip-agents", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result = install_runtime(
            args.project_root,
            dry_run=args.dry_run,
            activate_git_hook=args.activate_git_hook,
            install_agents=not args.skip_agents,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except RuntimeFailure as error:
        print(json.dumps({"code": error.code, "message": str(error), "recovery": error.recovery}, ensure_ascii=False), file=sys.stderr)
        return getattr(error, "exit_code", 1)


if __name__ == "__main__":
    raise SystemExit(main())
