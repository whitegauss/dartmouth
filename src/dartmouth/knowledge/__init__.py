"""資料庫: 文書・コード・過去の議事録から、議題に関係する部分を探してエージェントに渡す。

エージェントに道具（Web 検索・ファイル読み込み）を使わせると、何度も読み込みを繰り返して利用枠を大きく使う。
資料はこちらで探して渡し、エージェントには考えることだけを任せる。
"""

from .index import Chunk, KnowledgeBase, build_knowledge_base

__all__ = ["Chunk", "KnowledgeBase", "build_knowledge_base"]
