from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULTS = {
    "SUMMARY_MODEL": "gpt-5.2",
    "PRIOR_WORK_MODEL": "gpt-5.2",
    "METHOD_MODEL": "gpt-5.2",
    "CRITIQUE_MODEL": "gpt-5.2",
    "SUPERVISOR_MODEL": "gpt-5.3-codex",
    "SUMMARY_REASONING_EFFORT": "high",
    "PRIOR_WORK_REASONING_EFFORT": "high",
    "METHOD_REASONING_EFFORT": "high",
    "CRITIQUE_REASONING_EFFORT": "high",
    "SUPERVISOR_REASONING_EFFORT": "xhigh",
    "MAX_PARALLEL": "2",
    "MAX_REVISIONS": "2",
    "MAX_CHARS": "30000",
}

ALLOWED_EXTS = {".txt", ".md", ".pdf"}
ACTORS = ["supervisor", "summary", "prior_work", "method", "critique"]
SUB_AGENTS = ["summary", "prior_work", "method", "critique"]


@dataclass
class RuntimeConfig:
    summary_model: str
    prior_work_model: str
    method_model: str
    critique_model: str
    supervisor_model: str
    summary_reasoning_effort: str
    prior_work_reasoning_effort: str
    method_reasoning_effort: str
    critique_reasoning_effort: str
    supervisor_reasoning_effort: str
    max_parallel: int
    max_revisions: int
    max_chars: int


@dataclass
class ManifestEntry:
    source: str
    paper_id: str
    status: str
    run_id: str
    updated_at: str


@dataclass
class PaperMeta:
    source_path: Path
    title: str
    paper_id: str
    text_file: Path
    char_count: int
    output_dir: Path
    role_dir: Path
    needs_review: bool
