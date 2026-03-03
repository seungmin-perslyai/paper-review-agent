from __future__ import annotations

import threading
from pathlib import Path


class Logger:
    def __init__(self, run_log_dir: Path):
        self.run_log_dir = run_log_dir
        self.run_log_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.main_log = self.run_log_dir / "main.log"
        self.actor_logs = {
            "supervisor": self.run_log_dir / "supervisor.log",
            "summary": self.run_log_dir / "summary.log",
            "prior_work": self.run_log_dir / "prior_work.log",
            "method": self.run_log_dir / "method.log",
            "critique": self.run_log_dir / "critique.log",
        }
        self.main_log.write_text("", encoding="utf-8")
        for path in self.actor_logs.values():
            path.write_text("", encoding="utf-8")

    def ui(self, message: str) -> None:
        print(message, flush=True)
        with self.lock:
            with self.main_log.open("a", encoding="utf-8") as fh:
                fh.write(message.rstrip() + "\n")

    def actor(self, actor: str, message: str) -> None:
        path = self.actor_logs.get(actor, self.main_log)
        with self.lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(message.rstrip() + "\n")

    def actor_stream_from_file(self, actor: str, file_path: Path, label: str | None = None) -> None:
        head = label or file_path.name
        self.actor(actor, f"----- STREAM START :: {head} -----")
        if file_path.exists():
            with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    self.actor(actor, line.rstrip("\n"))
        self.actor(actor, f"----- STREAM END :: {head} -----")

    def actor_block_from_file(self, actor: str, file_path: Path, label: str) -> None:
        self.actor(actor, f"---------------- {label} :: {file_path.name} ----------------")
        if file_path.exists():
            with file_path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    self.actor(actor, line.rstrip("\n"))
        self.actor(actor, f"---------------- end {label} ----------------")
