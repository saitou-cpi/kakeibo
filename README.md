# MCP風ツールサーバ（家計簿）

個人用途で AI エージェントから呼べる “MCPサーバ風” の最小HTTPツール。

- 役割: 家計簿CSVの集計と要約を返す / Slackに通知を送る
- API: `/health`, `/read_csv`, `/summarize`, `/report`
- 前提CSV列: `計算対象,日付,内容,金額（円）,保有金融機関,大項目,中項目,メモ,振替,ID`

## セットアップ

1) 依存インストール

```
pip install -r requirements.txt
```

2) 環境変数を設定（`.env` を使う場合）

```
KAKEIBO_DIR=C:\\Users\\csnp0001\\Documents\\saito_app\\kakeibo\\csv
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
```

3) サーバ起動

```
uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```

4) 動作テスト

```
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/summarize -H "Content-Type: application/json" -d '{"month":"2025-08"}'
curl -X POST http://127.0.0.1:8000/report -H "Content-Type: application/json" -d '{"month":"2025-08","post_to_slack":true}'
```

## エンドポイント

- `/health` (GET): ヘルスチェックとCSVファイル一覧。
- `/read_csv` (POST): `{"filename":"foo.csv","limit":10}` でプレビュー取得。`filename` 省略でファイル一覧。
- `/summarize` (POST): `{"month":"YYYY-MM", "filename":"任意"}` 指定月の集計を返す。
- `/report` (POST): `{"month":"YYYY-MM","post_to_slack":true}` でSlackに要約を送信。

### Slack Slash Command（任意）

- `/slack/command` (POST, x-www-form-urlencoded): Slackのスラッシュコマンド用。
- 例: コマンドの `text` に「8月の収支を教えて」「先月の支出」などを入力。
- 応答: 呼び出しユーザーにエフェメラルで「収入/支出/収支」を返信。

設定手順（Slack側）
- Slack App を作成 → Slash Commands を追加（例: `/kakeibo`）。
- Request URL に `http(s)://<公開URL>/slack/command` を設定。
- Basic情報の「Signing Secret」を `.env` の `SLACK_SIGNING_SECRET` に設定。
-（代替・非推奨）Verification Token を `SLACK_VERIFICATION_TOKEN` に設定可能。
- ローカル動作は ngrok 等で 8000 を公開してテスト。

使い方（例）
- `/kakeibo 8月の収支を教えて` → `2025-08` と解釈して集計。
- `今月` / `先月` / `YYYY-MM` / `YYYY/MM` / `YYYY年M月` / `M月`（年は推定）をサポート。

## 仕様と安全設計

- 読み取り対象は `KAKEIBO_DIR` 配下のみ（パスバリデーション、`.csv` のみ）。
- デフォルトは読み取り専用。サーバ側で書き込みや任意HTTPは未実装。
- Slack送信は `SLACK_WEBHOOK_URL` が `https://hooks.slack.com/services/` で始まる場合のみ送信。
- CSVエンコーディング: `utf-8-sig` を優先、失敗時は `cp932` にフォールバック。
- 集計: `計算対象==1` の行を対象。`YYYY-MM` で月フィルタ。支出は負の金額を正に換算して合計。

## CSVサンプル (UTF-8)

```
計算対象,日付,内容,金額（円）,保有金融機関,大項目,中項目,メモ,振替,ID
1,2025/8/30,by Amazon 炭酸水 ラベルレス 1000ml ×15本 富士山の強炭酸水 バナジウム含有 ペットボトル 静岡県産 1L ボトル 割り材 販売: Amazon.co.jp,-1329,Amazon.co.jp,食費,食料品,,0,xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
1,2025/8/29,MYB,-362,イオンカード,食費,食費,,0,xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
0,2025/8/29,AMAZON.,-436,イオンカード,食費,食料品,,1,xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 備考

- 月指定は `YYYY-MM` のみ対応（例: `2025-08`）。
- Slack送信を行うには `SLACK_WEBHOOK_URL` を設定してください。
- Windows環境を想定し `Path` ベースで実装しています。
