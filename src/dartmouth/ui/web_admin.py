"""Web UI の管理画面の API: 過去の会議を読む・検索・ダウンロード、決定事項／未決事項／ToDo の横断管理、
使用量、設定ファイルとプロンプトの編集。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..adapters import REGISTRY
from ..config import load_config
from ..engine.context import default_prompts_dir
from ..storage import archive

DOWNLOADS = {
    "minutes": ("minutes.md", "text/markdown; charset=utf-8"),
    "draft": ("requirements_draft.md", "text/markdown; charset=utf-8"),
    "transcript": ("transcript.jsonl", "application/x-ndjson; charset=utf-8"),
}
CLAUDE_MODELS = ["opus", "sonnet", "haiku", "fable"]


class ItemStatusIn(BaseModel):
    meeting: str = Field(max_length=300)
    id: str = Field(pattern=r"^[DQT]-\d{3,}$")
    done: bool
    note: str = Field(default="", max_length=2000)


class ArchiveIn(BaseModel):
    archived: bool
    reason: str = Field(default="", max_length=500)


class FileIn(BaseModel):
    path: str = Field(max_length=4000)
    text: str = Field(max_length=500_000)


def _prompts_dir_for(cfg_path: Path) -> Path:
    try:
        cfg = load_config(cfg_path)
        if cfg.prompts_dir:
            return Path(cfg.prompts_dir)
    except Exception:  # noqa: BLE001
        pass
    return default_prompts_dir()


def _collect_extends(path: Path, out: list[Path], seen: set[Path]) -> None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return
    ext = data.get("extends") if isinstance(data, dict) else None
    if not ext:
        return
    for e in ([ext] if isinstance(ext, str) else list(ext)):
        ep = Path(e).expanduser()
        if not ep.is_absolute():
            ep = (path.parent / ep).resolve()
        if ep.exists() and ep not in seen:
            seen.add(ep)
            out.append(ep)
            _collect_extends(ep, out, seen)


def editable_files(hub) -> list[dict[str, Any]]:
    """画面から編集してよいファイル。dm web に渡した設定ファイル（とその extends 先）と、そのプロンプト（*.md）だけ。"""
    out: list[dict[str, Any]] = []
    seen: set[Path] = set()
    ext_configs: list[Path] = []
    for c in hub.configs:
        if c.exists() and c not in seen:
            seen.add(c)
            out.append({"path": str(c), "name": c.name, "kind": "config", "group": "設定ファイル"})
            _collect_extends(c, ext_configs, seen)
    for ec in ext_configs:
        out.append({"path": str(ec), "name": ec.name, "kind": "config", "group": "設定ファイル"})
    for c in hub.configs:
        pd = _prompts_dir_for(c)
        if not pd.is_dir():
            continue
        for p in sorted(pd.rglob("*.md")):
            p = p.resolve()
            if p in seen:
                continue
            seen.add(p)
            rel = p.relative_to(pd.resolve())
            group = "ペルソナ" if rel.parts[0] == "personas" else ("ラウンドの指示" if rel.parts[0] == "rounds" else "役割")
            out.append({"path": str(p), "name": str(rel), "kind": "prompt", "group": group})
    return out


def _check_config_change(old_text: str, new_text: str) -> None:
    """画面からの編集で、任意のプログラムを動かせる変更を入れさせない。

    - 登録済みでないアダプタ（"モジュール:クラス" や "ファイル.py:クラス"）を新しく足すこと
    - 実行するコマンド（command）を変えること
    """
    try:
        new = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as e:
        raise HTTPException(422, f"YAML として読めません: {e}") from e
    old = yaml.safe_load(old_text) or {} if old_text else {}
    old_parts = old.get("participants") or {}
    old_adapters = {p.get("adapter") for p in old_parts.values() if isinstance(p, dict)}
    for name, p in (new.get("participants") or {}).items():
        if not isinstance(p, dict):
            continue
        a = p.get("adapter")
        if a not in REGISTRY and a not in old_adapters:
            raise HTTPException(422, f"参加者 {name}: 登録されていないアダプタ {a!r} は画面からは追加できません（ファイルを直接編集してください）")
        before = old_parts.get(name, {}) if isinstance(old_parts.get(name), dict) else {}
        if p.get("command") != before.get("command"):
            raise HTTPException(422, f"参加者 {name}: command は画面からは変更できません（ファイルを直接編集してください）")


_models_cache: dict[str, Any] = {}


def agy_models() -> list[dict[str, str]]:
    if _models_cache.get("t", 0) > time.time() - 600:
        return _models_cache["agy"]
    found = shutil.which("agy")
    models: list[dict[str, str]] = []
    if found:
        try:
            out = subprocess.run([found, "models"], capture_output=True, text=True, timeout=30, check=False).stdout
            for line in out.splitlines():
                if "\t" in line:
                    mid, label = line.split("\t", 1)
                    models.append({"id": mid.strip(), "label": label.strip()})
        except (OSError, subprocess.TimeoutExpired):
            pass
    _models_cache.update(t=time.time(), agy=models)
    return models


def register_admin(app: FastAPI, hub, auth) -> None:
    from .web import record_to_client, render_md  # 循環 import を避ける
    from .web_ledger import register_ledger

    register_ledger(app, hub, auth)

    def all_meeting_dirs() -> list[Path]:
        out = []
        for base in hub.meetings_dirs():
            if base.is_dir():
                out += [d for d in base.iterdir() if (d / "transcript.jsonl").exists()]
        return out

    # ---- 過去の会議を読む

    @app.get("/api/archive/{meeting_id}", dependencies=auth)
    async def archive_view(meeting_id: str) -> JSONResponse:
        d = hub.find_meeting(meeting_id)
        recs = archive.records(d)
        sm = archive.summarize(d, recs)
        info = archive.participants_info(d)
        names = {n: i["display_name"] for n, i in info.items()} | {"user": "ユーザー"}
        roles = {n: i["role"] for n, i in info.items()}
        msgs = [m for m in (record_to_client(r, names, roles) for r in recs) if m]
        draft = d / "requirements_draft.md"
        return JSONResponse({
            "summary": {k: v for k, v in sm.items() if k not in ("minutes_md", "minutes_compact")},
            "messages": msgs,
            "minutes_html": render_md(sm["minutes_md"]),
            "draft_html": render_md(draft.read_text(encoding="utf-8")) if draft.exists() else None,
            "usage": archive.usage(d, recs),
            "participants": info,
            "live": hub.session is not None and hub.session.dir == d,
        })

    @app.post("/api/archive/{meeting_id}/archived", dependencies=auth)
    async def set_archived(meeting_id: str, body: ArchiveIn) -> JSONResponse:
        d = hub.find_meeting(meeting_id)
        if hub.session is not None and hub.session.dir == d and hub.running:
            raise HTTPException(409, "進行中の会議はアーカイブできません")
        archive.set_archived(d, body.archived, body.reason)
        return JSONResponse({"ok": True, "archived": archive.is_archived(d)})

    @app.delete("/api/archive/{meeting_id}", dependencies=auth)
    async def delete_meeting(meeting_id: str) -> JSONResponse:
        import shutil

        d = hub.find_meeting(meeting_id)
        if (hub.session is not None and hub.session.dir == d and hub.running) or archive.summarize(d).get("running"):
            raise HTTPException(409, "進行中の会議は削除できません")
        if hub.session is not None and hub.session.dir == d:
            await hub.detach()
        shutil.rmtree(d)
        return JSONResponse({"ok": True, "deleted": meeting_id})

    @app.get("/api/archive/{meeting_id}/download/{kind}", dependencies=auth)
    async def download(meeting_id: str, kind: str):
        if kind not in DOWNLOADS:
            raise HTTPException(404, "ありません")
        d = hub.find_meeting(meeting_id)
        fname, mime = DOWNLOADS[kind]
        p = d / fname
        if kind == "minutes":
            # transcript から作り直した最新版を渡す
            return _text_download(archive.summarize(d)["minutes_md"], f"{meeting_id}-minutes.md", mime)
        if not p.exists():
            raise HTTPException(404, "まだ書き出されていません")
        return FileResponse(p, media_type=mime, filename=f"{meeting_id}-{fname}")

    @app.get("/api/search", dependencies=auth)
    async def search(q: str = "") -> JSONResponse:
        if len(q.strip()) < 2:
            return JSONResponse({"hits": [], "note": "2文字以上で検索してください"})
        return JSONResponse({"hits": await asyncio.to_thread(archive.search, hub.meetings_dirs(), q, 200)})

    # ---- 決定事項・未決事項・ToDo の横断管理

    @app.get("/api/items", dependencies=auth)
    async def items() -> JSONResponse:
        return JSONResponse({"items": await asyncio.to_thread(_items)})

    def _items() -> list[dict[str, Any]]:
        out = []
        for d in all_meeting_dirs():
            try:
                sm = archive.summarize(d)
            except Exception:  # noqa: BLE001
                continue
            topics = {t["no"]: t["title"] for t in sm["topics"]}
            archived = bool(sm.get("archived"))
            for kind, key in (("decision", "decisions"), ("question", "open_questions"), ("todo", "todos")):
                for it in sm[key]:
                    out.append({"kind": kind, "meeting": sm["id"], "meeting_name": sm["name"], "meeting_date": (sm["started"] or "")[:10],
                                "archived": archived,
                                "topic_title": topics.get(it.get("topic"), ""), "owner": it.get("owner"), **it})
        out.sort(key=lambda x: (x["meeting_date"], x["meeting"], x["id"]), reverse=True)
        return out

    @app.get("/api/export.md", dependencies=auth)
    async def export_md(download: bool = False) -> PlainTextResponse:
        """全会議・全アイテムを1つの Markdown に（dm export-md と同じ。読み取りだけ）。"""
        from datetime import datetime

        from ..export_md import export_markdown
        from ..paths import ledger_path

        text = await asyncio.to_thread(export_markdown, hub.meetings_dirs(), ledger_path())
        headers = {"Content-Disposition": f'attachment; filename="dartmouth-items-{datetime.now():%Y%m%d}.md"'} if download else {}
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)

    @app.post("/api/items/status", dependencies=auth)
    async def item_status(body: ItemStatusIn) -> JSONResponse:
        d = hub.find_meeting(body.meeting)
        valid = {x["id"] for k in ("decisions", "open_questions", "todos") for x in archive.summarize(d)[k]}
        if body.id not in valid:
            raise HTTPException(404, "その項目はありません")
        return JSONResponse(archive.save_item_status(d, body.id, body.done, body.note))

    # ---- 使用量

    @app.get("/api/usage", dependencies=auth)
    async def usage() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(_usage))

    def _usage() -> dict[str, Any]:
        meetings = []
        for d in all_meeting_dirs():
            try:
                u = archive.usage(d)
            except Exception:  # noqa: BLE001
                continue
            if u["totals"]["calls"]:
                meetings.append(u)
        meetings.sort(key=lambda m: m["started"] or "")
        # 参加者ごと（表示名とモデルの組）に合算
        agg: dict[str, dict[str, Any]] = {}
        for m in meetings:
            for p in m["participants"]:
                key = f"{p['display_name']}|{p['model'] or ''}"
                a = agg.setdefault(key, {"display_name": p["display_name"], "model": p["model"], "adapter": p["adapter"],
                                         "calls": 0, "ok": 0, "timeout": 0, "failed": 0, "elapsed_s": 0.0, "tokens": 0,
                                         "chars": 0, "cost_usd": 0.0, "meetings": 0})
                for k in ("calls", "ok", "timeout", "failed", "tokens", "chars"):
                    a[k] += p[k]
                a["elapsed_s"] += p["elapsed_s"]
                a["cost_usd"] += p["cost_usd"]
                a["meetings"] += 1
        per = sorted(agg.values(), key=lambda a: -a["tokens"])
        for a in per:
            a["avg_s"] = round(a["elapsed_s"] / a["calls"], 1) if a["calls"] else 0
            a["fail_rate"] = round((a["timeout"] + a["failed"]) / a["calls"], 3) if a["calls"] else 0
            a["elapsed_s"] = round(a["elapsed_s"], 1)
            a["cost_usd"] = round(a["cost_usd"], 4)
        totals = {k: sum(a[k] for a in per) for k in ("calls", "ok", "timeout", "failed", "tokens")}
        totals["elapsed_s"] = round(sum(a["elapsed_s"] for a in per), 1)
        totals["cost_usd"] = round(sum(a["cost_usd"] for a in per), 4)
        totals["meetings"] = len(meetings)
        w = archive.window_usage(hub.meetings_dirs())
        seats = sorted(archive.seat_history(hub.meetings_dirs()).values(), key=lambda h: (h["persona"], -(h["avg_score"] or 0)))
        return {"meetings": meetings, "participants": per, "totals": totals, "window": w, "seats": seats}

    # ---- 設定とプロンプトの編集

    def _allowed(path: str) -> dict[str, Any]:
        p = str(Path(path).resolve())
        for f in editable_files(hub):
            if f["path"] == p:
                return f
        raise HTTPException(404, "このファイルは画面からは編集できません")

    @app.get("/api/settings/files", dependencies=auth)
    async def settings_files() -> JSONResponse:
        return JSONResponse({"files": editable_files(hub)})

    @app.get("/api/settings/file", dependencies=auth)
    async def settings_file(path: str) -> JSONResponse:
        f = _allowed(path)
        return JSONResponse({**f, "text": Path(f["path"]).read_text(encoding="utf-8")})

    @app.put("/api/settings/file", dependencies=auth)
    async def settings_save(body: FileIn) -> JSONResponse:
        f = _allowed(body.path)
        p = Path(f["path"])
        old = p.read_text(encoding="utf-8") if p.exists() else ""
        if f["kind"] == "config":
            _check_config_change(old, body.text)
            # 同じディレクトリの一時ファイルで検証する（相対パスの解決を同じにするため）
            tmp = p.with_name(f".{p.name}.validate.yaml")
            tmp.write_text(body.text, encoding="utf-8")
            try:
                load_config(tmp)
            except Exception as e:  # noqa: BLE001
                tmp.unlink(missing_ok=True)
                raise HTTPException(422, f"設定として正しくありません: {e}") from e
            tmp.unlink(missing_ok=True)
        if old:
            p.with_name(p.name + ".bak").write_text(old, encoding="utf-8")
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(body.text, encoding="utf-8")
        os.replace(tmp, p)
        return JSONResponse({"ok": True, "message": f"{f['name']} を保存しました（前の版は {p.name}.bak）。次に始める会議から反映されます"})

    @app.get("/api/models", dependencies=auth)
    async def models() -> JSONResponse:
        # agy models は数秒かかるので、イベントループを止めないよう別スレッドで
        return JSONResponse({"claude_code": [{"id": m, "label": m} for m in CLAUDE_MODELS], "antigravity_cli": await asyncio.to_thread(agy_models)})


def _text_download(text: str, filename: str, mime: str):
    from fastapi.responses import Response

    return Response(text, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
