"""dartmouth run / resume / web / export / init"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import load_config


def _run(session, ui: str, headless: bool, port: int | None = None, hosts: list[str] | None = None) -> int:
    if ui == "web" and not headless:
        from .ui.web import DEFAULT_PORT, run_web

        return asyncio.run(run_web(session, port=port or DEFAULT_PORT, extra_hosts=hosts))
    if headless or ui == "plain":
        from .ui.plain import run_plain

        try:
            return asyncio.run(run_plain(session, interactive=not headless))
        except KeyboardInterrupt:
            print(f"\n中断しました。再開: dartmouth resume {session.dir}")
            return 130
    from .ui.tui import run_tui

    return run_tui(session)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dartmouth", description="複数のAIエージェントと会議をして議事録を残す")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="会議を開始する")
    run.add_argument("-c", "--config", default="config.yaml", help="議題・会議設定の YAML（既定: config.yaml）")
    run.add_argument("-a", "--agents", default=None, help="議長・配役・バックエンド設定の YAML（別指定する場合）")
    run.add_argument("--chair", choices=["argon", "opus"], default=None, help="議長モデルを明示指定（argon または opus）")
    run.add_argument("--chair-effort", choices=["low", "medium", "high"], default=None, help="議長の推論 effort（low / medium / high）")
    run.add_argument("--group", default=None, help="議題グループ名で絞って実行する（topic.group）")
    run.add_argument("--ui", choices=["tui", "plain", "web"], default="tui")
    run.add_argument("--port", type=int, help="Web UI のポート（既定 8765）")
    run.add_argument("--host", action="append", help="Web UI で追加で待ち受けるアドレス（0.0.0.0 は不可）")
    run.add_argument("--headless", action="store_true", help="TUIなしで議題リストを流し、承認は保留にして終える")
    run.add_argument("--dummy", action="store_true", help="全参加者をダミーに差し替える（動作確認用）")

    resume = sub.add_parser("resume", help="中断した会議を再開する")
    resume.add_argument("meeting_dir")
    resume.add_argument("--ui", choices=["tui", "plain", "web"], default="tui")
    resume.add_argument("--port", type=int)
    resume.add_argument("--host", action="append")
    resume.add_argument("--headless", action="store_true")

    ext = sub.add_parser("extend", help="終わった会議を開き直し、指定した議題を延長して議論させる（総括も書き直す）")
    ext.add_argument("meeting_dir")
    ext.add_argument("topic", type=int, help="延長する議題の番号（議事録の「議題N」）")
    ext.add_argument("--note", default="", help="延長で議論してほしいこと（申し送りに足して、最優先で扱わせる）")
    ext.add_argument("--ui", choices=["tui", "plain", "web"], default="tui")
    ext.add_argument("--port", type=int)
    ext.add_argument("--host", action="append")
    ext.add_argument("--headless", action="store_true")

    export = sub.add_parser("export", help="議事録から要件定義書の下書きを書き出す")
    export.add_argument("meeting_dir")

    web = sub.add_parser("web", help="Web UI だけを起動し、画面から会議を始める・再開する")
    web.add_argument("-c", "--config", action="append", default=[],
                     help="画面で選べる設定ファイル（参加者の構成）。複数指定できる")
    web.add_argument("--config-dir", action="append", default=[],
                     help="設定ファイルを探す場所（直下の *.yaml）。既定はカレントディレクトリ・examples/・local/examples/")
    web.add_argument("--port", type=int)
    web.add_argument("--host", action="append")
    web.add_argument("--dev", action="store_true",
                     help="開発モード: 画面のファイルの変更でブラウザを自動で再読み込み、Python の変更でサーバーを再起動")

    its = sub.add_parser("items", help="未解決の未決事項と未完了の ToDo を一覧する（アーカイブした会議は除く）")
    its.add_argument("--meetings-dir", default="meetings")
    its.add_argument("--owner", help="担当で絞る（例: user）")
    its.add_argument("--all", action="store_true", help="アーカイブした会議と、保留にした項目も含める")
    its.add_argument("--done", nargs=2, metavar=("会議", "ID"), help="項目を済みにする（--note で理由）")
    its.add_argument("--hold", nargs=2, metavar=("会議", "ID"), help="項目を保留にする（一覧から外す。--note で理由）")
    its.add_argument("--note", default="")

    arc = sub.add_parser("archive", help="会議をアーカイブする（一覧と「決定・未決・ToDo」から隠す。記録と集計は残す）")
    arc.add_argument("meeting_dirs", nargs="+")
    arc.add_argument("--reason", default="")
    arc.add_argument("--undo", action="store_true", help="アーカイブを外す")

    kbp = sub.add_parser("kb", help="資料庫（関連資料を探して会議に渡す仕組み）を作る・試しに検索する")
    kbsub = kbp.add_subparsers(dest="kb_command", required=True)
    kb_build = kbsub.add_parser("build", help="索引を作る・更新する（変わったファイルだけ）")
    kb_build.add_argument("-c", "--config", required=True)
    kb_search = kbsub.add_parser("search", help="資料庫を検索して、会議で渡される抜粋を確かめる")
    kb_search.add_argument("-c", "--config", required=True)
    kb_search.add_argument("query")
    kb_search.add_argument("-k", type=int, default=6)

    models = sub.add_parser("models", help="使えるモデルを一覧する（gemini: Gemini API のキーで使えるもの）")
    models.add_argument("provider", choices=["gemini"])

    stats = sub.add_parser("stats", help="会議の呼び出し実績を、参加者×ラウンドごとに集計する（CSV も書ける）")
    stats.add_argument("meeting_dirs", nargs="+")
    stats.add_argument("--csv", help="集計行を書き出す CSV ファイル")
    stats.add_argument("--seats", action="store_true", help="ペルソナ×経路（モデル）の成績（座った回数・議長の採点・トークン）を出す")
    stats.add_argument("--cost", action="store_true", help="単価表（pricing.yaml）から実金額 cost_usd 列を足す（手元で見る用）")
    stats.add_argument("--public", action="store_true",
                       help="公開用: モデル名を抽象プロファイル（規模帯/effort ×単価比）に、金額を相対コスト cost_rel に、会議IDを S01… に置き換える")
    stats.add_argument("--pricing", help="単価表のファイル（既定: 環境変数 DARTMOUTH_PRICING か、個人用ディレクトリの pricing.yaml）")

    lint = sub.add_parser("lint-personas", help="ペルソナ定義（5ブロック標準テンプレート）を検査する。違反があれば終了コード1")
    lint.add_argument("dirs", nargs="*", default=["prompts/personas"], help="ペルソナのディレクトリかファイル（既定 prompts/personas）")
    lint.add_argument("--max-tokens", type=int, default=None, help="1ファイルのトークン上限（既定 350）")
    lint.add_argument("--stats", action="store_true", help="全ファイルのトークン数と、未移行ファイルの平均を出す")

    exp = sub.add_parser("export-md", help="全会議・全アイテム（決定・未決・ToDo）を1つの Markdown に書き出す（AI に渡す用。読み取りだけ）")
    exp.add_argument("--out", help="書き出し先（省略時は標準出力）。例: exports/context-pack.md")
    exp.add_argument("--meetings-dir", action="append", default=[], help="会議を探す場所（既定: meetings/ と個人用の meetings/）")
    exp.add_argument("--db", help="台帳のファイル（既定: 個人用ディレクトリの ledger.sqlite。なければ台帳の情報なしで出す）")
    leak = sub.add_parser("leakscan", help="公開リポジトリに個人の情報（パス・メール・鍵・固有語）が混ざっていないか調べる。あれば終了コード1")
    leak.add_argument("--staged", action="store_true", help="コミットしようとしている内容だけ（pre-commit 用）")
    leak.add_argument("--terms", help="固有語リストのファイル（省略時は環境変数 LEAKSCAN_TERMS、なければ個人用ディレクトリの leak_terms.txt）")
    leak.add_argument("--require-terms", action="store_true", help="固有語リストがなければ失敗にする")
    leak.add_argument("--max-ignores", type=int, default=5, help="leakscan:ignore の上限（既定5）")
    blind = sub.add_parser("blind", help="議事録からモデル名・ペルソナ名・条件名を伏せる（残れば終了コード1）")
    blind.add_argument("target", help="会議のディレクトリ（minutes.md を使う）か、議事録のファイル")
    blind.add_argument("-o", "--output", help="書き出し先（省略時は標準出力）")
    blind.add_argument("--personas-dir", help="伏せるペルソナ名を集めるディレクトリ（既定 prompts/personas）")
    blind.add_argument("--term", action="append", default=[], help="追加で伏せる語（何度でも）")

    from .ledger.cli import add_parser as add_ledger_parser

    add_ledger_parser(sub)

    init = sub.add_parser("init", help="examples/dummy.yaml をひな形にして config.yaml を作る")
    init.add_argument("dest", nargs="?", default="config.yaml")


    args = parser.parse_args(argv)

    def _load(path: str, agents_path: str | None = None, chair: str | None = None, effort: str | None = None):
        from .config import apply_chair_override

        p = Path(path)
        if not p.exists():
            hint = "dm init でひな形を作るか、" if path == "config.yaml" else ""
            raise SystemExit(f"設定ファイル {path} がありません。{hint}-c で設定ファイルを指定してください"
                             "（例: dm run -c examples/dummy.yaml --ui web）")
        if agents_path and not Path(agents_path).exists():
            raise SystemExit(f"議長・参加者設定ファイル {agents_path} がありません。")
        if not agents_path:
            import yaml

            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict) and "participants" not in raw and "extends" not in raw:
                for cand in (p.parent / "agents.yaml", Path("agents.yaml")):
                    if cand.exists():
                        agents_path = str(cand)
                        break
        cfg = load_config(path, agents_path=agents_path)
        return apply_chair_override(cfg, chair=chair, effort=effort)

    if args.command == "run":
        from .session import create_session

        cfg = _load(args.config, args.agents, args.chair, args.chair_effort)
        if args.group:
            picked = [t for t in cfg.meeting.topics if t.group == args.group]
            if not picked:
                avail = sorted({t.group for t in cfg.meeting.topics if t.group})
                raise SystemExit(f"グループ '{args.group}' の議題が見つかりません（候補: {', '.join(avail) or 'なし'}）")
            cfg.meeting.topics = picked
        if args.dummy:
            from .config import ParticipantConfig

            for n, p in list(cfg.participants.items()):
                base = p.model_dump(include={"role", "display_name", "timeout_s", "optional", "enabled", "share_scope", "retries", "color", "on_failure"})
                cfg.participants[n] = ParticipantConfig.model_validate({**base, "adapter": "dummy", "delay_s": [0.3, 1.5]})
        sess = create_session(cfg, headless=args.headless, source_config=None if args.dummy else args.config)
        if args.ui != "web":
            print(f"会議ディレクトリ: {sess.dir}", file=sys.stderr)
        raise SystemExit(_run(sess, args.ui, args.headless, args.port, args.host))
    if args.command == "resume":
        from .session import resume_session

        from .storage.lock import MeetingLocked

        try:
            sess = resume_session(args.meeting_dir, headless=args.headless)
        except MeetingLocked as e:
            raise SystemExit(str(e)) from None
        raise SystemExit(_run(sess, args.ui, args.headless, args.port, args.host))
    if args.command == "extend":
        from .session import extend_session
        from .storage.lock import MeetingLocked

        try:
            sess = extend_session(args.meeting_dir, args.topic, args.note, headless=args.headless)
        except (MeetingLocked, ValueError, FileNotFoundError) as e:
            raise SystemExit(str(e)) from None
        raise SystemExit(_run(sess, args.ui, args.headless, args.port, args.host))
    if args.command == "web":
        from .ui.web import DEFAULT_PORT, Hub, run_server

        configs = args.config
        for c in configs:
            _load(c)  # 起動前に設定ファイルを検証する
        from .paths import personal_dir

        pdir = personal_dir()
        defaults = [d for d in (Path("."), Path("examples"), Path("local/examples"), pdir, pdir / "examples") if d.is_dir()]
        dirs = [Path(d) for d in args.config_dir] or defaults
        mdirs = [d for d in (Path("meetings"), pdir / "meetings") if d.is_dir()] or [Path("meetings")]
        hub = Hub([Path(c) for c in configs], mdirs, dirs)
        found = hub.configs
        where = ", ".join(str(d) for d in hub.config_dirs) or "探す場所なし"
        print(f"画面で選べる設定ファイル: {len(found)} 件（{where} から）", file=sys.stderr)
        if not found:
            print("設定ファイルが見つからないので、画面から新しい会議は始められません（再開はできます）。"
                  "dm init で config.yaml を作るか、--config-dir で場所を指定してください", file=sys.stderr)

        async def _main() -> int:
            return await run_server(hub, port=args.port or DEFAULT_PORT, extra_hosts=args.host, dev=args.dev)

        raise SystemExit(asyncio.run(_main()))
    if args.command == "items":
        from .storage.archive import is_archived, save_item_status, summarize

        base = Path(args.meetings_dir)
        if args.done:
            d = base / args.done[0]
            save_item_status(d, args.done[1], True, args.note)
            print(f"{args.done[0]} {args.done[1]}: 済みにしました")
            return
        if args.hold:
            save_item_status(base / args.hold[0], args.hold[1], False, "保留: " + (args.note or "理由なし"))
            print(f"{args.hold[0]} {args.hold[1]}: 保留にしました")
            return
        for d in sorted(base.iterdir()):
            if not (d / "transcript.jsonl").exists() or (is_archived(d) and not args.all):
                continue
            sm = summarize(d)
            rows = [it for k in ("open_questions", "todos") for it in sm[k] if not it["done"]
                    and (not args.owner or (it.get("owner") or "") == args.owner)]
            held = [it for it in rows if (it.get("note") or "").startswith("保留")]
            rows = [it for it in rows if it not in held] if not args.all else rows
            if rows or held:
                print(f"## {d.name}" + (f"（ほかに保留 {len(held)} 件）" if held and not args.all else ""))
                for it in rows:
                    print(f"{it['id']} [{it.get('owner') or '-'}] {it['text'][:100]}")
        return
    if args.command == "archive":
        from .storage.archive import is_archived, set_archived

        for d in args.meeting_dirs:
            p = Path(d).resolve()
            if not (p / "transcript.jsonl").exists():
                print(f"会議ではありません: {d}", file=sys.stderr)
                continue
            set_archived(p, not args.undo, args.reason)
            print(f"{p.name}: {'アーカイブを外しました' if args.undo else 'アーカイブしました（' + (is_archived(p) or '') + '）'}")
        return
    if args.command == "kb":
        from .knowledge import build_knowledge_base

        cfg = _load(args.config)
        if not cfg.knowledge or not cfg.knowledge.sources:
            raise SystemExit(f"{args.config} に knowledge.sources がありません")
        kb = build_knowledge_base(cfg.knowledge, progress=lambda m: print(m, file=sys.stderr))
        if args.kb_command == "search":
            for i, c in enumerate(kb.search(args.query, args.k), 1):
                first = " ".join(c.text.split())[:160]
                print(f"{i}. {c.path}:{c.start}-{c.end}{'（非公開）' if c.private else ''}\n   {first}")
        return
    if args.command == "models":
        from .adapters.base import AdapterError
        from .adapters.gemini_api import api_key, list_models

        try:
            ms = list_models(api_key())
        except AdapterError as e:
            raise SystemExit(str(e))
        for m in ms:
            mid = m["name"].removeprefix("models/")
            print(f"{mid:45} 入力 {m.get('inputTokenLimit', '?'):>8} / 出力 {m.get('outputTokenLimit', '?'):>6}  {m.get('displayName', '')}")
        print("\n無料枠で使えるか・1日の回数の上限は、Google AI Studio のプロジェクトの画面で確かめてください", file=sys.stderr)
        return
    if args.command == "stats":
        import csv

        from .storage.archive import STAT_COLUMNS, seat_history, stat_rows

        if args.seats:
            dirs = sorted({Path(d).resolve().parent for d in args.meeting_dirs})
            hist = sorted(seat_history(dirs).values(), key=lambda h: (h["persona"], -(h["avg_score"] or 0)))
            print(f"{'ペルソナ':16} {'経路':12} {'モデル':28} {'座った':>4} {'採点':>5} {'Opus':>8} {'Argon':>8} {'採点数':>4} {'トークン/回':>10}")
            for h in hist:
                op = f"{h['opus_avg_score']}({h['opus_rated']})" if h.get("opus_avg_score") is not None else "—"
                ar = f"{h['argon_avg_score']}({h['argon_rated']})" if h.get("argon_avg_score") is not None else "—"
                print(f"{h['name'][:16]:16} {h['backend'][:12]:12} {str(h['model'] or '')[:28]:28} {h['seated']:>4} "
                      f"{h['avg_score'] if h['avg_score'] is not None else '—':>5} {op:>8} {ar:>8} {h['rated']:>4} {h['tokens_per_call'] or '—':>10}")
            return

        all_rows, totals = [], []
        for d in args.meeting_dirs:
            rows, total = stat_rows(Path(d).resolve())
            all_rows += rows
            totals.append(total)
        columns = list(STAT_COLUMNS)
        if args.cost or args.public:
            from .profiles import UnknownModel, load_pricing, private_stat_rows, public_stat_rows

            try:
                pricing = load_pricing(args.pricing)
                all_rows = public_stat_rows(all_rows, pricing) if args.public else private_stat_rows(all_rows, pricing)
            except (OSError, ValueError, UnknownModel) as e:
                raise SystemExit(f"エラー: {e}")
            columns.append("cost_rel" if args.public else "cost_usd")
        if args.public:
            # 会議ID・会議名・エラー本文は個人の用途が透けるので出さない。会議は登場順に S01, S02…
            ids = {m: f"S{i:02d}" for i, m in enumerate(dict.fromkeys(r["meeting"] for r in all_rows), 1)}
            for r in all_rows:
                r["meeting"] = ids[r["meeting"]]
                r["errors"] = sum((r.get("errors") or {}).values()) if isinstance(r.get("errors"), dict) else r.get("errors")
            totals = []
        for t in totals:
            print(f"■ {t['name']}（{t['meeting']}）: 呼び出し {t['calls']}回 · 呼び出しの合計 {t['elapsed_s']:.0f}秒（実時間 {t['wall_s']}秒）"
                  f" · トークン {t['tokens_total']:,}（cache read を除くと {t['tokens_excl_cache_read']:,}） · 失敗 {t['failures']} · 却下 {t['rejects']}"
                  f" · 議長のまとめ {t['chair_summary_s']:.0f}秒 · 議論役 {t['discussants_s']:.0f}秒 · 自己紹介のモデル呼び出し {t['intro_model_calls']}")
            ratio = t.get("context_ratio")
            print(f"  自動承認 {t['auto_approved']} · 席の自動補充 {t['seat_fallbacks']} · まとめの再要求 {t['summary_retries']}"
                  f" · ペルソナ出力の書式違反 {t['format_violations']}"
                  f" · 重複した資料請求 {t['kb_duplicates']} · 再試行で成功 {t['retried_ok']} · 空の応答 {t['empty_responses']}"
                  f" · プロンプトのうち資料由来 {'—' if ratio is None else f'{ratio:.0%}'}"
                  f"{'（50%超: ベクトル化の見直し対象）' if ratio and ratio > 0.5 else ''}")
        w = csv.DictWriter(open(args.csv, "w", newline="", encoding="utf-8") if args.csv else sys.stdout, fieldnames=columns, extrasaction="ignore")
        if not args.csv and totals:
            print()
        w.writeheader()
        w.writerows(all_rows)
        if args.csv:
            print(f"CSV: {args.csv}（{len(all_rows)} 行）")
        return
    if args.command == "export":
        from .session import export_meeting

        print(export_meeting(args.meeting_dir))
        return
    if args.command == "ledger":
        from .ledger.cli import run as run_ledger

        raise SystemExit(run_ledger(args))
    if args.command == "lint-personas":
        from .persona_lint import DEFAULT_MAX_TOKENS, lint as lint_personas

        raise SystemExit(lint_personas([Path(d) for d in args.dirs], args.max_tokens or DEFAULT_MAX_TOKENS, args.stats))
    if args.command == "export-md":
        from .export_md import export_markdown
        from .ledger.cli import default_meeting_dirs
        from .paths import ledger_path

        dirs = [Path(d) for d in args.meetings_dir] or default_meeting_dirs()
        text = export_markdown(dirs, Path(args.db) if args.db else ledger_path())
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8", newline="\n")
            print(f"{out} に書き出しました（{len(text)} 字）")
        else:
            sys.stdout.write(text)
        return
    if args.command == "leakscan":
        import os

        from .leakscan import main as leakscan
        from .paths import personal_dir

        terms = args.terms
        if not terms and not os.environ.get("LEAKSCAN_TERMS") and (personal_dir() / "leak_terms.txt").exists():
            terms = str(personal_dir() / "leak_terms.txt")
        argv = (["--staged"] if args.staged else []) + (["--terms", terms] if terms else []) \
            + (["--require-terms"] if args.require_terms else []) + ["--max-ignores", str(args.max_ignores)]
        raise SystemExit(leakscan(argv))
    if args.command == "blind":
        from .blind import run as run_blind

        raise SystemExit(run_blind(args.target, args.output, args.personas_dir, args.term))
    if args.command == "init":
        from .session import copy_example_config

        dest = Path(args.dest)
        if dest.exists():
            raise SystemExit(f"{dest} は既にあります")
        copy_example_config(dest)
        print(f"{dest} を作りました")
        return


if __name__ == "__main__":
    main()
