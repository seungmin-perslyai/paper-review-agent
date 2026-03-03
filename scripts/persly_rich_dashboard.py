#!/usr/bin/env python3
"""Rich dashboard for persly-paper-review agent logs."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from rich import box
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Persly Rich dashboard")
    parser.add_argument("--script", required=True, help="Path to codex_multi_review.sh")
    parser.add_argument("--run-log-dir", required=True, help="Run log directory")
    parser.add_argument("--run-id", required=True, help="Run identifier")
    parser.add_argument("--ui-lang", choices=["ko", "en"], default="en")
    parser.add_argument("--refresh-ms", type=int, default=180)
    parser.add_argument("--child-arg", action="append", default=[])
    return parser.parse_args()


def lang_text(lang: str, ko: str, en: str) -> str:
    return ko if lang == "ko" else en


def read_tail(path: Path, max_lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return lines[-max_lines:]
    except Exception:
        return []


def last_nonempty_line(path: Path) -> str:
    lines = read_tail(path, max_lines=200)
    for line in reversed(lines):
        text = line.strip()
        if text:
            return text
    return ""


def text_from_lines(lines: list[str]) -> Text:
    text = Text()
    if not lines:
        text.append("(no log yet)", style="dim")
        return text

    for line in lines:
        text.append_text(Text.from_ansi(line.rstrip("\n")))
        text.append("\n")
    if text and text.plain.endswith("\n"):
        text = text[:-1]
    return text


def build_actor_panel(title: str, path: Path, border_style: str, max_lines: int = 180) -> Panel:
    body = text_from_lines(read_tail(path, max_lines=max_lines))
    return Panel(body, title=title, border_style=border_style, box=box.ROUNDED, padding=(0, 1))


def build_header(lang: str) -> Panel:
    title = lang_text(lang, "PERSLY - Research Agent Console", "PERSLY - Research Agent Console")
    text = Text()
    text.append(title, style="bold cyan")
    return Panel(text, border_style="cyan", box=box.ROUNDED, padding=(0, 1))


def build_footer(lang: str, run_log_dir: Path) -> Panel:
    text = Text()
    text.append(
        lang_text(
            lang,
            "각 패널은 에이전트 전용 로그입니다. 전체 로그: " + str(run_log_dir),
            "Each panel shows per-agent logs. Full logs: " + str(run_log_dir),
        ),
        style="dim",
    )
    return Panel(text, border_style="grey37", box=box.ROUNDED, padding=(0, 1))


def dashboard_layout(
    lang: str,
    run_id: str,
    status: str,
    run_log_dir: Path,
    supervisor_log: Path,
    prior_work_log: Path,
    summary_log: Path,
    method_log: Path,
    critique_log: Path,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )

    layout["body"].split_row(Layout(name="left", ratio=7), Layout(name="right", ratio=5))
    layout["left"].split_column(
        Layout(name="supervisor", ratio=3),
        Layout(name="prior_work", ratio=2),
    )
    layout["right"].split_column(
        Layout(name="summary"),
        Layout(name="method"),
        Layout(name="critique"),
    )

    layout["header"].update(build_header(lang))
    layout["supervisor"].update(
        build_actor_panel("SUPERVISOR :: plan/review/synthesis", supervisor_log, "magenta", max_lines=220)
    )
    layout["prior_work"].update(build_actor_panel("PRIOR_WORK :: search/novelty/risk", prior_work_log, "green"))
    layout["summary"].update(build_actor_panel("SUMMARY :: draft stream", summary_log, "cyan"))
    layout["method"].update(build_actor_panel("METHOD :: draft stream", method_log, "blue"))
    layout["critique"].update(build_actor_panel("CRITIQUE :: draft stream", critique_log, "yellow"))
    layout["footer"].update(build_footer(lang, run_log_dir))
    return layout


def child_args_has_option(child_args: list[str], option: str) -> bool:
    for item in child_args:
        if item == option or item.startswith(option + "="):
            return True
    return False


def is_supervisor_start_command(text: str) -> bool:
    lower = text.lower()
    if ("리뷰" in text) or ("시작" in text) or ("분석" in text):
        return True
    if ("review" in lower) or ("start" in lower) or ("analy" in lower):
        return True
    return False


def waiting_dashboard_layout(
    lang: str,
    run_id: str,
    run_log_dir: Path,
    supervisor_log: Path,
    prior_work_log: Path,
    summary_log: Path,
    method_log: Path,
    critique_log: Path,
    command_buffer: str,
    notice: str,
) -> Layout:
    layout = dashboard_layout(
        lang=lang,
        run_id=run_id,
        status=lang_text(lang, "입력 대기", "waiting for command"),
        run_log_dir=run_log_dir,
        supervisor_log=supervisor_log,
        prior_work_log=prior_work_log,
        summary_log=summary_log,
        method_log=method_log,
        critique_log=critique_log,
    )
    prompt_line = f"supervisor> {command_buffer}"
    info_line = lang_text(
        lang,
        "시작 명령 입력 후 ENTER (예: 리뷰해줘)",
        "Type start command then press ENTER (e.g., review these papers)",
    )
    lines = [info_line, "", prompt_line]
    if notice:
        lines.extend(["", notice])
    text = Text("\n".join(lines), style="white")
    layout["supervisor"].update(
        Panel(
            text,
            title="SUPERVISOR :: plan/review/synthesis",
            subtitle=lang_text(lang, "ENTER 시작 · Ctrl+C 취소", "ENTER start · Ctrl+C cancel"),
            border_style="magenta",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    return layout


def prompt_supervisor_command(
    lang: str,
    run_id: str,
    run_log_dir: Path,
    supervisor_log: Path,
    prior_work_log: Path,
    summary_log: Path,
    method_log: Path,
    critique_log: Path,
) -> str:
    if not sys.stdin.isatty():
        return ""

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    command_buffer = ""
    notice = ""

    try:
        tty.setcbreak(fd)
        with Live(screen=True, auto_refresh=False) as live:
            live.update(
                waiting_dashboard_layout(
                    lang=lang,
                    run_id=run_id,
                    run_log_dir=run_log_dir,
                    supervisor_log=supervisor_log,
                    prior_work_log=prior_work_log,
                    summary_log=summary_log,
                    method_log=method_log,
                    critique_log=critique_log,
                    command_buffer=command_buffer,
                    notice=notice,
                ),
                refresh=True,
            )
            while True:
                key = sys.stdin.read(1)
                if not key:
                    continue
                if key == "\x03":
                    raise KeyboardInterrupt
                if key in ("\r", "\n"):
                    cmd = command_buffer.strip()
                    if not cmd:
                        notice = lang_text(lang, "명령을 입력해 주세요.", "Please enter a command.")
                        live.update(
                            waiting_dashboard_layout(
                                lang=lang,
                                run_id=run_id,
                                run_log_dir=run_log_dir,
                                supervisor_log=supervisor_log,
                                prior_work_log=prior_work_log,
                                summary_log=summary_log,
                                method_log=method_log,
                                critique_log=critique_log,
                                command_buffer=command_buffer,
                                notice=notice,
                            ),
                            refresh=True,
                        )
                        continue
                    if not is_supervisor_start_command(cmd):
                        notice = lang_text(
                            lang,
                            "시작 의도를 포함해 주세요 (리뷰/시작/분석).",
                            "Include start intent (review/start/analyze).",
                        )
                        live.update(
                            waiting_dashboard_layout(
                                lang=lang,
                                run_id=run_id,
                                run_log_dir=run_log_dir,
                                supervisor_log=supervisor_log,
                                prior_work_log=prior_work_log,
                                summary_log=summary_log,
                                method_log=method_log,
                                critique_log=critique_log,
                                command_buffer=command_buffer,
                                notice=notice,
                            ),
                            refresh=True,
                        )
                        continue
                    return cmd
                if key in ("\x7f", "\b", "\x08"):
                    command_buffer = command_buffer[:-1]
                    notice = ""
                    live.update(
                        waiting_dashboard_layout(
                            lang=lang,
                            run_id=run_id,
                            run_log_dir=run_log_dir,
                            supervisor_log=supervisor_log,
                            prior_work_log=prior_work_log,
                            summary_log=summary_log,
                            method_log=method_log,
                            critique_log=critique_log,
                            command_buffer=command_buffer,
                            notice=notice,
                        ),
                        refresh=True,
                    )
                    continue
                if key == "\x1b":
                    continue
                if key.isprintable():
                    command_buffer += key
                    notice = ""
                    live.update(
                        waiting_dashboard_layout(
                            lang=lang,
                            run_id=run_id,
                            run_log_dir=run_log_dir,
                            supervisor_log=supervisor_log,
                            prior_work_log=prior_work_log,
                            summary_log=summary_log,
                            method_log=method_log,
                            critique_log=critique_log,
                            command_buffer=command_buffer,
                            notice=notice,
                        ),
                        refresh=True,
                    )
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def launch_child(script_path: Path, run_log_dir: Path, ui_lang: str, child_args: list[str]) -> subprocess.Popen[str]:
    run_log_dir.mkdir(parents=True, exist_ok=True)
    main_log = run_log_dir / "rich_child_stdout.log"
    env = os.environ.copy()
    env["PERSLY_RICH_CHILD"] = "1"
    env["PERSLY_TMUX_CHILD"] = "1"
    env["PERSLY_LOG_DIR"] = str(run_log_dir)
    env["UI_LANG"] = ui_lang
    env["NO_COLOR"] = "1"

    cmd = ["bash", str(script_path), *child_args]
    out = main_log.open("w", encoding="utf-8", errors="replace")
    return subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=env, text=True)


def main() -> int:
    args = parse_args()

    script_path = Path(args.script).resolve()
    run_log_dir = Path(args.run_log_dir).resolve()
    lang = args.ui_lang

    supervisor_log = run_log_dir / "supervisor.log"
    prior_work_log = run_log_dir / "prior_work.log"
    summary_log = run_log_dir / "summary.log"
    method_log = run_log_dir / "method.log"
    critique_log = run_log_dir / "critique.log"

    child_args = list(args.child_arg)
    if not child_args_has_option(child_args, "--supervisor-command") and not child_args_has_option(child_args, "--yes"):
        try:
            cmd = prompt_supervisor_command(
                lang=lang,
                run_id=args.run_id,
                run_log_dir=run_log_dir,
                supervisor_log=supervisor_log,
                prior_work_log=prior_work_log,
                summary_log=summary_log,
                method_log=method_log,
                critique_log=critique_log,
            )
        except KeyboardInterrupt:
            print("", flush=True)
            return 130
        if cmd:
            child_args.extend(["--supervisor-command", cmd])

    child = launch_child(script_path, run_log_dir, lang, child_args)

    refresh_hz = max(1, int(1000 / max(50, args.refresh_ms)))
    finished_at = None

    final_rc = 0
    try:
        with Live(refresh_per_second=refresh_hz, screen=True) as live:
            while True:
                rc = child.poll()
                if rc is None:
                    status = lang_text(lang, "실행 중", "running")
                elif rc == 0:
                    status = lang_text(lang, "완료", "completed")
                else:
                    status = lang_text(lang, f"실패(rc={rc})", f"failed(rc={rc})")

                live.update(
                    dashboard_layout(
                        lang=lang,
                        run_id=args.run_id,
                        status=status,
                        run_log_dir=run_log_dir,
                        supervisor_log=supervisor_log,
                        prior_work_log=prior_work_log,
                        summary_log=summary_log,
                        method_log=method_log,
                        critique_log=critique_log,
                    )
                )

                if rc is not None:
                    final_rc = rc
                    if finished_at is None:
                        finished_at = time.time()
                    if time.time() - finished_at > 1.0:
                        break

                time.sleep(max(0.05, args.refresh_ms / 1000.0))
    except KeyboardInterrupt:
        try:
            child.terminate()
        except Exception:
            pass
        return 130

    if final_rc != 0:
        main_log = run_log_dir / "main.log"
        reason = last_nonempty_line(main_log)
        print(
            lang_text(
                lang,
                f"실행 실패(rc={final_rc}) · 로그: {main_log}",
                f"Run failed (rc={final_rc}) · log: {main_log}",
            ),
            flush=True,
        )
        if reason:
            print(lang_text(lang, f"원인: {reason}", f"Reason: {reason}"), flush=True)
        if sys.stdin.isatty():
            try:
                input(lang_text(lang, "Enter를 누르면 종료합니다... ", "Press Enter to close... "))
            except EOFError:
                pass
    return final_rc


if __name__ == "__main__":
    sys.exit(main())
