# Loomix (ルーミックス)

Karotter / Yudetter / あめ→はれ / Pitter / Amiverse のタイムラインを、ひとつの流れに織り交ぜるリーダー。

- 本番: https://loomix-z8wt.onrender.com
- リポジトリ: https://github.com/yubikirigenman2025-source/loomix

## v0.6 の変更: ブロック・ミュートの反映

連携している各SNSの「ブロック」「ミュート」「ミュートワード」を、Loomix のタイムラインに反映する（`safety.py`）。ブロック・ミュートした相手の投稿は表示せず、ミュートワードに一致する他人の投稿も隠す。本家の設定は変更しない（読み取りのみ）。

### サービス別の対応状況（2026-11-14 実測・クライアント解析）

| サービス | ブロック | ミュート | ミュートワード | 取得元 |
|---|---|---|---|---|
| Karotter | ○ | ○ | ○ | `GET /api/follow/block` / `/api/follow/mute`（`{users:[{id,username}]}`）、`GET /api/auth/me` の `mutedKeywords` |
| Yudetter | ○ | − | − | `GET /api/users/me/blocks`（要ログイン）。ミュート機能自体が無い |
| あめ→はれ | ○ | ○ | ○ | `GET /api/safety` の `blockedUsers` / `mutedUsers` / `muteWords` |
| Pitter | △ | − | − | 公式 openAPI の `action=me` に `blocked_handles` が含まれる場合のみ。一覧の正規経路は Web のセッションAPI（`system/Post.php?action=blocked_users`）で、openAPI からは届かない |
| Amiverse | × | × | × | Loomix の連携がID入力のみ（認証なし）のため、ユーザー固有の設定を取得できない |

### 仕組み

- **共有アーカイブ（`.cache/*.jsonl`）は触らない。** ブロック・ミュートは「応答を組み立てる段階」で投稿を間引くだけなので、他のユーザーや過去ログには影響しない（`Archive.newest/older_than` に `keep` 判定を渡している）。
- 対象リストは**ログイン時に取得**して `.cache/safety.json` に保存し、以後は10分より古くなると裏で取り直す（`/api/timeline` の応答は待たせない）。
- 取得に使う各SNSのセッション（Bearer / Cookie）は**メモリだけ**に置き、ディスクには書かない。サーバーが再起動してメモリが消えても、保存済みリストでフィルタは効き続ける（更新だけが次のログインまで止まる）。
- ラベルはハンドル（`@username`）とIDの両方で照合する。ミュートワードは大文字小文字を区別せず、**自分の投稿には適用しない**（本家の挙動に合わせる）。
- ユーザーとの紐付けは、セッショントークン内の `safetyKey`（16桁の乱数）だけ。リスト本体はサーバー側に置くので、Cookie は膨らまない。ログアウトで削除する。

### 追加API

| メソッド | パス | 内容 |
|---|---|---|
| `GET` | `/api/safety` | 反映状況（サービスごとの `status` / `blocked` / `muted` / `words` / `fetchedAt` / `error`、`canRefresh`） |
| `POST` | `/api/safety/refresh` | 保存済みセッションで全サービスを取り直す。材料が失効していれば `code: needs_relink` |

タイムライン応答にも `safety: {active, services:{...}, filtered:N}` が付く。設定画面には「ブロック・ミュート」欄（状態＋再取得）を置いた。

## v0.4 の変更

### トップページ（ゲート）と全SNSの認証必須化

- 未連携のときはトップページを表示し、**4つのSNSすべてに自分のアカウントで連携**しないと統合タイムラインに入れない。
- 各SNSのログイン画面と同じ経路で資格情報を検証する（`auth.py`）。

  | サービス | ログイン経路 | 入力 |
  |---|---|---|
  | Karotter | `POST https://api.karotter.com/api/auth/login` | メールアドレスまたはユーザー名＋パスワード |
  | Yudetter | `POST https://yudetter.com/api/auth/sign-in/email`（`Origin: https://yudetter.com` 必須） | メールアドレス＋パスワード |
  | あめ→はれ | `POST https://am-hr.com/api/auth/login`（`Origin: https://am-hr.com` 必須） | ユーザーIDまたはメール＋パスワード |
  | Pitter | `POST https://pitter.jp/system/openAPI.php?action=login` → `action=me` | メール＋パスワード＋APIキー（64桁hex） |

- **パスワードは保存しない**。検証に使うだけで、ディスクにもメモリにも残さない。
- 連携状況は **HMAC 署名したトークン**（`lx1.<claims>.<sig>`、60日）としてブラウザの localStorage にだけ置く。サーバーにセッションストアを持たないので、再デプロイや Free プランのスピンダウンでもログイン状態が残る（署名鍵 `LOOMIX_SESSION_SECRET` を固定した場合。未設定なら再起動で無効になり再連携）。
- `/api/timeline` と `/api/user` は連携が揃うまで **401 `auth_required`**。ゲートを通過できないとデータは取れない。
- 運用で必要なSNSだけにしたい場合は環境変数 `LOOMIX_AUTH_REQUIRED=karotter,pitter` のように指定する（既定は4つすべて）。署名鍵は `LOOMIX_SESSION_SECRET`、未指定なら `.cache/session-secret` を自動生成。

### サイドバーUI

- デスクトップ：左240pxの固定サイドバー（Loomixロゴ／ホーム・連携アカウント・プロフィール・設定／下部に連携アカウントのアイコンと「4 / 4 連携」・ログアウト）。
- スマホ（〜860px）：画面下部のボトムナビゲーションに切り替え。
- メインカラムは従来の統合タイムライン（無限スクロール・Lightbox・返信除外・60秒自動更新）をそのまま収容。

### 追加API

| メソッド | パス | 内容 |
|---|---|---|
| `GET` | `/api/auth/status` | セッションと連携状況（`Authorization: Bearer <token>`）。`linked` / `missing` / `gateOpen` / `providers` を返す |
| `POST` | `/api/auth/login` | `{service, identifier, password}` または Pitter は `{service, email, password, apiKey}`。成功で `token` と最新の `auth` |
| `POST` | `/api/auth/unlink` | `{service}` の連携を外す（4つ未満になるとタイムラインは閉じる） |
| `POST` | `/api/auth/logout` | セッションを破棄 |

## v0.3 の変更

- **無限スクロール**：一覧の底に近づくと `/api/timeline?before=<ISO8601|投稿ID>&limit=40` を1回だけ要求し、続きを差し込む。読み込み中は控えめな「読み込み中...」、履歴の終端では「これ以上はありません」を出す。
- **相手先に負荷をかけない設計**
  - **過去ログの永続アーカイブ**：一度取ったページは `.cache/{service}.jsonl` に追記して保持し、同じ過去範囲は外部APIを叩かず即返す。履歴の取得は「一生で1回」に近づく。
  - **カーソルで続きから**：Karotter は `cursor`（`pagination.nextCursor`）、Yudetter は `cursor`、あめ→はれは `beforeSeq`、Pitter は `page`。取得位置を覚えておき、次はそこから1ページだけ進める。
  - **レートリミット**：同一サービスへのリクエストは最低 0.5 秒間隔・毎分 60 回まで。同一サービスへの同時リクエストはロックで1本に絞る。
  - **差分同期**：新着は最新ページから取り込み、既知の投稿に当たった時点で止める。
  - **先読み**：次のページ分の余裕が減ったサービスだけ、裏で1ページだけ先に取る。
- **欠けないマージ**：全サービスが指定時刻より古い範囲を「40件以上」持っていることを確かめてから返す。届いていないときは空ページ＋`partial` を返し、クライアントが少し待って再要求する（黙って投稿を飛ばさない）。

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
| `before` | この時刻より古い投稿を返す。ISO8601（`2026-11-09T20:00:00Z`）または投稿ID（Loomix の `id`／先方の `rawId`）。省略すると最新 |
| `limit` | 返す件数（既定 40、最大 80） |
| `service` | `all`（既定）/ `karotter` / `yudetter` / `amehare` / `pitter` |
| `refresh` | `1` で新着同期を強制（最短60秒の間隔は守る） |

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
  ],
  "limit": 40,
  "nextBefore": "2026-11-09T20:00:00Z",
  "hasMore": true,
  "partial": false,
  "retryAfterMs": null,
  "archiveCounts": { "karotter": 320, "yudetter": 120, "amehare": 80, "pitter": 60 }
}
```

- `nextBefore` を次の `before` に渡すと続きが取れる（省略すると最新）。
- `partial: true` は「相手先の取得がまだ追いついていない」の意味。`retryAfterMs` だけ待って同じ `before` で再要求する。

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

### `GET /api/health`

バージョン、アーカイブ件数、各サービスの探索床（`floor`）・終端・カーソルの有無、レート制限の設定を返す。

## サービス別の取得元

| サービス | 取得元 | ページング | プロフィール | 本家URL |
|---|---|---|---|---|
| Karotter | `api.karotter.com/api/posts/recommended?limit=40&mode=latest` | `cursor`（`pagination.nextCursor`） | `/api/users/{username}` ＋ `/api/users/{username}/posts` | `karotter.com/{username}` |
| Yudetter | `/api/yudates?limit=40` | `cursor`（`nextCursor`） | `/api/users/{username}` ＋ `/api/users/{username}/yudates` | `yudetter.com/yudates/{id}` |
| あめ→はれ | `/api/timeline?bucket=latest&limit=40` | `beforeSeq` | `/api/users/{userId}?bucket=main` | `am-hr.com/posts/{id}` |
| Pitter | `/system/Post.php?action=latest&page=N`（20件/ページ） | `page` | `/system/Post.php?action=profile&handle=` | `pitter.jp/?post={id}` |

- Karotter のフィードは数値IDのみを返し、本家のパーマリンク（`p_xxxxx`）は公開APIから引けないため、カードのリンクは投稿者のプロフィールへ送っている。
- Pitter の画像・アイコンは相対パス（`system/icon_image/...`）で返るため、`https://pitter.jp/` を補っている。

## 構成

```
work/loomix/
├── server.py           # BFF（Python標準ライブラリ）
├── auth.py             # 各SNSのログイン検証とLoomixセッション
├── safety.py           # ブロック・ミュートの取得とタイムラインへの反映
├── test_safety.py      # 上記のオフライン検証（python3 test_safety.py）
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
- Karotter の一覧には、古い投稿のリポストが日付だけ古く混ざることがある。探索済みの床より下のものは表示せず、その時代まで読み進めたときに出す（ページの下端が飛ばないように）。
- アーカイブは `.cache/` に保存する。Render の無料プランは再起動・再デプロイでディスクが消えるため、そのときは初回 100/100/80/80 件から取り直す。
- 深く読むほど、全サービスの同じ時間帯を読む必要がある。Karotter は1ページ＝約1〜2時間、遅いサービスは1ページ数日〜数週間なので、深いほど先読みが効く。
- 連携認証も各SNSの非公開ログイン経路を使う。先方がログイン仕様（項目名・Origin検査・APIキー）を変えると、そのSNSだけ「連携できません」と表示される。`/api/auth/status` の `providers` と各ログイン失敗メッセージで検知する。
- 署名鍵は環境変数 `LOOMIX_SESSION_SECRET` に置く（未設定なら `.cache/session-secret` を自動生成。Render の無料プランは再起動でディスクが消えるため、その場合はトークンが無効になり再連携が必要）。
- ログアウト・連携解除はブラウザのトークンを破棄する。サーバーは資格情報を持たないので、発行済みの古いトークンは期限まで有効（ゲートは公開情報の入口を整える仕組みで、秘密の保護境界ではない）。
- ログイン試行は同一IPあたり5分で15回までに制限している（`X-Forwarded-For` の先頭を使用）。
- ブロック・ミュートの反映は、各SNSの内部APIが返す一覧に依存する。Yudetter の `/api/users/me/blocks` は応答の形が未実測のため寛容に解釈しており、形が変われば `/api/safety` の `status` が `error`/0件になる。Pitter・Amiverse は上表のとおり取得できない（本家では非表示でもLoomixには出る）。
- リポスト／引用は「投稿した人」で判定する。引用元の作者だけをブロックしている場合、Loomix は引用カードを別に描かないので追加の間引きはしていない。
