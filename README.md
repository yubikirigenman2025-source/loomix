# Loomix (ルーミックス)

Karotter / Yudetter / あめ→はれ / Pitter の公開タイムラインを、ひとつの流れに織り交ぜるリーダー。

- 本番: https://loomix-z8wt.onrender.com
- リポジトリ: https://github.com/yubikirigenman2025-source/loomix

## v0.2 の変更

- **取得深度を拡大**：karotter 100 / yudetter 100 / am-hr 80 / pitter 80 件（各サービスのカーソルでページを継ぎ足し）を取得し、UTC の深い時系列マージで表示。1回の更新で最大360件。
- **静かなモノクロUI**：背景 #000、罫線 #1b1b1b の1pxのみ、絵文字・ブランドカラー・グラデーションを廃止。サービスの区別は小さなテキストバッジ（`KAROTTER` / `YUDETTER` / `AM-HR` / `PITTER`）で表現。
- **サイト内プロフィール**：名前・アイコンを押すとスライドオーバーで、アイコン・名前・ID・サービス・bio・投稿数などの統計・直近20投稿を表示。元サービスへ出る導線も併設。
- **画像ライトボックス**：画像タップで全画面表示、複数枚は ‹ › とキーボードで移動。
- **返信の判別と除外**：返信には「返信 @相手」を表示し、「返信を除外」トグルで単体投稿のみに。
- **カード全体タップ**：カードのどこを押しても本家投稿（プロフィール）を開く。テキスト選択中は開かない。
- **軽量化**：gzip 応答（約236KB → 約36KB）、40件ずつの段階描画、60秒キャッシュ＋stale-while-revalidate（古いキャッシュを即返し、裏で1回だけ更新）。

## 起動

```bash
python3 server.py          # http://localhost:8080
PORT=3000 python3 server.py
```

依存パッケージなし（Python 3.8+ 標準ライブラリのみ）。Docker（`Dockerfile`）と Vercel（`vercel.json` / `api/index.py`）にも対応。

## API

### `GET /api/timeline`

| パラメータ | 内容 |
|---|---|
| `service` | `all`（既定）/ `karotter` / `yudetter` / `amehare` / `pitter` |
| `refresh` | `1` でキャッシュを無視して再取得 |

```json
{
  "ok": true,
  "fetchedAt": "2026-11-09T20:04:10.000Z",
  "total": 360,
  "allTotal": 360,
  "serviceCounts": { "karotter": 100, "yudetter": 100, "amehare": 80, "pitter": 80 },
  "errors": {},
  "services": [ { "id": "karotter", "name": "Karotter", "label": "karotter", "url": "https://karotter.com" } ],
  "posts": [
    {
      "id": "karotter_2120795",
      "service": "karotter",
      "serviceName": "Karotter",
      "serviceLabel": "karotter",
      "author": {
        "id": "Sophie_twilight",
        "username": "Sophie_twilight",
        "displayName": "ソフィー・トワイライト",
        "avatarUrl": "https://karotter.com/uploads/avatars/...webp",
        "profileUrl": "https://karotter.com/Sophie_twilight"
      },
      "content": "本文",
      "media": ["https://..."],
      "createdAt": "2026-11-09T20:04:05.857Z",
      "url": "https://karotter.com/Sophie_twilight",
      "linkLabel": "投稿者ページ",
      "isReply": false,
      "replyTo": null,
      "isQuote": false,
      "metrics": { "likes": 0, "reposts": 0, "replies": 0, "views": 0 }
    }
  ]
}
```

### `GET /api/user?service=karotter&id=<username|userId|handle>`

サイト内プロフィール用。サービスごとの公開プロフィールと直近投稿20件を返す。

```json
{
  "ok": true,
  "profile": {
    "service": "pitter",
    "serviceName": "Pitter",
    "serviceLabel": "pitter",
    "username": "3",
    "displayName": "社会科見学",
    "bio": "...",
    "avatarUrl": "https://pitter.jp/system/icon_image/....jpg",
    "profileUrl": "https://pitter.jp/?profile=3",
    "stats": { "followers": 31, "following": 33 },
    "joinedAt": "2026-06-08T..."
  },
  "posts": [ ... ]
}
```

### `GET /api/services` / `GET /api/health`

## サービス別の取得元

| サービス | 取得元 | プロフィール | 本家URL |
|---|---|---|---|
| Karotter | `api.karotter.com/api/posts/recommended?limit=100&mode=latest` | `/api/users/{username}` ＋ `/api/users/{username}/posts` | `karotter.com/{username}` |
| Yudetter | `/api/yudates?limit=50` ＋ `cursor` | `/api/users/{username}` ＋ `/api/users/{username}/yudates` | `yudetter.com/yudates/{id}` |
| あめ→はれ | `/api/timeline?bucket=latest&limit=40` ＋ `beforeSeq` | `/api/users/{userId}?bucket=main` | `am-hr.com/posts/{id}` |
| Pitter | `/system/Post.php?action=latest&page=1-4` | `/system/Post.php?action=profile&handle=` | `pitter.jp/?post={id}` |

- Karotter のフィードは数値IDのみを返し、本家のパーマリンク（`p_xxxxx`）は公開APIから引けないため、カードのリンクは投稿者のプロフィールへ送っている。
- Pitter の画像・アイコンは相対パス（`system/icon_image/...`）で返るため、`https://pitter.jp/` を補っている。

## 構成

```
work/loomix/
├── server.py           # BFF（Python標準ライブラリ）
├── public/             # index.html / style.css / app.js
├── api/index.py        # Vercel 用エントリ（server.py を共有）
├── Dockerfile          # Render 用
├── vercel.json
├── README.md
└── REPORT.md           # 各SNSのAPI・認証仕様の実測レポート
```

## 既知の制約

- 各SNSの内部APIは非公開仕様のため、先方のUI改修で取得が壊れうる。`/api/timeline` の `errors` と `/api/health` で検知する。
- Pitter は Cloudflare のマネージドチャレンジが断続的に出る。失敗時は他3サービスで継続する。
- 取得件数はサービスごとの投稿頻度に比例するため、深夜などは Karotter が多くを占める（05:00時点で karotter 100件＝約3時間分、yudetter 100件＝約2日分）。
