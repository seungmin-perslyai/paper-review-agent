"""OpenAI API orchestrator pipeline for persly paper review."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from .config import build_runtime_config, load_env_kv, resolve_preferring_root
from .llm import LLMRunner
from .logger import Logger
from .models import ACTORS, ALLOWED_EXTS, SUB_AGENTS, ManifestEntry, PaperMeta


class ReviewOrchestrator:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.root_dir = Path(__file__).resolve().parents[2]
        self.run_id = args.run_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.output_root_dir = Path(args.output).resolve()
        self.output_dir = self.output_root_dir / self.run_id
        self.prompt_dir = resolve_preferring_root(args.prompt_dir, self.root_dir)
        self.ui_lang = args.lang
        self.run_log_dir = Path(args.run_log_dir).resolve() if args.run_log_dir else self.output_dir / ".run_logs"
        self.state_dir = self.output_root_dir / ".state"
        self.manifest_file = self.state_dir / "review_manifest.tsv"
        self.work_dir = Path(tempfile.mkdtemp(prefix="persly_review_"))
        self.active_agents = self._parse_active_agents(getattr(args, "active_agents", ""))
        self.active_agent_set = set(self.active_agents)

        self.pdf_structure_tool_script = self.root_dir / "scripts" / "tools" / "pdf_structure_parser.py"
        self.figure_table_tool_script = self.root_dir / "scripts" / "tools" / "figure_table_interpreter.py"
        self.prior_work_search_tool_script = self.root_dir / "scripts" / "tools" / "prior_work_search.py"
        self.prior_work_openai_tool_agent_script = self.root_dir / "scripts" / "tools" / "prior_work_openai_tool_agent.py"

        self.logger = Logger(self.run_log_dir)
        self.llm = LLMRunner(self.logger)
        self.manifest_lock = threading.Lock()
        self.manifest: dict[str, ManifestEntry] = {}
        self.papers: list[PaperMeta] = []
        self.ordered_papers: list[PaperMeta] = []
        self.supervisor_plan_notes = ""

        self.config = build_runtime_config(args, self.root_dir)

    def __del__(self) -> None:
        try:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        except Exception:
            pass

    def lang_text(self, ko: str, en: str) -> str:
        return ko if self.ui_lang == "ko" else en

    def output_language_name(self) -> str:
        return "Korean" if self.ui_lang == "ko" else "English"

    def _parse_active_agents(self, raw: str) -> list[str]:
        default_agents = list(SUB_AGENTS)
        text = (raw or "").strip()
        if not text:
            return default_agents

        ordered: list[str] = []
        seen: set[str] = set()
        invalid: list[str] = []

        for token in text.split(","):
            name = token.strip().lower()
            if not name:
                continue
            if name not in SUB_AGENTS:
                invalid.append(name)
                continue
            if name in seen:
                continue
            seen.add(name)
            ordered.append(name)

        if invalid:
            raise ValueError(f"Invalid --active-agents values: {', '.join(invalid)}")
        if not ordered:
            raise ValueError("--active-agents must include at least one sub-agent.")
        return ordered

    def _is_agent_active(self, agent: str) -> bool:
        return agent in self.active_agent_set

    def _load_local_env(self) -> None:
        env_file = self.root_dir / ".env.local"
        if not env_file.exists():
            return
        for key, val in load_env_kv(env_file).items():
            os.environ[key] = val

    def _apply_supervisor_command(self) -> None:
        if not self.args.supervisor_command:
            return
        command_text = str(self.args.supervisor_command).strip()
        if not command_text:
            return
        self.logger.actor("supervisor", f"사용자 시작 명령: {command_text}")
        lower = command_text.lower()
        if (
            ("추가" in lower and "리뷰" in lower)
            or ("이어" in lower and "리뷰" in lower)
            or ("continue" in lower and "review" in lower)
            or ("new paper" in lower)
        ):
            self.args.continue_added = True
            self.logger.ui(self.lang_text("추가/미완료 논문 이어서 리뷰 모드 활성화", "Continue-added review mode enabled"))

    def _is_supervisor_start_command(self, text: str) -> bool:
        lower = text.lower()
        if ("리뷰" in text) or ("시작" in text) or ("분석" in text):
            return True
        if ("review" in lower) or ("start" in lower) or ("analy" in lower):
            return True
        return False

    def _ensure_supervisor_start_command(self) -> None:
        command_text = str(self.args.supervisor_command or "").strip()
        if command_text:
            self.args.supervisor_command = command_text
            return
        if self.args.yes or not sys.stdin.isatty():
            return

        self.logger.actor(
            "supervisor",
            self.lang_text(
                "명령 대기: 자연어로 시작 명령을 입력하세요 (예: 리뷰해줘)",
                "Waiting command: enter a natural-language start command (e.g., review these papers)",
            ),
        )
        while True:
            raw = input("supervisor> ").strip()
            if not raw:
                print(self.lang_text("명령을 입력해 주세요.", "Please enter a command."), flush=True)
                continue
            if not self._is_supervisor_start_command(raw):
                print(
                    self.lang_text(
                        "시작 의도를 포함해 주세요 (리뷰/시작/분석).",
                        "Include a start intent keyword (review/start/analyze).",
                    ),
                    flush=True,
                )
                continue
            self.args.supervisor_command = raw
            return

    def _load_manifest(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if not self.manifest_file.exists():
            self.manifest_file.write_text("", encoding="utf-8")
            self.manifest = {}
            return
        data: dict[str, ManifestEntry] = {}
        for raw in self.manifest_file.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split("\t")
            if len(parts) < 5:
                continue
            source, paper_id, status, run_id, updated_at = parts[:5]
            if not source:
                continue
            data[source] = ManifestEntry(
                source=source,
                paper_id=paper_id,
                status=status,
                run_id=run_id,
                updated_at=updated_at,
            )
        self.manifest = data

    def _save_manifest(self) -> None:
        lines: list[str] = []
        for source in sorted(self.manifest.keys()):
            entry = self.manifest[source]
            lines.append(
                "\t".join([entry.source, entry.paper_id, entry.status, entry.run_id, entry.updated_at])
            )
        self.manifest_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def _upsert_manifest(self, source: Path, paper_id: str, status: str) -> None:
        with self.manifest_lock:
            key = str(source)
            self.manifest[key] = ManifestEntry(
                source=key,
                paper_id=paper_id,
                status=status,
                run_id=self.run_id,
                updated_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            )
            self._save_manifest()

    def _slugify(self, text: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        return slug or "paper"

    def _title_from_file(self, path: Path) -> str:
        base = path.stem
        title = re.sub(r"[_-]+", " ", base)
        title = re.sub(r"\s+", " ", title).strip()
        return title or base

    def _collect_paper_files(self) -> list[Path]:
        inputs = list(self.args.papers)
        if not inputs:
            default = self.root_dir / "papers"
            if default.exists():
                inputs = [str(default)]
            else:
                raise FileNotFoundError("No paper input provided. Use --papers <path>.")

        files: list[Path] = []
        for raw in inputs:
            path = Path(raw).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"Input path does not exist: {path}")
            if path.is_file():
                if path.suffix.lower() in ALLOWED_EXTS:
                    files.append(path)
                continue
            for file_path in sorted(path.rglob("*")):
                if not file_path.is_file():
                    continue
                if file_path.suffix.lower() in ALLOWED_EXTS:
                    files.append(file_path.resolve())

        deduped: list[Path] = []
        seen: set[str] = set()
        for path in files:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)

        if not deduped:
            raise FileNotFoundError("No paper files found. Provide .txt, .md, or .pdf files.")
        return deduped

    def _extract_text_file(self, paper_path: Path, out_path: Path) -> int:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        ext = paper_path.suffix.lower()

        if ext == ".pdf":
            tmp_full = self.work_dir / f"pdf_extract_{paper_path.stem}_{os.getpid()}.txt"
            cmd = ["pdftotext", str(paper_path), str(tmp_full)]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"Failed to extract text from PDF: {paper_path}")
            text = tmp_full.read_text(encoding="utf-8", errors="replace")
            tmp_full.unlink(missing_ok=True)
        else:
            text = paper_path.read_text(encoding="utf-8", errors="replace")

        clipped = text[: self.config.max_chars]
        out_path.write_text(clipped, encoding="utf-8")
        return len(clipped)

    def _build_metadata(self) -> None:
        files = self._collect_paper_files()
        used_ids: set[str] = set()
        papers: list[PaperMeta] = []

        for path in files:
            title = self._title_from_file(path)
            base_id = self._slugify(title)

            known_id = ""
            entry = self.manifest.get(str(path))
            if entry and entry.paper_id:
                known_id = entry.paper_id

            paper_id = known_id or base_id
            if paper_id in used_ids:
                n = 2
                while f"{paper_id}-{n}" in used_ids:
                    n += 1
                paper_id = f"{paper_id}-{n}"
            used_ids.add(paper_id)

            text_file = self.work_dir / f"{paper_id}.paper.txt"
            char_count = self._extract_text_file(path, text_file)
            paper_out_dir = self.output_dir / paper_id
            needs_review = True
            if self.args.continue_added:
                existing_paper_dir = self._find_latest_paper_dir(paper_id)
                if existing_paper_dir and (existing_paper_dir / "review_report.md").exists():
                    needs_review = False
                    if existing_paper_dir != paper_out_dir:
                        paper_out_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(existing_paper_dir, paper_out_dir, dirs_exist_ok=True)

            paper = PaperMeta(
                source_path=path,
                title=title,
                paper_id=paper_id,
                text_file=text_file,
                char_count=char_count,
                output_dir=paper_out_dir,
                role_dir=self.work_dir / paper_id,
                needs_review=needs_review,
            )
            papers.append(paper)
            self._upsert_manifest(path, paper_id, "pending" if needs_review else "completed")

        self.papers = papers

    def _find_latest_paper_dir(self, paper_id: str) -> Path | None:
        if not self.output_root_dir.exists():
            return None

        latest_mtime = -1.0
        latest_dir: Path | None = None
        for run_dir in self.output_root_dir.iterdir():
            if not run_dir.is_dir():
                continue
            if run_dir.name.startswith("."):
                continue
            report = run_dir / paper_id / "review_report.md"
            if not report.exists():
                continue
            try:
                mtime = report.stat().st_mtime
            except OSError:
                continue
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_dir = report.parent
        return latest_dir

    def _render_template(self, name: str, replacements: dict[str, str]) -> str:
        template_path = self.prompt_dir / name
        if not template_path.exists():
            raise FileNotFoundError(f"Prompt template not found: {template_path}")
        text = template_path.read_text(encoding="utf-8")
        for key, val in replacements.items():
            text = text.replace("{{" + key + "}}", val)
        return text.rstrip() + "\n"

    def _find_paper_by_id(self, paper_id: str) -> PaperMeta | None:
        for paper in self.papers:
            if paper.paper_id == paper_id:
                return paper
        return None

    def _build_default_order(self) -> list[PaperMeta]:
        return sorted(self.papers, key=lambda p: (-p.char_count, p.paper_id))

    def _schedule_with_supervisor(self) -> None:
        prompt_text = self._render_template(
            "supervisor_planning.prompt.txt",
            {"OUTPUT_LANGUAGE_NAME": self.output_language_name()},
        )
        table_lines = ["", "PAPER_TABLE (paper_id<TAB>title<TAB>char_count):"]
        for paper in self.papers:
            table_lines.append(f"{paper.paper_id}\t{paper.title}\t{paper.char_count}")
        full_prompt = prompt_text + "\n".join(table_lines) + "\n"

        plan_response_path = self.work_dir / "supervisor_planning.response.json"
        obj = self.llm.run_json(
            "supervisor",
            self.config.supervisor_model,
            self.config.supervisor_reasoning_effort,
            full_prompt,
            plan_response_path,
        )

        ordered_ids = obj.get("ordered_paper_ids") if isinstance(obj.get("ordered_paper_ids"), list) else []
        ordered: list[PaperMeta] = []
        seen: set[str] = set()
        for item in ordered_ids:
            if not isinstance(item, str):
                continue
            paper = self._find_paper_by_id(item.strip())
            if not paper:
                continue
            if paper.paper_id in seen:
                continue
            seen.add(paper.paper_id)
            ordered.append(paper)

        if not ordered:
            ordered = self._build_default_order()

        for paper in self.papers:
            if paper.paper_id not in seen:
                ordered.append(paper)

        self.ordered_papers = ordered
        self.supervisor_plan_notes = str(obj.get("execution_notes", "")).strip()

        plan_markdown = str(obj.get("plan_markdown", "")).strip()
        if not plan_markdown:
            if self.ui_lang == "ko":
                plan_markdown = (
                    "# 슈퍼바이저 계획\n\n"
                    "- 계획 응답에 plan_markdown이 없어 기본 계획을 사용합니다.\n"
                    "- 대체 순서는 논문 길이(문자 수) 기준으로 정렬합니다."
                )
            else:
                plan_markdown = (
                    "# Supervisor Planning\n\n"
                    "- Planning response did not contain plan_markdown.\n"
                    "- Fallback order is based on paper length."
                )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "supervisor_plan.md").write_text(plan_markdown.rstrip() + "\n", encoding="utf-8")
        (self.output_dir / "supervisor_plan.json").write_text(
            json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        self.logger.ui(self.lang_text("슈퍼바이저 계획 수립 완료", "Supervisor planning completed"))
        self.logger.actor_block_from_file("supervisor", self.output_dir / "supervisor_plan.md", "PLAN")

    def _agent_section_label(self, agent: str, index: int) -> str:
        labels = {
            "ko": {
                "summary": ["개요", "핵심 기여", "주요 결과", "열린 질문"],
                "prior_work": ["관련 연구 지형", "가장 가까운 선행연구", "참신성/차별성 판정", "누락 인용 및 검증 쿼리"],
                "method": ["방법 파이프라인", "기술적 강점", "기술적 위험", "재현 가능성"],
                "critique": ["강점", "약점", "근거 부족 항목", "개선 제안"],
            },
            "en": {
                "summary": ["Overview", "Contributions", "Key Findings", "Open Questions"],
                "prior_work": [
                    "Prior-Work Landscape",
                    "Closest Prior Works",
                    "Novelty and Differentiation Judgment",
                    "Missing Citations and Verification Queries",
                ],
                "method": ["Method Pipeline", "Technical Strengths", "Technical Risks", "Reproducibility"],
                "critique": ["Strengths", "Weaknesses", "Missing Evidence", "Suggestions"],
            },
        }
        return labels[self.ui_lang][agent][index - 1]

    def _final_report_section_label(self, index: int) -> str:
        labels = {
            "ko": [
                "판정 스냅샷",
                "주장-근거 매트릭스",
                "방법론 스트레스 테스트",
                "실패 모드와 경계 조건",
                "재현 및 검증 계획",
                "전략적 권고안",
            ],
            "en": [
                "Decision Snapshot",
                "Claim-Evidence Matrix",
                "Methodological Stress Test",
                "Failure Modes and Boundary Conditions",
                "Replication and Validation Plan",
                "Strategic Recommendation",
            ],
        }
        return labels[self.ui_lang][index - 1]

    def _portfolio_section_label(self, index: int) -> str:
        labels = {
            "ko": ["포트폴리오 핵심 인사이트", "논문 간 비교 근거표", "리스크 레이더와 우선순위", "실행 계획 및 최종 권고"],
            "en": [
                "Portfolio-Level Insights",
                "Cross-Paper Evidence Grid",
                "Risk Radar and Prioritization",
                "Execution Plan and Final Recommendation",
            ],
        }
        return labels[self.ui_lang][index - 1]

    def _build_agent_prompt(
        self,
        agent: str,
        paper: PaperMeta,
        feedback: str,
        prev_draft_file: Path | None,
        paper_structure_file: Path,
        figure_table_file: Path,
        summary_context_file: Path | None,
        prior_work_context_file: Path | None,
        prior_work_tool_file: Path | None,
    ) -> str:
        template_name = {
            "summary": "agent_summary.prompt.txt",
            "prior_work": "agent_prior_work.prompt.txt",
            "method": "agent_method.prompt.txt",
            "critique": "agent_critique.prompt.txt",
        }[agent]

        prompt = self._render_template(
            template_name,
            {
                "OUTPUT_LANGUAGE_NAME": self.output_language_name(),
                "SECTION_1": self._agent_section_label(agent, 1),
                "SECTION_2": self._agent_section_label(agent, 2),
                "SECTION_3": self._agent_section_label(agent, 3),
                "SECTION_4": self._agent_section_label(agent, 4),
            },
        )

        blocks: list[str] = [
            "",
            f"PAPER_ID: {paper.paper_id}",
            f"PAPER_TITLE: {paper.title}",
            "PAPER_CONTENT_START",
            paper.text_file.read_text(encoding="utf-8", errors="replace"),
            "PAPER_CONTENT_END",
        ]

        if paper_structure_file.exists():
            blocks.extend(["", "PAPER_STRUCTURE_START", paper_structure_file.read_text(encoding="utf-8", errors="replace"), "PAPER_STRUCTURE_END"])
        if figure_table_file.exists():
            blocks.extend(["", "FIGURE_TABLE_ANALYSIS_START", figure_table_file.read_text(encoding="utf-8", errors="replace"), "FIGURE_TABLE_ANALYSIS_END"])
        if summary_context_file and summary_context_file.exists():
            blocks.extend(["", "SUMMARY_CONTEXT_START", summary_context_file.read_text(encoding="utf-8", errors="replace"), "SUMMARY_CONTEXT_END"])
        if prior_work_context_file and prior_work_context_file.exists():
            blocks.extend(["", "PRIOR_WORK_CONTEXT_START", prior_work_context_file.read_text(encoding="utf-8", errors="replace"), "PRIOR_WORK_CONTEXT_END"])
        if prior_work_tool_file and prior_work_tool_file.exists():
            blocks.extend(["", "PRIOR_WORK_TOOL_OUTPUT_START", prior_work_tool_file.read_text(encoding="utf-8", errors="replace"), "PRIOR_WORK_TOOL_OUTPUT_END"])
        if prev_draft_file and prev_draft_file.exists():
            blocks.extend(["", "PREVIOUS_DRAFT_START", prev_draft_file.read_text(encoding="utf-8", errors="replace"), "PREVIOUS_DRAFT_END"])
        if feedback.strip():
            blocks.extend(["", "SUPERVISOR_FEEDBACK_START", feedback.strip(), "SUPERVISOR_FEEDBACK_END"])
        if self.supervisor_plan_notes.strip():
            blocks.extend(["", "SUPERVISOR_PLAN_NOTES_START", self.supervisor_plan_notes.strip(), "SUPERVISOR_PLAN_NOTES_END"])

        return prompt + "\n".join(blocks) + "\n"

    def _build_quality_gate_prompt(self, paper: PaperMeta, agent: str, draft_file: Path, revision: int) -> str:
        prompt = self._render_template(
            "supervisor_quality_gate.prompt.txt",
            {"OUTPUT_LANGUAGE_NAME": self.output_language_name()},
        )
        blocks = [
            "",
            f"PAPER_ID: {paper.paper_id}",
            f"PAPER_TITLE: {paper.title}",
            f"AGENT_NAME: {agent}",
            f"REVISION: {revision}",
            "DRAFT_START",
            draft_file.read_text(encoding="utf-8", errors="replace"),
            "DRAFT_END",
        ]
        return prompt + "\n".join(blocks) + "\n"

    def _run_tool_subprocess(self, actor: str, cmd: list[str]) -> None:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            self.logger.actor(actor, line.rstrip("\n"))
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(f"Tool command failed (rc={rc}): {' '.join(cmd)}")

    def _run_pdf_structure_tool(self, paper: PaperMeta, out_json: Path, out_md: Path) -> None:
        self.logger.actor("prior_work", f"도구 실행: PDF 구조 파서 ({paper.paper_id})")
        cmd = [
            sys.executable,
            str(self.pdf_structure_tool_script),
            "--paper-id",
            paper.paper_id,
            "--paper-path",
            str(paper.source_path),
            "--paper-text",
            str(paper.text_file),
            "--output-json",
            str(out_json),
            "--output-md",
            str(out_md),
            "--ui-lang",
            self.ui_lang,
        ]
        self._run_tool_subprocess("prior_work", cmd)

    def _run_figure_table_tool(self, paper: PaperMeta, structure_json: Path, out_md: Path) -> None:
        self.logger.actor("prior_work", f"도구 실행: Figure/Table 해석 ({paper.paper_id})")
        cmd = [
            sys.executable,
            str(self.figure_table_tool_script),
            "--paper-id",
            paper.paper_id,
            "--paper-text",
            str(paper.text_file),
            "--structure-json",
            str(structure_json),
            "--output-md",
            str(out_md),
            "--ui-lang",
            self.ui_lang,
        ]
        self._run_tool_subprocess("prior_work", cmd)

    def _run_prior_work_seed_tool(self, paper: PaperMeta, out_md: Path) -> None:
        self.logger.actor("prior_work", f"도구 실행: prior-work 후보 추출 ({paper.paper_id})")
        cmd = [
            sys.executable,
            str(self.prior_work_search_tool_script),
            "--paper-id",
            paper.paper_id,
            "--paper-text",
            str(paper.text_file),
            "--output-md",
            str(out_md),
            "--ui-lang",
            self.ui_lang,
        ]
        self._run_tool_subprocess("prior_work", cmd)

    def _run_prior_work_openai_tool_agent(
        self,
        paper: PaperMeta,
        output_md: Path,
        summary_file: Path | None,
        feedback: str,
        prev_draft_file: Path | None,
    ) -> None:
        feedback_file: Path | None = None
        if feedback.strip():
            feedback_file = paper.role_dir / "prior_work.feedback.txt"
            feedback_file.write_text(feedback.strip() + "\n", encoding="utf-8")

        cmd = [
            sys.executable,
            str(self.prior_work_openai_tool_agent_script),
            "--model",
            self.config.prior_work_model,
            "--reasoning-effort",
            self.config.prior_work_reasoning_effort,
            "--paper-id",
            paper.paper_id,
            "--paper-title",
            paper.title,
            "--paper-path",
            str(paper.source_path),
            "--paper-text-file",
            str(paper.text_file),
            "--output-dir",
            str(paper.output_dir),
            "--output-md",
            str(output_md),
            "--ui-lang",
            self.ui_lang,
            "--pdf-structure-script",
            str(self.pdf_structure_tool_script),
            "--figure-table-script",
            str(self.figure_table_tool_script),
            "--prior-work-search-script",
            str(self.prior_work_search_tool_script),
        ]
        if summary_file and summary_file.exists() and summary_file.is_file():
            cmd.extend(["--summary-file", str(summary_file)])
        if feedback_file and feedback_file.exists():
            cmd.extend(["--feedback-file", str(feedback_file)])
        if prev_draft_file and prev_draft_file.exists():
            cmd.extend(["--previous-draft-file", str(prev_draft_file)])

        self.logger.actor("prior_work", f"OpenAI tool-calling 실행: prior_work ({paper.paper_id})")
        self._run_tool_subprocess("prior_work", cmd)

    def _run_quality_gate(self, paper: PaperMeta, agent: str, draft_file: Path, revision: int) -> tuple[bool, str, str]:
        prompt = self._build_quality_gate_prompt(paper, agent, draft_file, revision)
        out_json = paper.role_dir / f"{agent}.review.r{revision}.json"
        obj = self.llm.run_json(
            "supervisor",
            self.config.supervisor_model,
            self.config.supervisor_reasoning_effort,
            prompt,
            out_json,
        )

        raw_approved = obj.get("approved")
        if isinstance(raw_approved, bool):
            approved = raw_approved
        else:
            approved = str(raw_approved).strip().lower() in {"true", "1", "yes", "y"}

        score = str(obj.get("score", ""))
        feedback = str(obj.get("feedback", "")).strip()
        return approved, feedback, score

    def _review_one_agent(
        self,
        paper: PaperMeta,
        agent: str,
        out_file: Path,
        paper_structure_md: Path,
        figure_table_md: Path,
        summary_context_file: Path | None,
        prior_work_context_file: Path | None,
        prior_work_tool_file: Path | None,
    ) -> None:
        feedback = ""
        prev_draft: Path | None = None
        out_file.parent.mkdir(parents=True, exist_ok=True)
        paper.role_dir.mkdir(parents=True, exist_ok=True)

        model_map = {
            "summary": self.config.summary_model,
            "prior_work": self.config.prior_work_model,
            "method": self.config.method_model,
            "critique": self.config.critique_model,
        }
        effort_map = {
            "summary": self.config.summary_reasoning_effort,
            "prior_work": self.config.prior_work_reasoning_effort,
            "method": self.config.method_reasoning_effort,
            "critique": self.config.critique_reasoning_effort,
        }

        for revision in range(self.config.max_revisions + 1):
            self.logger.actor(agent, f"{paper.paper_id} {agent} 초안 생성 r{revision}")
            draft_tmp = paper.role_dir / f"{agent}.draft.r{revision}.md"

            if agent == "prior_work":
                self._run_prior_work_openai_tool_agent(
                    paper,
                    draft_tmp,
                    summary_context_file,
                    feedback,
                    prev_draft,
                )
                if draft_tmp.exists():
                    self.logger.actor_stream_from_file(agent, draft_tmp, draft_tmp.name)
            else:
                prompt_text = self._build_agent_prompt(
                    agent,
                    paper,
                    feedback,
                    prev_draft,
                    paper_structure_md,
                    figure_table_md,
                    summary_context_file,
                    prior_work_context_file,
                    prior_work_tool_file,
                )
                self.llm.run_text(agent, model_map[agent], effort_map[agent], prompt_text, draft_tmp)

            shutil.copy2(draft_tmp, out_file)

            approved, feedback, score = self._run_quality_gate(paper, agent, out_file, revision)
            if approved:
                self.logger.actor(agent, f"검수 통과: {paper.paper_id} {agent} r{revision} score={score or 'n/a'}")
                return

            self.logger.actor(agent, f"검수 재작성 요청: {paper.paper_id} {agent} r{revision}")
            prev_draft = out_file

        self.logger.actor(agent, f"최대 재작성 도달: {paper.paper_id} {agent}")

    def _write_inactive_agent_output(self, agent: str, out_file: Path) -> None:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        if self.ui_lang == "ko":
            text = (
                f"# {agent}\n\n"
                f"- 상태: 비활성화\n"
                f"- 사유: 실행 시 `--active-agents`에 `{agent}`가 포함되지 않았습니다.\n"
            )
        else:
            text = (
                f"# {agent}\n\n"
                f"- status: inactive\n"
                f"- reason: `{agent}` is not included in `--active-agents` for this run.\n"
            )
        out_file.write_text(text, encoding="utf-8")
        self.logger.actor(agent, self.lang_text("비활성화: 출력 파일에 스킵 사유 기록", "inactive: wrote skip note to output file"))

    def _build_single_merge_prompt(
        self,
        paper: PaperMeta,
        summary_file: Path,
        prior_work_file: Path,
        method_file: Path,
        critique_file: Path,
        paper_structure_file: Path,
        figure_table_file: Path,
    ) -> str:
        prompt = self._render_template(
            "supervisor_single_merge.prompt.txt",
            {
                "OUTPUT_LANGUAGE_NAME": self.output_language_name(),
                "SECTION_1": self._final_report_section_label(1),
                "SECTION_2": self._final_report_section_label(2),
                "SECTION_3": self._final_report_section_label(3),
                "SECTION_4": self._final_report_section_label(4),
                "SECTION_5": self._final_report_section_label(5),
                "SECTION_6": self._final_report_section_label(6),
            },
        )

        blocks = [
            "",
            f"PAPER_ID: {paper.paper_id}",
            f"PAPER_TITLE: {paper.title}",
            "SUMMARY_START",
            summary_file.read_text(encoding="utf-8", errors="replace"),
            "SUMMARY_END",
            "PRIOR_WORK_START",
            prior_work_file.read_text(encoding="utf-8", errors="replace"),
            "PRIOR_WORK_END",
            "METHOD_START",
            method_file.read_text(encoding="utf-8", errors="replace"),
            "METHOD_END",
            "CRITIQUE_START",
            critique_file.read_text(encoding="utf-8", errors="replace"),
            "CRITIQUE_END",
        ]
        if paper_structure_file.exists():
            blocks.extend(["PAPER_STRUCTURE_START", paper_structure_file.read_text(encoding="utf-8", errors="replace"), "PAPER_STRUCTURE_END"])
        if figure_table_file.exists():
            blocks.extend(["FIGURE_TABLE_ANALYSIS_START", figure_table_file.read_text(encoding="utf-8", errors="replace"), "FIGURE_TABLE_ANALYSIS_END"])

        return prompt + "\n".join(blocks) + "\n"

    def _process_one_paper(self, paper: PaperMeta) -> None:
        self.logger.ui(self.lang_text(f"논문 처리 시작: {paper.paper_id}", f"Processing paper: {paper.paper_id}"))
        self.logger.actor("supervisor", f"논문 시작: {paper.paper_id} ({paper.title})")

        paper.output_dir.mkdir(parents=True, exist_ok=True)
        paper.role_dir.mkdir(parents=True, exist_ok=True)

        paper_structure_json = paper.output_dir / "paper_structure.json"
        paper_structure_md = paper.output_dir / "paper_structure.md"
        figure_table_md = paper.output_dir / "figure_table_analysis.md"
        prior_work_seed_md = paper.output_dir / "prior_work_candidates.md"

        self._run_pdf_structure_tool(paper, paper_structure_json, paper_structure_md)
        self._run_figure_table_tool(paper, paper_structure_json, figure_table_md)

        needs_prior_work_seed = any(self._is_agent_active(agent) for agent in ("prior_work", "method", "critique"))
        if needs_prior_work_seed:
            self._run_prior_work_seed_tool(paper, prior_work_seed_md)

        summary_file = paper.output_dir / "summary.md"
        prior_work_file = paper.output_dir / "prior_work.md"
        method_file = paper.output_dir / "method.md"
        critique_file = paper.output_dir / "critique.md"
        review_report = paper.output_dir / "review_report.md"

        if self._is_agent_active("summary"):
            self._review_one_agent(
                paper,
                "summary",
                summary_file,
                paper_structure_md,
                figure_table_md,
                None,
                None,
                None,
            )
        else:
            self._write_inactive_agent_output("summary", summary_file)

        if self._is_agent_active("prior_work"):
            self._review_one_agent(
                paper,
                "prior_work",
                prior_work_file,
                paper_structure_md,
                figure_table_md,
                summary_file if summary_file.exists() else None,
                None,
                prior_work_seed_md if prior_work_seed_md.exists() else None,
            )
        else:
            self._write_inactive_agent_output("prior_work", prior_work_file)

        if self._is_agent_active("method"):
            self._review_one_agent(
                paper,
                "method",
                method_file,
                paper_structure_md,
                figure_table_md,
                summary_file if summary_file.exists() else None,
                prior_work_file if prior_work_file.exists() else None,
                prior_work_seed_md if prior_work_seed_md.exists() else None,
            )
        else:
            self._write_inactive_agent_output("method", method_file)

        if self._is_agent_active("critique"):
            self._review_one_agent(
                paper,
                "critique",
                critique_file,
                paper_structure_md,
                figure_table_md,
                summary_file if summary_file.exists() else None,
                prior_work_file if prior_work_file.exists() else None,
                prior_work_seed_md if prior_work_seed_md.exists() else None,
            )
        else:
            self._write_inactive_agent_output("critique", critique_file)

        merge_prompt = self._build_single_merge_prompt(
            paper,
            summary_file,
            prior_work_file,
            method_file,
            critique_file,
            paper_structure_md,
            figure_table_md,
        )
        self.llm.run_text(
            "supervisor",
            self.config.supervisor_model,
            self.config.supervisor_reasoning_effort,
            merge_prompt,
            review_report,
        )

        self._upsert_manifest(paper.source_path, paper.paper_id, "completed")
        self.logger.actor("supervisor", f"논문 완료: {paper.paper_id}")
        self.logger.ui(self.lang_text(f"논문 처리 완료: {paper.paper_id}", f"Completed paper: {paper.paper_id}"))

    def _build_global_merge_prompt(self) -> str:
        prompt = self._render_template(
            "supervisor_global_merge.prompt.txt",
            {
                "OUTPUT_LANGUAGE_NAME": self.output_language_name(),
                "GLOBAL_TITLE": self.lang_text("다중 논문 최종 리뷰", "Multi-Paper Final Review"),
                "PORTFOLIO_SECTION_1": self._portfolio_section_label(1),
                "PORTFOLIO_SECTION_2": self._portfolio_section_label(2),
                "PORTFOLIO_SECTION_3": self._portfolio_section_label(3),
                "PORTFOLIO_SECTION_4": self._portfolio_section_label(4),
            },
        )

        blocks: list[str] = [""]
        for paper in self.ordered_papers:
            report = paper.output_dir / "review_report.md"
            if not report.exists():
                raise FileNotFoundError(
                    f"Missing review report for global merge: paper={paper.paper_id}, file={report}"
                )
            blocks.extend(
                [
                    f"PAPER_ID: {paper.paper_id}",
                    f"PAPER_TITLE: {paper.title}",
                    "PAPER_REVIEW_START",
                    report.read_text(encoding="utf-8", errors="replace"),
                    "PAPER_REVIEW_END",
                    "",
                ]
            )

        return prompt + "\n".join(blocks) + "\n"

    def _run_parallel_reviews(self) -> None:
        pending = [p for p in self.ordered_papers if p.needs_review]
        skipped = [p for p in self.ordered_papers if not p.needs_review]

        for paper in skipped:
            self.logger.ui(
                self.lang_text(
                    f"기존 리뷰 재사용: {paper.paper_id} ({paper.title})",
                    f"Reusing existing review: {paper.paper_id} ({paper.title})",
                )
            )

        if not pending:
            self.logger.ui(
                self.lang_text(
                    "실행할 신규/미완료 논문이 없습니다. 기존 결과를 사용합니다.",
                    "No new/unfinished papers to run. Reusing existing outputs.",
                )
            )
            return

        failed = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_parallel) as executor:
            future_to_paper = {executor.submit(self._process_one_paper, paper): paper for paper in pending}
            for future in concurrent.futures.as_completed(future_to_paper):
                paper = future_to_paper[future]
                try:
                    future.result()
                except Exception as exc:
                    failed = True
                    self._upsert_manifest(paper.source_path, paper.paper_id, "failed")
                    self.logger.actor("supervisor", f"논문 실패: {paper.paper_id} error={exc}")

        if failed:
            raise RuntimeError("At least one paper review job failed.")

    def _print_runtime_summary(self) -> None:
        pending_count = sum(1 for p in self.papers if p.needs_review)
        reused_count = len(self.papers) - pending_count
        self.logger.ui(self.lang_text("실행 설정", "Runtime Configuration"))
        self.logger.ui(f"workspace={self.root_dir}")
        self.logger.ui(f"output_root={self.output_root_dir}")
        self.logger.ui(f"run_output={self.output_dir}")
        self.logger.ui(f"prompt_dir={self.prompt_dir}")
        self.logger.ui(
            "models: "
            f"summary={self.config.summary_model}, "
            f"prior_work={self.config.prior_work_model}, "
            f"method={self.config.method_model}, "
            f"critique={self.config.critique_model}, "
            f"supervisor={self.config.supervisor_model}"
        )
        self.logger.ui(
            "reasoning: "
            f"summary={self.config.summary_reasoning_effort}, "
            f"prior_work={self.config.prior_work_reasoning_effort}, "
            f"method={self.config.method_reasoning_effort}, "
            f"critique={self.config.critique_reasoning_effort}, "
            f"supervisor={self.config.supervisor_reasoning_effort}"
        )
        self.logger.ui(
            f"limits: max_parallel={self.config.max_parallel}, max_revisions={self.config.max_revisions}, max_chars={self.config.max_chars}"
        )
        self.logger.ui(f"active_agents={','.join(self.active_agents)}")
        if self.args.continue_added:
            self.logger.ui(f"continue_added=1 (pending={pending_count}, reused={reused_count})")
        else:
            self.logger.ui("continue_added=0 (full rerun mode)")

    def _print_scheduled_order(self) -> None:
        self.logger.ui(self.lang_text("예정 순서", "Scheduled Order"))
        for i, paper in enumerate(self.ordered_papers, start=1):
            mode = self.lang_text("run", "run") if paper.needs_review else self.lang_text("reuse", "reuse")
            self.logger.ui(f"{i}. {paper.paper_id} ({paper.title}) [{mode}]")

    def _confirm_start(self) -> bool:
        if str(self.args.supervisor_command or "").strip():
            return True
        if self.args.yes or not sys.stdin.isatty():
            return True
        return True

    def run(self) -> int:
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._load_local_env()

            if not os.environ.get("OPENAI_API_KEY"):
                raise RuntimeError("OPENAI_API_KEY is required. Set it in .env.local or environment.")

            if not self.prompt_dir.exists():
                raise FileNotFoundError(f"Prompt directory does not exist: {self.prompt_dir}")

            if not self.prior_work_openai_tool_agent_script.exists():
                raise FileNotFoundError(f"prior_work_openai_tool_agent.py not found: {self.prior_work_openai_tool_agent_script}")

            self.logger.ui("PERSLY - Research Agent Console")
            self.logger.ui(self.lang_text("OpenAI API 오케스트레이터 실행", "OpenAI API orchestrator started"))

            for actor in ACTORS:
                if actor in SUB_AGENTS and not self._is_agent_active(actor):
                    self.logger.actor(
                        actor,
                        self.lang_text(
                            f"{actor} 비활성화됨 (--active-agents에서 제외)",
                            f"{actor} inactive (excluded from --active-agents)",
                        ),
                    )
                else:
                    self.logger.actor(actor, self.lang_text(f"{actor} 작업 대기 중...", f"{actor} waiting..."))

            self._ensure_supervisor_start_command()
            self._apply_supervisor_command()
            self._load_manifest()
            self._build_metadata()
            self._print_runtime_summary()
            self._schedule_with_supervisor()
            self._print_scheduled_order()

            if not self._confirm_start():
                self.logger.ui(self.lang_text("사용자 취소로 종료", "Cancelled by user"))
                return 0

            self._run_parallel_reviews()

            self.logger.ui(self.lang_text("전체 병합 생성 중...", "Building global merged review..."))
            global_prompt = self._build_global_merge_prompt()
            final_file = self.output_dir / "final_review_all.md"
            self.llm.run_text(
                "supervisor",
                self.config.supervisor_model,
                self.config.supervisor_reasoning_effort,
                global_prompt,
                final_file,
            )

            self.logger.ui(self.lang_text("실행 완료", "Run completed"))
            self.logger.ui(f"final_review={final_file}")
            return 0
        except Exception as exc:
            self.logger.ui(f"ERROR: {exc}")
            self.logger.actor("supervisor", f"ERROR: {exc}")
            return 1
