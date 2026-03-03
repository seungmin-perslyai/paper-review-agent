from __future__ import annotations

import os
import re
from argparse import Namespace
from pathlib import Path

from .models import DEFAULTS, RuntimeConfig


def load_env_kv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def resolve_preferring_root(raw_path: str, root_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    root_candidate = (root_dir / path).resolve()
    if root_candidate.exists():
        return root_candidate
    return path.resolve()


def normalize_model_alias(model: str) -> str:
    alias = {
        "gpt-codex-5.3": "gpt-5.3-codex",
        "gpt-codex-5.2": "gpt-5.2-codex",
    }
    return alias.get(model, model)


def is_model_at_least_gpt_5_2(model: str) -> bool:
    if re.match(r"^gpt-5($|-)", model):
        return True
    m = re.match(r"^gpt-5\.(\d+)($|-)", model)
    return bool(m and int(m.group(1)) >= 2)


def detect_default_language() -> str:
    loc = os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    return "ko" if loc.lower().startswith("ko") else "en"


def build_runtime_config(args: Namespace, root_dir: Path) -> RuntimeConfig:
    env = load_env_kv(resolve_preferring_root(args.config, root_dir))

    def get_str(name: str, cli_value: str | None) -> str:
        if cli_value is not None:
            return cli_value
        return str(env.get(name, DEFAULTS[name]))

    def get_int(name: str, cli_value: int | None) -> int:
        if cli_value is not None:
            return cli_value
        raw = str(env.get(name, DEFAULTS[name]))
        return int(raw)

    cfg = RuntimeConfig(
        summary_model=normalize_model_alias(get_str("SUMMARY_MODEL", None)),
        prior_work_model=normalize_model_alias(get_str("PRIOR_WORK_MODEL", None)),
        method_model=normalize_model_alias(get_str("METHOD_MODEL", None)),
        critique_model=normalize_model_alias(get_str("CRITIQUE_MODEL", None)),
        supervisor_model=normalize_model_alias(get_str("SUPERVISOR_MODEL", None)),
        summary_reasoning_effort=get_str("SUMMARY_REASONING_EFFORT", None),
        prior_work_reasoning_effort=get_str("PRIOR_WORK_REASONING_EFFORT", None),
        method_reasoning_effort=get_str("METHOD_REASONING_EFFORT", None),
        critique_reasoning_effort=get_str("CRITIQUE_REASONING_EFFORT", None),
        supervisor_reasoning_effort=get_str("SUPERVISOR_REASONING_EFFORT", None),
        max_parallel=get_int("MAX_PARALLEL", args.max_parallel),
        max_revisions=get_int("MAX_REVISIONS", args.max_revisions),
        max_chars=get_int("MAX_CHARS", args.max_chars),
    )

    for model in [cfg.summary_model, cfg.prior_work_model, cfg.method_model, cfg.critique_model]:
        if not is_model_at_least_gpt_5_2(model):
            raise ValueError(f"Sub-agent model must be gpt-5.2 or higher: {model}")

    if cfg.supervisor_model != "gpt-5.3-codex":
        cfg.supervisor_model = "gpt-5.3-codex"
    cfg.supervisor_reasoning_effort = "xhigh"

    if cfg.max_parallel < 1:
        raise ValueError("MAX_PARALLEL must be >= 1")
    if cfg.max_revisions < 0:
        raise ValueError("MAX_REVISIONS must be >= 0")
    if cfg.max_chars < 1000:
        raise ValueError("MAX_CHARS must be >= 1000")

    return cfg

