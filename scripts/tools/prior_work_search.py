#!/usr/bin/env python3
"""Extract prior-work candidates from local paper text (offline seed)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract prior-work candidates from references/in-text citations")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--paper-text", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--ui-lang", choices=["ko", "en"], default="en")
    return parser.parse_args()


def lang_text(lang: str, ko: str, en: str) -> str:
    return ko if lang == "ko" else en


def extract_reference_lines(lines: list[str]) -> list[str]:
    ref_start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*(references|bibliography)\s*$", line, re.I):
            ref_start = i
            break
    if ref_start is None:
        return []
    ref_lines = [ln.strip() for ln in lines[ref_start + 1 :] if ln.strip()]
    return ref_lines[:400]


def parse_reference_candidates(ref_lines: list[str], limit: int = 20) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in ref_lines:
        cleaned = re.sub(r"^\[\d+\]\s*", "", line)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue
        if len(cleaned) > 220:
            cleaned = cleaned[:220].rstrip() + "..."
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= limit:
            break
    return out


def parse_inline_citations(text: str, limit: int = 15) -> list[str]:
    patterns = [
        r"\(([^()]{2,80}?,\s*(?:19|20)\d{2}[a-z]?)\)",
        r"\[([A-Z][A-Za-z\-]+(?:\s+et al\.)?,\s*(?:19|20)\d{2}[a-z]?)\]",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, text):
            token = re.sub(r"\s+", " ", m.group(1)).strip()
            if token.lower() in seen:
                continue
            seen.add(token.lower())
            found.append(token)
            if len(found) >= limit:
                return found
    return found


def build_queries(candidates: list[str], inline: list[str], lang: str, limit: int = 8) -> list[str]:
    raw = candidates[:4] + inline[:4]
    queries: list[str] = []
    seen: set[str] = set()
    for item in raw:
        base = re.sub(r"\.\.\.$", "", item)
        q = f"{base} {lang_text(lang, '논문', 'paper')}"
        if q.lower() in seen:
            continue
        seen.add(q.lower())
        queries.append(q)
        if len(queries) >= limit:
            break
    return queries


def main() -> int:
    args = parse_args()
    text = Path(args.paper_text).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    ref_lines = extract_reference_lines(lines)
    candidates = parse_reference_candidates(ref_lines)
    inline = parse_inline_citations(text)
    queries = build_queries(candidates, inline, args.ui_lang)

    out: list[str] = []
    out.append(f"# {lang_text(args.ui_lang, 'Prior-work 후보 추출(오프라인)', 'Prior-Work Candidate Extraction (Offline)')} · {args.paper_id}")
    out.append("")
    out.append(
        "- "
        + lang_text(
            args.ui_lang,
            "이 결과는 로컬 논문 텍스트 기반 후보 목록입니다. 웹 API 검색은 포함하지 않습니다.",
            "This output is a local-text candidate list. It does not include web API search.",
        )
    )
    out.append("")
    out.append(f"## {lang_text(args.ui_lang, '참고문헌 기반 후보', 'Reference-Based Candidates')}")
    if candidates:
        for item in candidates:
            out.append(f"- {item}")
    else:
        out.append("- " + lang_text(args.ui_lang, "참고문헌 섹션을 감지하지 못했습니다.", "No explicit references section detected."))
    out.append("")
    out.append(f"## {lang_text(args.ui_lang, '본문 인용 패턴 후보', 'In-Text Citation Candidates')}")
    if inline:
        for item in inline:
            out.append(f"- {item}")
    else:
        out.append("- " + lang_text(args.ui_lang, "인용 패턴 후보를 찾지 못했습니다.", "No in-text citation candidates detected."))
    out.append("")
    out.append(f"## {lang_text(args.ui_lang, '검증용 검색 쿼리', 'Verification Search Queries')}")
    if queries:
        for q in queries:
            out.append(f"- {q}")
    else:
        out.append("- " + lang_text(args.ui_lang, "생성 가능한 검색 쿼리가 없습니다.", "No useful search queries generated."))
    out.append("")
    out.append(f"## {lang_text(args.ui_lang, '리스크 신호', 'Risk Signals')}")
    if not candidates and not inline:
        out.append("- " + lang_text(args.ui_lang, "선행연구 근거가 약해 참신성 과대평가 위험이 큽니다.", "Weak prior-work evidence increases novelty-overclaim risk."))
    else:
        out.append("- " + lang_text(args.ui_lang, "최종 판단 전, 핵심 후보의 연도/문제설정/평가지표 일치 여부를 확인해야 합니다.", "Before final judgment, verify year/task/metric alignment for key candidates."))

    Path(args.output_md).write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
