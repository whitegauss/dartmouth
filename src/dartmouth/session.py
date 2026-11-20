"""会議ディレクトリの作成・再開と、エンジン／保存／ログの配線。"""

from __future__ import annotations

import os

import json

from typing import Any

import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .adapters import Adapter, create_adapter
from .config import Config, dump_config, load_config
from .engine import events as E
from .engine.context import PromptLibrary, default_prompts_dir
from .engine.flow import Engine
from .storage.export import render_requirements_draft
from .storage.transcript import TranscriptWriter, read_transcript

TRANSCRIPT = "transcript.jsonl"
MINUTES = "minutes.md"
CONFIG = "config.yaml"
APP_LOG = "app.log"
SOURCE = "source_config"  # この会議を始めた設定ファイルのパス（片付いた議題を移すため）
DRAFT = "requirements_draft.md"


def slugify(s: str) -> str:
    """英数字だけのスラッグにする。日本語だけの名前などで何も残らなければ、時刻で区別する（meeting-1530）。"""
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or f"meeting-{datetime.now().strftime('%H%M')}"


@dataclass
class Session:
    cfg: Config
    dir: Path
    engine: Engine
    writer: TranscriptWriter
    adapters: dict[str, Adapter]
    log_handler: logging.Handler
    lock: Any = None

    @property
    def meeting_id(self) -> str:
        return self.dir.name

    async def close(self) -> None:
        for a in self.adapters.values():
            try:
                await a.close()
            except Exception:  # noqa: BLE001
                logging.getLogger("dartmouth").exception("adapter close failed")
        self.writer.close()
        if self.lock is not None:
            self.lock.release()
        logging.getLogger("dartmouth").removeHandler(self.log_handler)
        self.log_handler.close()

    def export(self) -> Path:
        p = self.dir / DRAFT
        p.write_text(render_requirements_draft(self.engine.minutes, self.cfg, self.engine.stats), encoding="utf-8")
        return p


def _setup_log(d: Path) -> logging.Handler:
    h = logging.FileHandler(d / APP_LOG, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("dartmouth")
    root.setLevel(logging.INFO)
    root.addHandler(h)
    return h


def build_adapters(cfg: Config, overrides: dict[str, Adapter] | None = None) -> dict[str, Adapter]:
    out: dict[str, Adapter] = {}
    for name, p in cfg.participants.items():
        if not p.enabled:
            continue
        if overrides and name in overrides:
            out[name] = overrides[name]
        else:
            opts = p.options()
            if not cfg.meeting.tools_enabled:
                # 思考だけを任せる: 道具も、コードを読む作業ディレクトリも渡さない
                opts["tools_rounds"] = []
                opts.pop("cwd", None)
            out[name] = create_adapter(name, p.adapter, p.role, p.timeout_s, opts)
    return out


def _wire(cfg: Config, d: Path, *, headless: bool, adapters: dict[str, Adapter] | None) -> Session:
    from .storage.lock import MeetingLock

    lock = MeetingLock(d).acquire()  # 別のプロセスが進めていれば MeetingLocked
    source = (d / SOURCE).read_text(encoding="utf-8").strip() if (d / SOURCE).exists() else None
    log_handler = _setup_log(d)
    writer = TranscriptWriter(d / TRANSCRIPT)
    ads = build_adapters(cfg, adapters)
    prompts = PromptLibrary(Path(cfg.prompts_dir) if cfg.prompts_dir else default_prompts_dir())
    bus = E.EventBus()
    bus.add_handler(writer.handle)
    minutes_path = d / MINUTES

    def write_minutes(ev: E.Event) -> None:
        if ev.type == E.MINUTES:
            tmp = minutes_path.with_suffix(".md.tmp")
            tmp.write_text(ev.data["markdown"], encoding="utf-8")
            tmp.replace(minutes_path)
            write_digest(d, ev.data["markdown"])

    bus.add_handler(write_minutes)
    kb, llm = _knowledge(cfg)
    backends = build_backends(cfg, adapters)
    engine = Engine(cfg, d.name, ads, meeting_dir=d, prompts=prompts, headless=headless, bus=bus, kb=kb, local_llm=llm,
                    backends=backends)
    sess = Session(cfg, d, engine, writer, ads, log_handler, lock=lock)
    status_path = d / STATUS

    def write_status(ev: E.Event) -> None:
        # 別のプロセス（Web）が進行を見られるよう、状態をファイルに書く（一時ファイル→置き換えで、読み手が途中を見ない）
        if ev.type not in (E.RECORD, E.SPEECH_START, E.MEETING_END, E.AWAITING_APPROVAL):
            return
        st = live_state(sess)
        if ev.type == E.MEETING_END:
            st["ended"] = ev.data.get("reason")
        tmp = status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        tmp.replace(status_path)

    bus.add_handler(write_status)

    def on_end(ev: E.Event) -> None:
        if ev.type == E.MEETING_END and ev.data.get("reason") == "done":
            sess.export()
            sync_ledger(sess.dir)

    bus.add_handler(on_end)

    if source:
        from .topics_file import mark_topics_done

        log = logging.getLogger("dartmouth")

        def on_approved(ev: E.Event) -> None:
            # 承認された議題（延長の議題は除く）を、設定ファイルの「済んだ議題」へ移す
            if ev.type != E.RECORD or ev.data.get("event") != "approved":
                return
            title = ev.data.get("title") or ""
            if title.startswith("【延長】"):
                return
            try:
                moved = mark_topics_done(Path(source), [title], d.name)
                if moved:
                    log.info("marked done in %s: %s", source, moved)
            except Exception:  # noqa: BLE001  設定ファイルの更新に失敗しても会議は止めない
                log.exception("failed to mark topic done in %s", source)

        bus.add_handler(on_approved)
    return sess


def build_backends(cfg: Config, overrides: dict[str, Adapter] | None = None) -> dict[str, Adapter]:
    """議長が席に割り当てる経路（モデル）のアダプタ。席どうしで共有する。"""
    if not cfg.seating:
        return {}
    out: dict[str, Adapter] = {}
    for bid, b in cfg.seating.backends.items():
        if not b.enabled:
            continue
        if b.adapter == "gemini_api":
            import os

            env = b.options().get("api_key_env", "GEMINI_API_KEY")
            if not os.environ.get(env) and not (overrides and f"backend:{bid}" in overrides):
                logging.getLogger("dartmouth").warning("backend %s: %s がないので席の候補から外します", bid, env)
                continue
        key = f"backend:{bid}"
        if overrides and key in overrides:
            out[bid] = overrides[key]
            continue
        opts = b.options()
        if not cfg.meeting.tools_enabled:
            opts["tools_rounds"] = []
            opts.pop("cwd", None)
        out[bid] = create_adapter(bid, b.adapter, "member", b.timeout_s, opts)
    return out


STATUS = "status.json"
DIGEST = "digest.md"


def sync_ledger(d: Path) -> None:
    """会議が終わったら、その会議を台帳に取り込む（AI は使わない。失敗しても会議には影響させない）。"""
    if os.environ.get("DARTMOUTH_LEDGER", "").lower() in ("off", "0", "false"):
        return
    try:
        from .ledger import Ledger, sync_meeting
        from .paths import ledger_path

        from .agenda import split_ref
        from .ledger import sync_agenda

        lg = Ledger(ledger_path())
        try:
            src = (d / SOURCE).read_text(encoding="utf-8").strip() if (d / SOURCE).exists() else ""
            f, pid = split_ref(src) if src else (None, None)
            if pid and f and f.exists():
                sync_agenda(lg, f)
            sync_meeting(lg, d)
        finally:
            lg.close()
    except Exception:  # noqa: BLE001
        logging.getLogger("dartmouth").exception("台帳への取り込みに失敗しました: %s", d.name)


def write_digest(d: Path, markdown: str | None = None) -> Path:
    """議事録の短い版（決定・未解決の未決・未完了の ToDo）を digest.md に書く。次の会議の資料に使う。"""
    from .storage.archive import load_item_status
    from .storage.minutes import make_digest

    md = markdown if markdown is not None else (d / MINUTES).read_text(encoding="utf-8")
    done = {k for k, v in load_item_status(d).items() if v.get("done")}
    text = make_digest(md, done=done)
    if "## " not in text:
        logging.getLogger("dartmouth").warning("議事録に決定事項などの見出しがないので、digest は末尾だけにしました: %s", d.name)
    tmp = d / (DIGEST + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(d / DIGEST)
    return d / DIGEST


def live_state(s: "Session") -> dict[str, Any]:
    """画面に出す会議の状態。Web の /api/state と、CLI で進めている会議の status.json の両方で使う。"""
    import time

    from .ui.common import colors_for

    e = s.engine
    st: dict[str, Any] = e.status()
    now_wall, now_mono = time.time(), time.monotonic()
    st["inflight_since"] = {f.participant: now_wall - (now_mono - f.started) for f in e.inflight.values()}
    st["colors"] = colors_for(e)
    st["roles"] = {n: p.role for n, p in s.cfg.participants.items()}
    st["models"] = {n: (p.options().get("model") or p.adapter) for n, p in s.cfg.participants.items()}
    st["backends"] = dict(e.seat_backend)
    st["members"] = e.cur.members if e.cur else []
    st["max_calls"] = s.cfg.budget.max_calls
    st["approval_mode"] = s.cfg.meeting.approval
    st["meeting_name"] = s.cfg.meeting.name
    st["pid"] = os.getpid()
    return st


def _knowledge(cfg: Config):
    """資料庫を最新にして返す（設定がなければ None）。初回は埋め込みに時間がかかる。"""
    if not cfg.knowledge or not cfg.knowledge.sources:
        return None, None
    import sys

    from .knowledge import build_knowledge_base
    from .knowledge.localllm import LocalLLM

    kb = build_knowledge_base(cfg.knowledge, progress=lambda m: print(m, file=sys.stderr))
    ll = cfg.knowledge.local_llm
    return kb, (LocalLLM(ll.model, ll.url) if ll else None)


def create_session(
    cfg: Config,
    *,
    headless: bool = False,
    adapters: dict[str, Adapter] | None = None,
    today: date | None = None,
    source_config: str | Path | None = None,
) -> Session:
    base = Path(cfg.meetings_dir)
    stem = f"{(today or date.today()).isoformat()}-{cfg.meeting.slug or slugify(cfg.meeting.name)}"
    d = base / stem
    i = 2
    while d.exists():
        d = base / f"{stem}-{i}"
        i += 1
    d.mkdir(parents=True)
    # 再開用に、使った設定を会議ディレクトリへ残す
    if cfg.prompts_dir is None:
        cfg.prompts_dir = str(default_prompts_dir())
    (d / CONFIG).write_text(dump_config(cfg), encoding="utf-8")
    if source_config:
        (d / SOURCE).write_text(str(Path(source_config).resolve()), encoding="utf-8")
    return _wire(cfg, d, headless=headless, adapters=adapters)


def resume_session(d: str | Path, *, headless: bool = False, adapters: dict[str, Adapter] | None = None) -> Session:
    d = Path(d).resolve()
    if not (d / TRANSCRIPT).exists():
        raise FileNotFoundError(f"{d / TRANSCRIPT} がありません")
    cfg = load_config(d / CONFIG)
    sess = _wire(cfg, d, headless=headless, adapters=adapters)
    sess.engine.restore(list(read_transcript(d / TRANSCRIPT)))
    return sess


def extend_session(d: str | Path, topic: int, note: str = "", *, headless: bool = False,
                   adapters: dict[str, Adapter] | None = None) -> Session:
    """終わった会議を開き直し、議題 topic の延長を1本足す（会議の実行は呼び出し側で始める）。"""
    sess = resume_session(d, headless=headless, adapters=adapters)
    try:
        sess.engine.extend_after_done(topic, note)
    except Exception:
        # まだ何も呼んでいないので、記録とロックだけ閉じて戻す（会議ディレクトリは変えない）
        sess.writer.close()
        if sess.lock is not None:
            sess.lock.release()
        logging.getLogger("dartmouth").removeHandler(sess.log_handler)
        sess.log_handler.close()
        raise
    return sess


def export_meeting(d: str | Path) -> Path:
    """会議ディレクトリの transcript から議事録を再構築し、要件定義書の下書きを書き出す。"""
    d = Path(d).resolve()
    cfg = load_config(d / CONFIG)
    prompts = PromptLibrary(Path(cfg.prompts_dir) if cfg.prompts_dir else default_prompts_dir())
    engine = Engine(cfg, d.name, {}, meeting_dir=d, prompts=prompts)
    engine.restore(list(read_transcript(d / TRANSCRIPT)))
    p = d / DRAFT
    p.write_text(render_requirements_draft(engine.minutes, cfg, engine.stats), encoding="utf-8")
    (d / MINUTES).write_text(engine.minutes.render(), encoding="utf-8")
    write_digest(d)
    return p


def copy_example_config(dest: Path) -> None:
    src = Path(__file__).resolve().parents[2] / "config.example.yaml"
    shutil.copy(src, dest)
