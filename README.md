# Loomix (ルーミックス) 🧵

国産SNS（Karotter / Yudetter / あめ→はれ / Pitter）の公開タイムラインをひとつに織り交ぜる、統合クライアント・リーダー（Step 1 MVP）。

## 特徴

- **4大国産SNSのタイムラインを時系列マージ**：
  - 🥕 **Karotter**（おすすめ・最新カロート）
  - 🥚 **Yudetter**（最新ユデート）
  - ☀️ **あめ→はれ**（最新投稿）
  - 🕊️ **Pitter**（最新ピット）
- **CORS回避のBFFアーキテクチャ**：各サービスのCORS制限を解決する軽量プロキシサーバーを内蔵。
- **並列フェッチ＆キャッシュ**：4つのサービスをマルチスレッドで並列取得し、キャッシュ（20秒）により高速応答＆相手先サーバーへの負荷を最小化。
- **レスポンシブなモダンWebUI**：
  - 各SNSごとのブランドカラー・アイコンバッジ
  - サービス別絞り込みタブ（すべて / 各SNS）
  - 投稿画像グリッド表示＆本文リンク自動化
  - 30秒ごとの自動更新トグル

## 起動方法

### ローカル起動

```bash
# 依存パッケージなし（Python 3.8+ 標準ライブラリのみで動作）
python3 server.py
```

ブラウザで `http://localhost:8080` を開きます。

環境変数 `PORT` でポート番号を変更可能です：
```bash
PORT=3000 python3 server.py
```

## API仕様

- `GET /api/timeline`
  - クエリパラメータ：
    - `service`: `all`（デフォルト） | `karotter` | `yudetter` | `amehare` | `pitter`
    - `refresh`: `1`（キャッシュを無視して強制取得）
  - レスポンス：
    ```json
    {
      "ok": true,
      "fetchedAt": "2026-11-09T09:15:00.000Z",
      "total": 80,
      "serviceCounts": {
        "karotter": 20,
        "yudetter": 20,
        "amehare": 20,
        "pitter": 20
      },
      "posts": [
        {
          "id": "karotter_2118129",
          "service": "karotter",
          "serviceName": "Karotter",
          "serviceIcon": "🥕",
          "badgeColor": "#f97316",
          "author": {
            "username": "daily_reishou",
            "displayName": "デイリー冷笑",
            "avatarUrl": "https://karotter.com/uploads/avatars/...",
            "profileUrl": "https://karotter.com/daily_reishou"
          },
          "content": "投稿本文...",
          "media": [],
          "createdAt": "2026-11-09T09:12:24.673Z",
          "url": "https://karotter.com/daily_reishou/status/p_2118129",
          "metrics": { "likes": 0, "reposts": 0, "replies": 0 }
        }
      ]
    }
    ```

- `GET /api/services`：接続対応サービス一覧

## ディレクトリ構成

```
work/loomix/
├── server.py           # BFFサーバー（Python標準ライブラリ）
├── public/             # フロントエンド
│   ├── index.html      # メイン画面
│   ├── style.css       # スタイル（ダークテーマ・レスポンシブ）
│   └── app.js          # タイムライン描画・自動更新ロジック
├── REPORT.md           # API・認証仕様の調査レポート
└── README.md           # 本ドキュメント
```
