"""dm ledger: 台帳の取り込み・分類・参照。"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import CLOSED, DOMAINS, EXECUTABLE, KIND_LABEL, STATUS_LABEL, Ledger, find_agendas, sync, sync_agenda


def add_parser(sub: argparse._SubParsersAction) -> None:
    lp = sub.add_parser("ledger", help="台帳: 会議をまたいで決定・前提・不明点・協議事項・ToDo とマイルストーンを管理する（SQLite）")
    lp.add_argument("--db", help="台帳のファイル（既定: 個人用ディレクトリの ledger.sqlite）")
    lp.add_argument("--meetings-dir", action="append", default=[], help="会議を探す場所（既定: meetings/ と個人用の meetings/）")
    ls = lp.add_subparsers(dest="ledger_command", required=True)
    ls.add_parser("sync", help="会議の記録から台帳を更新する（AI は使わない）")
    c = ls.add_parser("classify", help="未分類の項目を AI でまとめて分類する（根拠・実行のしやすさ・SW/HW・マイルストーン・依存）")
    c.add_argument("-c", "--config", required=True, help="議長のアダプタを借りる設定ファイル")
    c.add_argument("-a", "--agents", help="議長・参加者の設定（別ファイルの場合）")
    c.add_argument("--project")
    c.add_argument("--batch", type=int, default=20, help="1回の呼び出しで分類する件数")
    c.add_argument("--max-batches", type=int, default=10, help="呼び出し回数の上限")
    c.add_argument("--all", action="store_true", help="分類済みもやり直す")
    o = ls.add_parser("organize", help="議長の判断で、案件をテーマにまとめ・同じ案件を統合し・テーマごとの共通マイルストーンを決める")
    o.add_argument("-c", "--config", required=True, help="議長のアダプタを借りる設定ファイル")
    o.add_argument("-a", "--agents")
    o.add_argument("--replace-milestones", action="store_true", help="既存のマイルストーンも置き換える")
    ap = ls.add_parser("apply", help="整理・分類の JSON をそのまま反映する（organize / classify と同じ形）")
    ap.add_argument("what", choices=["organize", "classify"])
    ap.add_argument("file")
    ap.add_argument("--by", default="manual", help="分類した人・モデルの名前（台帳に残す）")
    ap.add_argument("--replace-milestones", action="store_true")
    mg = ls.add_parser("merge", help="案件 alias を target に統合する")
    mg.add_argument("alias"); mg.add_argument("target")
    s = ls.add_parser("status", help="テーマ・案件ごとの現在地と、件数の概要")
    s.add_argument("--theme")
    li = ls.add_parser("list", help="項目を一覧する")
    li.add_argument("--project"); li.add_argument("--theme")
    li.add_argument("--kind", help="premise,decision,unknown,discussion,todo,question（カンマ区切り）")
    li.add_argument("--status", default="open", help="open（未完了と保留）/ done / held / dropped / superseded / all")
    li.add_argument("--exec", choices=list(EXECUTABLE), dest="executable")
    li.add_argument("-q", "--text", help="全文検索")
    sh = ls.add_parser("show", help="項目の詳細（根拠・依存・できるようになること）")
    sh.add_argument("uid")
    st = ls.add_parser("set", help="項目の状態を変える")
    st.add_argument("uid"); st.add_argument("status", choices=list(STATUS_LABEL)); st.add_argument("--note", default="")
    b = ls.add_parser("blocked", help="「これがないからこれができない」の一覧")
    b.add_argument("--project"); b.add_argument("--theme")
    r = ls.add_parser("ready", help="すぐ機械的に実行できる決定・ToDo")
    r.add_argument("--project"); r.add_argument("--theme")
    tl = ls.add_parser("timeline", help="見通し: 期限のあるマイルストーンを日付順に並べる（遅れているものに印）")
    tl.add_argument("--theme")
    m = ls.add_parser("matrix", help="テーマの共通マイルストーン × 案件の表")
    m.add_argument("theme")
    ms = ls.add_parser("milestone", help="共通マイルストーンを足す・直す・消す")
    ms.add_argument("action", choices=["list", "add", "set", "approve", "rm"])
    ms.add_argument("theme_or_id", help="list / add はテーマ、set / approve / rm はマイルストーンの id")
    ms.add_argument("--key"); ms.add_argument("--title"); ms.add_argument("--criteria"); ms.add_argument("--ord", type=int)
    th = ls.add_parser("theme", help="案件のテーマを変える")
    th.add_argument("project"); th.add_argument("theme")
    dp = ls.add_parser("dep", help="依存を足す・消す（src は dst がないとできない）。マイルストーンは m:<id>")
    dp.add_argument("action", choices=["add", "rm"]); dp.add_argument("src"); dp.add_argument("dst"); dp.add_argument("--note", default="")
    br = ls.add_parser("brief", help="案件の現状を Markdown で出す（次の会議の資料に使う）")
    br.add_argument("project")
    pk = ls.add_parser("pack", help="AI に渡す一括 Markdown（会議 → 議題 → 決定・未決・ToDo、各項目に状態・ボール・理由）")
    pk.add_argument("project", nargs="?", help="省くと全案件")
    pk.add_argument("--open", action="store_true", help="未完了と保留だけ")


def default_meeting_dirs() -> list[Path]:
    from ..paths import personal_dir

    return [d for d in (Path("meetings"), personal_dir() / "meetings") if d.is_dir()]


def _line(it: dict) -> str:
    tags = [KIND_LABEL.get(it["kind"], it["kind"])]
    if it["status"] != "open":
        tags.append(STATUS_LABEL.get(it["status"], it["status"]))
    if it.get("executable"):
        tags.append(EXECUTABLE[it["executable"]])
    if it.get("domain"):
        tags.append(DOMAINS.get(it["domain"], it["domain"]))
    if it.get("milestone_key"):
        tags.append(it["milestone_key"])
    return f"{it['uid']}  [{' / '.join(tags)}]  {it['text']}"


def run(args: argparse.Namespace) -> int:
    from ..paths import ledger_path

    lg = Ledger(args.db or ledger_path())
    mdirs = [Path(d) for d in args.meetings_dir] or default_meeting_dirs()
    cmd = args.ledger_command
    try:
        if cmd in ("sync", "classify", "organize", "status", "matrix", "timeline"):
            from ..paths import personal_dir

            for ag in find_agendas([personal_dir(), Path(".")]):
                sync_agenda(lg, ag)
        if cmd == "sync":
            r = sync(lg, mdirs)
            c = lg.counts()
            print(f"{r['meetings']} 会議を取り込み、新しい項目 {r['new_items']} 件（台帳 {lg.path}、全 {c['items']} 件・未分類 {c['unclassified']} 件）")
        elif cmd == "classify":
            from ..session import default_prompts_dir
            from .classify import chair_adapter, classify, meeting_dir_map

            sync(lg, mdirs)
            ad = chair_adapter(args.config, args.agents)
            tmpl = (default_prompts_dir() / "ledger_classify.md").read_text(encoding="utf-8")

            async def go():
                try:
                    return await classify(lg, ad, tmpl, meeting_dir_map(mdirs), project=args.project, batch_size=args.batch,
                                          max_batches=args.max_batches, reclassify=args.all,
                                          progress=lambda m: print(m, file=sys.stderr))
                finally:
                    await ad.close()

            r = asyncio.run(go())
            print(f"分類 {r['classified']} 件（呼び出し {r['batches']} 回、失敗 {r['failed']} 件、残り {r['remaining']} 件、"
                  f"マイルストーンの提案 {r['proposed_milestones']} 件）")
        elif cmd == "organize":
            from ..session import default_prompts_dir
            from .classify import chair_adapter, meeting_dir_map, organize

            sync(lg, mdirs)
            ad = chair_adapter(args.config, args.agents)
            tmpl = (default_prompts_dir() / "ledger_organize.md").read_text(encoding="utf-8")

            async def go_org():
                try:
                    return await organize(lg, ad, tmpl, meeting_dir_map(mdirs), replace_milestones=args.replace_milestones)
                finally:
                    await ad.close()

            r = asyncio.run(go_org())
            print(f"統合 {r['merged']} 件、テーマの変更 {r['themed']} 件、マイルストーン {r['milestones']} 件")
        elif cmd == "apply":
            import json

            from .classify import apply_classification_file, apply_organize

            data = json.loads(Path(args.file).read_text(encoding="utf-8"))
            if args.what == "organize":
                r = apply_organize(lg, data, replace_milestones=args.replace_milestones, source=args.by)
                print(f"統合 {r['merged']} 件、テーマの変更 {r['themed']} 件、マイルストーン {r['milestones']} 件")
            else:
                r = apply_classification_file(lg, data, args.by)
                print(f"分類 {r['classified']} 件（見つからない uid {r['missing']} 件、マイルストーンの提案 {r['proposed_milestones']} 件）")
        elif cmd == "merge":
            lg.merge_project(args.alias, args.target)
            lg.db.commit()
            print(f"{args.alias} → {args.target} に統合しました")
        elif cmd == "status":
            c = lg.counts()
            print("未完了: " + "、".join(f"{KIND_LABEL[k]} {v}" for k, v in c["open"].items() if v) + f"（未分類 {c['unclassified']} 件）")
            for th in ([args.theme] if args.theme else lg.themes()):
                mx = lg.matrix(th)
                names = {m["id"]: f"{m['key']} {m['title']}" for m in mx["milestones"]}
                print(f"\n## テーマ {th}" + (f"（提案中のマイルストーン {len(mx['proposed'])} 件）" if mx["proposed"] else ""))
                for row in mx["rows"]:
                    p = row["project"]
                    if not mx["milestones"]:
                        where = "マイルストーン未設定"
                    elif all(c["state"] == "empty" for c in row["cells"]):
                        where = "項目が段階に割り当てられていない"
                    else:
                        where = names.get(row["current"], "全段階済み")
                    blocked = sum(c["blocked"] for c in row["cells"])
                    print(f"- {p['id']}: 現在地 {where}" + (f"、止まっている項目 {blocked}" if blocked else "")
                          + (f"、段階未割り当ての未完了 {row['unassigned_open']}" if row["unassigned_open"] else ""))
        elif cmd == "list":
            its = lg.items(project=args.project, theme=args.theme, kind=args.kind,
                           status=None if args.status == "all" else args.status, executable=args.executable, text=args.text)
            for it in its:
                print(_line(it))
            print(f"（{len(its)} 件）", file=sys.stderr)
        elif cmd == "show":
            it = lg.item(args.uid)
            if not it:
                print(f"{args.uid} はありません"); return 1
            print(_line(it))
            for k, lab in (("rationale", "根拠"), ("needs", "分かればよいこと／決める相手／完了条件"), ("topic_title", "議題"), ("owner", "担当")):
                if it.get(k):
                    print(f"  {lab}: {it[k]}")
            for d in it["requires"]:
                mark = "済" if d["status"] in CLOSED else "未"
                print(f"  必要: [{mark}] {d['uid']} {d['text'] or ''}")
            for b in lg.blockers(args.uid):
                print(f"  ✗ できない理由: {b['dst']} {b['text'] or ''}")
            for u in lg.unlocks(args.uid):
                print(f"  → これが済むとできる: {u['uid']} {u['text']}")
        elif cmd == "set":
            lg.set_status(args.uid, args.status, args.note)
            _write_back(args.uid, args.status, args.note, mdirs)
            print(f"{args.uid}: {STATUS_LABEL[args.status]}")
        elif cmd == "blocked":
            for it in lg.blocked(project=args.project, theme=args.theme):
                print(_line(it))
                for b in it["blockers"]:
                    print(f"    ← ない: {b['dst']} {b['text'] or ''}")
        elif cmd == "ready":
            for it in lg.ready(project=args.project, theme=args.theme):
                print(_line(it))
        elif cmd == "timeline":
            rows = lg.timeline(args.theme)
            if not rows:
                print("期限のあるマイルストーンがありません（議題ファイルのマイルストーンに「期限: 2026-12」のように書く）")
            lab = {"done": "済", "doing": "進行中", "todo": "未着手", "empty": "項目なし", "na": "対象外"}
            for r in rows:
                mark = "⚠ 遅れ " if r["overdue"] else ("→ " if r["current"] else "  ")
                print(f"{mark}{r['due']:<10} {r['theme']} / {r['project']}  {r['key']} {r['title']}（{lab.get(r['state'], r['state'])}）")
        elif cmd == "matrix":
            mx = lg.matrix(args.theme)
            if not mx["milestones"]:
                print(f"テーマ {args.theme} にマイルストーンがありません（dm ledger milestone add {args.theme} --key M1 --title ...）")
                return 0
            lab = {"done": "■済", "doing": "▶中", "todo": "□未", "empty": "・", "na": "－"}
            print("案件".ljust(20) + "".join(m["key"].ljust(10) for m in mx["milestones"]))
            for row in mx["rows"]:
                print(row["project"]["id"][:19].ljust(20) + "".join(
                    (lab[c["state"]] + (f"{c['done']}/{c['total']}" if c["total"] else "")).ljust(10) for c in row["cells"]))
        elif cmd == "milestone":
            a, x = args.action, args.theme_or_id
            if a == "list":
                for m in lg.milestones(x):
                    print(f"{m['id']}  {m['key']}  {m['title']}" + (f"（{m['criteria']}）" if m["criteria"] else "")
                          + ("  ［提案中］" if m["status"] == "proposed" else ""))
            elif a == "add":
                if not (args.key and args.title):
                    print("--key と --title が要ります"); return 1
                print(lg.add_milestone(x, args.key, args.title, args.criteria or "", args.ord))
            elif a == "set":
                lg.update_milestone(int(x), {k: v for k, v in (("key", args.key), ("title", args.title),
                                                               ("criteria", args.criteria), ("ord", args.ord)) if v is not None})
            elif a == "approve":
                lg.update_milestone(int(x), {"status": "active"})
            elif a == "rm":
                lg.delete_milestone(int(x))
        elif cmd == "theme":
            lg.set_theme(args.project, args.theme)
        elif cmd == "dep":
            (lg.add_dep(args.src, args.dst, args.note) if args.action == "add" else lg.remove_dep(args.src, args.dst))
        elif cmd == "brief":
            print(lg.brief(args.project), end="")
        elif cmd == "pack":
            print(lg.context_pack(args.project, open_only=args.open), end="")
    except (KeyError, ValueError) as e:
        print(f"エラー: {e}")
        return 1
    except BrokenPipeError:  # head などで出力を途中で切られたとき
        sys.stderr.close()
        return 0
    finally:
        lg.close()
    return 0


def _write_back(uid: str, status: str, note: str, mdirs: list[Path]) -> None:
    """会議由来の決定・未決・ToDo は、会議の item_status.json にも反映する（「決定・未決・ToDo」画面と揃える）。"""
    from ..storage.archive import save_item_status

    meeting, _, local = uid.partition(":")
    if not local or local.startswith("P-"):
        return
    for base in mdirs:
        d = base / meeting
        if (d / "transcript.jsonl").exists():
            if status in ("done", "open"):
                save_item_status(d, local, status == "done", note)
            elif status == "held":
                save_item_status(d, local, False, "保留: " + (note or "理由なし"))
            return
