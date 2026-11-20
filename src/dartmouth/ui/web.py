"""Web UI（FastAPI）。エンジンのイベントを購読して表示し、操作をエンジンに渡すだけ（TUI と同じ立場）。

2つの起動のしかた:
- `dm run --ui web` / `dm resume --ui web`: 1つの会議を開いた状態で起動する
- `dm web -c base.yaml`: 会議を持たずに起動し、画面から「議題を入力して開始」「過去の会議の未決事項から開始」「再開」をする。
  同時に開く会議は1つ

待ち受けるのは 127.0.0.1 と Tailscale のアドレスだけ（0.0.0.0 では待ち受けない）。
- Linux に tailscale が入っている（常時稼働の小型サーバーなど）: Tailscale のアドレスで直接待ち受ける
- WSL で、Tailscale が Windows 側に入っている: WSL からは Tailscale のアドレスで待ち受けられないので、
  Windows の `tailscale serve` に中継させる（tailnet の中だけに公開。Windows の localhost は WSL に転送される）。
  中継は起動時に張り、終了時に外す
ログイン画面は作らず、起動時に表示するトークン付きURLで開く。トークンを受け取ったらクッキーに移し、
URL からは消す（ブラウザの履歴やスクリーンショットにトークンを残さないため）。
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse, StreamingResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from ..config import ContextItem, TopicConfig, apply_chair_override, load_config
from ..engine import commands
from ..engine import events as E
from ..engine.flow import PHASE_LABEL
from ..session import STATUS, Session, create_session, live_state, resume_session
from ..storage.lock import is_locked
from ..storage import archive
from ..storage.minutes import Proposal
from ..storage.transcript import read_transcript
from .common import ROUND_LABEL, clear_draft, colors_for, load_draft, save_draft, yaml_to_proposal

COOKIE = "dartmouth_token"
DEFAULT_PORT = 8765
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale が割り当てる範囲（CGNAT）
FORBIDDEN_HOSTS = {"0.0.0.0", "::", ""}
WINDOWS_TAILSCALE = "/mnt/c/Program Files/Tailscale/tailscale.exe"
STATIC = Path(__file__).with_name("web_static")


class _Asset:
    def __init__(self, path: Path):
        import gzip
        import hashlib
        import mimetypes

        self.mtime = path.stat().st_mtime
        self.raw = path.read_bytes()
        self.gz = gzip.compress(self.raw, 9)
        self.hash = hashlib.sha256(self.raw).hexdigest()[:12]
        self.etag = f'"{self.hash}"'
        mt = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.media_type = f"{mt}; charset=utf-8" if mt.startswith("text/") or mt.endswith("javascript") else mt


class _Assets:
    """web_static/ の CSS・JS を、中身のハッシュつきの URL で配る（変わったら URL が変わるので、ずっとキャッシュできる）。"""

    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[str, _Asset] = {}

    def get(self, name: str) -> _Asset | None:
        p = (self.root / name).resolve()
        if self.root.resolve() not in p.parents or not p.is_file() or p.suffix not in (".js", ".css", ".svg", ".woff2"):
            return None
        a = self._cache.get(name)
        if a is None or a.mtime != p.stat().st_mtime:
            a = self._cache[name] = _Asset(p)
        return a

    def url(self, name: str) -> str:
        a = self.get(name)
        return f"/static/{name}?v={a.hash}" if a else f"/static/{name}"

    def index_html(self) -> str:
        """index.html の CSS・JS の参照にハッシュを付け、あとから読む画面の JS の URL も渡す。"""
        import json
        import re

        html = (self.root / "index.html").read_text(encoding="utf-8")
        html = re.sub(r'(src|href)="/static/([^"?]+)"', lambda m: f'{m.group(1)}="{self.url(m.group(2))}"', html)
        lazy = {p.name: self.url(f"js/{p.name}") for p in sorted((self.root / "js").glob("*.js"))}
        return html.replace("</head>", f"<script>window.__assets={json.dumps(lazy)}</script>\n</head>", 1)


assets = _Assets(STATIC)

# 生の HTML は無効（エージェントの出力にスクリプトが含まれても動かない）。javascript: のリンクも markdown-it が弾く
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")


def render_md(text: str) -> str:
    return _md.render(text or "")


# ---------------------------------------------------------------------- Tailscale


def find_tailscale() -> tuple[str, str] | None:
    """(実行ファイル, 種類) を返す。種類は native（この Linux で動いている）か windows（WSL から Windows 側を使う）。"""
    native = shutil.which("tailscale")
    if native:
        return native, "native"
    if Path(WINDOWS_TAILSCALE).exists():
        return WINDOWS_TAILSCALE, "windows"
    return None


def _ts(args: list[str], exe: str, timeout: float = 15) -> subprocess.CompletedProcess:
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, check=False)


def tailscale_ipv4() -> str | None:
    """この Linux で直接待ち受けられる Tailscale の IPv4 アドレス。tailscale が無い・未接続・Windows 側なら None。"""
    found = find_tailscale()
    if not found or found[1] != "native":
        return None
    try:
        out = _ts(["ip", "-4"], found[0], timeout=3).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    for line in out.splitlines():
        try:
            ip = ipaddress.ip_address(line.strip())
        except ValueError:
            continue
        if ip.version == 4 and ip in TAILSCALE_NET:
            return str(ip)
    return None


class WindowsServeRelay:
    """WSL から Windows の `tailscale serve` に中継を張る。自分が張った中継だけを外す。"""

    def __init__(self, exe: str, port: int):
        self.exe, self.port = exe, port
        self.url: str | None = None
        self.active = False

    def _dns_name(self) -> str | None:
        try:
            d = json.loads(_ts(["status", "--json"], self.exe).stdout)
            return (d.get("Self") or {}).get("DNSName", "").rstrip(".") or None
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
            return None

    def _port_in_use(self) -> bool:
        try:
            d = json.loads(_ts(["serve", "status", "--json"], self.exe).stdout or "{}")
        except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
            return False
        return str(self.port) in json.dumps(d.get("TCP") or {}) or f":{self.port}" in json.dumps(d.get("Web") or {})

    def start(self) -> str | None:
        """中継を張って URL を返す。張れなければ理由を出して None。"""
        name = self._dns_name()
        if not name:
            print("  （Tailscale に接続していないので、tailnet には公開しません）", file=sys.stderr)
            return None
        if self._port_in_use():
            print(f"  （Tailscale のポート {self.port} はすでに別の serve で使われているので、中継しません）", file=sys.stderr)
            return None
        r = _ts(["serve", "--bg", "--yes", "--http", str(self.port), f"http://127.0.0.1:{self.port}"], self.exe)
        if r.returncode != 0:
            print(f"  （tailscale serve に失敗しました: {(r.stderr or r.stdout).strip()[:200]}）", file=sys.stderr)
            return None
        self.active = True
        self.url = f"http://{name}:{self.port}"
        return self.url

    def stop(self) -> None:
        if self.active:
            _ts(["serve", "--http", str(self.port), "off"], self.exe)
            self.active = False


def bind_hosts(extra: list[str] | None = None, use_tailscale: bool = True) -> list[str]:
    hosts = ["127.0.0.1"]
    ts = tailscale_ipv4() if use_tailscale else None
    if ts:
        hosts.append(ts)
    for h in extra or []:
        if h in FORBIDDEN_HOSTS:
            raise ValueError(f"{h!r} では待ち受けません（全インターフェースに公開されるため）。Tailscale のアドレスを指定してください")
        if h not in hosts:
            hosts.append(h)
    return hosts


# ---------------------------------------------------------------------- 会議の管理


class Hub:
    """今開いている会議（最大1つ）と、画面から会議を始めるための設定ファイルの一覧。"""

    def __init__(self, configs: list[Path] | None = None, extra_dirs: list[Path] | None = None,
                 config_dirs: list[Path] | None = None):
        self.explicit = [Path(c).resolve() for c in (configs or [])]
        self.extra_dirs = [Path(d).resolve() for d in (extra_dirs or [])]
        # 画面で選べる設定ファイルを探す場所（直下の *.yaml / *.yml だけ。サブフォルダは見ない）
        dirs = [Path(d).resolve() for d in (config_dirs or [])] + [c.parent for c in self.explicit]
        self.config_dirs = list(dict.fromkeys(dirs))
        self._cfg_cache: dict[Path, tuple[float, bool]] = {}
        self.session: Session | None = None
        self.task: asyncio.Task | None = None
        self.watch_pref: Path | None = None  # 見ている「別のプロセスの会議」
        self.dismissed: set[str] = set()      # 見るのをやめた会議

    def _is_config(self, p: Path) -> bool:
        """参加者の定義、または議題（meeting）の定義がある YAML を会議の設定ファイルとみなす（更新日時でキャッシュ）。"""
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return False
        hit = self._cfg_cache.get(p)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            from ..agenda import is_agenda

            ok = isinstance(data, dict) and (
                (isinstance(data.get("participants"), dict) and bool(data["participants"]))
                or (isinstance(data.get("meeting"), dict) and bool(data["meeting"]))
                or is_agenda(data)
            )
        except Exception:  # noqa: BLE001
            ok = False
        self._cfg_cache[p] = (mtime, ok)
        return ok

    @property
    def configs(self) -> list[Path]:
        """画面で選べる設定ファイル: -c で渡したもの＋探す場所にあるもの。"""
        out = list(self.explicit)
        for d in self.config_dirs:
            if not d.is_dir():
                continue
            for p in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
                p = p.resolve()
                if p.name.startswith(".") or p in out:
                    continue
                if self._is_config(p):
                    out.append(p)
        return out

    def config_by_path(self, path: str) -> Path:
        """画面から渡された設定ファイル（議題ファイルなら「ファイル#案件id」）を、選べるものか確かめて返す。"""
        from ..agenda import find, load, split_ref

        f, pid = split_ref(path)
        p = f.resolve()
        if p not in self.configs:
            raise HTTPException(400, "その設定ファイルは選べません")
        if pid is None:
            return p
        try:
            find(load(p), pid)
        except (KeyError, ValueError):
            raise HTTPException(400, f"案件 {pid} が議題ファイルにありません") from None
        return Path(f"{p}#{pid}")

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def attach(self, session: Session) -> None:
        self.session = session
        self.task = asyncio.create_task(session.engine.run())

    async def detach(self) -> None:
        """会議を閉じる。進行中なら止める（transcript に中断を記録するので、あとで再開できる）。"""
        if self.task and not self.task.done():
            self.task.cancel()
        if self.task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        if self.session:
            await self.session.close()
        self.session, self.task = None, None

    def watching(self) -> Path | None:
        """会議を開いていないときに、自動で表示する「別のプロセスで進行中の会議」。見るのをやめたものは除く。"""
        if self.session is not None:
            return None
        pref = self.watch_pref if self.watch_pref is not None and self.watch_pref.name not in self.dismissed else None
        if pref is not None and is_locked(pref):
            return pref
        live = []
        for base in self.meetings_dirs():
            if not base.is_dir():
                continue
            for d in base.iterdir():
                if (d / "transcript.jsonl").exists() and d.name not in self.dismissed and is_locked(d):
                    live.append(d)
        if live:
            self.watch_pref = max(live, key=lambda d: (d / "transcript.jsonl").stat().st_mtime)
            return self.watch_pref
        return pref  # 終わった会議は、閉じるまで表示したままにする

    def meetings_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        for c in self.configs:
            with contextlib.suppress(Exception):
                d = Path(load_config(c).meetings_dir)
                if d not in dirs:
                    dirs.append(d)
        if self.session and self.session.dir.parent not in dirs:
            dirs.append(self.session.dir.parent)
        for d in self.extra_dirs:
            if d not in dirs:
                dirs.append(d)
        return dirs

    def find_meeting(self, meeting_id: str) -> Path:
        """一覧に出る会議ディレクトリだけを受け付ける（任意のパスを開かせない）。"""
        for d in self.meetings_dirs():
            if d.is_dir() and meeting_id in {p.name for p in d.iterdir() if p.is_dir()}:
                return d / meeting_id
        raise HTTPException(404, "会議が見つかりません")


def meeting_summary(d: Path) -> dict[str, Any]:
    """会議ディレクトリの状態（一覧と「未決事項から始める」用）。"""
    return archive.summarize(d)


# ---------------------------------------------------------------------- 表示用の変換


HIDDEN_EVENTS = {"stats"}
DIVIDER_PHASES = {"independent": "独立回答", "critique": "相互批判", "summary": "議長のまとめ"}


def _proposal_from_text(text: str) -> Proposal | None:
    from ..engine.flow import extract_json

    try:
        return Proposal.model_validate(extract_json(text))
    except Exception:  # noqa: BLE001
        return None


def record_to_client(r: dict[str, Any], names: dict[str, str], roles: dict[str, str] | None = None) -> dict[str, Any] | None:
    """transcript の1行を画面のログ1件にする（表示しないものは None）。

    kind: message（発言）/ summary（議長のまとめのカード）/ final（総括）/ divider（ラウンドの区切り）/ system / error
    """
    ev = r.get("event")
    if ev in HIDDEN_EVENTS:
        return None
    kind, speaker = r.get("kind"), r.get("speaker")
    item: dict[str, Any] = {
        "seq": r.get("seq"),
        "topic": r.get("topic"),
        "round": r.get("round"),
        "round_label": ROUND_LABEL.get(r.get("round") or "", r.get("round") or ""),
        "speaker": speaker,
        "name": names.get(speaker or "", speaker),
        "role": (roles or {}).get(speaker or "", ""),
        "kind": kind,
        "status": r.get("status"),
        "event": ev,
        "to": names.get(r["to"], r["to"]) if r.get("to") and r.get("to") != "user" else None,
        "elapsed_s": round((r.get("elapsed_ms") or 0) / 1000),
        "ts": r.get("ts"),
    }
    text = r.get("text") or ""
    if kind == "message" and r.get("round") == "plan":
        return None  # 席決めの JSON は見せない（結果は「議長が席を決めました」の行で出る）
    if ev == "checkpoint":
        st = r.get("state") or {}
        phase = st.get("phase")
        if phase not in DIVIDER_PHASES or r.get("topic") is None:
            return None
        label = DIVIDER_PHASES[phase]
        if phase == "critique":
            label += f" {st.get('critique_done', 0) + 1}回目"
        item.update(kind="divider", html=esc_html(label))
        return item
    if kind == "message" and r.get("round") == "summary" and r.get("topic") is not None:
        # 議長のまとめ（JSON）は整形したカードにする。承認のボタンは画面側で付ける
        from .common import format_proposal_md

        p = _proposal_from_text(text)
        item["kind"] = "summary"
        item["html"] = render_md(format_proposal_md(p.model_dump()) if p else text)
    elif kind == "message" and r.get("round") == "summary":
        item["kind"] = "final"
        item["html"] = render_md(text)
    else:
        item["html"] = render_md(text)
    if ev == "topic_start":
        item["title"] = r.get("title")
    if ev == "approved":
        item["edited"] = "修正あり" in text
        item["auto"] = bool(r.get("auto"))
    return item


def esc_html(s: str) -> str:
    from html import escape

    return escape(s)


def proposal_view(p: dict[str, Any]) -> dict[str, Any]:
    pr = Proposal.model_validate(p)
    from .common import format_proposal_md

    return {"summary_html": render_md(pr.summary), "handover_html": render_md(pr.handover),
            "html": render_md(format_proposal_md(pr.model_dump())), **pr.model_dump()}


# ---------------------------------------------------------------------- リクエストの形


class TopicIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    premise: str = Field(default="", max_length=20000)


class StartIn(BaseModel):
    config: int = 0  # Hub.configs の番号（互換用）
    config_path: str | None = None  # 画面で選んだ議題設定ファイル（一覧に出たものだけ受け付ける）
    agents_config_path: str | None = None  # 議長・配役設定ファイル（別指定する場合）
    chair: str | None = None  # "neon" | "argon" | "opus"
    chair_effort: str | None = None  # "low" | "medium" | "high"
    name: str = Field(default="", max_length=200)
    purpose: str = Field(default="", max_length=20000)
    topics: list[TopicIn] = Field(default_factory=list, max_length=20)  # 空なら設定ファイルの議題を使う
    from_meeting: str | None = None  # この会議の議事録を前提資料にする（互換用。from_meetings を推奨）
    from_meetings: list[str] = Field(default_factory=list, max_length=20)  # 複数の会議から引き継ぐ
    include_minutes: bool = True


class TopicEditIn(BaseModel):
    config_path: str
    action: str  # add / update / remove
    title: str
    new_title: str | None = None
    premise: str | None = None
    group: str | None = None


class ExtendIn(BaseModel):
    topic: int
    note: str = ""


class CommandIn(BaseModel):
    line: str = Field(max_length=20000)


class DraftIn(BaseModel):
    text: str = Field(max_length=200000)


# ---------------------------------------------------------------------- アプリ


def create_app(token: str, *, session: Session | None = None, hub: Hub | None = None) -> FastAPI:
    hub = hub or Hub()
    if session is not None and hub.session is None:
        hub.session = session  # テストや dm run --ui web 用（エンジンの起動は呼び出し側）
    app = FastAPI(title="Dartmouth", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub = hub
    # 画面の自動再読み込み用（dm web --dev）。boot はプロセスごと、asset_version は画面のファイルが変わるたびに変わる
    app.state.boot = secrets.token_hex(4)
    app.state.asset_version = 0
    app.state.dev = False

    def _versions() -> dict[str, Any]:
        return {"boot": app.state.boot, "asset_version": app.state.asset_version, "dev": app.state.dev}

    def valid(t: str | None) -> bool:
        return bool(t) and secrets.compare_digest(t, token)  # type: ignore[arg-type]

    def require_auth(request: Request) -> None:
        if valid(request.cookies.get(COOKIE)) or valid(request.query_params.get("token")):
            return
        raise HTTPException(status_code=401, detail="トークンが必要です（起動時に表示されたURLで開いてください）")

    def current() -> Session:
        if hub.session is None:
            raise HTTPException(409, "開いている会議がありません")
        return hub.session

    auth = [Depends(require_auth)]

    def _label(c: Path) -> str:
        try:
            return str(c.relative_to(Path.cwd().resolve()))
        except ValueError:
            return str(c)

    def _done_infos(done) -> list[dict[str, Any]]:
        return [{"title": d.title, "premise": str((d.model_extra or {}).get("premise") or ""), "meeting": d.meeting or "",
                 "done_at": d.done_at or ""} for d in done]

    def _config_infos() -> list[dict[str, Any]]:
        import yaml
        from ..config import MeetingConfig

        from ..agenda import is_agenda, normalize

        out = []
        for i, c in enumerate(hub.configs):
            try:
                raw_ag = yaml.safe_load(c.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                raw_ag = None
            if is_agenda(raw_ag):
                # 議題ファイルは案件ごとに1項目にする（画面ではテーマ → 案件のプルダウン）
                ag = normalize(raw_ag)
                for th in ag["themes"]:
                    for p in th["projects"]:
                        out.append({"index": f"{i}#{p['id']}", "name": c.name, "label": p["name"], "path": f"{c}#{p['id']}",
                                    "agenda": str(c), "theme": th["name"], "project": p["id"], "description": p.get("description", ""),
                                    "meeting_name": p["name"], "purpose": p.get("purpose", ""),
                                    "topics": [t if isinstance(t, dict) else {"title": t} for t in p["topics"]],
                                    "done_topics": [{"title": d.get("title", ""), "premise": str(d.get("premise") or ""),
                                                     "meeting": str(d.get("meeting") or ""), "done_at": str(d.get("done_at") or "")}
                                                    for d in p["done_topics"]],
                                    "done_count": len(p["done_topics"]), "topic_only": True, "participants": [],
                                    "has_agents": False, "chair": None, "backends": []})
                for t in (o for o in out if o.get("agenda") == str(c)):
                    t["topics"] = [{"title": str(x.get("title", "")), "premise": str(x.get("premise") or ""),
                                    **({"group": str(x["group"])} if x.get("group") else {})} for x in t["topics"]]
                continue
            info: dict[str, Any] = {"index": i, "name": c.name, "label": _label(c), "path": str(c),
                                    "meeting_name": "", "purpose": "", "topics": [], "participants": [],
                                    "has_agents": False, "topic_only": False, "chair": None, "backends": []}
            try:
                cfg = load_config(c)
                info["done_count"] = len(cfg.meeting.done_topics)
                info["done_topics"] = _done_infos(cfg.meeting.done_topics)
                ch = cfg.participants.get(cfg.chair)
                chair_info = {"id": cfg.chair, "display_name": (ch.display_name or cfg.chair.capitalize()) if ch else cfg.chair,
                              "model": ch.options().get("model") if ch else None}
                backends = [k for k, b in cfg.seating.backends.items() if b.enabled] if cfg.seating else []
                info.update(meeting_name=cfg.meeting.name, purpose=cfg.meeting.purpose,
                            topics=[{"title": t.title, "premise": t.premise, **({"group": t.group} if t.group else {})} for t in cfg.meeting.topics],
                            participants=[{"id": n, "display_name": p.display_name or n.capitalize(), "role": p.role,
                                           "model": p.options().get("model")} for n, p in cfg.participants.items() if p.enabled],
                            has_agents=True, chair=chair_info, backends=backends)
            except Exception as e:  # noqa: BLE001
                try:
                    raw = yaml.safe_load(c.read_text(encoding="utf-8")) or {}
                    if isinstance(raw, dict) and "participants" not in raw and "extends" not in raw:
                        mc = MeetingConfig.model_validate(raw.get("meeting") or {})
                        info["done_count"] = len(mc.done_topics)
                        info["done_topics"] = _done_infos(mc.done_topics)
                        info.update(meeting_name=mc.name, purpose=mc.purpose,
                                    topics=[{"title": t.title, "premise": t.premise, **({"group": t.group} if t.group else {})} for t in mc.topics],
                                    topic_only=True)
                    else:
                        info["error"] = str(e)
                except Exception:  # noqa: BLE001
                    info["error"] = str(e)
            out.append(info)
        return out

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        t = request.query_params.get("token")
        if valid(t):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
            return resp
        if not valid(request.cookies.get(COOKIE)):
            return HTMLResponse(UNAUTHORIZED_HTML, status_code=401)
        html = assets.index_html().encode("utf-8")
        if "gzip" in request.headers.get("accept-encoding", ""):
            import gzip

            return Response(gzip.compress(html, 6), media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache", "Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    @app.get("/static/{name:path}", dependencies=auth)
    async def static_file(name: str, request: Request):
        """画面の CSS / JS。URL に中身のハッシュ（?v=）が付くので長くキャッシュさせ、gzip で送る。"""
        a = assets.get(name)
        if a is None:
            raise HTTPException(404)
        if request.headers.get("if-none-match") == a.etag:
            return Response(status_code=304, headers={"ETag": a.etag})
        headers = {"ETag": a.etag, "Vary": "Accept-Encoding",
                   "Cache-Control": "public, max-age=31536000, immutable" if request.query_params.get("v") == a.hash else "no-cache"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(a.gz, media_type=a.media_type, headers={**headers, "Content-Encoding": "gzip"})
        return Response(a.raw, media_type=a.media_type, headers=headers)

    @app.get("/api/state", dependencies=auth)
    async def state() -> JSONResponse:
        s = hub.session
        if s is None:
            w = await asyncio.to_thread(hub.watching)
            if w is not None:
                st = watch_state(w)
                st.update(_versions())
                return JSONResponse(st)
            return JSONResponse({"mode": "home", "configs": await asyncio.to_thread(_config_infos), **_versions()})
        st: dict[str, Any] = live_state(s)
        st["mode"] = "meeting"
        st["meeting"] = {"name": s.cfg.meeting.name, "id": s.meeting_id, "running": hub.running}
        st.update(_versions())
        return JSONResponse(st)

    # 後方互換（W-1 のテストとステータス表示）
    @app.get("/api/status", dependencies=auth)
    async def status() -> JSONResponse:
        s = current()
        st: dict[str, Any] = s.engine.status()
        st["meeting"] = {"name": s.cfg.meeting.name, "id": s.meeting_id}
        return JSONResponse(st)

    # ---- 会議中

    @app.get("/api/events", dependencies=auth)
    async def events(request: Request) -> StreamingResponse:
        if hub.session is None and (w := hub.watching()) is not None:
            return StreamingResponse(_watch_stream(request, hub, w), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        s = current()
        return StreamingResponse(_event_stream(request, hub, s), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/minutes", dependencies=auth)
    async def minutes() -> JSONResponse:
        if hub.session is None and (w := hub.watching()) is not None:
            p = w / "minutes.md"
            return JSONResponse({"html": render_md(p.read_text(encoding="utf-8")) if p.exists() else ""})
        s = current()
        return JSONResponse({"html": render_md(s.engine.minutes.render())})

    @app.get("/api/proposal", dependencies=auth)
    async def proposal() -> JSONResponse:
        s = current()
        p = s.engine.current_proposal()
        if p is None:
            return JSONResponse({"proposal": None})
        cur = s.engine.cur
        return JSONResponse({"proposal": proposal_view(p), "topic": cur.no if cur else None, "title": cur.title if cur else None})

    @app.post("/api/command", dependencies=auth)
    async def command(body: CommandIn) -> JSONResponse:
        s = current()
        if not hub.running:
            raise HTTPException(409, "会議は終わっています")
        res = commands.handle(s.engine, body.line)
        if res.ui_action == "quit":
            await hub.detach()
            return JSONResponse({"message": "会議を閉じました（再開できます）", "action": "home"})
        return JSONResponse({"message": res.message, "level": res.level, "action": res.ui_action})

    @app.get("/api/draft", dependencies=auth)
    async def get_draft() -> JSONResponse:
        s = current()
        p = s.engine.current_proposal()
        if p is None or s.engine.cur is None:
            raise HTTPException(409, "承認待ちのまとめはありません")
        return JSONResponse({"text": load_draft(s.dir, s.engine.cur.no, p)})

    @app.put("/api/draft", dependencies=auth)
    async def put_draft(body: DraftIn) -> JSONResponse:
        s = current()
        if s.engine.cur is None or s.engine.current_proposal() is None:
            raise HTTPException(409, "承認待ちのまとめはありません")
        save_draft(s.dir, s.engine.cur.no, body.text)
        return JSONResponse({"ok": True})

    @app.post("/api/approve_edit", dependencies=auth)
    async def approve_edit(body: DraftIn) -> JSONResponse:
        s = current()
        cur = s.engine.cur
        if cur is None or s.engine.current_proposal() is None:
            raise HTTPException(409, "承認待ちのまとめはありません")
        try:
            edited = yaml_to_proposal(body.text)
        except Exception as e:  # noqa: BLE001
            save_draft(s.dir, cur.no, body.text)
            raise HTTPException(422, f"修正内容を読めませんでした（書きかけは保存済み）: {e}") from e
        if edited is None:
            raise HTTPException(422, "中身が空です")
        msg = s.engine.approve(edited)
        clear_draft(s.dir, cur.no)
        return JSONResponse({"message": msg})

    @app.post("/api/close", dependencies=auth)
    async def close() -> JSONResponse:
        if hub.session is None and (w := hub.watching()) is not None:
            hub.dismissed.add(w.name)  # 見るのをやめる（CLI 側の会議はそのまま進む）
            return JSONResponse({"ok": True})
        """会議を閉じてホームに戻る。進行中なら中断として記録する（あとで再開できる）。"""
        await hub.detach()
        return JSONResponse({"ok": True})

    # ---- ホーム（会議を持たない起動のとき）

    @app.get("/api/meetings", dependencies=auth)
    async def meetings() -> JSONResponse:
        return JSONResponse({"meetings": await asyncio.to_thread(_meetings)})

    def _meetings() -> list[dict[str, Any]]:
        out = []
        for d in hub.meetings_dirs():
            if not d.is_dir():
                continue
            for p in d.iterdir():
                if (p / "transcript.jsonl").exists():
                    with contextlib.suppress(Exception):
                        sm = meeting_summary(p)
                        out.append({k: sm[k] for k in ("id", "name", "state", "phase_label", "resumable", "running", "updated", "archived")}
                                   | {"open_count": len(sm["open_questions"]), "todo_count": len(sm["todos"])})
        out.sort(key=lambda x: x["updated"] or "", reverse=True)
        return out

    @app.get("/api/meetings/{meeting_id}", dependencies=auth)
    async def meeting_detail(meeting_id: str) -> JSONResponse:
        sm = meeting_summary(hub.find_meeting(meeting_id))
        sm["final_summary_html"] = render_md(sm["final_summary"])
        return JSONResponse(sm)

    @app.post("/api/start", dependencies=auth)
    async def start(body: StartIn) -> JSONResponse:
        if hub.running:
            raise HTTPException(409, "別の会議が進行中です")
        if body.config_path:
            cfg_path = hub.config_by_path(body.config_path)
        else:
            configs = hub.configs
            if not (0 <= body.config < len(configs)):
                raise HTTPException(400, "設定ファイルを選んでください")
            cfg_path = configs[body.config]
        agents_path = hub.config_by_path(body.agents_config_path) if body.agents_config_path else None
        from ..agenda import split_ref

        cfg_file = split_ref(cfg_path)[0]
        if agents_path is None:
            import yaml

            raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict) and "participants" not in raw and "extends" not in raw:
                for cand in (cfg_file.parent / "agents.yaml", Path("agents.yaml")):
                    if cand.exists():
                        agents_path = cand
                        break
                else:
                    ag = next((c for c in hub.configs if c.name == "agents.yaml"), None)
                    if ag:
                        agents_path = ag
        try:
            cfg = load_config(cfg_path, agents_path=agents_path)
            apply_chair_override(cfg, body.chair, body.chair_effort)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"{cfg_file.name} を読めません: {e}") from e
        if body.name.strip():
            cfg.meeting.name = body.name.strip()
            cfg.meeting.slug = None
        if body.purpose.strip():
            cfg.meeting.purpose = body.purpose.strip()
        if body.topics:
            cfg.meeting.topics = [TopicConfig(title=t.title.strip(), premise=t.premise.strip()) for t in body.topics]
        elif not cfg.meeting.topics:
            raise HTTPException(422, "議題がありません。議題を入力するか、議題のある設定ファイルを選んでください")
        sources = list(dict.fromkeys(([body.from_meeting] if body.from_meeting else []) + body.from_meetings))
        if body.include_minutes:
            for mid in sources:
                prev = meeting_summary(hub.find_meeting(mid))
                text = prev["minutes_compact"]
                if prev["final_summary"]:
                    text += "\n\n## 前回の総括\n" + prev["final_summary"]
                cfg.meeting.context.append(ContextItem(text=text, label=f"前回の会議（{prev['id']}）の議事録"))
        await hub.detach()
        # 資料庫の準備（初回は埋め込み）に時間がかかることがあるので、イベントループを止めない
        sess = await asyncio.to_thread(lambda: create_session(cfg, source_config=cfg_path))
        hub.attach(sess)
        return JSONResponse({"ok": True, "id": sess.meeting_id})

    @app.post("/api/watch/{meeting_id}", dependencies=auth)
    async def watch(meeting_id: str) -> JSONResponse:
        if hub.session is not None:
            raise HTTPException(409, "会議を開いています。閉じてから見てください")
        d = hub.find_meeting(meeting_id)
        hub.dismissed.discard(d.name)
        hub.watch_pref = d
        return JSONResponse({"ok": True})

    @app.post("/api/resume/{meeting_id}", dependencies=auth)
    async def resume(meeting_id: str) -> JSONResponse:
        if hub.running:
            raise HTTPException(409, "別の会議が進行中です")
        d = hub.find_meeting(meeting_id)
        sm = meeting_summary(d)
        if sm.get("running"):
            raise HTTPException(409, "この会議は別のプロセス（dm run など）で進行中です。終わるまで待つか、過去の会議の画面で記録を見てください")
        if not sm["resumable"]:
            raise HTTPException(409, "この会議は終わっています")
        from ..storage.lock import MeetingLocked

        await hub.detach()
        try:
            hub.attach(await asyncio.to_thread(resume_session, d))
        except MeetingLocked as e:
            raise HTTPException(409, str(e)) from None
        return JSONResponse({"ok": True, "id": meeting_id})

    @app.post("/api/config/topics", dependencies=auth)
    async def edit_config_topic(body: TopicEditIn) -> JSONResponse:
        """画面から設定ファイルの議題を足す・直す・消す（書き換える前の版は .bak に残す）。"""
        from ..topics_file import edit_topic

        p = hub.config_by_path(body.config_path)
        try:
            await asyncio.to_thread(edit_topic, p, body.action, body.title, new_title=body.new_title,
                                    premise=body.premise, group=body.group)
        except KeyError:
            raise HTTPException(404, f"「{body.title}」が設定ファイルにありません（ほかで書き換えられた可能性があります）") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        hub._cfg_cache.pop(p, None)
        return JSONResponse({"ok": True})

    @app.post("/api/extend/{meeting_id}", dependencies=auth)
    async def extend(meeting_id: str, body: ExtendIn) -> JSONResponse:
        """終わった会議を開き直し、議題の延長を1本足して回す。"""
        d = hub.find_meeting(meeting_id)
        if hub.running:
            raise HTTPException(409, "別の会議が進行中です")
        if hub.session is not None and hub.session.dir == d:
            await hub.detach()  # 終わったばかりで画面に開いている会議（ロックを持っている）を閉じる
        sm = meeting_summary(d)
        if sm.get("running"):
            raise HTTPException(409, "この会議は別のプロセスで進行中です")
        if not sm.get("extendable"):
            raise HTTPException(409, "終わった会議だけ延長できます（中断中なら再開してください）")
        from ..session import extend_session
        from ..storage.lock import MeetingLocked

        await hub.detach()
        try:
            sess = await asyncio.to_thread(extend_session, d, body.topic, body.note)
        except MeetingLocked as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        hub.attach(sess)
        return JSONResponse({"ok": True, "id": meeting_id, "topic": len(sess.engine.topics)})

    from .web_admin import register_admin

    register_admin(app, hub, auth)
    return app


async def _event_stream(request: Request, hub: Hub, s: Session) -> AsyncIterator[bytes]:
    """SSE。まず transcript の既存分を送り、そのあと新しいイベントを流す（再読み込み・再接続で抜けも重複もしない）。"""
    q = s.engine.bus.subscribe()  # 既存分を読む前に購読して、隙間を作らない
    names = s.engine.names
    roles = {n: p.role for n, p in s.cfg.participants.items()}
    try:
        last = 0
        for r in read_transcript(s.dir / "transcript.jsonl"):
            last = max(last, r.get("seq") or 0)
            item = record_to_client(r, names, roles)
            if item:
                yield _sse("msg", item)
        yield _sse("ready", {"seq": last})
        while True:
            if await request.is_disconnected() or hub.session is not s:
                return
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield b": keepalive\n\n"
                continue
            if ev.type == E.RECORD:
                if (ev.data.get("seq") or 0) <= last:
                    continue
                item = record_to_client(ev.data, names, roles)
                if item:
                    yield _sse("msg", item)
            elif ev.type == E.MINUTES:
                yield _sse("minutes", {"html": render_md(ev.data["markdown"])})
            elif ev.type == E.AWAITING_APPROVAL:
                yield _sse("approval", {"topic": ev.data["topic"], "title": ev.data["title"], "proposal": proposal_view(ev.data["proposal"])})
            elif ev.type == E.SPEECH_START:
                yield _sse("speaking", {"participant": ev.data["participant"], "round": ev.data["round"]})
            elif ev.type == E.NOTICE:
                yield _sse("notice", {"text": ev.data["text"], "level": ev.data.get("level", "info")})
            elif ev.type == E.PAUSED:
                yield _sse("paused", {"participant": names.get(ev.data["participant"], ev.data["participant"]), "reason": ev.data["reason"]})
            elif ev.type == E.MEETING_END:
                yield _sse("end", {"reason": ev.data["reason"]})
    finally:
        s.engine.bus.unsubscribe(q)


def watch_state(d: Path) -> dict[str, Any]:
    """CLI など別のプロセスで進んでいる会議の状態（status.json と transcript から作る。操作はできない）。"""
    import time

    p = d / STATUS
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {"phase": "", "phase_label": "", "topic_no": None, "topic_title": None, "topics_total": 0, "critique_done": 0,
              "critique_rounds": 0, "inflight": {}, "calls_left": 0, "rounds_left": 0, "absent": [], "waiting": None,
              "paused": None, "participants": {}}
        # status.json を書かない古いプロセス: 設定と transcript から、進行・席・予算をおおまかに出す
        try:
            c = load_config(d / "config.yaml")
            st.update(topics_total=len(c.meeting.topics), max_calls=c.budget.max_calls, critique_rounds=c.meeting.critique_rounds,
                      participants={n: p.display_name or n.capitalize() for n, p in c.participants.items()})
        except Exception:  # noqa: BLE001
            c = None
        calls = 0
        for r in read_transcript(d / "transcript.jsonl"):
            calls += 1 if r.get("call") else 0
            if r.get("event") == "seating":
                st["participants"].update({x["persona"]: x.get("name", x["persona"]) for x in r.get("seats", [])})
                st["members"] = ["chair"] + [x["persona"] for x in r.get("seats", [])] if "chair" in st["participants"] else [x["persona"] for x in r.get("seats", [])]
            if r.get("event") == "checkpoint":
                cs = r.get("state") or {}
                st.update(phase=cs.get("phase", ""), topic_no=r.get("topic"), critique_done=cs.get("critique_done", 0))
            elif r.get("event") == "topic_start":
                st["topic_title"] = r.get("title")
        from ..engine.flow import PHASE_LABEL, Phase

        if c is not None:
            st["calls_left"] = max(0, c.budget.max_calls - calls)
        try:
            st["phase_label"] = PHASE_LABEL[Phase(st["phase"])]
        except (ValueError, KeyError):
            pass
    now = time.time()
    st["inflight"] = {n: {**v, "elapsed_s": round(now - st.get("inflight_since", {}).get(n, now), 1)} for n, v in st.get("inflight", {}).items()}
    running = is_locked(d)
    if not running:
        st["inflight"] = {}
    st["waiting"] = None  # 承認などの操作は CLI 側で行う
    st["mode"] = "meeting"
    st["readonly"] = True
    st["meeting"] = {"name": st.get("meeting_name") or d.name, "id": d.name, "running": running}
    return st


async def _watch_stream(request: Request, hub: Hub, d: Path) -> AsyncIterator[bytes]:
    """別のプロセスが書いている transcript.jsonl を追いかけて流す（tail -f）。"""
    cfg_names: dict[str, str] = {}
    roles: dict[str, str] = {}
    try:
        c = load_config(d / "config.yaml")
        cfg_names = {n: p.display_name or n.capitalize() for n, p in c.participants.items()}
        roles = {n: p.role for n, p in c.participants.items()}
    except Exception:  # noqa: BLE001
        pass
    tp = d / "transcript.jsonl"
    pos, buf, last_min = 0, b"", 0.0
    first = True
    while True:
        if await request.is_disconnected() or hub.session is not None or hub.watching() != d:
            return
        names = dict(cfg_names)
        try:
            names.update((json.loads((d / STATUS).read_text(encoding="utf-8")).get("participants") or {}))
        except (OSError, ValueError):
            pass
        with open(tp, "rb") as fh:
            fh.seek(pos)
            chunk = fh.read()
        pos += len(chunk)
        buf += chunk
        *lines, buf = buf.split(b"\n")  # 書きかけの最後の行は次に回す
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            item = record_to_client(r, names, roles)
            if item:
                yield _sse("msg", item)
        if first:
            yield _sse("ready", {"seq": 0})
            first = False
        mp = d / "minutes.md"
        if mp.exists() and mp.stat().st_mtime > last_min:
            last_min = mp.stat().st_mtime
            yield _sse("minutes", {"html": render_md(mp.read_text(encoding="utf-8"))})
        if not is_locked(d):
            yield _sse("end", {"reason": "done"})
            return
        await asyncio.sleep(0.7)


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


# ---------------------------------------------------------------------- 起動


class _Server(uvicorn.Server):
    """シグナルは run_server 側でまとめて扱う（サーバーを2つ動かすため、uvicorn に横取りさせない）。"""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield

    def install_signal_handlers(self) -> None:  # 古い uvicorn 向け
        pass


async def run_server(hub: Hub, *, port: int = DEFAULT_PORT, extra_hosts: list[str] | None = None,
                     use_tailscale: bool = True, dev: bool = False) -> int:
    from . import devreload

    hosts = bind_hosts(extra_hosts, use_tailscale)
    relay: WindowsServeRelay | None = None
    found = find_tailscale() if use_tailscale else None
    if found and found[1] == "windows":
        relay = WindowsServeRelay(found[0], port)
    # --dev で再起動したときは、トークンを引き継ぐ（ブラウザで開き直さなくてよい）
    token = (os.environ.get(devreload.ENV_TOKEN) if dev else None) or secrets.token_urlsafe(24)
    app = create_app(token, hub=hub)
    app.state.dev = dev
    resume_dir = os.environ.get(devreload.ENV_RESUME) if dev else None
    if resume_dir and hub.session is None and Path(resume_dir, "transcript.jsonl").exists():
        try:
            hub.attach(resume_session(resume_dir))
            print(f"再起動前の会議を再開しました: {resume_dir}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001  別のプロセスが進めている、など
            print(f"再起動前の会議は再開しませんでした: {e}", file=sys.stderr)
    servers = [_Server(uvicorn.Config(app, host=h, port=port, log_level="warning", lifespan="off",
                                      timeout_graceful_shutdown=2)) for h in hosts]
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    serve_tasks = [asyncio.create_task(s.serve()) for s in servers]
    if hub.session:
        print(f"会議ディレクトリ: {hub.session.dir}", file=sys.stderr)
    for h in hosts:
        print(f"  http://{h}:{port}/?token={token}", file=sys.stderr)
    relay_url = None
    if relay and dev and os.environ.get(devreload.ENV_RELAY) == "1":
        # 再起動前に張った中継をそのまま使う
        relay.active = True
        name = await asyncio.to_thread(relay._dns_name)
        relay_url = relay.url = f"http://{name}:{port}" if name else None
    elif relay:
        relay_url = await asyncio.to_thread(relay.start)
    if relay_url:
        print(f"  {relay_url}/?token={token}   ← tailnet の中（スマホなど）から", file=sys.stderr)
    elif len(hosts) == 1:
        print("  （Tailscale のアドレスが見つからないので、この PC の中からだけ開けます）", file=sys.stderr)
    print("Ctrl+C で終了（進行中の会議は中断として記録され、あとで再開できます）", file=sys.stderr)
    restart = False
    watcher: asyncio.Task | None = None
    if dev:
        print("開発モード: 画面のファイルを保存するとブラウザが再読み込みし、Python を保存するとサーバーが再起動します", file=sys.stderr)

        async def watch() -> None:
            nonlocal restart
            static = devreload.snapshot(devreload.STATIC, ("*",))
            py = devreload.snapshot(devreload.PACKAGE, ("*.py",))
            pending: list[Path] = []
            waiting_noted = False
            while True:
                await asyncio.sleep(0.5)
                s2 = devreload.snapshot(devreload.STATIC, ("*",))
                if devreload.changed(static, s2):
                    static = s2
                    app.state.asset_version += 1
                    print("画面のファイルが変わりました → ブラウザを再読み込みします", file=sys.stderr)
                p2 = devreload.snapshot(devreload.PACKAGE, ("*.py",))
                diff = devreload.changed(py, p2)
                if diff:
                    py = p2
                    pending += diff
                if pending:
                    if devreload.restart_allowed(hub):
                        print(f"Python のコードが変わりました（{', '.join(p.name for p in pending[:3])}）→ 再起動します", file=sys.stderr)
                        restart = True
                        stop.set()
                        return
                    if not waiting_noted:
                        print("Python のコードが変わりました。発言が終わって手が空いたら再起動します", file=sys.stderr)
                        waiting_noted = True

        watcher = asyncio.create_task(watch())
    rc = 0
    try:
        await stop.wait()
    finally:
        if watcher:
            watcher.cancel()
        if hub.task and hub.task.done() and not hub.task.cancelled() and hub.task.exception():
            print(f"内部エラーで停止しました: {hub.task.exception()!r}", file=sys.stderr)
            rc = 1
        last_dir = hub.session.dir if hub.session else None
        reopen = last_dir if (restart and hub.session is not None and hub.running) else None
        await hub.detach()
        if relay and not restart:
            await asyncio.to_thread(relay.stop)
        for s in servers:
            s.should_exit = True
        await asyncio.gather(*serve_tasks, return_exceptions=True)
        if restart:
            devreload.exec_restart(token, reopen, bool(relay and relay.active))
        if last_dir:
            print(f"記録: {last_dir}\n再開: dm resume {last_dir} --ui web", file=sys.stderr)
    return rc


async def run_web(session: Session, *, port: int = DEFAULT_PORT, extra_hosts: list[str] | None = None,
                  use_tailscale: bool = True) -> int:
    """1つの会議を開いた状態で起動する（dm run --ui web）。"""
    hub = Hub()
    hub.attach(session)
    return await run_server(hub, port=port, extra_hosts=extra_hosts, use_tailscale=use_tailscale)


UNAUTHORIZED_HTML = """<!doctype html><html lang="ja"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dartmouth</title>
<body style="font-family:system-ui;margin:2rem"><h1>Dartmouth</h1>
<p>起動時に表示されたトークン付きのURLで開いてください。</p></body></html>"""
