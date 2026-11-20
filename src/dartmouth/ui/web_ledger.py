"""Web UI: 台帳（決定・前提・不明点・協議事項・ToDo と共通マイルストーン）の API。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from ..ledger import BALL_LABEL, DOMAINS, EXECUTABLE, KIND_LABEL, STATUS_LABEL, Ledger, ball_of, sync, sync_agenda
from ..paths import ledger_path

log = logging.getLogger("dartmouth.web")


class StatusIn(BaseModel):
    status: str
    note: str = ""


class ItemIn(BaseModel):
    fields: dict[str, Any]


class DepIn(BaseModel):
    src: str
    dst: str
    note: str = ""
    remove: bool = False


class MilestoneIn(BaseModel):
    theme: str
    key: str
    title: str
    criteria: str = ""
    due: str = ""


class DueIn(BaseModel):
    project: str
    milestone: int
    due: str = ""


class MilestoneEditIn(BaseModel):
    fields: dict[str, Any]


class ThemeIn(BaseModel):
    theme: str


class CellIn(BaseModel):
    project: str
    milestone: int
    state: str | None = None
    note: str = ""


class ProposeIn(BaseModel):
    config_path: str
    chair: str | None = None
    chair_effort: str | None = None


class ClassifyIn(BaseModel):
    config_path: str
    project: str | None = None
    max_batches: int = 5


def register_ledger(app: FastAPI, hub, auth) -> None:
    job: dict[str, Any] = {"running": False, "log": [], "result": None, "error": None}
    tasks: set[asyncio.Task] = set()  # 実行中の分類（参照を持っておかないと途中で回収される）
    proposing: set[str] = set()  # 議題を作っている途中の案件

    def open_ledger() -> Ledger:
        return Ledger(ledger_path())

    def call(fn, *a, **kw):
        lg = open_ledger()
        try:
            return fn(lg, *a, **kw)
        except KeyError as e:
            raise HTTPException(404, f"{e} はありません") from None
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        finally:
            lg.close()

    def mdirs() -> list[Path]:
        return [d for d in hub.meetings_dirs() if d.is_dir()]

    def agendas() -> list[Path]:
        from ..agenda import is_agenda
        import yaml

        out = []
        for c in hub.configs:
            try:
                if is_agenda(yaml.safe_load(c.read_text(encoding="utf-8"))):
                    out.append(c)
            except Exception:  # noqa: BLE001
                continue
        return out

    def agenda_of_theme(theme: str) -> Path | None:
        from ..agenda import load

        for ag in agendas():
            try:
                if any(th["name"] == theme for th in load(ag)["themes"]):
                    return ag
            except Exception:  # noqa: BLE001
                continue
        return None

    def check_due(due: str) -> None:
        from ..agenda import due_key

        if due.strip() and not due_key(due):
            raise ValueError(f"期限「{due}」を読めません（2026 / 2026-12 / 2026-12-31 / 2027年3月 など）")

    def sync_all(lg: Ledger) -> dict[str, Any]:
        for ag in agendas():
            sync_agenda(lg, ag)
        return sync(lg, mdirs())

    @app.get("/api/ledger", dependencies=auth)
    async def ledger_items(theme: str | None = None, project: str | None = None, kind: str | None = None,
                           status: str = "open", text: str | None = None, executable: str | None = None,
                           milestone: int | None = None, ball: str | None = None) -> JSONResponse:
        def go(lg: Ledger):
            for ag in agendas():
                try:
                    sync_agenda(lg, ag)
                except Exception:  # noqa: BLE001  議題ファイルの書き損じで台帳を見られなくしない
                    log.exception("agenda sync failed: %s", ag)
            its = lg.items(theme=theme or None, project=project or None, kind=kind or None,
                           status=None if status == "all" else status, text=text or None, executable=executable or None,
                           milestone=milestone)
            for it in its:
                it["blocked_by"] = [b["dst"] for b in lg.blockers(it["uid"])] if it["status"] in ("open", "held") else []
                it["ball_now"] = ball_of(it, bool(it["blocked_by"]))
            if ball:
                its = [it for it in its if it["ball_now"] == ball]
            meta = {r["key"]: r["value"] for r in lg.q("SELECT * FROM meta")}
            return {"items": its, "counts": lg.counts(), "themes": lg.themes(), "projects": lg.projects(), "meta": meta,
                    "labels": {"kind": KIND_LABEL, "status": STATUS_LABEL, "executable": EXECUTABLE, "domain": DOMAINS,
                               "ball": BALL_LABEL},
                    "db": str(lg.path)}
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.get("/api/ledger/matrix/{theme}", dependencies=auth)
    async def ledger_matrix(theme: str) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(call, lambda lg: lg.matrix(theme)))

    @app.get("/api/ledger/item/{uid:path}", dependencies=auth)
    async def ledger_item(uid: str) -> JSONResponse:
        def go(lg: Ledger):
            it = lg.item(uid)
            if it is None:
                raise KeyError(uid)
            it["blockers"] = lg.blockers(uid)
            it["unlocks"] = lg.unlocks(uid)
            it["milestones"] = lg.milestones(it["theme"]) if it.get("theme") else []
            return it
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.post("/api/ledger/sync", dependencies=auth)
    async def ledger_sync() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(call, sync_all))

    @app.post("/api/ledger/item/{uid:path}/status", dependencies=auth)
    async def ledger_status(uid: str, body: StatusIn) -> JSONResponse:
        from ..ledger.cli import _write_back

        def go(lg: Ledger):
            lg.set_status(uid, body.status, body.note)
            _write_back(uid, body.status, body.note, mdirs())
            return {"ok": True}
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.post("/api/ledger/item/{uid:path}", dependencies=auth)
    async def ledger_edit(uid: str, body: ItemIn) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(call, lambda lg: (lg.update_item(uid, body.fields), {"ok": True})[1]))

    @app.post("/api/ledger/dep", dependencies=auth)
    async def ledger_dep(body: DepIn) -> JSONResponse:
        def go(lg: Ledger):
            (lg.remove_dep(body.src, body.dst) if body.remove else lg.add_dep(body.src, body.dst, body.note))
            return {"ok": True}
        return JSONResponse(await asyncio.to_thread(call, go))

    # マイルストーンは、議題ファイルにあるテーマなら議題ファイルに書き（正本）、台帳に取り込み直す
    @app.post("/api/ledger/milestone", dependencies=auth)
    async def ledger_ms_add(body: MilestoneIn) -> JSONResponse:
        from ..topics_file import edit_agenda_milestone

        def go(lg: Ledger):
            check_due(body.due)
            ag = agenda_of_theme(body.theme)
            if ag:
                edit_agenda_milestone(ag, body.theme, "add", body.key, {"title": body.title, "criteria": body.criteria, "due": body.due})
                sync_agenda(lg, ag)
                return {"ok": True, "agenda": str(ag)}
            return {"id": lg.add_milestone(body.theme, body.key, body.title, body.criteria, due=body.due)}
        return JSONResponse(await asyncio.to_thread(call, go))

    def _ms(lg: Ledger, mid: int) -> dict[str, Any]:
        r = lg.q("SELECT * FROM milestones WHERE id=?", (mid,))
        if not r:
            raise KeyError(f"マイルストーン {mid}")
        return r[0]

    @app.post("/api/ledger/milestone/{mid}", dependencies=auth)
    async def ledger_ms_edit(mid: int, body: MilestoneEditIn) -> JSONResponse:
        from ..topics_file import edit_agenda_milestone

        def go(lg: Ledger):
            m = _ms(lg, mid)
            check_due(str(body.fields.get("due") or ""))
            ag = agenda_of_theme(m["theme"]) if str(m["source"]).startswith("agenda:") else None
            if ag:
                f = {k: v for k, v in body.fields.items() if k in ("key", "title", "criteria", "due")}
                edit_agenda_milestone(ag, m["theme"], "update", m["key"], f)
                if "ord" in body.fields:
                    # 並び順は議題ファイルの中の位置で表す。ord より小さいものの数が、移す先の位置
                    others = [x for x in lg.milestones(m["theme"], include_proposed=False) if x["id"] != mid]
                    pos = sum(1 for x in others if x["ord"] < int(body.fields["ord"]))
                    edit_agenda_milestone(ag, m["theme"], "move", str(body.fields.get("key") or m["key"]), {"to": pos})
                sync_agenda(lg, ag)
            else:
                lg.update_milestone(mid, body.fields)
            return {"ok": True}
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.delete("/api/ledger/milestone/{mid}", dependencies=auth)
    async def ledger_ms_del(mid: int) -> JSONResponse:
        from ..topics_file import edit_agenda_milestone

        def go(lg: Ledger):
            m = _ms(lg, mid)
            ag = agenda_of_theme(m["theme"]) if str(m["source"]).startswith("agenda:") else None
            if ag:
                edit_agenda_milestone(ag, m["theme"], "remove", m["key"])
                sync_agenda(lg, ag)
            else:
                lg.delete_milestone(mid)
            return {"ok": True}
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.post("/api/ledger/due", dependencies=auth)
    async def ledger_due(body: DueIn) -> JSONResponse:
        """案件ごとの期限（テーマ共通の期限の上書き）。議題ファイルの案件なら議題ファイルに書く。"""
        from ..topics_file import set_agenda_project_due

        def go(lg: Ledger):
            check_due(body.due)
            m = _ms(lg, body.milestone)
            ag = agenda_of_theme(m["theme"]) if str(m["source"]).startswith("agenda:") else None
            if ag:
                set_agenda_project_due(ag, body.project, m["key"], body.due)
                sync_agenda(lg, ag)
                if not body.due.strip():
                    lg.db.execute("UPDATE project_milestones SET due='' WHERE project=? AND milestone=?", (body.project, body.milestone))
                    lg.db.commit()
            else:
                lg.set_project_due(body.project, body.milestone, body.due)
            return {"ok": True}
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.get("/api/ledger/candidates", dependencies=auth)
    async def ledger_candidates(project: str) -> JSONResponse:
        """ホームで議題の候補にする、案件の未解決事項（協議事項・不明点・未分類の未決）。重要なものから。"""
        from ..ledger.agenda_propose import candidates

        return JSONResponse(await asyncio.to_thread(call, lambda lg: {"items": candidates(lg, project), "labels": KIND_LABEL}))

    @app.get("/api/agenda/proposal", dependencies=auth)
    async def agenda_proposal(project: str) -> JSONResponse:
        """前の議長の提案（未解決事項が変わっていなければ）。なければ proposal: null。"""
        from ..ledger.agenda_propose import candidates, cached

        def go(lg: Ledger):
            return {"proposal": cached(lg, project), "open_items": len(candidates(lg, project)),
                    "running": project in proposing}
        return JSONResponse(await asyncio.to_thread(call, go))

    @app.post("/api/agenda/propose", dependencies=auth)
    async def agenda_propose(body: ProposeIn) -> JSONResponse:
        """議長に、案件の未解決事項を束ねて議題にしてもらう（数十秒〜数分かかる）。"""
        from ..agenda import split_ref
        from ..engine.context import default_prompts_dir
        from ..ledger import classify as cl
        from ..ledger.agenda_propose import propose

        ref = hub.config_by_path(body.config_path)
        pid = split_ref(ref)[1]
        if not pid:
            raise HTTPException(400, "議題ファイルの案件を選んでください")
        if pid in proposing:
            raise HTTPException(409, "この案件の議題を作っている途中です")
        agents = next((c for c in hub.configs if c.name == "agents.yaml"), None)
        try:
            ad = await asyncio.to_thread(cl.chair_adapter, str(ref), None, str(agents) if agents else None,
                                         body.chair, body.chair_effort)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"議長のアダプタを作れません: {e}") from None
        tmpl = (default_prompts_dir() / "agenda_propose.md").read_text(encoding="utf-8")
        purpose = ""
        try:
            from ..config import load_config

            purpose = load_config(ref, agents_path=agents).meeting.purpose if agents else ""
        except Exception:  # noqa: BLE001
            pass
        proposing.add(pid)
        lg = open_ledger()
        try:
            for ag in agendas():
                sync_agenda(lg, ag)
            await asyncio.to_thread(sync, lg, mdirs())
            res = await propose(lg, ad, pid, tmpl, purpose)
        except Exception as e:  # noqa: BLE001
            log.exception("agenda propose failed")
            raise HTTPException(502, f"議長が議題を作れませんでした: {e}") from None
        finally:
            proposing.discard(pid)
            lg.close()
            await ad.close()
        return JSONResponse({"proposal": res})

    @app.get("/api/ledger/timeline", dependencies=auth)
    async def ledger_timeline(theme: str | None = None) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(call, lambda lg: {"rows": lg.timeline(theme or None)}))

    @app.post("/api/ledger/project/{pid}/theme", dependencies=auth)
    async def ledger_theme(pid: str, body: ThemeIn) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(call, lambda lg: (lg.set_theme(pid, body.theme.strip() or pid), {"ok": True})[1]))

    @app.post("/api/ledger/cell", dependencies=auth)
    async def ledger_cell(body: CellIn) -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(
            call, lambda lg: (lg.set_project_milestone(body.project, body.milestone, body.state, body.note), {"ok": True})[1]))

    @app.get("/api/ledger/pack", dependencies=auth)
    async def ledger_pack(project: str | None = None, open: bool = False) -> PlainTextResponse:
        """AI に渡す一括 Markdown（コンテキストパック）。project を省くと全案件。"""
        def go(lg: Ledger):
            for ag in agendas():
                try:
                    sync_agenda(lg, ag)
                except Exception:  # noqa: BLE001
                    log.exception("agenda sync failed: %s", ag)
            return lg.context_pack(project or None, open_only=open)
        return PlainTextResponse(await asyncio.to_thread(call, go), media_type="text/markdown; charset=utf-8")

    @app.get("/api/ledger/brief/{pid}", dependencies=auth)
    async def ledger_brief(pid: str) -> PlainTextResponse:
        return PlainTextResponse(await asyncio.to_thread(call, lambda lg: lg.brief(pid)), media_type="text/markdown; charset=utf-8")

    @app.get("/api/ledger/classify", dependencies=auth)
    async def ledger_classify_state() -> JSONResponse:
        return JSONResponse({**job, "log": job["log"][-20:],
                             "configs": [{"path": str(p), "name": p.name} for p in hub.configs if p.name != "agents.yaml"]})

    @app.post("/api/ledger/classify", dependencies=auth)
    async def ledger_classify(body: ClassifyIn) -> JSONResponse:
        if job["running"]:
            raise HTTPException(409, "分類はすでに実行中です")
        cfg = hub.config_by_path(body.config_path)
        agents = next((c for c in hub.configs if c.name == "agents.yaml"), None)
        from ..engine.context import default_prompts_dir
        from ..ledger.classify import chair_adapter, classify, meeting_dir_map

        try:
            ad = await asyncio.to_thread(chair_adapter, str(cfg), None, str(agents) if agents else None)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"議長のアダプタを作れません: {e}") from None
        tmpl = (default_prompts_dir() / "ledger_classify.md").read_text(encoding="utf-8")
        job.update(running=True, log=[], result=None, error=None)

        async def run() -> None:
            lg = open_ledger()
            try:
                await asyncio.to_thread(sync_all, lg)
                job["result"] = await classify(lg, ad, tmpl, meeting_dir_map(mdirs()), project=body.project or None,
                                               max_batches=max(1, min(body.max_batches, 50)), progress=job["log"].append)
            except Exception as e:  # noqa: BLE001
                log.exception("ledger classify failed")
                job["error"] = str(e)
            finally:
                lg.close()
                await ad.close()
                job["running"] = False

        t = asyncio.get_running_loop().create_task(run())
        tasks.add(t)
        t.add_done_callback(tasks.discard)
        return JSONResponse({"ok": True})
