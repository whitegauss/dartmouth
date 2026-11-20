"""台帳: 会議をまたいで「何が決まり、何が決まっておらず、何をすればよいか」を管理する SQLite。

正本は各会議の transcript.jsonl（と item_status.json）で、台帳はそこから作り直せる索引。
AI の分類（根拠・実行のしやすさ・SW/HW・マイルストーン・依存）と、台帳だけで付けた状態は
取り込みをやり直しても消えない。

用語
- 案件（project）: 設定ファイル1つ（homeserver.yaml など）。meeting.project で明示もできる
- テーマ（theme）: 案件をまとめる大きな単位（機材の導入、システム検証など）。共通マイルストーンを持つ
- 項目（item）の種類:
    premise 前提 / decision 決定 / unknown 不明点（何が分かればよいか）/
    discussion 協議事項（誰と何を決めるか）/ todo 実行すること / question 未決（未分類）
- 依存（deps）: src が dst を必要とする（dst がないと src ができない）。ノードは項目の uid か "m:<マイルストーンid>"
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from ..agenda import due_key
from ..storage.minutes import EXTERNAL_OWNERS, HUMAN_OWNERS

SCHEMA_VERSION = "1"
KINDS = ("premise", "decision", "unknown", "discussion", "todo", "question")
KIND_LABEL = {"premise": "前提", "decision": "決定", "unknown": "不明点", "discussion": "協議事項",
              "todo": "ToDo", "question": "未決（未分類）"}
STATUSES = ("open", "done", "held", "dropped", "superseded")
STATUS_LABEL = {"open": "未完了", "done": "済み", "held": "保留", "dropped": "取り下げ", "superseded": "置き換え済み"}
EXECUTABLE = {"auto": "機械的に実行できる", "judgment": "判断が要る", "blocked": "前提・不明点待ち"}
DOMAINS = {"software": "ソフトウェア", "hardware": "ハードウェア", "both": "SW＋HW", "life": "生活", "other": "その他"}
CLOSED = ("done", "dropped", "superseded")
# ボール: 次に誰が動く番か。self / ai / external は手でも付けられる（items.ball）。ほかは状態と依存から決まる
BALL_LABEL = {"self": "本人", "ai": "AI", "external": "外部待ち", "blocked": "依存待ち", "held": "保留", "none": "—"}
BALL_MANUAL = ("self", "ai", "external")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects(
  id TEXT PRIMARY KEY, name TEXT, theme TEXT, kind TEXT DEFAULT '', config TEXT DEFAULT '',
  theme_manual INTEGER DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS project_alias(alias TEXT PRIMARY KEY, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS milestones(
  id INTEGER PRIMARY KEY, theme TEXT NOT NULL, ord INTEGER NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL,
  criteria TEXT DEFAULT '', status TEXT DEFAULT 'active', source TEXT DEFAULT '', due TEXT DEFAULT '', UNIQUE(theme, key));
CREATE TABLE IF NOT EXISTS project_milestones(
  project TEXT, milestone INTEGER, state TEXT, note TEXT DEFAULT '', updated_at TEXT, due TEXT DEFAULT '',
  PRIMARY KEY(project, milestone));
CREATE TABLE IF NOT EXISTS items(
  uid TEXT PRIMARY KEY, project TEXT, meeting TEXT, meeting_date TEXT, topic INTEGER, topic_title TEXT DEFAULT '',
  local_id TEXT, src_kind TEXT, kind TEXT, text TEXT, owner TEXT DEFAULT '',
  status TEXT DEFAULT 'open', status_note TEXT DEFAULT '', status_manual INTEGER DEFAULT 0,
  rationale TEXT DEFAULT '', needs TEXT DEFAULT '', executable TEXT DEFAULT '', domain TEXT DEFAULT '',
  milestone INTEGER, classified_at TEXT, classifier TEXT DEFAULT '', archived INTEGER DEFAULT 0, updated_at TEXT,
  ball TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS items_project ON items(project, status);
CREATE INDEX IF NOT EXISTS items_kind ON items(kind, status);
CREATE INDEX IF NOT EXISTS items_milestone ON items(milestone);
CREATE INDEX IF NOT EXISTS items_unclassified ON items(classified_at);
CREATE TABLE IF NOT EXISTS deps(
  src TEXT NOT NULL, dst TEXT NOT NULL, rel TEXT NOT NULL DEFAULT 'requires', note TEXT DEFAULT '', source TEXT DEFAULT '',
  PRIMARY KEY(src, dst, rel));
CREATE INDEX IF NOT EXISTS deps_dst ON deps(dst);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(uid UNINDEXED, text, rationale, needs, tokenize='trigram');
"""


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        # 古い台帳に、あとから足した列を足す
        for table, col in (("milestones", "due"), ("project_milestones", "due"), ("items", "ball")):
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema', ?)", (SCHEMA_VERSION,))
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.db
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def q(self, sql: str, args: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, tuple(args))]

    # ------------------------------------------------------------------ 取り込み

    def resolve_project(self, pid: str) -> str:
        """統合した案件の別名を、統合先に読み替える。"""
        seen = set()
        while pid not in seen:
            seen.add(pid)
            r = self.db.execute("SELECT target FROM project_alias WHERE alias=?", (pid,)).fetchone()
            if not r:
                break
            pid = r[0]
        return pid

    def merge_project(self, alias: str, target: str) -> None:
        """案件 alias を target に統合する（項目と、案件ごとのマイルストーンの状態を移す）。以後の取り込みも target に入る。"""
        target = self.resolve_project(target)
        if alias == target:
            return
        if self.db.execute("SELECT 1 FROM projects WHERE id=?", (target,)).fetchone() is None:
            raise KeyError(target)
        self.db.execute("UPDATE items SET project=? WHERE project=?", (target, alias))
        self.db.execute("UPDATE OR IGNORE project_milestones SET project=? WHERE project=?", (target, alias))
        self.db.execute("DELETE FROM project_milestones WHERE project=?", (alias,))
        self.db.execute("DELETE FROM projects WHERE id=?", (alias,))
        self.db.execute("UPDATE project_alias SET target=? WHERE target=?", (target, alias))
        self.db.execute("INSERT OR REPLACE INTO project_alias VALUES(?,?)", (alias, target))

    def upsert_project(self, pid: str, name: str, theme: str | None, config: str = "") -> None:
        row = self.db.execute("SELECT theme_manual FROM projects WHERE id=?", (pid,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO projects(id, name, theme, config, updated_at) VALUES(?,?,?,?,?)",
                            (pid, name, theme or pid, config, now()))
        elif row["theme_manual"]:
            self.db.execute("UPDATE projects SET name=?, config=?, updated_at=? WHERE id=?", (name, config, now(), pid))
        else:
            self.db.execute("UPDATE projects SET name=?, config=?, theme=COALESCE(?, theme), updated_at=? WHERE id=?",
                            (name, config, theme, now(), pid))

    def upsert_item(self, it: dict[str, Any]) -> bool:
        """会議から取った項目を入れる。AI の分類と台帳だけの状態は残す。新しく入れたら True。"""
        row = self.db.execute("SELECT status_manual, kind, classified_at FROM items WHERE uid=?", (it["uid"],)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO items(uid, project, meeting, meeting_date, topic, topic_title, local_id, src_kind, kind, text, owner,"
                " status, status_note, archived, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["uid"], it["project"], it["meeting"], it.get("meeting_date", ""), it.get("topic"), it.get("topic_title", ""),
                 it["local_id"], it["src_kind"], it["src_kind"], it["text"], it.get("owner") or "",
                 it.get("status", "open"), it.get("status_note", ""), int(it.get("archived", False)), now()))
            self._fts(it["uid"])
            return True
        sets = "project=?, meeting_date=?, topic=?, topic_title=?, text=?, owner=?, archived=?, updated_at=?"
        args: list[Any] = [it["project"], it.get("meeting_date", ""), it.get("topic"), it.get("topic_title", ""), it["text"],
                           it.get("owner") or "", int(it.get("archived", False)), now()]
        if not row["status_manual"]:
            sets += ", status=?, status_note=?"
            args += [it.get("status", "open"), it.get("status_note", "")]
        self.db.execute(f"UPDATE items SET {sets} WHERE uid=?", (*args, it["uid"]))
        self._fts(it["uid"])
        return False

    def _fts(self, uid: str) -> None:
        r = self.db.execute("SELECT text, rationale, needs FROM items WHERE uid=?", (uid,)).fetchone()
        self.db.execute("DELETE FROM items_fts WHERE uid=?", (uid,))
        if r:
            self.db.execute("INSERT INTO items_fts(uid, text, rationale, needs) VALUES(?,?,?,?)", (uid, r[0], r[1], r[2]))

    # ------------------------------------------------------------------ 分類・編集

    def apply_classification(self, uid: str, c: dict[str, Any], classifier: str, theme: str) -> None:
        """AI の分類を1項目に反映する。マイルストーンはキーで受け、テーマの中で id に直す。"""
        cur = self.db.execute("SELECT src_kind, kind FROM items WHERE uid=?", (uid,)).fetchone()
        if cur is None:
            return
        kind = c.get("kind") or cur["kind"]
        # 会議の決定は決定のまま（前提への付け替えだけは許す）。未決は不明点か協議事項に分ける
        allowed = {"decision": {"decision", "premise"}, "question": {"unknown", "discussion"},
                   "todo": {"todo"}, "premise": {"premise"}}.get(cur["src_kind"], set(KINDS))
        if kind not in allowed:
            kind = cur["kind"] if cur["kind"] in allowed else next(iter(sorted(allowed)))
        ms = None
        if c.get("milestone"):
            r = self.db.execute("SELECT id FROM milestones WHERE theme=? AND key=?", (theme, c["milestone"])).fetchone()
            ms = r["id"] if r else None
        self.db.execute(
            "UPDATE items SET kind=?, rationale=?, needs=?, executable=?, domain=?, milestone=COALESCE(?, milestone),"
            " classified_at=?, classifier=? WHERE uid=?",
            (kind, c.get("rationale", ""), c.get("needs", ""), c.get("executable", "") if c.get("executable") in EXECUTABLE else "",
             c.get("domain", "") if c.get("domain") in DOMAINS else "", ms, now(), classifier, uid))
        if cur["src_kind"] == "premise":
            # 議題の依頼文から前提が読み取れなければ、一覧から外す（取り込み直しても戻さない）
            if (c.get("rationale") or "").strip():
                self.db.execute("UPDATE items SET status='open', status_manual=0 WHERE uid=? AND status='dropped'"
                                " AND status_note='前提を含まない依頼文'", (uid,))
            else:
                self.db.execute("UPDATE items SET status='dropped', status_note='前提を含まない依頼文', status_manual=1"
                                " WHERE uid=? AND status='open'", (uid,))
        valid = {r["uid"] for r in self.db.execute("SELECT uid FROM items")}
        self.db.execute("DELETE FROM deps WHERE src=? AND source='ai'", (uid,))
        for dst in c.get("requires") or []:
            if dst in valid and dst != uid:
                self.db.execute("INSERT OR IGNORE INTO deps(src, dst, rel, source) VALUES(?,?, 'requires', 'ai')", (uid, dst))
        for old in c.get("supersedes") or []:
            if old in valid and old != uid:
                self.db.execute("INSERT OR IGNORE INTO deps(src, dst, rel, source) VALUES(?,?, 'supersedes', 'ai')", (uid, old))
                self.db.execute("UPDATE items SET status='superseded', status_note=?, status_manual=1 WHERE uid=? AND status='open'",
                                (f"{uid} で置き換え", old))
        self._fts(uid)

    def set_status(self, uid: str, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"状態は {', '.join(STATUSES)} のどれか")
        n = self.db.execute("UPDATE items SET status=?, status_note=?, status_manual=1, updated_at=? WHERE uid=?",
                            (status, note, now(), uid)).rowcount
        if not n:
            raise KeyError(uid)
        self.db.commit()

    def update_item(self, uid: str, fields: dict[str, Any]) -> None:
        ok = {"kind", "rationale", "needs", "executable", "domain", "milestone", "owner", "ball"}
        if not fields.get("kind", "x"):
            fields = {k: v for k, v in fields.items() if k != "kind"}
        bad = set(fields) - ok
        if bad:
            raise ValueError("変えられない項目: " + ", ".join(sorted(bad)))
        if "kind" in fields and fields["kind"] not in KINDS:
            raise ValueError("kind が不正")
        if fields.get("ball") and fields["ball"] not in BALL_MANUAL:
            raise ValueError(f"ball は {', '.join(BALL_MANUAL)} か空")
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        n = self.db.execute(f"UPDATE items SET {sets}, updated_at=? WHERE uid=?", (*fields.values(), now(), uid)).rowcount
        if not n:
            raise KeyError(uid)
        self._fts(uid)
        self.db.commit()

    def add_dep(self, src: str, dst: str, note: str = "", rel: str = "requires") -> None:
        if src == dst:
            raise ValueError("自分自身には依存できません")
        for node in (src, dst):
            if not self._node_exists(node):
                raise KeyError(node)
        if rel == "requires" and src in self.ancestors(dst):
            raise ValueError(f"循環します（{dst} はすでに {src} を必要としています）")
        self.db.execute("INSERT OR REPLACE INTO deps(src, dst, rel, note, source) VALUES(?,?,?,?, 'manual')", (src, dst, rel, note))
        self.db.commit()

    def remove_dep(self, src: str, dst: str, rel: str = "requires") -> None:
        self.db.execute("DELETE FROM deps WHERE src=? AND dst=? AND rel=?", (src, dst, rel))
        self.db.commit()

    def _node_exists(self, node: str) -> bool:
        if node.startswith("m:"):
            return self.db.execute("SELECT 1 FROM milestones WHERE id=?", (node[2:],)).fetchone() is not None
        return self.db.execute("SELECT 1 FROM items WHERE uid=?", (node,)).fetchone() is not None

    def set_theme(self, project: str, theme: str) -> None:
        n = self.db.execute("UPDATE projects SET theme=?, theme_manual=1 WHERE id=?", (theme, project)).rowcount
        if not n:
            raise KeyError(project)
        self.db.commit()

    # ------------------------------------------------------------------ マイルストーン

    def milestones(self, theme: str, include_proposed: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM milestones WHERE theme=?" + ("" if include_proposed else " AND status='active'") + " ORDER BY ord, id"
        return self.q(sql, (theme,))

    def add_milestone(self, theme: str, key: str, title: str, criteria: str = "", ord_: int | None = None,
                      status: str = "active", source: str = "manual", due: str = "") -> int:
        if ord_ is None:
            r = self.db.execute("SELECT COALESCE(MAX(ord), 0) + 10 FROM milestones WHERE theme=?", (theme,)).fetchone()
            ord_ = r[0]
        cur = self.db.execute("INSERT INTO milestones(theme, ord, key, title, criteria, status, source, due) VALUES(?,?,?,?,?,?,?,?)"
                              " ON CONFLICT(theme, key) DO UPDATE SET title=excluded.title, criteria=excluded.criteria, due=excluded.due",
                              (theme, ord_, key, title, criteria, status, source, due))
        self.db.commit()
        mid = cur.lastrowid or self.db.execute("SELECT id FROM milestones WHERE theme=? AND key=?", (theme, key)).fetchone()[0]
        return int(mid)

    def update_milestone(self, mid: int, fields: dict[str, Any]) -> None:
        ok = {"title", "criteria", "ord", "status", "key", "due"}
        fields = {k: v for k, v in fields.items() if k in ok}
        if fields.get("status") not in (None, "active", "proposed"):
            raise ValueError("status は active か proposed")
        if fields:
            self.db.execute(f"UPDATE milestones SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), mid))
            self.db.commit()

    def delete_milestone(self, mid: int) -> None:
        self.db.execute("UPDATE items SET milestone=NULL WHERE milestone=?", (mid,))
        self.db.execute("DELETE FROM project_milestones WHERE milestone=?", (mid,))
        self.db.execute("DELETE FROM deps WHERE src=? OR dst=?", (f"m:{mid}", f"m:{mid}"))
        self.db.execute("DELETE FROM milestones WHERE id=?", (mid,))
        self.db.commit()

    def set_project_milestone(self, project: str, mid: int, state: str | None, note: str = "") -> None:
        """案件の、あるマイルストーンでの状態を手で決める（None で自動判定に戻す）。"""
        if state is None:
            # 手で決めた状態だけを外す（案件ごとの期限は残す）
            self.db.execute("UPDATE project_milestones SET state=NULL, note='' WHERE project=? AND milestone=?", (project, mid))
        else:
            if state not in ("todo", "doing", "done", "na"):
                raise ValueError("state は todo / doing / done / na")
            self.db.execute("INSERT INTO project_milestones(project, milestone, state, note, updated_at) VALUES(?,?,?,?,?)"
                            " ON CONFLICT(project, milestone) DO UPDATE SET state=excluded.state, note=excluded.note,"
                            " updated_at=excluded.updated_at", (project, mid, state, note, now()))
        self.db.commit()

    # ------------------------------------------------------------------ 参照・分析

    def items(self, *, project: str | None = None, theme: str | None = None, kind: str | None = None,
              status: str | None = "open", milestone: int | None = None, text: str | None = None,
              executable: str | None = None, include_archived: bool = False, limit: int = 2000) -> list[dict[str, Any]]:
        where, args = ["1=1"], []
        if project:
            where.append("i.project=?"); args.append(project)
        if theme:
            where.append("p.theme=?"); args.append(theme)
        if kind:
            kinds = kind.split(",")
            where.append(f"i.kind IN ({','.join('?' * len(kinds))})"); args += kinds
        if status == "open":
            where.append("i.status IN ('open','held')")
        elif status:
            where.append("i.status=?"); args.append(status)
        if milestone is not None:
            where.append("i.milestone=?"); args.append(milestone)
        if executable:
            where.append("i.executable=?"); args.append(executable)
        if not include_archived:
            where.append("i.archived=0")
        if text:
            if len(text) >= 3:
                where.append("i.uid IN (SELECT uid FROM items_fts WHERE items_fts MATCH ?)")
                args.append('"' + text.replace('"', '""') + '"')
            else:
                where.append("(i.text LIKE ? OR i.rationale LIKE ? OR i.needs LIKE ?)"); args += [f"%{text}%"] * 3
        sql = (f"SELECT i.*, p.theme, p.name AS project_name, m.key AS milestone_key, m.title AS milestone_title"
               f" FROM items i LEFT JOIN projects p ON p.id=i.project LEFT JOIN milestones m ON m.id=i.milestone"
               f" WHERE {' AND '.join(where)} ORDER BY i.meeting_date DESC, i.meeting DESC, i.local_id LIMIT ?")
        return self.q(sql, (*args, limit))

    def item(self, uid: str) -> dict[str, Any] | None:
        rows = self.q("SELECT i.*, p.theme, m.key AS milestone_key, m.title AS milestone_title FROM items i"
                      " LEFT JOIN projects p ON p.id=i.project LEFT JOIN milestones m ON m.id=i.milestone WHERE i.uid=?", (uid,))
        if not rows:
            return None
        it = rows[0]
        it["requires"] = self.q("SELECT d.dst AS uid, d.note, d.source, i.text, i.status, i.kind FROM deps d"
                                " LEFT JOIN items i ON i.uid=d.dst WHERE d.src=? AND d.rel='requires'", (uid,))
        it["enables"] = self.q("SELECT d.src AS uid, d.note, d.source, i.text, i.status, i.kind FROM deps d"
                               " LEFT JOIN items i ON i.uid=d.src WHERE d.dst=? AND d.rel='requires'", (uid,))
        it["supersedes"] = self.q("SELECT dst AS uid FROM deps WHERE src=? AND rel='supersedes'", (uid,))
        return it

    def milestone_reached(self, project: str, mid: int) -> bool:
        """案件がマイルストーンに達したか: 手で done / na にしたか、割り当てた項目がすべて済み（1件以上）。"""
        r = self.db.execute("SELECT state FROM project_milestones WHERE project=? AND milestone=?", (project, mid)).fetchone()
        if r and r["state"]:
            return r["state"] in ("done", "na")
        sts = [x[0] for x in self.db.execute("SELECT status FROM items WHERE project=? AND milestone=? AND archived=0"
                                              " AND status!='dropped'", (project, mid))]
        return bool(sts) and all(s in CLOSED for s in sts)

    def _done(self, node: str, project: str | None = None) -> bool:
        if node.startswith("m:"):
            return bool(project) and self.milestone_reached(project, int(node[2:]))
        r = self.db.execute("SELECT status FROM items WHERE uid=?", (node,)).fetchone()
        return bool(r) and r["status"] in CLOSED and r["status"] != "dropped"

    def ancestors(self, node: str) -> set[str]:
        """node が（間接的にも）必要とするもの。"""
        seen: set[str] = set()
        stack = [node]
        while stack:
            n = stack.pop()
            for r in self.db.execute("SELECT dst FROM deps WHERE src=? AND rel='requires'", (n,)):
                if r[0] not in seen:
                    seen.add(r[0]); stack.append(r[0])
        return seen

    def blockers(self, uid: str) -> list[dict[str, Any]]:
        """uid ができない理由: 必要としているもので、まだ済んでいないもの（直接のもの）。"""
        row = self.db.execute("SELECT project FROM items WHERE uid=?", (uid,)).fetchone()
        project = row["project"] if row else None
        out = []
        for r in self.q("SELECT d.dst, d.note, COALESCE(i.text, m.key || ' ' || m.title) AS text, i.kind, i.status"
                        " FROM deps d LEFT JOIN items i ON i.uid=d.dst"
                        " LEFT JOIN milestones m ON d.dst = 'm:' || m.id WHERE d.src=? AND d.rel='requires'", (uid,)):
            if not self._done(r["dst"], project):
                out.append(r)
        return out

    def blocked(self, project: str | None = None, theme: str | None = None) -> list[dict[str, Any]]:
        """「これがないからこれができない」: 未完了の項目のうち、済んでいない依存先があるもの。"""
        out = []
        for it in self.items(project=project, theme=theme, status="open"):
            b = self.blockers(it["uid"])
            if b:
                out.append({**it, "blockers": b})
        return out

    def ready(self, project: str | None = None, theme: str | None = None) -> list[dict[str, Any]]:
        """すぐ機械的に実行できるもの: 未完了の決定・ToDo で、実行のしやすさが auto、依存先がすべて済んでいる。"""
        return [it for it in self.items(project=project, theme=theme, status="open", kind="decision,todo", executable="auto")
                if it["status"] == "open" and not self.blockers(it["uid"])]

    def unlocks(self, uid: str) -> list[dict[str, Any]]:
        """「これがあるからこれができる」: uid が済むと、依存先がすべて揃う項目。"""
        out = []
        for it in self.item(uid)["enables"] if self.item(uid) else []:
            rest = [b for b in self.blockers(it["uid"]) if b["dst"] != uid]
            if not rest and it["status"] not in CLOSED:
                out.append(it)
        return out

    def themes(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT theme FROM projects WHERE theme IS NOT NULL ORDER BY theme")]

    def projects(self, theme: str | None = None) -> list[dict[str, Any]]:
        if theme:
            return self.q("SELECT * FROM projects WHERE theme=? ORDER BY id", (theme,))
        return self.q("SELECT * FROM projects ORDER BY theme, id")

    def matrix(self, theme: str) -> dict[str, Any]:
        """テーマの共通マイルストーン × 案件の表。横（同じ段階の案件どうし）と縦（案件の中の段階）で比べる。

        セルの状態は、手で決めたものがあればそれ、なければ項目から: 項目がなければ empty、
        すべて済みなら done、一部済み・または前の段階が済んでいれば doing、ほかは todo。
        案件の「現在地」は、最初に done でない段階。
        """
        mss = self.milestones(theme, include_proposed=False)
        for m in mss:
            m["due_key"] = due_key(m.get("due") or "")
        today = datetime.now().strftime("%Y-%m-%d")
        rows = []
        for p in self.projects(theme):
            cells = []
            pms = {r["milestone"]: r for r in self.q("SELECT * FROM project_milestones WHERE project=?", (p["id"],))}
            manual = {k: v for k, v in pms.items() if v["state"]}
            prev_done = True
            current = None
            for m in mss:
                its = self.q("SELECT uid, kind, status, executable FROM items WHERE project=? AND milestone=? AND archived=0",
                             (p["id"], m["id"]))
                live = [i for i in its if i["status"] != "dropped"]
                done = [i for i in live if i["status"] in CLOSED]
                blocked_n = sum(1 for i in live if i["status"] not in CLOSED and self.blockers(i["uid"]))
                if m["id"] in manual:
                    state = manual[m["id"]]["state"]
                elif not live:
                    state = "empty"
                elif len(done) == len(live):
                    state = "done"
                elif done or prev_done:
                    state = "doing"
                else:
                    state = "todo"
                open_kinds: dict[str, int] = {}
                for i in live:
                    if i["status"] not in CLOSED:
                        open_kinds[i["kind"]] = open_kinds.get(i["kind"], 0) + 1
                due = (pms.get(m["id"]) or {}).get("due") or m.get("due") or ""
                cells.append({"milestone": m["id"], "state": state, "total": len(live), "done": len(done),
                              "blocked": blocked_n, "open": open_kinds, "due": due, "due_key": due_key(due),
                              "overdue": bool(due_key(due) and due_key(due) < today and state not in ("done", "na")),
                              "note": manual.get(m["id"], {}).get("note", "")})
                if current is None and state not in ("done", "na", "empty"):
                    current = m["id"]
                prev_done = state in ("done", "na", "empty") and prev_done
            unassigned = self.db.execute("SELECT COUNT(*) FROM items WHERE project=? AND milestone IS NULL AND archived=0"
                                         " AND status IN ('open','held')", (p["id"],)).fetchone()[0]
            rows.append({"project": p, "cells": cells, "current": current, "unassigned_open": unassigned})
        return {"theme": theme, "milestones": mss, "rows": rows,
                "proposed": [m for m in self.milestones(theme) if m["status"] == "proposed"]}

    def timeline(self, theme: str | None = None) -> list[dict[str, Any]]:
        """期限のあるマイルストーンを、案件ごとに日付順に並べる（見通し）。"""
        out = []
        for th in ([theme] if theme else self.themes()):
            mx = self.matrix(th)
            for row in mx["rows"]:
                for m, c in zip(mx["milestones"], row["cells"]):
                    if c["due_key"]:
                        out.append({"theme": th, "project": row["project"]["id"], "project_name": row["project"]["name"],
                                    "milestone": m["id"], "key": m["key"], "title": m["title"], "due": c["due"],
                                    "due_key": c["due_key"], "state": c["state"], "overdue": c["overdue"],
                                    "current": row["current"] == m["id"]})
        return sorted(out, key=lambda x: (x["due_key"], x["theme"], x["project"]))

    def set_project_due(self, project: str, mid: int, due: str) -> None:
        if due and not due_key(due):
            raise ValueError(f"期限「{due}」を読めません（2026 / 2026-12 / 2026-12-31 / 2027年3月 など）")
        self.db.execute("INSERT INTO project_milestones(project, milestone, due, updated_at) VALUES(?,?,?,?)"
                        " ON CONFLICT(project, milestone) DO UPDATE SET due=excluded.due, updated_at=excluded.updated_at",
                        (project, mid, due, now()))
        self.db.commit()

    def counts(self) -> dict[str, Any]:
        c = {k: 0 for k in KINDS}
        for r in self.db.execute("SELECT kind, COUNT(*) FROM items WHERE status IN ('open','held') AND archived=0 GROUP BY kind"):
            c[r[0]] = r[1]
        unclassified = self.db.execute("SELECT COUNT(*) FROM items WHERE classified_at IS NULL AND archived=0").fetchone()[0]
        return {"open": c, "unclassified": unclassified,
                "items": self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                "projects": self.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]}

    def brief(self, project: str) -> str:
        """次の会議に渡す、案件の現状（Markdown）。"""
        p = self.db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        if p is None:
            raise KeyError(project)
        out = [f"# 案件「{p['name'] or project}」の現状（台帳より）"]
        mx = self.matrix(p["theme"])
        row = next((r for r in mx["rows"] if r["project"]["id"] == project), None)
        if mx["milestones"] and row:
            cur = next((m for m in mx["milestones"] if m["id"] == row["current"]), None)
            out += ["", "## マイルストーン（テーマ「" + p["theme"] + "」共通）"]
            lab = {"done": "済", "doing": "進行中", "todo": "未着手", "empty": "項目なし", "na": "対象外"}
            for m, c in zip(mx["milestones"], row["cells"]):
                mark = " ← 現在地" if cur and m["id"] == cur["id"] else ""
                out.append(f"- {m['key']} {m['title']}: {lab.get(c['state'], c['state'])}（{c['done']}/{c['total']}）{mark}")

        def sec(title: str, kind: str, extra: str = "") -> None:
            its = self.items(project=project, kind=kind, status="open")
            if not its:
                return
            out.extend(["", f"## {title}"])
            for it in its[:30]:
                line = f"- [{it['uid']}] {it['text']}"
                if extra == "rationale" and it["rationale"]:
                    line += f"（根拠: {it['rationale']}）"
                if extra == "premise":
                    line = f"- [{it['uid']}] {it['rationale'] or it['text']}"
                if extra == "needs" and it["needs"]:
                    line += f" → 分かればよいこと: {it['needs']}"
                if it.get("executable"):
                    line += f" ［{EXECUTABLE.get(it['executable'], it['executable'])}］"
                out.append(line)

        sec("前提", "premise", "premise")
        sec("決定（未実現）", "decision", "rationale")
        sec("不明点", "unknown", "needs")
        sec("協議事項", "discussion", "needs")
        sec("未決（未分類）", "question")
        sec("ToDo", "todo")
        bl = self.blocked(project=project)
        if bl:
            out += ["", "## できないこととその理由"]
            for it in bl[:20]:
                out.append(f"- [{it['uid']}] {it['text']} ← " + "、".join(f"[{b['dst']}] {b['text'] or ''}" for b in it["blockers"]))
        return "\n".join(out) + "\n"


    def context_pack(self, project: str | None = None, open_only: bool = False) -> str:
        """AI に渡すための一括 Markdown（コンテキストパック）。案件を省くと全案件。

        見出しの階層と固定フィールドだけで書く: 案件 → 会議 → 議題 → 種類 → 項目。各項目に ID・状態・ボール
        （次に誰が動く番か）・理由を並べ、画面のどのビューを見ているかに頼らずに読めるようにする。
        """
        pids = [project] if project else [p["id"] for p in self.q("SELECT id FROM projects ORDER BY theme, id")]
        parts = [self._pack_project(pid, open_only, legend=bool(project)) for pid in pids]
        if not project:
            head = [f"# 台帳コンテキストパック（全 {len(pids)} 案件）", "", f"> 生成 {now()}", PACK_LEGEND]
            parts = ["\n".join(head)] + [p.replace("\n#", "\n##").replace("# ", "## ", 1) for p in parts]
        return "\n\n".join(parts) + "\n"

    def _pack_project(self, project: str, open_only: bool, legend: bool = True) -> str:
        p = self.db.execute("SELECT * FROM projects WHERE id=?", (project,)).fetchone()
        if p is None:
            raise KeyError(project)
        its = self.items(project=project, status="open" if open_only else None, limit=100000)
        its.sort(key=lambda i: (i["meeting_date"] or "", i["meeting"] or "", i["topic"] or 0, i["local_id"] or ""))
        blockers = {i["uid"]: self.blockers(i["uid"]) for i in its if i["status"] in ("open", "held")}
        for i in its:
            i["ball_now"] = ball_of(i, bool(blockers.get(i["uid"])))
        live = [i for i in its if i["status"] in ("open", "held")]
        counts = {b: sum(1 for i in live if i["ball_now"] == b) for b in ("self", "ai", "external", "blocked", "held")}
        out = [f"# 案件「{p['name'] or project}」（{project}）", "",
               f"> テーマ: {p['theme'] or '—'} / 生成 {now()} / 未完了 {len(live)} 件（"
               + "・".join(f"{BALL_LABEL[b]} {n}" for b, n in counts.items() if n) + "）"]
        if legend:
            out.append(PACK_LEGEND)

        mx = self.matrix(p["theme"]) if p["theme"] else {"milestones": [], "rows": []}
        row = next((r for r in mx["rows"] if r["project"]["id"] == project), None)
        if mx["milestones"] and row:
            out += ["", "## マイルストーン"]
            lab = {"done": "済", "doing": "進行中", "todo": "未着手", "empty": "項目なし", "na": "対象外"}
            for m, c in zip(mx["milestones"], row["cells"]):
                extra = (f" / 期限 {c['due']}" + ("（遅れ）" if c["overdue"] else "") if c["due"] else "")
                out.append(f"- {m['key']} {m['title']} | 状態: {lab.get(c['state'], c['state'])}（{c['done']}/{c['total']}）{extra}"
                           + (" ← 現在地" if row["current"] == m["id"] else ""))

        # ボール別の索引: AI が「いま自分が進められるもの」を最初に切り分けられるように
        out += ["", "## ボール別の未完了（索引）"]
        for b in ("ai", "self", "external", "blocked", "held"):
            sel = [i for i in live if i["ball_now"] == b]
            if not sel:
                continue
            out += ["", f"### {BALL_LABEL[b]}（{len(sel)}）"]
            for i in sel:
                why = ""
                if b == "blocked":
                    why = " ← 待ち: " + "、".join(x["dst"] for x in blockers[i["uid"]])
                out.append(f"- [{i['uid']}] {_short(i['text'])}（{KIND_LABEL.get(i['kind'], i['kind'])}）{why}")
        if not live:
            out.append("- （なし）")

        groups = (("決定事項", ("decision",)), ("未決事項", ("unknown", "discussion", "question")),
                  ("ToDo", ("todo",)), ("前提", ("premise",)))
        cur_meeting, cur_topic = None, None
        for i in its:
            if i["meeting"] != cur_meeting:
                cur_meeting, cur_topic = i["meeting"], None
                out += ["", f"## 会議 {i['meeting_date'] or ''} {i['meeting']}"]
                for t in sorted({x["topic"] for x in its if x["meeting"] == cur_meeting}, key=lambda t: t or 0):
                    tits = [x for x in its if x["meeting"] == cur_meeting and x["topic"] == t]
                    title = next((x["topic_title"] for x in tits if x["topic_title"]), "")
                    out += ["", f"### 議題{t}: {title}" if t else "### 議題外"]
                    for label, kinds in groups:
                        sel = [x for x in tits if x["kind"] in kinds]
                        if sel:
                            out += [f"#### {label}"] + [_pack_item(x, blockers.get(x["uid"]) or []) for x in sel]
        sup = self.q("SELECT d.src, d.dst FROM deps d JOIN items i ON i.uid=d.dst WHERE d.rel='supersedes' AND i.project=?", (project,))
        if sup:
            out += ["", "## 統合・置き換え"] + [f"- [{r['dst']}] → [{r['src']}] に統合" for r in sup]
        return "\n".join(out)


PACK_LEGEND = ("> 読み方: 項目の ID は `会議ID:ローカルID`（画面では `#/ledger/item/<ID>` で開ける）。"
               "ボール＝次に誰が動く番か: 本人（ユーザーの判断・作業が要る）／AI（AI が進められる）／外部待ち／"
               "依存待ち（ほかの項目が済むまで進めない）／保留。")
_STATE = {"decision": {"open": "未実現", "done": "実現済み"}}


def _one(text: str) -> str:
    return " ".join((text or "").split())


def _short(text: str, n: int = 60) -> str:
    t = _one(text)
    return t if len(t) <= n else t[:n] + "…"


def ball_of(it: dict[str, Any], blocked: bool) -> str:
    """次に誰が動く番か。手で付けた値 → 閉じた・保留・依存待ち → 担当 → 種類 の順で決める。"""
    if it.get("status") in CLOSED:
        return "none"
    if it.get("ball") in BALL_MANUAL:
        return it["ball"]
    if it.get("status") == "held":
        return "held"
    if blocked:
        return "blocked"
    if it.get("kind") == "premise":
        return "none"
    if it.get("kind") == "decision" and it.get("executable") != "auto":
        return "none"  # 方針の決定は誰かの番ではない（実行することは ToDo か、機械的に実行できる決定に出る）
    owner = (it.get("owner") or "").strip()
    if owner in HUMAN_OWNERS:
        return "self"
    if owner in EXTERNAL_OWNERS:
        return "external"
    if owner:
        return "ai"  # 参加者の id（古い会議）。AI がやるものに担当は要らない
    if it.get("kind") in ("discussion", "question") or it.get("executable") == "judgment":
        return "self"
    return "ai"


def _pack_item(it: dict[str, Any], blockers: list[dict[str, Any]]) -> str:
    state = _STATE.get(it["kind"], {}).get(it["status"]) or STATUS_LABEL.get(it["status"], it["status"])
    fields = [f"状態: {state}"]
    if it["ball_now"] != "none":
        fields.append(f"ボール: {BALL_LABEL[it['ball_now']]}")
    if it.get("milestone_key"):
        fields.append(f"段階: {it['milestone_key']}")
    if it.get("executable"):
        fields.append(EXECUTABLE.get(it["executable"], it["executable"]))
    lines = [f"- [{it['uid']}] {_one(it['text'])}", "  - " + " | ".join(fields)]
    if it.get("rationale"):
        lines.append(f"  - {'要点' if it['kind'] == 'premise' else '理由'}: {_one(it['rationale'])}")
    if it.get("needs"):
        lab = {"unknown": "分かればよいこと", "discussion": "決める相手・内容"}.get(it["kind"], "完了条件")
        lines.append(f"  - {lab}: {_one(it['needs'])}")
    if blockers:
        lines.append("  - 待ち: " + "、".join(f"[{b['dst']}] {_one(b['text'] or '')}" for b in blockers))
    if it.get("status_note"):
        lines.append(f"  - メモ: {_one(it['status_note'])}")
    return "\n".join(lines)


# ---------------------------------------------------------------------- 会議からの取り込み

def _project_of(d: Path) -> tuple[str, str, str | None, str]:
    """会議ディレクトリから (案件id, 名前, テーマ, 設定ファイル) を決める。"""
    cfg: dict[str, Any] = {}
    try:
        cfg = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        pass
    meeting = cfg.get("meeting") or {}
    src = ""
    if (d / "source_config").exists():
        src = (d / "source_config").read_text(encoding="utf-8").strip()
    pid = meeting.get("project") or (Path(src).stem if src else None)
    if not pid:
        # 設定ファイルが分からない会議は、ディレクトリ名から日付と連番を除いたもの
        parts = d.name.split("-")
        rest = parts[3:] if len(parts) > 3 and parts[0].isdigit() else parts
        if rest and rest[-1].isdigit():
            rest = rest[:-1]
        pid = "-".join(rest) or d.name
    return pid, meeting.get("name") or pid, meeting.get("theme"), src


def sync_meeting(lg: Ledger, d: Path) -> int:
    """1会議を取り込む。新しく入った項目の数を返す。"""
    from ..storage import archive

    recs = archive.records(d)
    if not recs:
        return 0
    sm = archive.summarize(d, recs)
    pid, name, theme, src = _project_of(d)
    alias = pid
    pid = lg.resolve_project(pid)
    added = 0
    date = (sm.get("started") or "")[:10]
    topics = {t["no"]: t["title"] for t in sm["topics"]}
    with lg.tx():
        if pid == alias:
            lg.upsert_project(pid, name, theme, src)
        for src_kind, key in (("decision", "decisions"), ("question", "open_questions"), ("todo", "todos")):
            for it in sm[key]:
                note = it.get("note") or ""
                status = "done" if it.get("done") else ("held" if note.startswith("保留") else "open")
                added += lg.upsert_item({
                    "uid": f"{d.name}:{it['id']}", "project": pid, "meeting": d.name, "meeting_date": date,
                    "topic": it.get("topic"), "topic_title": topics.get(it.get("topic"), ""), "local_id": it["id"],
                    "src_kind": src_kind, "text": it["text"], "owner": it.get("owner"), "status": status,
                    "status_note": note, "archived": bool(sm.get("archived"))})
        # 議題の前提（premise）を「前提」として入れる。延長の議題は自動生成の前提なので除く
        cfg = {}
        try:
            cfg = yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            pass
        tlist = (cfg.get("meeting") or {}).get("topics") or []
        for no, title in topics.items():
            t = tlist[no - 1] if 0 < no <= len(tlist) else None
            premise = (t.get("premise") if isinstance(t, dict) else "") or ""
            if premise.strip() and not title.startswith("【延長】"):
                added += lg.upsert_item({
                    "uid": f"{d.name}:P-{no:03d}", "project": pid, "meeting": d.name, "meeting_date": date, "topic": no,
                    "topic_title": title, "local_id": f"P-{no:03d}", "src_kind": "premise", "text": " ".join(premise.split()),
                    "archived": bool(sm.get("archived"))})
    return added


def sync(lg: Ledger, meeting_dirs: Iterable[Path]) -> dict[str, int]:
    """会議ディレクトリ群（の下の会議）をすべて取り込む。"""
    n_meet = n_new = 0
    for base in meeting_dirs:
        if not base.is_dir():
            continue
        cands = [base] if (base / "transcript.jsonl").exists() else sorted(p for p in base.iterdir() if (p / "transcript.jsonl").exists())
        for d in cands:
            n_new += sync_meeting(lg, d)
            n_meet += 1
    lg.db.execute("INSERT OR REPLACE INTO meta VALUES('synced_at', ?)", (now(),))
    lg.db.commit()
    return {"meetings": n_meet, "new_items": n_new}


def topic_context(d: Path) -> dict[int, dict[str, Any]]:
    """分類の根拠に使う、議題ごとの要約・合意点・対立点（transcript から）。"""
    from ..storage import archive

    m = archive.minutes_from(archive.records(d), d.name)
    return {no: {"title": s.title, "summary": s.summary, "agreements": s.agreements, "disagreements": s.disagreements}
            for no, s in m.topics.items()}


def dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def sync_agenda(lg: Ledger, path: str | Path) -> dict[str, int]:
    """議題ファイルのテーマ・案件・マイルストーン（期限つき）を台帳に入れる。議題ファイルが正本。"""
    from ..agenda import load

    ag = load(path)
    src = f"agenda:{Path(path).resolve()}"
    n_p = n_m = 0
    with lg.tx():
        for th in ag["themes"]:
            for p in th["projects"]:
                pid = lg.resolve_project(p["id"])
                if lg.db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone() is None:
                    lg.db.execute("INSERT INTO projects(id, name, theme, config, theme_manual, updated_at) VALUES(?,?,?,?,1,?)",
                                  (pid, p["name"], th["name"], f"{Path(path).resolve()}#{p['id']}", now()))
                else:
                    lg.db.execute("UPDATE projects SET name=?, theme=?, theme_manual=1, config=?, updated_at=? WHERE id=?",
                                  (p["name"], th["name"], f"{Path(path).resolve()}#{p['id']}", now(), pid))
                n_p += 1
            keys = []
            for i, m in enumerate(th["milestones"]):
                keys.append(m["key"])
                lg.db.execute("INSERT INTO milestones(theme, ord, key, title, criteria, status, source, due) VALUES(?,?,?,?,?, 'active', ?, ?)"
                              " ON CONFLICT(theme, key) DO UPDATE SET ord=excluded.ord, title=excluded.title, criteria=excluded.criteria,"
                              " status='active', source=excluded.source, due=excluded.due",
                              (th["name"], (i + 1) * 10, m["key"], m["title"], m["criteria"], src, m["due"]))
                n_m += 1
            # 議題ファイルから消えたマイルストーン（議題ファイル由来のもの）は消す
            for r in lg.q("SELECT id, key FROM milestones WHERE theme=? AND source=?", (th["name"], src)):
                if r["key"] not in keys:
                    lg.db.execute("UPDATE items SET milestone=NULL WHERE milestone=?", (r["id"],))
                    lg.db.execute("DELETE FROM project_milestones WHERE milestone=?", (r["id"],))
                    lg.db.execute("DELETE FROM milestones WHERE id=?", (r["id"],))
            # 案件ごとの期限（議題ファイルの「マイルストーン期限」）
            for p in th["projects"]:
                for key, due in (p.get("milestone_due") or p.get("マイルストーン期限") or {}).items():
                    r = lg.db.execute("SELECT id FROM milestones WHERE theme=? AND key=?", (th["name"], str(key))).fetchone()
                    if r:
                        lg.db.execute("INSERT INTO project_milestones(project, milestone, due, updated_at) VALUES(?,?,?,?)"
                                      " ON CONFLICT(project, milestone) DO UPDATE SET due=excluded.due",
                                      (lg.resolve_project(p["id"]), r["id"], str(due), now()))
    return {"projects": n_p, "milestones": n_m}


def find_agendas(dirs: Iterable[Path]) -> list[Path]:
    """ディレクトリ直下の議題ファイル（テーマ: の一覧を持つ YAML）。"""
    from ..agenda import is_agenda

    out = []
    for d in dirs:
        if not Path(d).is_dir():
            continue
        for f in sorted(list(Path(d).glob("*.yaml")) + list(Path(d).glob("*.yml"))):
            try:
                if is_agenda(yaml.safe_load(f.read_text(encoding="utf-8"))):
                    out.append(f.resolve())
            except Exception:  # noqa: BLE001
                continue
    return out
