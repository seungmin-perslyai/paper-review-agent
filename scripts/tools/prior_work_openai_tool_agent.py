#!/usr/bin/env python3
"""OpenAI Responses API tool-calling agent for prior-work drafting."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from openai import OpenAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prior-work agent via OpenAI API tool calling")
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", default="high")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--paper-title", required=True)
    parser.add_argument("--paper-path", required=True)
    parser.add_argument("--paper-text-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--ui-lang", choices=["ko", "en"], default="en")
    parser.add_argument("--pdf-structure-script", required=True)
    parser.add_argument("--figure-table-script", required=True)
    parser.add_argument("--prior-work-search-script", required=True)
    parser.add_argument("--summary-file")
    parser.add_argument("--feedback-file")
    parser.add_argument("--previous-draft-file")
    parser.add_argument("--max-steps", type=int, default=16)
    return parser.parse_args()


def log(msg: str) -> None:
    print(msg, flush=True)


def lang_text(lang: str, ko: str, en: str) -> str:
    return ko if lang == "ko" else en


def read_text(path: str | Path, default: str = "") -> str:
    p = Path(path)
    if not p.exists():
        return default
    return p.read_text(encoding="utf-8", errors="replace")


def trim_text(text: str, limit: int = 24000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[TRUNCATED]..."


def get_field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def response_output_items(response: Any) -> list[Any]:
    out = get_field(response, "output")
    if out is None:
        return []
    return list(out)


def extract_function_calls(response: Any) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []
    for item in response_output_items(response):
        if get_field(item, "type") != "function_call":
            continue
        calls.append(
            {
                "name": str(get_field(item, "name", "")),
                "arguments": str(get_field(item, "arguments", "{}")),
                "call_id": str(get_field(item, "call_id", "")),
            }
        )
    return calls


def extract_output_text(response: Any) -> str:
    txt = get_field(response, "output_text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()

    parts: list[str] = []
    for item in response_output_items(response):
        if get_field(item, "type") != "message":
            continue
        for content in get_field(item, "content", []) or []:
            ctype = get_field(content, "type")
            if ctype not in {"output_text", "text"}:
                continue
            ctext = get_field(content, "text")
            if isinstance(ctext, str) and ctext:
                parts.append(ctext)
    return "".join(parts).strip()


def create_response(client: OpenAI, **kwargs: Any) -> Any:
    try:
        return client.responses.create(**kwargs)
    except Exception:
        if "reasoning" in kwargs:
            retry = dict(kwargs)
            retry.pop("reasoning", None)
            return client.responses.create(**retry)
        raise


class ToolContext:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.output_dir = Path(args.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.paper_structure_json = self.output_dir / "paper_structure.json"
        self.paper_structure_md = self.output_dir / "paper_structure.md"
        self.figure_table_md = self.output_dir / "figure_table_analysis.md"
        self.prior_work_seed_md = self.output_dir / "prior_work_candidates.md"

    def run_python(self, script_path: str, script_args: list[str]) -> dict[str, Any]:
        cmd = [sys.executable, script_path, *script_args]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"returncode={proc.returncode}",
                "stderr": trim_text(proc.stderr, 8000),
                "stdout": trim_text(proc.stdout, 8000),
            }
        return {"ok": True}

    def tool_parse_pdf_structure(self, _: dict[str, Any]) -> dict[str, Any]:
        log("[TOOL] parse_pdf_structure")
        if self.paper_structure_json.exists() and self.paper_structure_md.exists():
            return {
                "ok": True,
                "cached": True,
                "paper_structure_json": str(self.paper_structure_json),
                "paper_structure_md": str(self.paper_structure_md),
                "paper_structure_content": trim_text(read_text(self.paper_structure_md), 20000),
            }
        result = self.run_python(
            self.args.pdf_structure_script,
            [
                "--paper-id",
                self.args.paper_id,
                "--paper-path",
                self.args.paper_path,
                "--paper-text",
                self.args.paper_text_file,
                "--output-json",
                str(self.paper_structure_json),
                "--output-md",
                str(self.paper_structure_md),
                "--ui-lang",
                self.args.ui_lang,
            ],
        )
        if not result["ok"]:
            return result
        return {
            "ok": True,
            "cached": False,
            "paper_structure_json": str(self.paper_structure_json),
            "paper_structure_md": str(self.paper_structure_md),
            "paper_structure_content": trim_text(read_text(self.paper_structure_md), 20000),
        }

    def tool_interpret_figure_table(self, _: dict[str, Any]) -> dict[str, Any]:
        log("[TOOL] interpret_figure_table")
        if self.figure_table_md.exists():
            return {
                "ok": True,
                "cached": True,
                "figure_table_md": str(self.figure_table_md),
                "figure_table_content": trim_text(read_text(self.figure_table_md), 20000),
            }
        if not self.paper_structure_json.exists():
            auto = self.tool_parse_pdf_structure({})
            if not auto.get("ok"):
                return auto
        result = self.run_python(
            self.args.figure_table_script,
            [
                "--paper-id",
                self.args.paper_id,
                "--paper-text",
                self.args.paper_text_file,
                "--structure-json",
                str(self.paper_structure_json),
                "--output-md",
                str(self.figure_table_md),
                "--ui-lang",
                self.args.ui_lang,
            ],
        )
        if not result["ok"]:
            return result
        return {
            "ok": True,
            "cached": False,
            "figure_table_md": str(self.figure_table_md),
            "figure_table_content": trim_text(read_text(self.figure_table_md), 20000),
        }

    def tool_search_prior_work_candidates(self, _: dict[str, Any]) -> dict[str, Any]:
        log("[TOOL] search_prior_work_candidates")
        if self.prior_work_seed_md.exists():
            return {
                "ok": True,
                "cached": True,
                "prior_work_candidates_md": str(self.prior_work_seed_md),
                "prior_work_candidates_content": trim_text(read_text(self.prior_work_seed_md), 20000),
            }
        result = self.run_python(
            self.args.prior_work_search_script,
            [
                "--paper-id",
                self.args.paper_id,
                "--paper-text",
                self.args.paper_text_file,
                "--output-md",
                str(self.prior_work_seed_md),
                "--ui-lang",
                self.args.ui_lang,
            ],
        )
        if not result["ok"]:
            return result
        return {
            "ok": True,
            "cached": False,
            "prior_work_candidates_md": str(self.prior_work_seed_md),
            "prior_work_candidates_content": trim_text(read_text(self.prior_work_seed_md), 20000),
        }

    def dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        mapping = {
            "parse_pdf_structure": self.tool_parse_pdf_structure,
            "interpret_figure_table": self.tool_interpret_figure_table,
            "search_prior_work_candidates": self.tool_search_prior_work_candidates,
        }
        if name not in mapping:
            return {"ok": False, "error": f"unknown tool: {name}"}
        return mapping[name](args)


def build_tools() -> list[dict[str, Any]]:
    empty_params = {"type": "object", "properties": {}, "additionalProperties": False}
    return [
        {
            "type": "function",
            "name": "parse_pdf_structure",
            "description": "Run PDF structure parser and return section/caption extraction results.",
            "parameters": empty_params,
        },
        {
            "type": "function",
            "name": "interpret_figure_table",
            "description": "Interpret figure/table captions and return reviewer checks.",
            "parameters": empty_params,
        },
        {
            "type": "function",
            "name": "search_prior_work_candidates",
            "description": "Extract prior-work candidates and verification queries from local paper text.",
            "parameters": empty_params,
        },
    ]


def build_prompts(args: argparse.Namespace) -> tuple[str, str]:
    sections = (
        [
            "1) 관련 연구 지형",
            "2) 가장 가까운 선행연구",
            "3) 참신성/차별성 판정",
            "4) 누락 인용 및 검증 쿼리",
        ]
        if args.ui_lang == "ko"
        else [
            "1) Prior-Work Landscape",
            "2) Closest Prior Works",
            "3) Novelty and Differentiation Judgment",
            "4) Missing Citations and Verification Queries",
        ]
    )
    language_name = "Korean" if args.ui_lang == "ko" else "English"
    system_prompt = f"""
You are PRIOR_WORK_AGENT.
You must produce a markdown report in {language_name} only.
Do not use any other language.

Before writing the final answer, call these tools at least once:
- parse_pdf_structure
- interpret_figure_table
- search_prior_work_candidates

Your final markdown must contain exactly these four sections:
{sections[0]}
{sections[1]}
{sections[2]}
{sections[3]}

Quality rules:
- Ground every major claim in available evidence.
- Distinguish confirmed evidence vs uncertainty.
- Include concrete verification queries when uncertain.
"""

    paper_excerpt = trim_text(read_text(args.paper_text_file), 22000)
    summary_text = trim_text(read_text(args.summary_file), 8000) if args.summary_file else ""
    feedback_text = trim_text(read_text(args.feedback_file), 6000) if args.feedback_file else ""
    previous_text = trim_text(read_text(args.previous_draft_file), 8000) if args.previous_draft_file else ""

    user_parts: list[str] = [
        f"PAPER_ID: {args.paper_id}",
        f"PAPER_TITLE: {args.paper_title}",
        f"PAPER_PATH: {args.paper_path}",
        "PAPER_CONTENT_EXCERPT_START",
        paper_excerpt,
        "PAPER_CONTENT_EXCERPT_END",
    ]
    if summary_text:
        user_parts.extend(["SUMMARY_CONTEXT_START", summary_text, "SUMMARY_CONTEXT_END"])
    if previous_text:
        user_parts.extend(["PREVIOUS_DRAFT_START", previous_text, "PREVIOUS_DRAFT_END"])
    if feedback_text:
        user_parts.extend(["SUPERVISOR_FEEDBACK_START", feedback_text, "SUPERVISOR_FEEDBACK_END"])
    user_prompt = "\n".join(user_parts)
    return system_prompt.strip(), user_prompt


def main() -> int:
    args = parse_args()
    if not os.environ.get("OPENAI_API_KEY"):
        log("[ERROR] OPENAI_API_KEY is not set.")
        return 1

    output_path = Path(args.output_md)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(args)
    client = OpenAI()
    tools = build_tools()
    system_prompt, user_prompt = build_prompts(args)

    required_tools = {"parse_pdf_structure", "interpret_figure_table", "search_prior_work_candidates"}
    called_tools: set[str] = set()

    reasoning = {"effort": args.reasoning_effort} if args.reasoning_effort else None
    request_kwargs: dict[str, Any] = {
        "model": args.model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "tools": tools,
        "tool_choice": "required",
    }
    if reasoning:
        request_kwargs["reasoning"] = reasoning

    log(f"[OPENAI] responses.create model={args.model} reasoning={args.reasoning_effort}")
    response = create_response(client, **request_kwargs)

    final_md = ""
    for step in range(args.max_steps):
        calls = extract_function_calls(response)
        if calls:
            tool_outputs: list[dict[str, Any]] = []
            for call in calls:
                name = call["name"]
                called_tools.add(name)
                try:
                    call_args = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    call_args = {}
                result = ctx.dispatch(name, call_args if isinstance(call_args, dict) else {})
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": call["call_id"],
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )

            next_kwargs: dict[str, Any] = {
                "model": args.model,
                "previous_response_id": get_field(response, "id"),
                "input": tool_outputs,
                "tools": tools,
            }
            if reasoning:
                next_kwargs["reasoning"] = reasoning
            response = create_response(client, **next_kwargs)
            continue

        missing = sorted(required_tools - called_tools)
        if missing:
            log(f"[OPENAI] missing tool calls -> requesting: {', '.join(missing)}")
            followup_kwargs: dict[str, Any] = {
                "model": args.model,
                "previous_response_id": get_field(response, "id"),
                "input": [
                    {
                        "role": "user",
                        "content": f"Before final answer, call these missing tools: {', '.join(missing)}",
                    }
                ],
                "tools": tools,
                "tool_choice": "required",
            }
            if reasoning:
                followup_kwargs["reasoning"] = reasoning
            response = create_response(client, **followup_kwargs)
            continue

        final_md = extract_output_text(response)
        if final_md:
            break

        followup_kwargs = {
            "model": args.model,
            "previous_response_id": get_field(response, "id"),
            "input": [{"role": "user", "content": "Provide final markdown now."}],
            "tools": tools,
        }
        if reasoning:
            followup_kwargs["reasoning"] = reasoning
        response = create_response(client, **followup_kwargs)

    if not final_md.strip():
        log("[ERROR] No final markdown produced by OpenAI response.")
        return 1

    output_path.write_text(final_md.rstrip() + "\n", encoding="utf-8")
    log(f"[DONE] prior_work draft saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
