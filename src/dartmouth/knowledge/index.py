"""資料庫の索引（作る・更新する・探す）。

索引は設定ファイルの横の index_dir に置く（chunks.jsonl・vectors.npy・manifest.json）。
変わったファイルだけ埋め込み直す。検索はベクトル（意味の近さ）と文字の2-gram（言葉の一致）の順位を合成する。
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .chunker import chunk_text
from .embedders import Embedder, make_embedder

TEXT_SUFFIXES = {".py", ".md", ".markdown", ".txt", ".yaml", ".yml", ".toml", ".json", ".js", ".html", ".css", ".csv", ".sh"}
MAX_FILE_BYTES = 400_000


@dataclass
class Chunk:
    id: str  # "<label>/<相対パス>:<開始>-<終了>"
    source: str  # 資料の名前（label）
    path: str  # 表示用の相対パス
    start: int
    end: int
    text: str
    private: bool = False

    @property
    def cite(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


def _bigrams(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", text.lower())
    return [t[i:i + 2] for i in range(len(t) - 1)]


class KnowledgeBase:
    def __init__(self, index_dir: Path, embedder: Embedder):
        self.index_dir = Path(index_dir)
        self.embedder = embedder
        self.chunks: list[Chunk] = []
        self.vectors = np.zeros((0, 0), dtype=np.float32)
        self._df: Counter = Counter()
        self._tfs: list[Counter] = []

    # ---------------------------------------------------------------- 作る

    def update(self, sources: list[dict[str, Any]], progress=None) -> dict[str, int]:
        """sources: [{path, glob, label, private}]。変わったファイルだけ埋め込み直して保存する。"""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        manifest_p = self.index_dir / "manifest.json"
        old_manifest = json.loads(manifest_p.read_text()) if manifest_p.exists() else {}
        same_model = old_manifest.get("embedder") == self.embedder.id()
        old_files = old_manifest.get("files", {}) if same_model else {}
        old_chunks, old_vecs = self._load_raw() if same_model else ([], None)
        by_file: dict[str, list[int]] = {}
        for i, c in enumerate(old_chunks):
            by_file.setdefault(c["file"], []).append(i)

        files: dict[str, float] = {}
        keep_rows: list[int] = []
        new_chunks: list[dict[str, Any]] = []
        for src in sources:
            base = Path(src["path"])
            label = src.get("label") or base.name
            for f in sorted(base.glob(src.get("glob", "**/*"))):
                if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES or f.stat().st_size > MAX_FILE_BYTES:
                    continue
                if any(part.startswith(".") or part in ("__pycache__", "node_modules") for part in f.relative_to(base).parts):
                    continue
                key = str(f.resolve())
                mtime = f.stat().st_mtime
                files[key] = mtime
                if old_files.get(key) == mtime and key in by_file:
                    keep_rows += by_file[key]
                    continue
                try:
                    text = f.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                rel = str(f.relative_to(base))
                for rc in chunk_text(text, f.suffix.lower()):
                    new_chunks.append({"file": key, "source": label, "path": f"{label}/{rel}", "start": rc.start, "end": rc.end,
                                       "text": rc.text, "private": bool(src.get("private"))})
        kept = [old_chunks[i] for i in keep_rows]
        vecs_kept = old_vecs[keep_rows] if (old_vecs is not None and keep_rows) else None
        if new_chunks:
            if progress:
                progress(f"資料庫: {len(new_chunks)} 件のかたまりを埋め込みます（{self.embedder.id()}）")
            texts = [f"{c['path']}\n{c['text']}" for c in new_chunks]
            vecs_new = np.concatenate([self.embedder.embed(texts[i:i + 64]) for i in range(0, len(texts), 64)])
        else:
            vecs_new = None
        parts = [v for v in (vecs_kept, vecs_new) if v is not None]
        vectors = np.concatenate(parts) if parts else np.zeros((0, 1), dtype=np.float32)
        all_chunks = kept + new_chunks
        with open(self.index_dir / "chunks.jsonl", "w", encoding="utf-8") as fh:
            for c in all_chunks:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        np.save(self.index_dir / "vectors.npy", vectors)
        manifest_p.write_text(json.dumps({"embedder": self.embedder.id(), "files": files}, ensure_ascii=False))
        self._set(all_chunks, vectors)
        return {"files": len(files), "chunks": len(all_chunks), "embedded": len(new_chunks)}

    def _load_raw(self) -> tuple[list[dict[str, Any]], np.ndarray | None]:
        cp, vp = self.index_dir / "chunks.jsonl", self.index_dir / "vectors.npy"
        if not (cp.exists() and vp.exists()):
            return [], None
        chunks = [json.loads(l) for l in cp.read_text(encoding="utf-8").splitlines() if l.strip()]
        vecs = np.load(vp)
        return (chunks, vecs) if len(chunks) == len(vecs) else ([], None)

    def load(self) -> "KnowledgeBase":
        chunks, vecs = self._load_raw()
        self._set(chunks, vecs if vecs is not None else np.zeros((0, 1), dtype=np.float32))
        return self

    def _set(self, raw: list[dict[str, Any]], vectors: np.ndarray) -> None:
        self.chunks = [Chunk(id=f"{c['path']}:{c['start']}-{c['end']}", source=c["source"], path=c["path"], start=c["start"],
                             end=c["end"], text=c["text"], private=c.get("private", False)) for c in raw]
        norms = np.linalg.norm(vectors, axis=1, keepdims=True) if len(vectors) else None
        self.vectors = vectors / np.maximum(norms, 1e-9) if norms is not None else vectors
        self._tfs = [Counter(_bigrams(c.path + " " + c.text)) for c in self.chunks]
        self._df = Counter()
        for tf in self._tfs:
            self._df.update(tf.keys())

    # ---------------------------------------------------------------- 探す

    def get(self, chunk_id: str) -> Chunk | None:
        return next((c for c in self.chunks if c.id == chunk_id), None)

    def search(self, query: str, k: int = 6, allow_private: bool = True, exclude: Iterable[str] = ()) -> list[Chunk]:
        if not self.chunks or not query.strip():
            return []
        skip = set(exclude)
        idx = [i for i, c in enumerate(self.chunks) if (allow_private or not c.private) and c.id not in skip]
        if not idx:
            return []
        # ベクトル（意味の近さ）
        qv = self.embedder.embed([query])[0]
        qv = qv / max(float(np.linalg.norm(qv)), 1e-9)
        vs = self.vectors[idx] @ qv
        v_rank = {idx[i]: r for r, i in enumerate(np.argsort(-vs))}
        # 文字の2-gram の BM25（言葉の一致。関数名や設定名を確実に拾う）
        q = Counter(_bigrams(query))
        n = len(self.chunks)
        avg = sum(sum(tf.values()) for tf in self._tfs) / max(n, 1)
        bm = []
        for i in idx:
            tf, dl, s = self._tfs[i], sum(self._tfs[i].values()), 0.0
            for g in q:
                f = tf.get(g, 0)
                if f:
                    df = self._df[g]
                    s += math.log(1 + (n - df + 0.5) / (df + 0.5)) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * dl / max(avg, 1)))
            bm.append(s)
        b_rank = {idx[i]: r for r, i in enumerate(np.argsort(-np.array(bm)))}
        # 順位の合成（Reciprocal Rank Fusion）
        fused = sorted(idx, key=lambda i: -(1 / (60 + v_rank[i]) + 1 / (60 + b_rank[i])))
        return [self.chunks[i] for i in fused[:k]]


def build_knowledge_base(kcfg, base_dir: Path | None = None, progress=None) -> KnowledgeBase:
    """設定（config.KnowledgeConfig）から資料庫を作り、索引を最新にして返す。"""
    emb = make_embedder(kcfg.embedder.backend, kcfg.embedder.model, kcfg.embedder.url)
    kb = KnowledgeBase(Path(kcfg.index_dir), emb)
    sources = [{"path": s.path, "glob": s.glob, "label": s.label, "private": s.private} for s in kcfg.sources]
    stats = kb.update(sources, progress=progress)
    if progress:
        progress(f"資料庫: {stats['files']} ファイル、{stats['chunks']} 件（今回埋め込んだのは {stats['embedded']} 件）")
    return kb


def chunk_to_dict(c: Chunk) -> dict[str, Any]:
    return asdict(c)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0", "false")
