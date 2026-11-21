"""設定ファイル（config.yaml）の読み込みと検証。"""

from __future__ import annotations

import os

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TopicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    premise: str = ""
    # 議題グループ（1つの YAML に複数の議題セットをまとめて抽出したいときに使う）
    group: str | None = None
    # 省略時は有効な全参加者。議長は常に参加する。
    participants: list[str] | None = None
    # 議題ごとに議長を替える場合に指定する。
    chair: str | None = None
    # 延長の議題のとき、元の議題の番号（エンジンが設定する）
    extension_of: int | None = None


class DoneTopic(BaseModel):
    """片付いた議題（承認された議題をエンジンが移す）。会議では使わない記録。"""

    model_config = ConfigDict(extra="allow")

    title: str
    meeting: str | None = None
    done_at: str | None = None


class ContextItem(BaseModel):
    """会議全体の前提資料。private=True のものは share_scope=all の参加者にだけ渡す。"""

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    file: str | None = None
    private: bool = False
    label: str | None = None
    # 渡すラウンド（例: [independent, summary]）。省略時は全ラウンド。大きな資料を毎ターン送らないため
    rounds: list[str] | None = None

    @model_validator(mode="after")
    def _one_source(self) -> "ContextItem":
        if (self.text is None) == (self.file is None):
            raise ValueError("context item needs exactly one of 'text' or 'file'")
        return self


class MeetingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "meeting"
    slug: str | None = None
    # 台帳での案件名とテーマ（省略時: 案件は設定ファイル名、テーマは案件名。テーマは台帳の画面でも変えられる）
    project: str | None = None
    theme: str | None = None
    purpose: str = ""
    topics: list[TopicConfig] = Field(default_factory=list)
    done_topics: list[DoneTopic] = Field(default_factory=list)  # 片付いた議題（画面には出さない）
    context: list[ContextItem] = Field(default_factory=list)
    critique_rounds: int = 1
    # False にすると議長は独立回答・相互批判に加わらず、論点整理とまとめだけを担当する
    chair_participates: bool = True
    # 議論が足りない議題の延長。議長がまとめで申し出て（申し送り必須）、承認されたものを
    # 全議題のあと、予算が残っていれば回す。延長した議題からさらに延長はしない
    extensions: bool = True
    max_extensions: int = 2
    # False にすると、エージェントに道具（Web 検索・ファイル読み込み）を使わせない。資料は資料庫から渡す
    agent_tools: bool = True
    # 道具の切り替えを1行で（off: 道具なし / read_only: 読み取り系の道具あり）。指定すると agent_tools より優先する
    tools: Literal["off", "read_only"] | None = None
    # 事前調査。chair にすると、議題ごとに独立回答の前に議長が Web で調べ、出典つきの調査メモを全員に渡す。
    # 資料にない事実（制度・料金・仕様・数値）に会議が弱いのを補う。tools: off でも議長の調査だけは Web 検索を使う
    research: Literal["off", "chair"] = "off"
    research_max_chars: int = 3000

    @field_validator("tools", mode="before")
    @classmethod
    def _tools_bool(cls, v: Any) -> Any:
        # YAML では off / on が真偽値として読まれるので、そのまま受け付ける
        if v is False:
            return "off"
        if v is True:
            return "read_only"
        return v

    @property
    def tools_enabled(self) -> bool:
        return self.tools != "off" if self.tools is not None else self.agent_tools
    # human: 議題ごとに人間が承認する / auto: 議長のまとめをそのまま承認して進む（人間はあとで取り下げられる）
    approval: Literal["human", "auto"] = "human"
    critique_parallel: bool = False
    display: Literal["as_arrived", "all_at_once"] = "as_arrived"
    # False にすると各ラウンドの後で /next を待つ。
    auto_advance: bool = True
    intro: bool = True
    language: str = "日本語"

    @field_validator("topics", mode="before")
    @classmethod
    def _topics_from_strings(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [{"title": t} if isinstance(t, str) else t for t in v]
        return v


class KnowledgeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # 設定ファイルからの相対パス
    glob: str = "**/*"
    label: str | None = None
    private: bool = False  # share_scope: public_only の参加者には渡さない


class EmbedderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["fastembed", "ollama", "hash"] = "fastembed"
    model: str | None = None
    url: str | None = None


class LocalLLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["ollama"] = "ollama"
    model: str
    url: str = "http://127.0.0.1:11434"


class KnowledgeConfig(BaseModel):
    """資料庫。議題に関係する部分を探して各ターンに渡す（エージェントに調べ物をさせない）。"""

    model_config = ConfigDict(extra="forbid")

    sources: list[KnowledgeSource] = Field(default_factory=list)
    embedder: EmbedderConfig = Field(default_factory=EmbedderConfig)
    index_dir: str = ".dartmouth-index"  # 設定ファイルからの相対パス
    top_k: int = 6  # 議題の開始時に渡す件数
    request_k: int = 3  # 【資料請求】1回で足す件数
    requests_per_topic: int = 8
    max_chars: int = 8000  # 1ターンに渡す関連資料の上限（文字数）
    # ローカル LLM（Ollama）。設定すると、上限を超えた関連資料を議題に沿って要約・圧縮する（考えさせない仕事）
    local_llm: LocalLLMConfig | None = None


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls: int = 40
    max_chars_per_message: int = 4000
    # 独立回答・相互批判ラウンドの会議全体での上限。
    max_rounds: int = 30
    # 会議全体のトークンの目安（任意）。今は延長を回すかどうかの判断にだけ使う（通常の議題は止めない）
    max_tokens: int | None = None
    # Claude の5時間枠の目安（直近5時間に全会議で使った、cache read を除くトークン）。超えたら議長は Claude の席を避ける
    claude_5h_tokens: int | None = None
    # アダプタごとの5時間枠の目安（例: {claude_code: 100000, antigravity_cli: 200000}）。超えたら議長はそのアダプタの席を避ける
    adapter_5h_tokens: dict[str, int] = Field(default_factory=dict)


class ParticipantConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    adapter: str
    role: str = "member"
    display_name: str | None = None
    timeout_s: float = 180
    optional: bool = False
    enabled: bool = True
    share_scope: Literal["all", "public_only"] = "all"
    retries: int = 1
    color: str | None = None
    # 公開用の抽象ID（例: chair-A、seat-H1）。dm stats --public で参加者IDの代わりに出す。実モデルとの対応はこの設定ファイル側にだけ置く
    public_id: str | None = None
    # 再試行しても失敗したとき: pause=会議を一時停止してユーザーに聞く / skip=そのラウンドは欠席で続行
    # 省略時は、議長なら pause、それ以外は skip
    on_failure: Literal["pause", "skip"] | None = None
    # ペルソナ（できること・できないこと・得意なこと）。書いておくと、自己紹介はモデルを呼ばずにこの文を流し、
    # 毎ターンの「あなたの立場」にも入る。persona_file は設定ファイルからの相対パス
    persona: str | None = None
    persona_file: str | None = None

    def persona_text(self) -> str | None:
        if self.persona:
            return self.persona.strip()
        if self.persona_file:
            return parse_persona(Path(self.persona_file))[1]
        return None

    def failure_policy(self) -> str:
        if self.on_failure:
            return self.on_failure
        return "pause" if self.role == "chair" and not self.optional else "skip"

    def options(self) -> dict[str, Any]:
        """アダプタ固有の追加設定（model, tools など）。"""
        return dict(self.model_extra or {})


def parse_persona(path: Path) -> tuple[dict[str, Any], str]:
    """ペルソナのファイル。先頭の --- で囲んだ部分（name, role, summary）と本文に分ける。"""
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            meta = yaml.safe_load(parts[1]) or {}
            text = parts[2]
    meta.setdefault("name", path.stem)
    meta.setdefault("role", "member")
    meta.setdefault("summary", "")
    return meta, text.strip()


class SeatingConfig(BaseModel):
    """議長が議題ごとに席を決める。ペルソナの一覧と、使えるモデル・経路の一覧から選ぶ。

    機密を見てよいかは経路（backends）の share_scope で決まる。議長の選び方に関係なく、
    public_only の経路の席には機密が伏せ字で届く。
    """

    model_config = ConfigDict(extra="forbid")

    personas: list[str]  # ペルソナの ID（personas_dir のファイル名、拡張子なし）
    personas_dir: str | None = None  # 省略時は prompts_dir/personas
    backends: dict[str, ParticipantConfig]  # 経路。adapter・model・share_scope・timeout_s・note など
    # 毎議題かならず座る席（例: [{persona: manager, backend: agy}]）。議長はこれに加えて min〜max 席を選ぶ
    fixed: list[dict[str, Any]] = Field(default_factory=list)  # backends: [..] でローテーション
    min_seats: int = 2
    max_seats: int = 3
    # 試行席の数。議長が選んだ席のうちこの数だけ、そのペルソナで試した回数の少ない経路（モデル）に替える（成績を集めるため）
    explore_seats: int = 0

    @model_validator(mode="after")
    def _check(self) -> "SeatingConfig":
        if not self.personas or not self.backends:
            raise ValueError("seating needs personas and backends")
        if not 1 <= self.min_seats <= self.max_seats:
            raise ValueError("seating: 1 <= min_seats <= max_seats")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    meeting: MeetingConfig = Field(default_factory=MeetingConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    participants: dict[str, ParticipantConfig]
    knowledge: KnowledgeConfig | None = None
    seating: SeatingConfig | None = None
    prompts_dir: str | None = None
    meetings_dir: str = "meetings"

    @model_validator(mode="after")
    def _check(self) -> "Config":
        enabled = {n for n, p in self.participants.items() if p.enabled}
        if not enabled:
            raise ValueError("no enabled participants")
        chairs = [n for n in enabled if self.participants[n].role == "chair"]
        if len(chairs) != 1:
            raise ValueError(f"exactly one enabled participant must have role 'chair' (found {chairs})")
        for t in self.meeting.topics:
            for n in (t.participants or []) + ([t.chair] if t.chair else []):
                if n not in self.participants:
                    raise ValueError(f"topic '{t.title}' references unknown participant '{n}'")
                if not self.participants[n].enabled:
                    raise ValueError(f"topic '{t.title}' references disabled participant '{n}'")
        if "user" in self.participants:
            raise ValueError("'user' is reserved")
        return self

    @property
    def chair(self) -> str:
        return next(n for n, p in self.participants.items() if p.enabled and p.role == "chair")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _resolve_raw_paths(data: dict[str, Any], base: Path) -> None:
    """Resolve relative paths in raw YAML dict against `base` (the file's directory)."""
    def _res(p: str) -> str:
        return p if Path(p).expanduser().is_absolute() else str((base / p).resolve())

    m = data.get("meeting")
    if isinstance(m, dict) and isinstance(m.get("context"), list):
        for item in m["context"]:
            if isinstance(item, dict) and isinstance(item.get("file"), str):
                item["file"] = _res(item["file"])
    parts = data.get("participants")
    if isinstance(parts, dict):
        for part in parts.values():
            if not isinstance(part, dict):
                continue
            ad = part.get("adapter")
            if isinstance(ad, str) and ".py:" in ad:
                fpath, cls = ad.rsplit(":", 1)
                part["adapter"] = f"{_res(fpath)}:{cls}"
            if isinstance(part.get("persona_file"), str):
                part["persona_file"] = _res(part["persona_file"])
            if isinstance(part.get("cwd"), str):
                part["cwd"] = _res(part["cwd"])
    st = data.get("seating")
    if isinstance(st, dict):
        if isinstance(st.get("personas_dir"), str):
            st["personas_dir"] = _res(st["personas_dir"])
        if isinstance(st.get("backends"), dict):
            for b in st["backends"].values():
                if isinstance(b, dict):
                    ad = b.get("adapter")
                    if isinstance(ad, str) and ".py:" in ad:
                        fpath, cls = ad.rsplit(":", 1)
                        b["adapter"] = f"{_res(fpath)}:{cls}"
                    if isinstance(b.get("cwd"), str):
                        b["cwd"] = _res(b["cwd"])
    kn = data.get("knowledge")
    if isinstance(kn, dict):
        if isinstance(kn.get("sources"), list):
            for src in kn["sources"]:
                if isinstance(src, dict) and isinstance(src.get("path"), str):
                    src["path"] = _res(src["path"])
        if isinstance(kn.get("index_dir"), str):
            kn["index_dir"] = _res(kn["index_dir"])
    if isinstance(data.get("prompts_dir"), str):
        data["prompts_dir"] = _res(data["prompts_dir"])
    if isinstance(data.get("meetings_dir"), str):
        data["meetings_dir"] = _res(data["meetings_dir"])


def _load_yaml_with_extends(path: Path, seen: set[Path] | None = None) -> dict[str, Any]:
    rp = path.resolve()
    seen = set(seen or ())
    if rp in seen:
        raise ValueError(f"circular extends: {rp}")
    seen.add(rp)
    data = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config must be a mapping: {rp}")
    ext = data.pop("extends", None)
    _resolve_raw_paths(data, rp.parent)
    if not ext:
        return data
    exts = [ext] if isinstance(ext, str) else list(ext)
    merged: dict[str, Any] = {}
    for e in exts:
        ep = Path(e).expanduser()
        if not ep.is_absolute():
            ep = rp.parent / ep
        merged = _deep_merge(merged, _load_yaml_with_extends(ep, seen))
    return _deep_merge(merged, data)


LOCAL_CONFIG = "config.local.yaml"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """dm run が読む設定ファイルを1つだけ決める: -c ＞ 環境変数 DARTMOUTH_CONFIG ＞ ./config.local.yaml。

    どれも無ければ案内を出して終了コード1で止まる（個人用の場所や別名のファイルには落とさない）。
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("DARTMOUTH_CONFIG")
    if env:
        if not Path(env).expanduser().exists():
            raise SystemExit(f"DARTMOUTH_CONFIG の設定ファイル {env} がありません。")
        return Path(env).expanduser()
    if Path(LOCAL_CONFIG).exists():
        return Path(LOCAL_CONFIG)
    raise SystemExit(
        "設定ファイルがありません。次のどれかで用意してください（DARTMOUTH_CONFIG ＞ ./config.local.yaml の順に読みます）:\n"
        f"  dm init                                  # ひな形（examples/dummy.yaml）から {LOCAL_CONFIG} を作る\n"
        "  export DARTMOUTH_CONFIG=/path/to/config.yaml")


def load_config(path: str | Path, agents_path: str | Path | None = None) -> Config:
    from .agenda import config_data, is_agenda, split_ref

    path, pid = split_ref(path)
    data = _load_yaml_with_extends(path)
    if is_agenda(data):
        # 議題ファイルの1案件（議題.yaml#homeserver）を、ふつうの設定ファイルの形にする
        data = config_data(data, pid)
        _resolve_raw_paths(data, path.resolve().parent)
    if agents_path is not None:
        ag = _load_yaml_with_extends(Path(agents_path))
        for k in ("participants", "seating", "budget"):
            if k in ag:
                data[k] = ag[k]
    cfg = Config.model_validate(data)
    base = path.parent
    # 相対パスは設定ファイルの場所から解決する（extends なしのデフォルト値などにも適用）。
    if cfg.prompts_dir and not Path(cfg.prompts_dir).is_absolute():
        cfg.prompts_dir = str((base / cfg.prompts_dir).resolve())
    if not Path(cfg.meetings_dir).is_absolute():
        cfg.meetings_dir = str((base / cfg.meetings_dir).resolve())
    return cfg


# 議長に選べるモデル。Gemini 系は agy（Antigravity CLI）の gemini-4-<名前>-<effort>
GEMINI_CHAIRS = ("neon", "argon")
CHAIRS = (*GEMINI_CHAIRS, "opus")


def apply_chair_override(cfg: Config, chair: str | None = None, effort: str | None = None) -> Config:
    """議長モデル（neon / argon / opus）と effort（low / medium / high）を明示指定で上書きする。"""
    if not chair and not effort:
        return cfg
    cid = cfg.chair
    cur = cfg.participants[cid]
    opts = cur.options()
    eff = effort or opts.get("effort")
    if not eff:
        m = str(opts.get("model") or "")
        eff = m.rsplit("-", 1)[-1] if any(m.startswith(f"gemini-4-{g}-") for g in GEMINI_CHAIRS) else "medium"
    if eff not in ("low", "medium", "high"):
        eff = "medium"

    target = chair
    if target is None:
        model = str(opts.get("model") or "")
        target = "opus" if cur.adapter == "claude_code" else next((g for g in GEMINI_CHAIRS if g in model), None)
    if target in GEMINI_CHAIRS:
        new_opts = {k: v for k, v in opts.items() if k not in ("effort",)}
        new_opts["model"] = f"gemini-4-{target}-{eff}"
        data = cur.model_dump(exclude_none=True)
        data.update(adapter="antigravity_cli", display_name=f"議長({target.capitalize()})", **new_opts)
        cfg.participants[cid] = ParticipantConfig.model_validate(data)
    elif target == "opus":
        new_opts = dict(opts)
        new_opts["model"] = "opus"
        new_opts["effort"] = eff
        data = cur.model_dump(exclude_none=True)
        data.update(adapter="claude_code", display_name="議長(Opus)", **new_opts)
        cfg.participants[cid] = ParticipantConfig.model_validate(data)
    return cfg


def dump_config(cfg: Config) -> str:
    return yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True), allow_unicode=True, sort_keys=False)
