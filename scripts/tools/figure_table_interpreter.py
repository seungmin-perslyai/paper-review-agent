#!/usr/bin/env python3
"""Generate deterministic figure/table interpretation notes from parsed structure."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interpret figure/table captions")
    parser.add_argument("--paper-id", required=True)
    parser.add_argument("--paper-text", required=True)
    parser.add_argument("--structure-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--ui-lang", choices=["ko", "en"], default="en")
    return parser.parse_args()


def lang_text(lang: str, ko: str, en: str) -> str:
    return ko if lang == "ko" else en


def infer_claim(caption: str, kind: str, lang: str) -> str:
    c = caption.lower()
    if "ablation" in c:
        return lang_text(lang, "구성요소별 기여도(소거 실험) 주장을 뒷받침하려는 의도", "Intended to support component-contribution (ablation) claims")
    if any(k in c for k in ["trade-off", "pareto", "latency", "throughput", "cost"]):
        return lang_text(lang, "성능-비용/지연 간 트레이드오프 주장을 뒷받침하려는 의도", "Intended to support performance-vs-cost/latency trade-off claims")
    if any(k in c for k in ["architecture", "framework", "pipeline"]):
        return lang_text(lang, "모델/시스템 구조 설명 주장을 뒷받침하려는 의도", "Intended to support architecture/pipeline explanation claims")
    if kind == "table":
        return lang_text(lang, "베이스라인 대비 정량 비교 우위 주장을 뒷받침하려는 의도", "Intended to support quantitative superiority over baselines")
    return lang_text(lang, "핵심 관찰을 시각적으로 전달하려는 의도", "Intended to convey key observations visually")


def infer_checks(caption: str, kind: str, lang: str) -> list[str]:
    checks = [
        lang_text(lang, "축/단위/범례가 명시되어 해석 가능한지 확인", "Verify axes/units/legend are explicit and interpretable"),
        lang_text(lang, "비교 대상(베이스라인/조건)이 동일한 예산·설정인지 확인", "Verify compared systems share the same budget/configuration"),
    ]
    c = caption.lower()
    if "error bar" not in c and "std" not in c and "confidence" not in c:
        checks.append(lang_text(lang, "불확실성(표준편차/신뢰구간) 표기가 누락되었는지 확인", "Check whether uncertainty markers (std/CI) are missing"))
    if kind == "table" and re.search(r"\b(sota|state[- ]of[- ]the[- ]art)\b", c):
        checks.append(lang_text(lang, "SOTA 비교의 재현 설정과 공정성 조건을 별도로 점검", "Audit reproducibility/fairness conditions for SOTA comparisons"))
    return checks


def render_items(items: list[dict[str, object]], kind: str, lang: str) -> list[str]:
    lines: list[str] = []
    if not items:
        lines.append(
            "- "
            + lang_text(
                lang,
                "감지된 항목이 없습니다. 본문에서 Figure/Table 언급과 부록 이미지를 수동 점검하세요.",
                "No items detected. Manually inspect in-text Figure/Table references and appendix images.",
            )
        )
        return lines

    for item in items:
        label = str(item.get("label", "")).strip() or ("Figure ?" if kind == "figure" else "Table ?")
        caption = str(item.get("caption", "")).strip()
        line_no = item.get("line_no", "?")
        claim = infer_claim(caption, kind, lang)
        checks = infer_checks(caption, kind, lang)
        lines.append(f"- **{label}** (line {line_no})")
        lines.append(f"  - {lang_text(lang, '캡션', 'Caption')}: {caption or lang_text(lang, '(없음)', '(none)')}")
        lines.append(f"  - {lang_text(lang, '해석 가설', 'Interpretation hypothesis')}: {claim}")
        for check in checks:
            lines.append(f"  - {lang_text(lang, '검증 체크', 'Verification check')}: {check}")
    return lines


def main() -> int:
    args = parse_args()
    structure = json.loads(Path(args.structure_json).read_text(encoding="utf-8"))
    figures = structure.get("figure_captions") or []
    tables = structure.get("table_captions") or []

    lines: list[str] = []
    lines.append(f"# {lang_text(args.ui_lang, 'Figure/Table 해석 메모', 'Figure/Table Interpretation Notes')} · {args.paper_id}")
    lines.append("")
    lines.append(f"- {lang_text(args.ui_lang, '입력 Figure 수', 'Detected figure count')}: {len(figures)}")
    lines.append(f"- {lang_text(args.ui_lang, '입력 Table 수', 'Detected table count')}: {len(tables)}")
    lines.append("")
    lines.append(f"## {lang_text(args.ui_lang, 'Figure 해석', 'Figure Interpretation')}")
    lines.extend(render_items(figures, "figure", args.ui_lang))
    lines.append("")
    lines.append(f"## {lang_text(args.ui_lang, 'Table 해석', 'Table Interpretation')}")
    lines.extend(render_items(tables, "table", args.ui_lang))
    lines.append("")
    lines.append(f"## {lang_text(args.ui_lang, '리뷰어 주의사항', 'Reviewer Cautions')}")
    lines.append(
        "- "
        + lang_text(
            args.ui_lang,
            "이 문서는 캡션 텍스트 기반 자동 해석 초안이며, 실제 이미지 픽셀 수준 해석은 포함하지 않습니다.",
            "This is a caption-text-based draft interpretation; it does not include pixel-level image understanding.",
        )
    )
    lines.append(
        "- "
        + lang_text(
            args.ui_lang,
            "핵심 결론 반영 전, 본문/부록의 원 도표를 최종 확인하세요.",
            "Before final conclusions, verify against the original figures/tables in the paper/appendix.",
        )
    )
    Path(args.output_md).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
