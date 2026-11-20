"""比較実験の実行とセッション台帳への記録。"""

from __future__ import annotations

import argparse
import asyncio
import csv
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from dartmouth.adapters import AdapterError, Reply, TurnContext, create_adapter
from dartmouth.config import CHAIRS, Config, apply_chair_override
from dartmouth.leakscan import PATTERNS
from dartmouth.storage.archive import classify_error

from .validate_output import validate_output

WARNING_TOKENS = 360_000
ABORT_TOKENS = 480_000
ADAPTERS = {"A": "antigravity_cli", "B": "antigravity_cli", "C": "claude_code", "D": "claude_code"}
SEAT_COUNTS = {"A": 2, "B": 4, "C": 2, "D": 4}
PERSONAS = Path(__file__).resolve().parents[1] / "prompts/personas"
ID_PATTERN = r"^[A-Za-z0-9_-]+$"
MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"
CSV_FIELDS = (
    "order", "session_id", "phase", "topic", "condition", "blind_id", "status", "retry_of",
    "roles", "calls", "input_tokens", "output_tokens", "total_tokens", "duration_seconds",
    "retries", "format_error", "share_scope", "score", "double_check_score", "log_ref", "notes",
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Backend(Settings):
    adapter: Literal["claude_code", "antigravity_cli"]
    model: str = Field(pattern=MODEL_PATTERN)
    effort: Literal["low", "medium", "high", "xhigh"]


class Seat(Backend):
    id: str = Field(pattern=ID_PATTERN)
    persona: str = Field(pattern=ID_PATTERN)
    fallback: Backend | None = None


class Condition(Settings):
    seats: list[Seat]
    roles_standard: Literal["neon", "argon", "opus"]
    roles_reversed: Literal["neon", "argon", "opus"]
    chair_effort: Literal["low", "medium", "high"] = "medium"


class Topic(Settings):
    prompt: str = Field(min_length=1)
    restricted: bool = False
    share_scope: Literal["private", "public"] = "private"


class Experiment(Settings):
    topics: dict[str, Topic]
    conditions: dict[str, Condition]
    pilot_topic: str
    pilot_conditions: tuple[str, str]
    max_chars: int = Field(default=400, gt=0)
    forbidden_words: tuple[str, ...] = ()
    timeout_s: float = Field(default=300, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_design(self) -> Experiment:
        if len(self.topics) != 3 or any(not re.fullmatch(ID_PATTERN, topic) for topic in self.topics):
            raise ValueError("議題IDを3件指定してください")
        if set(self.conditions) != ADAPTERS.keys():
            raise ValueError("条件 A〜D を指定してください")
        if (
            self.pilot_topic not in self.topics or len(set(self.pilot_conditions)) != 2
            or not set(self.pilot_conditions) <= self.conditions.keys()
        ):
            raise ValueError("パイロットには既存の議題1件と異なる条件2件を指定してください")
        if any(not word.strip() for word in self.forbidden_words):
            raise ValueError("禁止語には空文字を指定できません")
        for name, condition in self.conditions.items():
            seats = condition.seats
            effort = "high" if SEAT_COUNTS[name] == 2 else "low"
            if (
                len(seats) != SEAT_COUNTS[name] or len({seat.id for seat in seats}) != len(seats)
                or any(seat.id == "chair" or seat.adapter != ADAPTERS[name] or seat.effort != effort for seat in seats)
            ):
                raise ValueError(f"条件 {name} の席数・ID・アダプタ・effort を確認してください")
            if condition.roles_standard == condition.roles_reversed:
                raise ValueError(f"条件 {name} の標準・逆順には異なる議長を指定してください")
        return self


class Aborted(Exception):
    """ログと台帳に書ける固定の停止理由。"""


@dataclass
class Usage:
    calls: int = 0
    retries: int = 0
    known_tokens: int = 0
    missing_total: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    missing_input: int = 0
    missing_output: int = 0
    format_error: int = 0
    warned: bool = False
    stop: str = ""

    def record(self, reply: Reply, backend: Backend) -> None:
        metrics = reply.meta.get("usage") or {}
        for field in ("input_tokens", "output_tokens"):
            value = metrics.get(field)
            if isinstance(value, int) and value >= 0:
                if field == "input_tokens" and backend.adapter == "claude_code":
                    value += sum(metrics.get(key) or 0 for key in ("cache_creation_input_tokens", "cache_read_input_tokens"))
                setattr(self, field, getattr(self, field) + value)
            else:
                missing = "missing_input" if field == "input_tokens" else "missing_output"
                setattr(self, missing, getattr(self, missing) + 1)
        if reply.tokens is None:
            self.missing_total += 1
        else:
            self.known_tokens += reply.tokens
        if self.known_tokens >= WARNING_TOKENS and not self.warned:
            logging.warning("セッションの計測済みトークンが %d に到達しました", WARNING_TOKENS)
            self.warned = True
        if self.known_tokens >= ABORT_TOKENS:
            self.stop = "token_limit"


def sanitize(value: str, pattern: str) -> str:
    """ログの自由入力を識別子に制限し、秘密情報のパターンを除去する。"""
    if not re.fullmatch(pattern, value) or any(rule.search(value) for _, rule in PATTERNS):
        return "[REDACTED]"
    return value


def log_call(path: Path, seat: str, model: str, reason: str, switches: int, prompt: str) -> None:
    record = {
        "seat_id": sanitize(seat, ID_PATTERN),
        "model": sanitize(model, MODEL_PATTERN),
        "switch_reason": reason,
        "switch_count": switches,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def error_kind(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    message = str(error).lower()
    if re.search(r"\b429\b|too many requests|rate.limit", message):
        return "http_429"
    if re.search(r"\b5[0-9]{2}\b", message):
        return "http_5xx"
    if re.search(r"timed? out|timeout", message):
        return "timeout"
    if classify_error(message) in {"利用上限", "回数上限"} or "usage limit" in message:
        return "quota"
    return "adapter_error"


def chair_backend(condition: Condition, roles: str) -> Backend:
    cfg = Config(participants={"chair": {"role": "chair", "adapter": "claude_code"}})
    chair = getattr(condition, roles)
    assert chair in CHAIRS
    apply_chair_override(cfg, chair, condition.chair_effort)
    participant = cfg.participants["chair"]
    options = participant.options()
    return Backend(adapter=participant.adapter, model=options["model"], effort=options.get("effort", condition.chair_effort))


class Session:
    def __init__(self, settings: Experiment, topic: Topic, session_id: str, directory: Path, factory):
        self.settings = settings
        self.topic = topic
        self.id = session_id
        self.directory = directory
        self.factory = factory
        self.usage = Usage()
        self.switched: set[str] = set()
        self.messages: list[tuple[str, str]] = []

    def abort(self, reason: str) -> None:
        if not self.usage.stop:
            self.usage.stop = reason
        raise Aborted(self.usage.stop)

    async def call(self, seat: str, primary: Backend, fallback: Backend | None, round_: str, prompt: str) -> str:
        if self.usage.stop:
            raise Aborted(self.usage.stop)
        backend = fallback if seat in self.switched else primary
        reason = ""
        retried = False
        while True:
            if self.usage.stop:
                raise Aborted(self.usage.stop)
            self.usage.calls += 1
            options = {"model": backend.model, "effort": backend.effort, "tools_rounds": []}
            if seat == "chair" and backend.adapter == "antigravity_cli":
                options.pop("effort")
            adapter = self.factory(seat, backend.adapter, "chair" if seat == "chair" else "member",
                                   self.settings.timeout_s, options)
            context = TurnContext(prompt, seat, adapter.role, round_, None, self.id)
            try:
                reply = await asyncio.wait_for(adapter.respond(context), self.settings.timeout_s)
            except (AdapterError, OSError, TimeoutError, asyncio.CancelledError) as error:
                log_call(self.directory / "calls.jsonl", seat, backend.model, reason, int(seat in self.switched), prompt)
                self.usage.missing_total += 1
                self.usage.missing_input += 1
                self.usage.missing_output += 1
                if isinstance(error, asyncio.CancelledError):
                    raise
                kind = error_kind(error)
                if kind in {"http_429", "http_5xx", "timeout"} and not retried:
                    retried = True
                    self.usage.retries += 1
                    continue
                if kind == "quota" and fallback and seat not in self.switched:
                    if self.topic.restricted and fallback.adapter != primary.adapter:
                        self.abort("restricted_fallback")
                    self.switched.add(seat)
                    backend = fallback
                    reason = "quota"
                    continue
                self.abort(kind)
            finally:
                await adapter.close()
            model = reply.meta.get("model")
            log_call(self.directory / "calls.jsonl", seat, model if isinstance(model, str) else backend.model,
                     reason, int(seat in self.switched), prompt)
            self.usage.record(reply, backend)
            if self.usage.stop:
                raise Aborted(self.usage.stop)
            result, invalid = validate_output(reply.text, self.settings.max_chars, self.settings.forbidden_words)
            self.usage.format_error += int(invalid)
            return result

    def prompt(self, persona: str, instruction: str, history: str = "") -> str:
        return (
            f"{persona}\n\n議題\n{self.topic.prompt}\n\n{history}\n\n{instruction}\n"
            f"【結論】【根拠】【未決】をこの順で使い、全体を{self.settings.max_chars}字以内に収めてください。"
        )

    def history(self) -> str:
        return "\n\n".join(f"@{seat}\n{text}" for seat, text in self.messages)

    async def run(self, condition: Condition, roles: str) -> str:
        personas = {seat.id: (PERSONAS / f"{seat.persona}.md").read_text(encoding="utf-8") for seat in condition.seats}
        chair = chair_backend(condition, roles)
        if self.topic.restricted and chair.adapter != condition.seats[0].adapter:
            self.abort("restricted_chair")

        tasks = []
        async with asyncio.TaskGroup() as group:
            for seat in condition.seats:
                prompt = self.prompt(personas[seat.id], "他の席から独立して、自分の案を示してください。")
                tasks.append(group.create_task(self.call(seat.id, seat, seat.fallback, "independent", prompt)))
        self.messages.extend((seat.id, task.result()) for seat, task in zip(condition.seats, tasks))
        for _ in range(2):
            for seat in condition.seats:
                prompt = self.prompt(
                    personas[seat.id],
                    "他の各席の案の弱点と取り入れたい点を挙げ、更新した案を示してください。",
                    self.history(),
                )
                reply = await self.call(seat.id, seat, seat.fallback, "critique", prompt)
                self.messages.append((seat.id, reply))
        prompt = self.prompt(
            (PERSONAS / "chair.md").read_text(encoding="utf-8"),
            "決定事項案、ToDo、未決事項を整理してください。",
            self.history(),
        )
        return await self.call("chair", chair, None, "summary", prompt)


class DryAdapter:
    def __init__(self, name, adapter, role, timeout, options):
        self.role = role

    async def respond(self, context: TurnContext) -> Reply:
        return Reply("【結論】検証用の応答\n【根拠】模擬実行\n【未決】なし", tokens=20,
                     meta={"usage": {"input_tokens": 10, "output_tokens": 10}})

    async def close(self) -> None:
        pass


def main_plan(topics):
    import random

    plan = [(topic, condition) for topic in sorted(topics) for condition in sorted(ADAPTERS)]
    random.Random(20261119).shuffle(plan)
    return plan


async def run_session(settings, topic, condition, phase, order, roles, artifacts, factory, retry_of=""):
    session_id = uuid.uuid4().hex
    directory = artifacts / session_id
    directory.mkdir(mode=0o700, parents=True)
    session = Session(settings, settings.topics[topic], session_id, directory, factory)
    started = time.monotonic()
    status = "COMPLETED"
    notes = []
    try:
        summary = await session.run(settings.conditions[condition], roles)
        (directory / "review.json").write_text(json.dumps({"summary": summary, "discussion": session.messages}, ensure_ascii=False), encoding="utf-8")
    except Aborted as error:
        status = "ABORTED"
        notes.append(str(error))
    except Exception:
        status = "ABORTED"
        notes.append(session.usage.stop or "execution_error")
    usage = session.usage
    unknown = []
    for field, missing in (("input_tokens", usage.missing_input), ("output_tokens", usage.missing_output),
                           ("total_tokens", usage.missing_total)):
        if missing:
            unknown.append(field)
    if unknown:
        notes.append("usage_unavailable=" + "|".join(unknown))
    return {
        "order": order, "session_id": session_id, "phase": phase, "topic": topic, "condition": condition,
        "blind_id": uuid.uuid4().hex, "status": status, "retry_of": retry_of, "roles": roles,
        "calls": usage.calls, "input_tokens": "" if usage.missing_input else usage.input_tokens,
        "output_tokens": "" if usage.missing_output else usage.output_tokens,
        "total_tokens": "" if usage.missing_total else usage.known_tokens,
        "duration_seconds": round(time.monotonic() - started, 3),
        "retries": usage.retries, "format_error": usage.format_error,
        "share_scope": settings.topics[topic].share_scope,
        "log_ref": f"{session_id}/calls.jsonl" if usage.calls else "",
        "notes": ";".join(notes),
    }


def execute(settings, args, parser):
    csv_path = args.csv
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    # 同じ台帳に対する再実行判定と行の追記を直列化する。
    with csv_path.with_name(csv_path.name + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with csv_path.open("a+", encoding="utf-8", newline="") as stream:
            stream.seek(0)
            reader = csv.DictReader(stream)
            rows = list(reader)
            if reader.fieldnames is not None and tuple(reader.fieldnames) != CSV_FIELDS:
                parser.error("CSV の列が実験台帳の定義と一致しません")
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            if reader.fieldnames is None:
                writer.writeheader()
            phase = "pilot" if args.pilot else "main"
            roles = args.roles or "roles_standard"
            plan = [(settings.pilot_topic, condition) for condition in settings.pilot_conditions] if args.pilot else main_plan(settings.topics)
            jobs = [(order, topic, condition, phase, roles, "") for order, (topic, condition) in enumerate(plan, 1)]
            if args.retry_session:
                original = next((row for row in rows if row["session_id"] == args.retry_session), None)
                if not original or original["status"] != "ABORTED" or original["retry_of"]:
                    parser.error("再実行には初回 ABORTED のセッションIDを指定してください")
                retries = {row["retry_of"] for row in rows if row["retry_of"]}
                if original["session_id"] in retries or len(retries) >= 2:
                    parser.error("再実行の上限に達しています")
                if original["topic"] not in settings.topics or original["condition"] not in settings.conditions:
                    parser.error("再実行の議題または条件が設定にありません")
                if args.roles and args.roles != original["roles"]:
                    parser.error("再実行では議長の割当てを変更できません")
                jobs = [(original["order"], original["topic"], original["condition"], original["phase"],
                         original["roles"], original["session_id"])]
            else:
                existing = {(row["phase"], row["topic"], row["condition"]) for row in rows if row["status"] != "PLANNED"}
                jobs = [job for job in jobs if (job[3], job[1], job[2]) not in existing]
            factory = DryAdapter if args.dry_run else create_adapter
            for order, topic, condition, phase, roles, retry_of in jobs:
                row = asyncio.run(run_session(settings, topic, condition, phase, order, roles, args.artifacts, factory, retry_of))
                writer.writerow(row)
                stream.flush()
                print(row["session_id"], row["status"], row["notes"])


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/experiment.yaml"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pilot", action="store_true")
    mode.add_argument("--retry-session")
    parser.add_argument("--roles", choices=("roles_standard", "roles_reversed"))
    execution = parser.add_mutually_exclusive_group(required=True)
    execution.add_argument("--dry-run", action="store_true")
    execution.add_argument("--live", action="store_true")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args(argv)
    if args.artifacts is None:
        args.artifacts = Path("local/experiment-dry" if args.dry_run else "local/experiment")
    if args.csv is None:
        args.csv = args.artifacts / "sessions.csv" if args.dry_run else Path("docs/experiment-sessions.csv")
    # CLI アダプタの診断ログには引数や stderr が含まれうる。
    logging.getLogger("dartmouth.adapter").disabled = True
    try:
        settings = Experiment.model_validate(yaml.safe_load(args.config.read_text(encoding="utf-8")))
    except (OSError, ValidationError, yaml.YAMLError):
        parser.error("実験設定を読み込めません。設定のパスと YAML の形式を確認してください")
    if args.live:
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "GOOGLE_API_KEY",
                    "GEMINI_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                    "CLAUDE_CODE_USE_FOUNDRY", "GOOGLE_GENAI_USE_VERTEXAI"):
            os.environ.pop(key, None)
    try:
        execute(settings, args, parser)
    except OSError:
        parser.exit(1, "実験台帳を読み書きできません\n")


if __name__ == "__main__":
    main()
