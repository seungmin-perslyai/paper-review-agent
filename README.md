# Multi-Agent Paper Review System

요구사항을 반영한 논문 리뷰 협업 시스템입니다.

- 논문 여러 개 입력 가능
- `summary`, `prior_work`, `method`, `critique` 전담 에이전트 분리
- `supervisor` 에이전트가 각 결과물 검수 후 수정 지시(재작성 루프)
- 논문별 출력 디렉터리를 분리해서 내용 혼합 방지
- 에이전트별 모델 설정 가능

## Directory Structure

```text
side_1.paper_review/
├─ config/
│  └─ codex_agents.env
├─ prompts/
│  ├─ supervisor_planning.prompt.txt
│  ├─ supervisor_quality_gate.prompt.txt
│  ├─ supervisor_single_merge.prompt.txt
│  ├─ supervisor_global_merge.prompt.txt
│  ├─ agent_summary.prompt.txt
│  ├─ agent_prior_work.prompt.txt
│  ├─ agent_method.prompt.txt
│  └─ agent_critique.prompt.txt
├─ scripts/
│  ├─ codex_multi_review.sh
│  ├─ persly_review_orchestrator.py
│  ├─ persly_rich_dashboard.py
│  ├─ review_core/
│  │  ├─ cli.py
│  │  ├─ pipeline.py
│  │  ├─ llm.py
│  │  ├─ config.py
│  │  ├─ logger.py
│  │  └─ models.py
│  └─ tools/
│     ├─ pdf_structure_parser.py
│     ├─ figure_table_interpreter.py
│     ├─ prior_work_search.py
│     └─ prior_work_openai_tool_agent.py
├─ papers/
└─ outputs/               # 실행 시 생성
```

## How It Works

1. 입력된 논문 파일(`.txt`, `.md`, `.pdf`)을 로드
2. 리뷰 시작 직후 supervisor가 먼저 planning 수행 (`supervisor_plan.md/json` 생성)
3. supervisor planning의 순서를 기반으로 논문 처리 스케줄 확정
4. 각 논문에 대해 PDF 구조 파서 + Figure/Table 해석 툴 + prior-work 후보 추출 툴(로컬 오프라인) 실행
5. 모든 에이전트(`summary/prior_work/method/critique/supervisor`)는 OpenAI Responses API 기반으로 동작
6. `prior_work` 에이전트는 OpenAI Responses API tool-calling으로 내부 툴 호출 + 초안 생성
7. supervisor가 품질 검수 후 필요 시 재작성 피드백 전달
8. 최종적으로 각 논문마다 supervisor가 통찰 중심 종합 리포트 `review_report.md` 생성
9. 전체 논문을 하나의 `final_review_all.md`로 병합

## Bash Launcher + OpenAI API Run (Recommended)

사전 조건:

- `uv` 설치 (Python 실행/의존성 관리)
- `OPENAI_API_KEY` 설정 (프로젝트 루트 `.env.local`)
- PDF를 입력할 경우 `pdftotext` 설치

의존성 설치:

```bash
cd /Users/seungminha/project_codex/side_1.paper_review
uv sync
```

환경 변수 파일 설정:

```bash
cd /Users/seungminha/project_codex/side_1.paper_review
cp .env.sample .env.local
```

`.env.local` 파일에서 값을 직접 입력:

```bash
OPENAI_API_KEY=your_api_key_here
```

스크립트는 실행 시 프로젝트 루트의 `.env.local`을 자동으로 읽습니다.

인증 처리:

- 실행 시 `.env.local`의 `OPENAI_API_KEY`를 로드
- 키가 없으면 즉시 종료하고 설정 방법을 안내
- `codex login`은 더 이상 필요하지 않음 (모든 에이전트가 OpenAI API 기반)

모델/런타임 설정:

`config/codex_agents.env`

```bash
SUMMARY_MODEL=gpt-5.2
PRIOR_WORK_MODEL=gpt-5.2
METHOD_MODEL=gpt-5.2
CRITIQUE_MODEL=gpt-5.2
SUPERVISOR_MODEL=gpt-5.3-codex
SUMMARY_REASONING_EFFORT=high
PRIOR_WORK_REASONING_EFFORT=high
METHOD_REASONING_EFFORT=high
CRITIQUE_REASONING_EFFORT=high
SUPERVISOR_REASONING_EFFORT=xhigh
MAX_PARALLEL=2
MAX_REVISIONS=2
MAX_CHARS=30000
```

모델 정책:

- `summary/prior_work/method/critique`는 `gpt-5.2` 이상만 허용
- supervisor는 `gpt-5.3-codex`로 고정
- supervisor reasoning effort는 `xhigh`로 강제
- `gpt-codex-5.3`를 넣어도 자동으로 `gpt-5.3-codex`로 정규화

프롬프트 커스터마이징:

- 기본 프롬프트는 `prompts/`에 분리되어 있어 직접 수정 가능
- 대체 프롬프트 세트를 쓰려면 `--prompt-dir <dir>` 사용

실행:

권장 실행 커맨드(`persly-paper-review` alias):

```bash
persly-paper-review --papers /Users/seungminha/project_codex/side_1.paper_review/papers --output /Users/seungminha/project_codex/side_1.paper_review/outputs --yes
```

alias가 아직 없을 때:

```bash
bash /Users/seungminha/project_codex/side_1.paper_review/scripts/codex_multi_review.sh \
  --papers /Users/seungminha/project_codex/side_1.paper_review/papers \
  --output /Users/seungminha/project_codex/side_1.paper_review/outputs \
  --yes
```

프로젝트 내부 상대경로 실행:

```bash
bash scripts/codex_multi_review.sh --papers papers --output outputs
```

UI 동작:

- 시작 시 블록 형태 배너/단계 패널/상태 로그를 표시
- 인터랙티브 터미널에서는 실행 초기에 언어 선택 UI 표시
- 언어 선택 UI 조작: `SPACE`로 체크/해제, `ENTER`로 실행
- 언어 선택 이동: `↑/↓` 또는 `k/j`
- 직접 지정하려면 `--lang ko` 또는 `--lang en`
- 인터랙티브 터미널에서는 언어 선택 다음에 에이전트 선택 UI 표시(기본: 전부 선택)
- 에이전트 선택 UI 조작: `SPACE`로 체크/해제, `ENTER`로 실행
- 직접 지정하려면 `--active-agents summary,prior_work,method,critique` (쉼표 구분)
- `--supervisor-command`을 주지 않으면 Rich 대시보드의 supervisor 패널에서 자연어 시작 명령을 입력받은 뒤 시작합니다
- 직접 지정하려면 `--supervisor-command "리뷰해줘"` 사용 (이 경우 명령 입력 대기 없이 시작)
- 선택한 언어는 UI뿐 아니라 생성되는 모든 Markdown 결과물(`summary/prior_work/method/critique/review/final/supervisor_plan`)에도 동일하게 적용
- 인터랙티브 터미널에서는 Rich 분할 대시보드를 기본으로 실행
- 5분할 배치: 왼쪽 상단 `supervisor`, 왼쪽 하단 `prior_work`, 오른쪽 상/중/하 `summary`, `method`, `critique`
- Rich 실행에 필요한 환경이 없으면 `uv sync`를 먼저 실행해야 함
- 각 pane은 해당 에이전트의 로그만 출력 (교차 출력 없음)
- 대시보드 로그는 시간/INFO 태그 없이, 현재 작업/입출력 중심으로 표시
- `summary`/`prior_work`/`method` 완료 직후에도 `supervisor 승인(quality gate)` 단계가 있으면 후속 에이전트가 대기로 남는 것이 정상이며, 해당 대기 사유를 로그에 표시
- `summary` 다음 순서는 `prior_work`이며, 이후 `method`, 마지막 `critique` 순서로 진행
- 생성 텍스트는 미리보기 일부가 아니라 전체 본문을 pane에 스트리밍 출력
- 긴 출력은 로그 파일(`outputs/<run_id>/.run_logs`)에서도 전체 확인 가능
- 자동 대시보드를 모두 끄려면 `--no-rich-dashboard --no-tmux-dashboard`
- Rich 대시보드를 강제로 쓰려면 `--rich-dashboard`
- Rich 대시보드를 끄려면 `--no-rich-dashboard`
- tmux 대시보드를 강제로 쓰려면 `--tmux-dashboard`
- 기본 확인 프롬프트를 생략하려면 `--yes`
- `--yes`를 주면 언어/에이전트/supervisor 입력 UI도 건너뛰고 옵션/기본값으로 즉시 실행
- 컬러 출력 비활성화는 `--no-color`
- API 호출 상세 로그는 `--verbose-ui`

논문 경로 여러 개를 동시에 입력:

```bash
bash scripts/codex_multi_review.sh \
  --papers papers/set_a \
  --papers papers/set_b \
  --output outputs
```

런타임 오버라이드:

```bash
bash scripts/codex_multi_review.sh \
  --papers papers \
  --lang ko \
  --active-agents summary,method,critique \
  --max-parallel 3 \
  --max-revisions 1
```

추가 논문 이어서 리뷰(미완료만 실행):

```bash
# 기존 완료 논문은 재사용하고, 새로 추가된/미완료 논문만 실행
persly-paper-review \
  --papers /Users/seungminha/project_codex/side_1.paper_review/papers \
  --output /Users/seungminha/project_codex/side_1.paper_review/outputs \
  --continue-added \
  --yes
```

자연어 supervisor 명령으로 동일 모드 활성화:

```bash
persly-paper-review \
  --papers /Users/seungminha/project_codex/side_1.paper_review/papers \
  --output /Users/seungminha/project_codex/side_1.paper_review/outputs \
  --supervisor-command "추가 논문만 이어서 리뷰" \
  --yes
```

동작 방식:

- 파일 리스트만 읽는 것으로는 "이미 리뷰 완료된 논문"을 안정적으로 구분하기 어려워서, 상태 파일을 함께 사용
- `outputs/.state/review_manifest.tsv`에 `source_path`, `paper_id`, `status` 등을 기록
- `--continue-added`일 때는 `review_report.md`가 이미 있는 논문은 재실행하지 않고 재사용
- 누락/실패 상태 논문만 다시 실행하고, 마지막에 `final_review_all.md`를 다시 병합

프롬프트 디렉터리 교체:

```bash
bash scripts/codex_multi_review.sh \
  --papers papers \
  --prompt-dir /path/to/custom-prompts \
  --output outputs
```

대시보드 동작 제어 예시:

```bash
# 자동 대시보드 전체 비활성화 (일반 로그 모드)
persly-paper-review --papers /Users/seungminha/project_codex/side_1.paper_review/papers --output /Users/seungminha/project_codex/side_1.paper_review/outputs --no-rich-dashboard --no-tmux-dashboard

# 강제 Rich 대시보드 실행
persly-paper-review --papers /Users/seungminha/project_codex/side_1.paper_review/papers --output /Users/seungminha/project_codex/side_1.paper_review/outputs --rich-dashboard

# 강제 tmux 대시보드 실행
persly-paper-review --papers /Users/seungminha/project_codex/side_1.paper_review/papers --output /Users/seungminha/project_codex/side_1.paper_review/outputs --tmux-dashboard
```

## Output

논문별 출력(run 단위로 분리):

```text
outputs/<run_id>/<paper_id>/
├─ summary.md
├─ prior_work.md
├─ method.md
├─ critique.md
├─ paper_structure.json
├─ paper_structure.md
├─ figure_table_analysis.md
├─ prior_work_candidates.md
└─ review_report.md
```

전체 병합 출력:

- `outputs/<run_id>/final_review_all.md`
- `outputs/<run_id>/supervisor_plan.md`
- `outputs/<run_id>/supervisor_plan.json`
- `outputs/.state/review_manifest.tsv`
- `outputs/<run_id>/.run_logs/supervisor.log`
- `outputs/<run_id>/.run_logs/summary.log`
- `outputs/<run_id>/.run_logs/prior_work.log`
- `outputs/<run_id>/.run_logs/method.log`
- `outputs/<run_id>/.run_logs/critique.log`

## Agent Model Configuration

`OpenAI API` 설정:

- `config/codex_agents.env`

## Notes

- 논문 내용 혼합 방지를 위해 출력 경로를 논문 단위로 격리합니다.
- supervisor는 스케줄 순서를 결정하고, 최종 병합도 같은 순서를 유지합니다.
- supervisor 최종 리포트는 단순 요약이 아니라 근거-판단-권고 중심의 종합 분석을 목표로 합니다.
- `summary/prior_work/method/critique/supervisor` 전부 OpenAI Responses API로 실행됩니다.
- `prior_work` 초안은 OpenAI API tool-calling으로 생성되며, 내부 툴(`pdf_structure_parser`, `figure_table_interpreter`, `prior_work_search`)을 함수 호출 방식으로 사용합니다.
- `prior_work_search.py`는 현재 로컬 텍스트 기반 후보 추출이며, 웹 API 검색은 별도 연동 시 확장 가능합니다.
