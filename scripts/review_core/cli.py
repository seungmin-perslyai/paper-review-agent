from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import detect_default_language
from .pipeline import ReviewOrchestrator


def parse_args() -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Persly paper review OpenAI orchestrator")
    parser.add_argument("--papers", action="append", default=[], help="Paper file/dir (repeatable)")
    parser.add_argument("--output", default=str(root_dir / "outputs"))
    parser.add_argument("--config", default=str(root_dir / "config" / "codex_agents.env"))
    parser.add_argument("--prompt-dir", default=str(root_dir / "prompts"))
    parser.add_argument("--max-parallel", type=int)
    parser.add_argument("--max-revisions", type=int)
    parser.add_argument("--max-chars", type=int)
    parser.add_argument("--continue-added", action="store_true")
    parser.add_argument("--supervisor-command", default="")
    parser.add_argument("--lang", choices=["ko", "en"], default=os.environ.get("UI_LANG") or detect_default_language())
    parser.add_argument("--active-agents", default="summary,prior_work,method,critique")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--verbose-ui", action="store_true")
    parser.add_argument("--run-id", default=os.environ.get("PERSLY_RUN_ID", ""))
    parser.add_argument("--run-log-dir", default=os.environ.get("PERSLY_LOG_DIR", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        orch = ReviewOrchestrator(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1
    return orch.run()
