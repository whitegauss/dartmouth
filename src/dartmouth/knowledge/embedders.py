"""埋め込み（ベクトル化）。どれも課金0円で手元で動く。

- fastembed: CPU で動く ONNX のモデル（既定）。初回だけモデルをダウンロードする
- ollama: Ollama の埋め込みモデル（bge-m3 など）。Ollama を入れた機械で使う
- hash: 文字の並びのハッシュ（テスト用。モデル不要）
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path

import numpy as np

DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CACHE_DIR = Path(os.environ.get("DARTMOUTH_CACHE", Path.home() / ".cache" / "dartmouth")) / "fastembed"


class Embedder:
    name = "base"

    def embed(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def id(self) -> str:
        return self.name


class FastEmbedEmbedder(Embedder):
    name = "fastembed"

    def __init__(self, model: str | None = None):
        self.model_name = model or DEFAULT_FASTEMBED_MODEL
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise RuntimeError("資料庫のベクトル検索には fastembed が要ります: uv tool install --editable '.[kb]' --force "
                                   "（開発中なら uv sync --extra kb）") from e
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(self.model_name, cache_dir=str(CACHE_DIR))
        return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.array(list(self._load().embed(texts)), dtype=np.float32)

    def id(self) -> str:
        return f"fastembed:{self.model_name}"


class OllamaEmbedder(Embedder):
    name = "ollama"

    def __init__(self, model: str = "bge-m3", url: str = "http://127.0.0.1:11434"):
        self.model, self.url = model, url.rstrip("/")

    def embed(self, texts: list[str]) -> np.ndarray:
        body = json.dumps({"model": self.model, "input": texts}).encode()
        req = urllib.request.Request(f"{self.url}/api/embed", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read())
        return np.array(data["embeddings"], dtype=np.float32)

    def id(self) -> str:
        return f"ollama:{self.model}"


class HashEmbedder(Embedder):
    """文字の2-gram と3-gram をハッシュして数える。モデルなしで動く（テストやお試し用）。"""

    name = "hash"

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            t = t.lower()
            for n in (2, 3):
                for j in range(len(t) - n + 1):
                    h = int(hashlib.md5(t[j:j + n].encode()).hexdigest()[:8], 16)
                    out[i, h % self.dim] += 1
        return out

    def id(self) -> str:
        return f"hash:{self.dim}"


def make_embedder(backend: str, model: str | None = None, url: str | None = None) -> Embedder:
    if backend == "fastembed":
        return FastEmbedEmbedder(model)
    if backend == "ollama":
        return OllamaEmbedder(model or "bge-m3", url or "http://127.0.0.1:11434")
    if backend == "hash":
        return HashEmbedder()
    raise ValueError(f"unknown embedder backend: {backend}")
