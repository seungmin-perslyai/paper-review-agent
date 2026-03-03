#!/usr/bin/env python3
"""Extract lightweight structure hints from paper text."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse paper structure from extracted text")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--paper-path", required=True)
    parser.add_argument("--paper-text", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--ui-lang", choices=["ko", "en"], default="en")
    return parser.parse_args()


def lang_text(lang: str, ko: str, en: str) -> str:
    return ko if lang == "ko" else en


def is_heading_candidate(line: str) -> bool:
    s = line.strip()
    if len(s) < 4 or len(s) > 120:
        return False
    if any(ch in s for ch in ["=", "←", "→", "{", "}", "∑", "∀", "∇", "∈", "⊂", "⊆"]):
        return False
    alpha_count = sum(ch.isalpha() for ch in s)
    if alpha_count < 3:
        return False
    if alpha_count / max(len(s), 1) < 0.45:
        return False
    if re.match(r"^\d+(\.\d+)*\s+[A-Za-z가-힣]", s):
        return True
    if s.isupper() and 1 <= len(s.split()) <= 10:
        return True
    if re.match(r"^(Abstract|Introduction|Background|Method|Methods|Experiments|Results|Discussion|Conclusion|References)\b", s, re.I):
        return True
    return False


def extract_sections(lines: list[str]) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines, start=1):
        if not is_heading_candidate(line):
            continue
        title = re.sub(r"\s+", " ", line.strip())
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        sections.append({"title": title, "line_no": idx})
        if len(sections) >= 40:
            break
    return sections


def extract_caption_items(lines: list[str], kind: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if kind == "figure":
        pat = re.compile(r"^\s*(Figure|Fig\.?)\s*(\d+[A-Za-z]?)\s*[:.\-]?\s*(.*)$", re.I)
    else:
        pat = re.compile(r"^\s*(Table)\s*(\d+[A-Za-z]?)\s*[:.\-]?\s*(.*)$", re.I)

    for idx, line in enumerate(lines, start=1):
        m = pat.match(line.strip())
        if not m:
            continue
        label = f"{m.group(1)} {m.group(2)}"
        caption = re.sub(r"\s+", " ", m.group(3)).strip()
        items.append({"label": label, "caption": caption, "line_no": idx})
    return items


def write_markdown(
    out_path: Path,
    paper_id: str,
    paper_path: str,
    sections: list[dict[str, object]],
    figures: list[dict[str, object]],
    tables: list[dict[str, object]],
    lang: str,
) -> None:
    lines: list[str] = []
    lines.append(f"# {lang_text(lang, '논문 구조 파싱', 'Paper Structure Parse')} · {paper_id}")
    lines.append("")
    lines.append(f"- {lang_text(lang, '원본 파일', 'Source file')}: `{paper_path}`")
    lines.append(f"- {lang_text(lang, '감지된 섹션 수', 'Detected section count')}: {len(sections)}")
    lines.append(f"- {lang_text(lang, '감지된 Figure 캡션 수', 'Detected figure caption count')}: {len(figures)}")
    lines.append(f"- {lang_text(lang, '감지된 Table 캡션 수', 'Detected table caption count')}: {len(tables)}")
    lines.append("")

    lines.append(f"## {lang_text(lang, '섹션 후보', 'Section Candidates')}")
    if sections:
        for item in sections:
            lines.append(f"- line {item['line_no']}: {item['title']}")
    else:
        lines.append(f"- {lang_text(lang, '명확한 섹션 헤더를 찾지 못했습니다.', 'No clear section headers were detected.')}")
    lines.append("")

    lines.append(f"## {lang_text(lang, 'Figure 캡션', 'Figure Captions')}")
    if figures:
        for item in figures:
            cap = item["caption"] or lang_text(lang, "(캡션 텍스트 없음)", "(no caption text)")
            lines.append(f"- line {item['line_no']}: **{item['label']}** - {cap}")
    else:
        lines.append(f"- {lang_text(lang, 'Figure 캡션 패턴을 찾지 못했습니다.', 'No figure-caption pattern detected.')}")
    lines.append("")

    lines.append(f"## {lang_text(lang, 'Table 캡션', 'Table Captions')}")
    if tables:
        for item in tables:
            cap = item["caption"] or lang_text(lang, "(캡션 텍스트 없음)", "(no caption text)")
            lines.append(f"- line {item['line_no']}: **{item['label']}** - {cap}")
    else:
        lines.append(f"- {lang_text(lang, 'Table 캡션 패턴을 찾지 못했습니다.', 'No table-caption pattern detected.')}")
    lines.append("")

    lines.append(f"## {lang_text(lang, '파서 메모', 'Parser Notes')}")
    lines.append(
        f"- {lang_text(lang, '이 결과는 텍스트 기반 휴리스틱이며, PDF 레이아웃/폰트 정보 손실로 누락이 있을 수 있습니다.', 'This output is text-heuristic based; PDF layout/font loss may cause omissions.')}"
    )
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    text = Path(args.paper_text).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    sections = extract_sections(lines)
    figures = extract_caption_items(lines, "figure")
    tables = extract_caption_items(lines, "table")

    payload = {
        "paper_id": args.paper_id,
        "paper_path": args.paper_path,
        "sections": sections,
        "figure_captions": figures,
        "table_captions": tables,
        "parser_notes": "heuristic_text_parser",
    }
    Path(args.output_json).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    write_markdown(
        out_path=Path(args.output_md),
        paper_id=args.paper_id,
        paper_path=args.paper_path,
        sections=sections,
        figures=figures,
        tables=tables,
        lang=args.ui_lang,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
