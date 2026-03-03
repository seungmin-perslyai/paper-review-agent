from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from openai import OpenAI

from .logger import Logger


class LLMRunner:
    def __init__(self, logger: Logger):
        self.logger = logger

    def _create_response(self, client: OpenAI, **kwargs: Any) -> Any:
        try:
            return client.responses.create(**kwargs)
        except Exception:
            if "reasoning" in kwargs:
                retry = dict(kwargs)
                retry.pop("reasoning", None)
                return client.responses.create(**retry)
            raise

    def _get_field(self, obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    def _response_items(self, response: Any) -> list[Any]:
        out = self._get_field(response, "output")
        if out is None:
            return []
        return list(out)

    def _extract_output_text(self, response: Any) -> str:
        txt = self._get_field(response, "output_text")
        if isinstance(txt, str) and txt.strip():
            return txt.strip()

        parts: list[str] = []
        for item in self._response_items(response):
            if self._get_field(item, "type") != "message":
                continue
            for content in self._get_field(item, "content", []) or []:
                ctype = self._get_field(content, "type")
                if ctype not in {"output_text", "text"}:
                    continue
                ctext = self._get_field(content, "text")
                if isinstance(ctext, str) and ctext:
                    parts.append(ctext)
        return "".join(parts).strip()

    def _parse_json_relaxed(self, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if fence:
            try:
                parsed = json.loads(fence.group(1))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass

        match = re.search(r"\{.*\}", text, re.S)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        return {}

    def run_text(self, actor: str, model: str, reasoning_effort: str, prompt_text: str, output_file: Path) -> str:
        client = OpenAI()
        kwargs: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": prompt_text}],
        }
        if reasoning_effort:
            kwargs["reasoning"] = {"effort": reasoning_effort}

        self.logger.actor(actor, f"생성 시작: {output_file.name} (model={model}, effort={reasoning_effort or 'default'})")
        response = self._create_response(client, **kwargs)
        text = self._extract_output_text(response)

        if not text:
            followup = {
                "model": model,
                "previous_response_id": self._get_field(response, "id"),
                "input": [{"role": "user", "content": "Return final output now."}],
            }
            if reasoning_effort:
                followup["reasoning"] = {"effort": reasoning_effort}
            response = self._create_response(client, **followup)
            text = self._extract_output_text(response)

        if not text:
            raise RuntimeError(f"No output text generated for {output_file}")

        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text.rstrip() + "\n", encoding="utf-8")
        self.logger.actor_stream_from_file(actor, output_file, output_file.name)
        self.logger.actor(actor, f"생성 완료: {output_file}")
        return text

    def run_json(self, actor: str, model: str, reasoning_effort: str, prompt_text: str, output_file: Path) -> dict[str, Any]:
        raw = self.run_text(actor, model, reasoning_effort, prompt_text, output_file)
        obj = self._parse_json_relaxed(raw)
        if obj:
            return obj

        retry_prompt = (
            prompt_text.rstrip()
            + "\n\nIMPORTANT: Return only one valid JSON object with no extra prose, no markdown fence."
        )
        raw_retry = self.run_text(actor, model, reasoning_effort, retry_prompt, output_file)
        return self._parse_json_relaxed(raw_retry)
