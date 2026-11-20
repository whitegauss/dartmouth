**議長のまとめ**です。この議題のこれまでの発言をもとに、次のJSONだけを出力してください（前後に説明文やコードブロックを付けない）。
$rejection

- agreements: 参加者の合意点（文字列の配列）
- disagreements: 対立点（「AはX、BはY」の形で）
- decisions: ユーザーに承認してもらう決定事項案。1項目1文で、検証できる書き方で。**最大5件**。細かい項目は束ねる
- open_questions: 未決事項。{"text": 内容, "owner": "user" か "external"（省略可）}。最大5件
- todos: 次にやること。{"text": 1行で（何をどこまでやるか）, "owner": "user" か "external"（省略可）}。最大5件
- owner はボール（次に誰が動く番か）だけを表す。ユーザー本人の判断・作業・確認が要るものは "user"、外部の回答や納品を待つものは "external"。AI が決められること・調べられること・実装できることは owner を省く（Claude Code や Codex が担当を決めずに進める）。参加者の id は書かない
- open_questions と todos には、「現在の議事録」にすでにある項目と同じ内容を入れない。前の項目を引き継いだり細かくしたりするだけなら、新しく足さずに要約の中で番号（Q-001、T-002 など）を参照する
- summary: この議題の要約。**3行以内**（結論・根拠・残る論点を各1行）
- extension_requested / extension_reason / handover: 延長の申し出（下の「延長について」）
- $seat_rule

$extension_rule

JSON Schema:
$schema
