# 国産SNS API・認証仕様 調査レポート（Loomix 連携可否）

調査日：2026-11-09（JST）／調査者：ぴくる（サブエージェント）
調査方法：ブラウザ・HTTPクライアント（curl）・各サービスのフロントエンドJSバンドル読解・公開エンドポイントへの実リクエストによる実測。
（ログインが必要な書き込み系は実行せず、未認証リクエストの応答コードで存在と認証要否のみ確認。実データの投稿・いいね等は行っていません。）

---

## 1. 総括

**結論：Loomix は「ブラウザから各SNSのAPIを直接叩く」構成では成立しない。サーバー側プロキシ（BFF）方式が必須。**

理由（すべて実測）：

| サービス | 外部オリジンからのAPI呼び出し |
|---|---|
| Karotter | `api.karotter.com` は `Access-Control-Allow-Origin` を `https://karotter.com` / `https://karotter.jp` / `http://localhost:5173` / `capacitor://localhost` にしか返さない（外部オリジンではヘッダ無し） |
| Yudetter | 外部 `Origin` 付きリクエストは公開GETでも **403** |
| あめ→はれ | CORS ヘッダ自体が存在しない（`OPTIONS` も 404） |
| Amiverse | `api.amiverse.net` は `https://amiverse.net` のみ許可 |
| Pitter | CORS ヘッダ無し＋Cloudflare マネージドチャレンジが断続的に発動 |

そのうえで、**公式に外部クライアント向けAPIを公開しているのは Pitter だけ**（APIキー＋Bearer）。
Karotter は ActivityPub を名乗るが投稿は連合されず（outbox 空）、コンテンツ取得は非公開の内部RESTのみ。
Yudetter は本人が運営しているので、APIを自分で増やせる唯一のサービス。

---

## 2. 比較マトリクス

| 項目 | Karotter | Yudetter | あめ→はれ | Pitter | Amiverse |
|---|---|---|---|---|---|
| 運営ドメイン | karotter.com | yudetter.com | am-hr.com | pitter.jp（アプリ） / maine.pitter.jp（LP） | amiverse.net（API: api.amiverse.net） |
| 公式APIドキュメント | なし（/docs,/developer,/openapi.json はSPAのフォールバック） | なし | なし | **あり（アプリ内「設定→APIキー」に使用手順を明記）** | なし |
| 外部向け公式API | なし | なし（自作可能） | なし（内部に cross-origin 用の口あり） | **あり：`/system/openAPI.php`** | なし（OAuth自作の記載あるが公開エンドポイントは未確認） |
| ActivityPub | **一部実装**（nodeinfo/webfinger/actor あり／outbox 空で投稿は非連合） | なし | なし | なし | 記載あり（外部からは nodeinfo/webfinger 404 で確認できず） |
| RSS / WebSub / Webhook | なし | なし | なし | なし | なし |
| 内部APIの形式 | REST（`https://api.karotter.com/api/...`） | REST（同一オリジン `/api/...`） | REST（同一オリジン `/api/...`） | PHP（`system/*.php?action=...`） | Rails RPC（`POST /v1/...`） |
| 認証（人） | メール+パスワード / Google OAuth / Discord OAuth | メール or ユーザーID + パスワード / Google OAuth / メールOTP | ユーザーID or メール + パスワード | メール + パスワード（＋Turnstile系のbot対策は不明） | メール+パスワード（＋任意でANYUR連携） |
| セッション | HttpOnly Cookie（SameSite=Strict, Secure）＋ CSRFトークン／**Bearer も受理** | Cookie（Better Auth） | HttpOnly Cookie ＋ 独自ヘッダ `x-amehare-session-token` | PHPセッションCookie ＋ CSRFトークン | Rails Cookie（`_amiverse`）＋ `X-CSRF-Token` |
| 外部アプリ用トークン | 発行手段は非公開（Bearer は実装済み） | なし | ログイン応答の `sessionToken` をヘッダで利用可 | **APIキー＋Bearer（公式）** | なし |
| 公開読み取り（TL等） | 可（`/api/posts/recommended`, `/api/posts/{id}`, `/api/search` 等は無認証） | 可（`/api/yudates`, `/api/explore*`, `/api/rankings`） | 可（`/api/timeline`, `/api/posts/{id}`, `/api/users/{id}`） | 可（`Post.php?action=latest/global`）／公式APIは要トークン | 可（匿名セッション＋CSRFの2段階で `/v1/feeds/recommended`） |
| フォロー中TL | **要認証**（401） | 要認証 | 要認証 | 要トークン | 要認証 |
| 投稿（POST） | 要認証（401で存在確認） | 要認証 | 要認証 | 公式API `post` / `reply` / `quote` | 要CSRF＋セッション |
| いいね・リアクション | 要認証（`/posts/{id}/like`） | 要認証 | 要認証（`/posts/{id}/reaction-toggle`） | `like` / `unlike`（公式API） | `POST /v1/posts/{aid}/reaction` |
| レートリミット | `ratelimit-policy: 10000;w=60`（API全体・IP単位と推測） | ヘッダ無し（不明） | ヘッダ無し（不明） | 不明（Cloudflare側の制限はあり） | 不明 |
| 備考 | 16589ユーザー/106万投稿（nodeinfo）。CSPでAPIホスト4系統を許可（冗長化） | マーケット／ユードル（独自通貨）／ゲーム／ランキング等の独自APIが豊富 | 感情でTLを自動住み分け（`timelineBucket` main/sub、`classificationSource: ai_auto`） | ライブ配信（LiveKit）、投げ銭、団体バッジ等 | ドット絵（drawings）、AIチャット（Amis）、トレンド |

---

## 3. サービス別詳細

### 3.1 Karotter（https://karotter.com）

**構成**：React SPA（`/assets/index-*.js`、rolldown/Vite）＋ nginx ＋ Cloudflare。APIは別ホスト `https://api.karotter.com/api`。CSPの `connect-src` には `api.karotter.com` / `api.karotter.jp` / `api.karotter.net` / `apikarotter.karon.jp` が並び、冗長化・移行の痕跡。

**公式ドキュメント**：なし。`/docs` `/developer` `/openapi.json` `/rss` はいずれもSPAのHTML（200だが中身はアプリのシェル）。`developers.` `dev.` `docs.` サブドメインは名前解決不可。`robots.txt` は `Disallow: /api/`。

**公開API（無認証で実測 200）**
- `GET /api/posts/{id}` → `{"post":{...}}`
  例：`id`(数値), `parentId`, `federateExternally`, `content`, `mediaUrls[]`, `createdAt`(UTC ISO), `author{id,username,displayName,avatarUrl,...}`, `likesCount`/`rekarotsCount`/`repliesCount`/`viewsCount`, `visibility"PUBLIC"`, `reactionSummary[]`, `liked/rekaroted/bookmarked`（閲覧者フラグ）, `poll`, `isR18`, `canInteract`
- `GET /api/posts/recommended?limit=10&mode=algorithm|latest` → `{"posts":[...],"pagination":{"page":1,"limit":10,"mode":"algorithm","hasNext":true}}`
- `GET /api/users/{username}` → `{"user":{id,username,displayName,bio,avatarUrl,followersCount,followingCount,postsCount,level,experience,isBotAccount,isPrivate,...}}`（例：`/api/users/pickle_asst`）
- `GET /api/search?q=...`（ユーザー＋投稿、約27KB）／`/api/search/trending/topics?limit=5`
- `GET /api/users/recommended?limit=3`／`GET /api/communities`／`GET /api/social/link-preview?url=...`（OGP展開の代理）

**要認証（401 `{"error":"認証が必要です"}` で存在確認）**
- `GET /api/posts/timeline?limit=10&mode=latest&federation=mixed`（フォロー中TL。`federation` パラメータあり＝連合対応の設計）
- `POST /api/posts`、`POST /api/posts/{id}/like`、`/rekarot`、`/bookmark`
- ログイン状態で実測した周辺API：`/api/auth/me`, `/api/dm/unread/count`, `/api/notifications/unread/count`, `/api/social/stories`, `/api/social/circles`, `/api/communities/home-timelines`, `/api/birthdays/upcoming`, `/api/v2/feed/views`, `/api/radio/active`, `/api/legal/summary`

**認証**
- `POST /api/auth/login`：`identifier`（メールまたはユーザー名）＋ `password`。バリデーション文言から項目を確認（「メールアドレスまたはユーザー名を入力してください」→「パスワードを入力してください」）
- OAuth：`GET /api/auth/oauth/google/callback` は 302 で `https://karotter.com/login?oauthError=...` へ。登録画面の文言は「Google・Discordなら基本情報を自動入力／メールでも登録できます」→ Google と Discord の OAuth を実装
- CSRF：`GET /api/auth/csrf-token` → `{"csrfToken":"v1.<uuid>.<hmac>"}`
- リフレッシュ：`POST /api/auth/refresh-token` に本文でトークンを送ると **403「このクライアントではリフレッシュトークン本文送信は許可されていません」**。Webクライアントは HttpOnly Cookie 経由に限定されている
- Cookie：`karotter_device_id=...; HttpOnly; Secure; SameSite=Strict; Max-Age=31536000`
- **Bearer**：`Authorization: Bearer <不正値>` で `401 {"error":"無効なトークンです"}`（未認証時は「認証が必要です」）。＝Bearerトークン方式は実装済み（ネイティブアプリ向けと推測）だが、第三者アプリへの発行手順は非公開

**CORS（実測）**：`Access-Control-Allow-Origin` ＋ `Access-Control-Allow-Credentials: true` を返すのは `karotter.com` / `karotter.jp` / `localhost:5173` / `capacitor://localhost` のみ。`https://loomix.example` では ACAO なし（＝ブラウザ遮断）。

**ActivityPub**
- `/.well-known/nodeinfo` → `{"links":[{"rel":"http://nodeinfo.diaspora.software/ns/schema/2.1","href":"https://karotter.com/nodeinfo/2.1"}]}`
- `/nodeinfo/2.1` → `{"software":{"name":"karotter","version":"2.0.0"},"protocols":["activitypub"],"openRegistrations":true,"usage":{"users":{"total":16589},"localPosts":1063899}}`
- `/.well-known/webfinger?resource=acct:{user}@karotter.com` → JRD（self が `https://karotter.com/users/{user}`、`application/activity+json`）
- `GET /users/{user}`（`Accept: application/activity+json`）→ Actor JSON（`type":"Service"`（BOT時）, `inbox`/`outbox`/`followers`/`following`, `publicKey`, `endpoints.sharedInbox: https://karotter.com/inbox`）
- `GET /users/{user}/followers` → `OrderedCollection`（例：`totalItems:185`）
- **ただし `outbox` は `totalItems:0` / `orderedItems:[]`**。投稿JSONも `federateExternally:false` が既定。`/{user}/status/{shortId}` も HTML のみ
→ **身元（Actor）層だけがAP対応で、投稿は連合されない。AP経由でTLは読めない。**

**Webhook/RSS**：クライアントコード・公開エンドポイントのいずれにもWebhook機能は見つからず（サーバー側の送信機能の有無までは不明）。RSS/Atom/WebSubも無し。

**その他**：投稿は数値IDと短縮IDの二系統（例：`/api/posts/2118044` ↔ `/ChocolatyPurin/status/p_1psezw0`）。`/discover` は SSR されたSEOページで `before/{postId}` のカーソル式ページング。

---

### 3.2 Yudetter（https://yudetter.com）※ユーザー本人が開発・運営

**構成**：React SPA（Vite）＋ Express（`x-powered-by: Express`）＋ Cloudflare。APIは同一オリジン `/api/*`。

**公式ドキュメント**：なし（`/docs` 等はSPAのシェル）。`robots.txt` はCloudflareのContent-Signal（`search=yes, ai-train=no, use=reference`）付き。

**公開API（無認証で実測 200）**
- `GET /api/yudates?limit=...` → `{"items":[...],"nextCursor":70316}`（カーソル方式）
  投稿フィールド：`id`, `content`, `imageUrl`, `mediaUrls[]`, `embeddedHtml`, `author{id,clerkId,username,displayName,email(空),bio,avatarUrl,followerCount,followingCount,yudateCount,isVerified,yudedollar,equippedBadges,...}`, `likeCount`, `reyudateCount`, `replyCount`, `isLiked`, `reactions[]`, `poll`, `quotedYudate`, `replyToId`, `superYudateAmount`, `visibility:"public"`, `createdAt`(UTC ISO)
- `GET /api/explore`（約28KB）／`/api/explore/recommended?limit=`／`/api/explore/trends`（`[{"topic":"過疎","posts":"12"},...]`）／`/api/explore/search?q=`（約184KB、投稿＋ユーザー横断）／`/api/explore/mentions`
- `GET /api/users/{username}`（例 `/api/users/yudetamago`）／`GET /api/rankings`（daily/weekly、約58KB）

**要認証**：`GET /api/users/me` → 401 `{"error":"認証が必要です。ログインしてから再度お試しください。"}`

**認証（Better Auth）**
- `GET /api/auth/ok` → `{"ok":true}`、`GET /api/auth/get-session` → `null`（未ログイン時）
- クライアントコードに `/sign-in/email`、`/sign-in/email-otp`、`/sign-up/email`
- サインイン画面（実測）：**「Googleで続ける」／メールアドレスまたはユーザーID＋パスワード／パスワード再設定**
- セッションはCookie方式（`Authorization: Bearer` は受理されない：不正Bearerでも通常の401文言）

**内部APIの全量（バンドルから抽出）**：`/api/yudates`, `/api/yudates/{id}`, `/api/explore*`, `/api/users`, `/api/users/me`, `/api/users/me/follow-requests`, `/api/users/sign-out`, `/api/notifications`, `/api/notifications/read`, `/api/rankings`, `/api/rankings/opt-in|opt-out`, `/api/upload`, `/api/market/items`, `/api/games`, `/api/games/store`, `/api/yudecoin/wallet|market|chart|trade`, `/api/wallet/history`, `/api/badges/shop|mine|unequip`, `/api/auth/*`

**CORS**：外部 `Origin: https://loomix.example` を付けると公開GETでも **403**（Origin検証で遮断）。`Origin: https://yudetter.com` なら 200。
→ プロキシから叩く場合も「`Origin` を付けない」か自ドメインの値にする必要がある。

**Loomixにとっての意味**：運営者＝ユーザー本人なので、**Yudetter側に OAuth2 / 個人アクセストークン（PAT）エンドポイントを新設するのが最も正攻法**。Better Auth にはAPIキープラグイン等があり、追加実装コストは小さい。

---

### 3.3 あめ→はれ（https://am-hr.com）

**構成**：素のJavaScript（`/app.js` 352KB）＋サーバーサイドHTML。CSP `connect-src 'self'`＝APIは同一オリジン `/api/*`。

**公式ドキュメント**：なし（`/docs` 等はトップHTMLを返す）。

**公開API（無認証で実測 200）**
- `GET /api/timeline?bucket=latest|main|foryou&limit=20&beforeSeq=...` → `{"timeline":[{id(uuid), seq, kind, createdAt:"YYYY-MM-DD HH:MM:SS"(タイムゾーン表記なし), actor{userId,displayName,avatarUrl,avatarSeed}, canonical{body,imageUrl,createdAt,timelineBucket,weather,classificationSource:"ai_auto",reactionType,author,...}}]}`
- `GET /api/posts/{uuid}` → `{"activity":{...}}`（単体投稿）
- `GET /api/users/{userId}?bucket=main`（プロフィール＋投稿、約29KB）
- `GET /api/auth/session` → `{"server":{"publicOrigin":"https://am-hr.com","appEnv":"production"},"me":null,...}`
- `GET /api/announcements`

**要認証（同一オリジンOrigin付きで実測）**
- `POST /api/posts` → 401 `{"error":"ログインが必要です。"}`
- `POST /api/posts/{id}/reaction-toggle`（いいね切替）、`/reaction-pour`、`/comments`、`/report`、`/timeline-bucket`
- `POST /api/users/{id}/mute|block|report`、`/api/profile/*`（`userid` 変更、アバター、メール認証開始/確認 等）
- 管理系：`/api/admin/*`（レポート、アナウンス、チェックリスト、影の可視性設定）

**認証**
- `POST /api/auth/login`（`identifier`＝ユーザーID or メール、`password`）→ 不正時 401 `{"error":"ユーザーIDまたはパスワードが違います。"}`。成功時レスポンスに **`sessionToken`** を含み、クライアントは localStorage系に保存して **`x-amehare-session-token` ヘッダ**でも送る（Cookieと併用）
- `POST /api/auth/register`（`displayName`, `email`, `password`(8文字以上), `termsAccepted`）、`/api/auth/password/reset/request|confirm`、`/api/auth/logout`、`/api/auth/account`、`/api/auth/legal-consent`
- **cross-origin 用の別系統**：`/api/internal/auth/login` / `/api/internal/auth/register`（存在確認済み・入力バリデーションまで動作）＝APIを別オリジンに置く構成を想定した実装が残っている

**CSRF/クロスサイト対策（実測）**：`Origin` 無し or 外部Originの POST は **403 `{"error":"Cross-site request was blocked."}`**。`Origin: https://am-hr.com` なら通過（未ログインなら401）。CORSヘッダは付与されない。

**Loomixにとっての意味**：`sessionToken` ヘッダ経路があるので、**サーバー側でログイン→トークン保持**は技術的に可能。ただし運営（しゃち氏）に「Loomixのオリジンを cross-origin 許可してほしい」と打診するのが筋（`/api/internal/*` が既にあるので受け入れ余地あり）。

---

### 3.4 Pitter（https://pitter.jp / アプリは pitter.jp、LPは maine.pitter.jp）

**構成**：PHPアプリ（`system/*.php`）＋ Cloudflare（マネージドチャレンジ）＋ LiveKit（配信）。フロントは `script6.1.js`（952KB）。

**公式の外部API（最重要）**：`POST /system/openAPI.php?action=...`
- 未認証時のエラーが全アクションを列挙：
  `login, logout, me, timeline, global_timeline, notifications, read_notifications, my_posts, my_replies, user, user_posts, post, reply, like, unlike, repost, unrepost, quote, follow, unfollow, bot_post, track_event`
- 手順（アプリ内「設定 → APIキー」に明記）：
  1. 設定画面でAPIキーを生成（**1アカウント1つ**、64桁の16進数）
  2. `POST /system/openAPI.php?action=login` に `email` / `password` / `api_key` を送る
  3. 返ってきた `token` を以降 `Authorization: Bearer {token}` で付与
- 実測：トークン無しは全アクション `401 {"ok":false,"error":"Authorization: Bearer {token} ヘッダーが必要です"}`。`login` は `{}` → 「メールアドレスの形式が正しくありません」、`api_key` 不正 → 「api_key の形式が正しくありません（64文字の16進数が必要です）」。**仕様が明確で、Loomix向けに最も実装しやすい。**

**内部API（自社Webクライアント用、無認証GETも可）**
- `GET /system/Post.php?action=latest|global|timeline|recommended|following&page=` → `{"ok":true,"data":[{"id":"29001","parent_id":null,"user_handle":"3","user_name":"社会科見学",...}]}`（`latest`/`global` は無認証で約24KB取得可）
- `GET /system/auth.php?action=csrf` → `{"success":true,"token":"..."}`／`action=me` → `{"success":false,"message":"未ログイン"}`
- その他：`system/search.php`（search/trends/recommended_users/news/buzz/inspiration）、`system/store.php`（コイン・バッジ・ギフト）、`system/bookmark.php`、`system/dm.php`、`system/creator_studio.php`（配信・アナリティクス）、`system/community.php`、`system/feeds.php`、`system/list.php`、`system/notice.php`、`system/admin.php`、`system/advertisement.php`
- 設定系：`auth.php?action=update_privacy|update_personal_info|linked_accounts|switch_account|domain_*`（独自ドメインをIDにする団体機能）

**制約**
- CORSヘッダ無し。**Cloudflareのマネージドチャレンジが断続的に発動**（同一条件でも 403 "Just a moment..." が返ることがある）。サーバー側プロキシにはリトライ・指数バックオフとブラウザ相当のヘッダが必須
- APIキーは1アカウント1つ、再生成すると旧キーは無効化（UI文言「現在のAPIキーを無効にして新しいキーを生成します」）
- トークンの寿命・失効仕様は未公開（要検証。実装時は401で再ログインする設計に）

---

### 3.5 Amiverse（https://amiverse.net／作品ページ https://kisana.me/works/amiverse/）

**構成**：Next.js（フロント）＋ Rails（API `https://api.amiverse.net/v1`）＋ Cloudflare。画像は `m.amiverse.net`。

**API方式**：RPC寄り。読み取りでも **POST** を使う（`POST /v1/feeds/{recommended|current|following}`、body `{cursor}`）。
- ブートストラップ：`GET /v1/start` → `{"csrf_token":"...","is_signed_in":false}` ＋ `_amiverse` Cookie（HttpOnly・暗号化Railsセッション）
- 以後 `X-CSRF-Token` ヘッダ＋`credentials: include`。CSRF不備は `419 {"code":"invalid_csrf_token"}`
- **匿名でも公開フィードは読める**（`/v1/start` → `POST /v1/feeds/recommended` が 200。実測 30KB）

**データ構造（実測）**
`{"posts":[{"aid":"3q8d1ynnt5lyia","content":"...","visibility":"opened","status":"normal","created_at":"2025-12-05T08:18:47.157+09:00","rating":"general","replies_count":1,"quotes_count":0,"views_count":0,"diffuses_count":2,"reactions_count":4,"reactions":[{"aid":"...","name":"💕","name_id":"two_hearts","reactions_count":4,"reacted":false}],"media":[],"drawings":[{"aid":"c4b2swl2062g4a","name":"これからよろしくです！！","description":"...","image_url":"https://m.amiverse.net/..."}],"account":{"aid":"qf9f1w9r3789ws","name":"あめのこ","name_id":"Amenoko","icon_url":"..."}}],"feed":[{"type":"post","post_aid":"..."}]}`

**エンドポイント（バンドルから抽出）**
`/v1/start`, `/v1/feeds/{recommended|current|following}`, `/v1/trends`, `/v1/posts/{aid}`, `/v1/posts/{aid}/reaction`, `/v1/posts/{aid}/diffuse`（リポスト相当）, `/v1/posts/{aid}/card`, `/v1/questions/{aid}/card`, `/v1/accounts/@`（プロフィール）, `/v1/chats*`（DM・グループ・申請）, `/v1/notifications`, `/v1/notifications/unread_count`, `/v1/emojis/groups`, `/v1/reports`, `/v1/settings/dm`, `/v1/webpush_subscriptions`, `/v1/amis/cards`（AI）。AIチャットは `text/event-stream`（SSE）でストリーミング。

**ActivityPub**：作品ページには「ActivityPubに対応していてMisskeyやMastodonなどと投稿のやり取りができる」と記載があるが、実測では
`/.well-known/webfinger`（両ホスト）、`/nodeinfo/2.0`、`/users/{name}`、`/@name`（Accept: activity+json）はいずれも 404／HTML。
→ **外部からAPで読み書きできる状態は確認できず**（実装中・別ホスト・停止のいずれか）。

**OAuth**：作品ページに「OAuthの認可・リソースサーバーを完全自作」と記載（ANYUR がIdP）。しかし `/oauth/authorize`、`.well-known/oauth-authorization-server` は 404。GitHub `kisana-me/anyur` は非公開（404）。
→ **サードパーティクライアント登録の公開窓口は見つからず**。個別に運営（きさな氏）へ打診が必要。

**CORS**：`Access-Control-Allow-Origin` は `https://amiverse.net` のみ。

---

### 3.6 参考：Subnect（https://subnect.com）／Meax（https://meax.jp）

- **Subnect**：当環境からの全リクエストが Cloudflare の 403（`cf-mitigated: challenge` 相当のHTML）。`/robots.txt` すら 403。**外部からのAPI調査は不可（壁）**。ブラウザ経由での目視確認が必要。
- **Meax**：静的なHTML主体（トップ約84KB）。`/api/` 404、`/developer` 404、`/openapi.json` 404。`/docs` はHTMLページ。**JSON APIは確認できず**。robots.txt に広告iframe除外などの運用メモあり。

---

## 4. Loomix への設計提言

### 4.1 全体アーキテクチャ（結論：BFF方式）

```
[ユーザーのブラウザ]
      │  （Loomixのセッションのみ）
      ▼
[Loomix サーバー（BFF）]  ← ここに各SNSの認証情報を暗号化保管
      │ サービス別アダプタ（Karotter/Yudetter/am-hr/Pitter/Amiverse）
      ▼
[各SNSのAPI]  ※CORS制約はサーバー間なので無関係
```

- **認証情報はブラウザに一切渡さない**。フロントはLoomixのAPIだけを叩き、Loomixが各SNSへ代理リクエストする。
- 各SNSへのリクエストはサーバーから出すため、**CORSの許可リストは問題にならない**（ただし `Origin` を付けない／付ける場合は各サービスの自ドメインにする。Yudetterは外部Originで403、am-hrは外部OriginのPOSTで403）。
- **共通データモデル**に正規化する：`{service, id, author{id,name,handle,avatar}, body, media[], createdAt(ISO8601 UTC), url, counts{like,reply,repost,view}, viewer{liked,bookmarked}}`
  - 時刻の注意：Karotter=UTC ISO、Yudetter=UTC ISO、Amiverse=`+09:00`付きISO、**am-hrは `YYYY-MM-DD HH:MM:SS` でTZ表記なし（JSTとして解釈）**、Pitterは形式未確認（実装時に要確認）。
  - ID系の注意：Karotter=数値ID＋短縮ID（`p_xxxx`）、Yudetter=数値、am-hr=UUID＋`seq`、Amiverse=`aid`（文字列）、Pitter=数値文字列。

### 4.2 サービス別の接続方式（推奨順）

| サービス | 推奨方式 | 根拠 | リスク/注意 |
|---|---|---|---|
| **Yudetter** | まず**Yudetter側にOAuth2 or 個人アクセストークン（PAT）を実装**し、Loomixはそれを利用 | 本人がコードを所有。Better Auth にプラグイン追加で実現可能 | 実装コストは本人側。暫定はサーバー側ログイン（Cookie保持） |
| **Pitter** | **公式API（APIキー＋Bearer）** | 唯一の公式外部API。読み書き両対応 | ログインにパスワードが必要 → **一度だけ受け取り、トークン化後はパスワードを破棄**。APIキーはユーザーが自分の設定画面で発行 |
| **あめ→はれ** | `sessionToken` ヘッダ方式（サーバー側ログイン）＋運営に正規の提携打診 | `x-amehare-session-token` と `/api/internal/*` が既にある | 外部OriginPOSTは403。規約面の確認が必要 |
| **Karotter** | **運営（かろん氏）へAPI/OAuthクライアント申請**。暫定はサーバー側でCookieセッション保持 | Bearerは実装済み・公開GETは豊富 | 公式発行手順が無い。Cookie預かりは規約・保守リスク大。`robots.txt` の `Disallow: /api/` に留意 |
| **Amiverse** | 運営（きさな氏）へOAuthクライアント登録を打診 | OAuthサーバを自作済みと明記 | 公開窓口なし。暫定は匿名セッション＋CSRFで公開フィードのみ |

### 4.3 実装フェーズ案

1. **読み取り専用MVP（認証不要）**：Karotter `/api/posts/recommended`＋`/api/search`、Yudetter `/api/yudates`＋`/api/explore`、am-hr `/api/timeline`、Amiverse 匿名 `/v1/start`→`/v1/feeds/recommended` を統合表示。ここだけで「Loomixの体験」を検証できる。
2. **Pitter公式API接続**：アカウント連携（APIキー＋ログイン）→ タイムライン読み取り → 投稿/いいねの書き込み。
3. **あめ→はれ／Karotterの認証接続**：サーバー側セッション保持。401時は「再連携が必要」とUIで明示。
4. **Yudetterにトークン発行APIを追加**し、Loomixのリファレンス実装にする。
5. **Karotter／Amiverseは提携交渉**。成立すれば公式トークンでタイムライン・投稿・いいねまで開放。

### 4.4 運用・安全面

- **レートリミット**：Karotter は `10000req/60s`（IP単位と推測）。プロキシはIPを共有するため、ユーザーごとのキャッシュ（推奨30〜60秒）とリクエスト統合が必須。他サービスはヘッダ非公開なので、指数バックオフ＋サーキットブレーカーを共通実装。
- **Cloudflare**：Pitter/Amiverse/Yudetter はCloudflare配下。サーバーからのアクセスはブラウザ相当のUA・Accept・リトライが必要。Pitterは特にチャレンジが断続的に出る。
- **401/403の扱い**：401＝認証切れ（再連携導線）、403＝CORS/CSRF/ブロック（設定ミスか仕様変更）。両者を混同しない。
- **秘密情報**：トークン・CookieはKMS等で暗号化保管、ログに出さない。パスワードは保存しない（Pitterのログインも交換後破棄）。
- **規約順守**：`robots.txt`（Karotterは `/api/` を除外、Yudetterは `ai-train=no`）を尊重し、スクレイピング的な高頻度アクセスを避ける。公開APIが無いサービスは「提携」を前提にするのが安全。

### 4.5 Loomix が抱える構造的リスク

- **仕様が非公開で予告なく変わる**：Karotter/Yudetter/am-hr/Amiverse の内部APIはフロントエンドの実装詳細。UIの変更で壊れる。アダプタを薄く保ち、壊れたら素早く切り替えられる設計に。
- **認証の作り直しリスク**：Cookie預かり方式は、各サービスがSameSite/ドメイン/2FAを変更すると即死。公式トークン取得へ移行できる余地を常に残す。
- **同一性担保**：各サービスのユーザーIDをLoomix側のアカウントに紐付け、複数サービスの「同一人物」をユーザーが明示的に接続する設計（自動名寄せは誤結合のリスク）。

---

## 5. 未確認・要フォロー事項

1. **Karotter**：ログイン成功時のレスポンス形式（`accessToken` の有無・寿命・リフレッシュ手順）。※パスワードが手元に無いため未検証。APIキー発行画面の有無も未確認。
2. **Pitter**：`openAPI.php` のトークン寿命・失効・レート制限。`post` の必須パラメータ（文字数上限500/1000、メディア添付方法）。
3. **あめ→はれ**：`sessionToken` の有効期限と、`/api/internal/*` を外部オリジンから使う際の条件（現在は403）。
4. **Amiverse**：ActivityPub の実体（別ホストか、停止中か）、OAuthクライアント登録の可否。
5. **Subnect**：Cloudflareで全リクエスト403。ブラウザでの目視確認、または運営への問い合わせが必要。
6. **各サービスの利用規約**：API利用・外部クライアント・スクレイピングに関する条項の原文確認（今回は未精読）。

---

## 6. 付録：実測の生データ

`work/loomix/` 配下に保存：
- `karotter/`：`actor_pickle_asst.json`（AP Actor）、`api_user_pickle_asst.json`、`post_2118044.json`、`rec.json`（recommendedフィード）、`outbox.json`、`discover.html`、`js/`（フロントのチャンク）
- `yudetter/`：`yd.json`（/api/yudates）、`index.js`（バンドル）、`index.html`
- `amhr/`：`tl.json`（/api/timeline）、`app.js`（352KBバンドル）、`index.html`
- `pitter/`：`openapi.out`（アクション一覧のエラー）、`script6.1.js`、`root.html`（APIキー設定UIの文言）
- `amiverse/`：`start.json`（CSRF）、`feed.json`（公開フィード）、`js/`（Next.jsチャンク）
- 補助スクリプト：`probe_host.sh`、`probe_post.sh`、`ap_probe.sh`

主要な実測レスポンス（再現用）：

```
# Karotter: 公開フィード
curl 'https://api.karotter.com/api/posts/recommended?limit=2&mode=algorithm'
# Karotter: CORS（外部オリジンはACAO無し）
curl -D - -o /dev/null 'https://api.karotter.com/api/posts/2118044' -H 'Origin: https://loomix.example'
# Karotter: AP
curl -H 'Accept: application/activity+json' 'https://karotter.com/users/pickle_asst'

# Yudetter: 公開TL
curl 'https://yudetter.com/api/yudates?limit=1'
# Yudetter: 外部オリジンは403
curl -D - -o /dev/null 'https://yudetter.com/api/yudates?limit=1' -H 'Origin: https://loomix.example'

# あめ→はれ: 公開TL
curl 'https://am-hr.com/api/timeline?bucket=latest'

# Pitter: 公式APIのアクション一覧（未認証エラーに列挙される）
curl 'https://pitter.jp/system/openAPI.php'
# Pitter: ログイン
curl -X POST 'https://pitter.jp/system/openAPI.php?action=login' \
  -H 'Content-Type: application/json' \
  -d '{"email":"...","password":"...","api_key":"<64桁hex>"}'

# Amiverse: 匿名セッション→公開フィード
curl -c jar 'https://api.amiverse.net/v1/start'      # csrf_token と _amiverse Cookie
curl -b jar -X POST 'https://api.amiverse.net/v1/feeds/recommended' \
  -H "X-CSRF-Token: <上記>" -H 'Content-Type: application/json' -d '{}'
```
