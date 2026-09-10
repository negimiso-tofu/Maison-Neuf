# Munder Difflin

AIコーディングエージェント（Claude／Codex）とGPT画像生成を「AI社員」に見立て、
仮想オフィスで働く様子を眺めるツール。メイド版。

体制：画像＝リュミエール（GPT）／設計＝クラリス（Claude）／構築＝Codex。

- 設計の詳細は [`DESIGN.md`](./DESIGN.md) を参照
- `preview.html` — 現行の暫定プレビュー（クラリス・リュミエールの2名のみ稼働）

## 動かし方

```
python -m http.server 8744
```

起動後、ブラウザで `http://localhost:8744/preview.html` を開く。
（`file://` で直接開くとアニメーションが動かないため、必ずサーバー経由で開くこと）
