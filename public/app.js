// Loomix 0.4 — frontend
//
//  - 未連携ならトップページ（ゲート）を表示し、4つのSNSすべての連携が揃うと本体に入る
//  - 本体はサイドバー（ホーム / 連携アカウント / プロフィール / 設定）＋統合タイムライン
//  - 一覧はサーバー側のカーソル（before）で少しずつ読み込む

const PAGE = 40;
const RETRY_BASE_MS = 1200;
const MAX_AUTO_RETRY = 3;
const TOKEN_KEY = "loomix.session";

const ROUTES = {
  home: "ホーム",
  accounts: "連携アカウント",
  profile: "プロフィール",
  settings: "設定",
};

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  auth: null,
  route: "home",
  started: false,

  posts: [],
  ids: new Set(),
  nextBefore: null,
  hasMore: true,
  filter: localStorage.getItem("loomix.filter") || "all",
  hideReplies: localStorage.getItem("loomix.hideReplies") === "1",
  autoRefresh: localStorage.getItem("loomix.auto") !== "0",
  isFetching: false,
  loadingFirst: false,
  bootRetries: 0,
  gen: 0,
  emptyStreak: 0,
  nextAllowedAt: 0,
  fetchedAt: null,
  errors: {},
  profileCache: new Map(),
};

const gateEl = document.getElementById("gate");
const appEl = document.getElementById("app");
const gateCount = document.getElementById("gateCount");
const gateStatus = document.getElementById("gateStatus");
const gateNotice = document.getElementById("gateNotice");
const linkList = document.getElementById("linkList");
const sideUser = document.getElementById("sideUser");
const topTitle = document.getElementById("topTitle");
const topTools = document.getElementById("topTools");
const filtersEl = document.getElementById("filters");
const viewHome = document.getElementById("view-home");
const viewAccounts = document.getElementById("view-accounts");
const viewProfile = document.getElementById("view-profile");
const viewSettings = document.getElementById("view-settings");

const feedEl = document.getElementById("feed");
const emptyEl = document.getElementById("empty");
const endEl = document.getElementById("feedEnd");
const moreBtn = document.getElementById("moreBtn");
const loadingLine = document.getElementById("loadingLine");
const stampEl = document.getElementById("stamp");
const statusEl = document.getElementById("statusLine");
const refreshBtn = document.getElementById("refreshBtn");
const autoEl = document.getElementById("autoRefresh");
const hideRepliesEl = document.getElementById("hideReplies");
const backdrop = document.getElementById("backdrop");
const drawer = document.getElementById("drawer");
const drawerBody = document.getElementById("drawerBody");
const drawerClose = document.getElementById("drawerClose");
const toastEl = document.getElementById("toast");

let autoTimer = null;
let lastLoad = 0;
let retryTimer = null;
let profileSeq = 0;
let chaining = false;
let toastTimer = null;

/* ------------------------------------------------------------ helpers */

function esc(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function linkify(text) {
  const safe = esc(text);
  return safe.replace(/(https?:\/\/[^\s<]+)/g, (url) => {
    const trimmed = url.replace(/[.,、。)]+$/, "");
    const tail = url.slice(trimmed.length);
    return `<a href="${trimmed}" target="_blank" rel="noopener noreferrer">${trimmed}</a>${tail}`;
  });
}

function parseDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return isNaN(d.getTime()) ? null : d;
}

function timeLabel(value) {
  const d = parseDate(value);
  if (!d) return "";
  const diff = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diff < 60) return "たった今";
  if (diff < 3600) return `${Math.floor(diff / 60)}分`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}時間`;
  if (diff < 86400 * 3) return `${Math.floor(diff / 86400)}日`;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return sameYear
    ? `${d.getMonth() + 1}/${d.getDate()}`
    : `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
}

function fullTime(value) {
  const d = parseDate(value);
  if (!d) return "";
  return d.toLocaleString("ja-JP", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function num(n) {
  const v = Number(n) || 0;
  if (v >= 10000) return (v / 10000).toFixed(1).replace(/\.0$/, "") + "万";
  return String(v);
}

function toast(text) {
  toastEl.textContent = text;
  toastEl.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
}

function providerSpec(service) {
  const providers = (state.auth && state.auth.providers) || {};
  return providers[service] || { title: service, host: "", fields: [], help: "" };
}

function providerName(service) {
  return providerSpec(service).title || service;
}

/* ------------------------------------------------------------ auth */

function setToken(token) {
  state.token = token || "";
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (state.token) h.Authorization = "Bearer " + state.token;
  return h;
}

async function apiFetch(path, opts) {
  const options = Object.assign({ cache: "no-store" }, opts || {});
  options.headers = authHeaders((opts && opts.headers) || {});
  const res = await fetch(path, options);
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    if (res.status === 401 && data && data.code === "auth_required") {
      authLost(data);
      throw new Error("auth_required");
    }
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.data = data;
    throw err;
  }
  return res.json();
}

async function loadStatus() {
  const res = await fetch("/api/auth/status", { headers: authHeaders(), cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  state.auth = data;
  if (!data.authenticated) setToken("");
  return data;
}

function authLost(data) {
  if (data && data.auth) state.auth = data.auth;
  if (!state.auth || !state.auth.authenticated) setToken("");
  resetTimeline();
  showGate("セッションが切れました。もう一度アカウントを連携してください。");
}

/* ------------------------------------------------------------ gate */

function showGate(notice) {
  stopAuto();
  state.started = false;
  gateEl.hidden = false;
  appEl.hidden = true;
  if (notice) {
    gateNotice.textContent = notice;
    gateNotice.hidden = false;
  } else {
    gateNotice.hidden = true;
  }
  renderGate();
  document.title = "Loomix";
  // ログアウト後に #/settings などが残るとゲートの邪魔になるので、URLをトップに戻す
  if (location.hash && location.hash !== "#link") {
    history.replaceState(null, "", location.pathname + location.search);
  }
}

function linkCard(service, manage) {
  const spec = providerSpec(service);
  const linked = state.auth && state.auth.linked && state.auth.linked[service];
  const host = spec.host ? `<div class="lc-host">${esc(spec.host)}</div>` : "";
  const label = spec.title || service;
  const stateBadge = linked
    ? `<span class="lc-state on">連携済み</span>`
    : `<span class="lc-state">未連携</span>`;

  if (linked) {
    const avatar = linked.avatarUrl
      ? `<img src="${esc(linked.avatarUrl)}" alt="" referrerpolicy="no-referrer" loading="lazy">`
      : `<span class="ph">${esc((linked.displayName || linked.handle || "?").slice(0, 1))}</span>`;
    const profile = linked.profileUrl
      ? `<a class="lc-link" href="${esc(linked.profileUrl)}" target="_blank" rel="noopener noreferrer">本家で開く</a>`
      : "";
    const unlink = manage
      ? `<button class="btn" type="button" data-unlink="${esc(service)}">解除</button>`
      : "";
    return `<article class="link-card is-linked" data-service="${esc(service)}">
      <div class="lc-head">
        <div><div class="lc-name">${esc(label)}</div>${host}</div>
        ${stateBadge}
      </div>
      <div class="lc-body">
        <div class="lc-who">
          ${avatar}
          <div>
            <div class="who-name">${esc(linked.displayName || linked.handle || "")}</div>
            <div class="who-handle">@${esc(linked.handle || "")}</div>
          </div>
          ${unlink}
        </div>
        ${profile ? `<div class="lc-actions" style="margin-top:10px">${profile}</div>` : ""}
      </div>
    </article>`;
  }

  const fields = (spec.fields || []).map((f) => {
    const id = `f-${service}-${f.key}`;
    return `<div class="field">
      <label for="${id}">${esc(f.label)}</label>
      <input id="${id}" name="${esc(f.key)}" type="${esc(f.type || "text")}"
             autocomplete="${esc(f.autocomplete || "off")}" autocapitalize="off"
             spellcheck="false" required>
    </div>`;
  }).join("");

  const help = spec.help ? `<p class="lc-help">${esc(spec.help)}</p>` : "";
  const signup = spec.signupUrl
    ? `<a class="lc-link" href="${esc(spec.signupUrl)}" target="_blank" rel="noopener noreferrer">${esc(spec.registerNote || "登録ページ")}</a>`
    : "";

  return `<article class="link-card" data-service="${esc(service)}">
    <div class="lc-head">
      <div><div class="lc-name">${esc(label)}</div>${host}</div>
      ${stateBadge}
    </div>
    <div class="lc-body">
      <form class="lc-form" data-link-form data-service="${esc(service)}">
        ${fields}
        ${help}
        <div class="lc-actions">
          <button class="btn solid" type="submit">連携する</button>
          ${signup}
        </div>
        <p class="lc-error" data-error></p>
      </form>
    </div>
  </article>`;
}

function renderGate() {
  const auth = state.auth;
  const linkedCount = auth ? (auth.progress ? auth.progress.linked : Object.keys(auth.linked || {}).length) : 0;
  const requiredCount = auth && auth.required ? auth.required.length : 4;
  gateCount.textContent = `${linkedCount} / ${requiredCount}`;

  if (!auth) {
    gateStatus.textContent = "接続できませんでした";
    linkList.innerHTML = `<article class="link-card"><div class="lc-body" style="padding-top:14px">
      <p class="lc-help">サーバーに接続できませんでした。少し待ってから再読み込みしてください。</p>
    </div></article>`;
    return;
  }

  const providers = Object.keys(auth.providers || {});
  linkList.innerHTML = providers.map((s) => linkCard(s, false)).join("");
  gateStatus.textContent = auth.gateOpen
    ? `${linkedCount} / ${requiredCount} 連携済み`
    : `${linkedCount} / ${requiredCount} 連携済み ・ あと${requiredCount - linkedCount}件`;
}

async function submitLink(form) {
  const service = form.dataset.service;
  const card = form.closest(".link-card");
  const errorEl = card ? card.querySelector("[data-error]") : null;
  const button = form.querySelector("button[type=submit]");
  const inApp = !appEl.hidden;

  const body = { service };
  new FormData(form).forEach((value, key) => { body[key] = String(value); });

  if (errorEl) errorEl.textContent = "";
  if (button) {
    button.disabled = true;
    button.textContent = "確認中...";
  }

  try {
    const data = await apiFetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (data.token) setToken(data.token);
    state.auth = data.auth;
    toast(`${providerName(service)}と連携しました`);

    if (inApp) {
      renderSideUser();
      if (state.route === "accounts") renderAccounts();
      if (state.auth.gateOpen) startAutoIfOn();
    } else {
      renderGate();
      if (state.auth.gateOpen) {
        setTimeout(() => { if (state.auth && state.auth.gateOpen) openApp(); }, 550);
      }
    }
  } catch (err) {
    if (err.message === "auth_required") return;
    if (errorEl) errorEl.textContent = err.message || "連携できませんでした";
    if (button) {
      button.disabled = false;
      button.textContent = "連携する";
    }
  }
}

async function unlink(service) {
  if (!window.confirm(`${providerName(service)}の連携を解除しますか？`)) return;
  try {
    const data = await apiFetch("/api/auth/unlink", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service }),
    });
    state.auth = data.auth;
    if (!data.auth.authenticated) setToken("");
    if (!data.auth.gateOpen) {
      resetTimeline();
      showGate("連携を解除しました。4つすべてを連携するとタイムラインが開きます。");
      return;
    }
    renderSideUser();
    renderAccounts();
    toast(`${providerName(service)}の連携を解除しました`);
  } catch (err) {
    if (err.message !== "auth_required") toast(err.message || "解除できませんでした");
  }
}

async function logout() {
  try {
    const data = await apiFetch("/api/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    state.auth = data.auth || null;
  } catch (err) {
    state.auth = null;
  }
  setToken("");
  resetTimeline();
  if (!state.auth) {
    try { await loadStatus(); } catch (err) { state.auth = null; }
  }
  showGate("ログアウトしました。");
  toast("ログアウトしました");
}

/* ------------------------------------------------------------ app shell */

function openApp() {
  gateEl.hidden = true;
  appEl.hidden = false;
  renderSideUser();
  if (!state.started) {
    state.started = true;
    startAutoIfOn();
  }
  if (!location.hash || location.hash === "#link") {
    location.hash = "#/home";
  }
  route();
}

function renderSideUser() {
  const auth = state.auth;
  if (!auth) { sideUser.innerHTML = ""; return; }
  const linked = Object.values(auth.linked || {});
  const required = (auth.required || []).length;
  const avatars = linked.slice(0, 5).map((a) => (
    a.avatarUrl
      ? `<img src="${esc(a.avatarUrl)}" alt="" referrerpolicy="no-referrer" loading="lazy">`
      : `<span class="ph"></span>`
  )).join("");

  sideUser.innerHTML = `
    <div class="su-row">
      <div class="su-avatars">${avatars}</div>
      <div>
        <div class="su-name">自分のアカウント</div>
        <div class="su-label">${linked.length} / ${required} 連携</div>
      </div>
    </div>
    <button class="side-logout" id="sideLogout" type="button">ログアウト</button>
  `;
  const btn = document.getElementById("sideLogout");
  if (btn) btn.addEventListener("click", logout);
}

function route() {
  const raw = location.hash.replace(/^#\/?/, "").split("?")[0];
  const name = ROUTES[raw] ? raw : "home";
  state.route = name;

  if (!state.auth || !state.auth.gateOpen) {
    showGate();
    return;
  }

  gateEl.hidden = true;
  appEl.hidden = false;

  const views = { home: viewHome, accounts: viewAccounts, profile: viewProfile, settings: viewSettings };
  Object.keys(views).forEach((key) => { views[key].hidden = key !== name; });

  document.querySelectorAll("[data-route]").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.route === name);
  });

  topTitle.textContent = ROUTES[name];
  filtersEl.hidden = name !== "home";
  topTools.hidden = name !== "home";

  if (name === "accounts") renderAccounts();
  if (name === "profile") renderProfilePage();
  if (name === "settings") renderSettings();
  if (name === "home" && !state.posts.length && !state.loadingFirst) loadFirstPage();

  window.scrollTo({ top: 0, behavior: "auto" });
}

/* ------------------------------------------------------------ pages */

function renderAccounts() {
  const auth = state.auth;
  if (!auth) return;
  const providers = Object.keys(auth.providers || {});
  const linked = Object.keys(auth.linked || {}).length;
  const required = (auth.required || []).length;
  const remaining = Math.max(0, required - linked);

  viewAccounts.innerHTML = `
    <div class="page-head">
      <h2>連携アカウント</h2>
      <p>${linked} / ${required} 連携中。${remaining ? `あと${remaining}件の連携でタイムラインが開きます。` : "タイムラインが開いています。"}
      連携を解除するとタイムラインは閉じます。</p>
    </div>
    <div class="page-body">
      <div class="link-list">${providers.map((s) => linkCard(s, true)).join("")}</div>
    </div>
  `;
}

function profileCard(acc) {
  const spec = providerSpec(acc.service);
  const avatar = acc.avatarUrl
    ? `<img class="avatar" src="${esc(acc.avatarUrl)}" alt="" referrerpolicy="no-referrer" loading="lazy">`
    : `<span class="avatar-fallback">${esc((acc.displayName || "?").slice(0, 1))}</span>`;
  const key = `${acc.service}:${esc(acc.handle)}`;
  return `<article class="pf-card" data-service="${esc(acc.service)}">
    <div class="pf-card-top">
      <span role="button" tabindex="0" data-profile="${key}" data-service="${esc(acc.service)}" data-handle="${esc(acc.handle)}">${avatar}</span>
      <div style="min-width:0">
        <div class="pf-card-name" role="button" tabindex="0" data-profile="${key}" data-service="${esc(acc.service)}" data-handle="${esc(acc.handle)}">${esc(acc.displayName || acc.handle)}</div>
        <div class="pf-card-sub">
          <span>@${esc(acc.handle)}</span>
          <span class="svc">${esc(spec.host || spec.title || acc.service)}</span>
        </div>
      </div>
    </div>
    <div class="pf-card-stats" data-stats><span>読み込み中</span></div>
    <div class="pf-card-foot">
      ${acc.profileUrl ? `<a class="lc-link" href="${esc(acc.profileUrl)}" target="_blank" rel="noopener noreferrer">本家で開く</a>` : ""}
    </div>
  </article>`;
}

function statsMarkup(profile) {
  const stats = (profile && profile.stats) || {};
  const rows = [
    ["posts", "投稿"],
    ["followers", "フォロワー"],
    ["following", "フォロー中"],
    ["reactions", "リアクション"],
  ];
  const cells = rows
    .filter(([k]) => stats[k] !== undefined && stats[k] !== null)
    .map(([k, label]) => `<span><b>${num(stats[k])}</b>${label}</span>`);
  return cells.length ? cells.join("") : `<span>公開されている統計はありません</span>`;
}

async function fillProfileCard(acc) {
  const card = viewProfile.querySelector(`.pf-card[data-service="${acc.service}"]`);
  if (!card) return;
  const slot = card.querySelector("[data-stats]");
  const cacheKey = `${acc.service}:${acc.handle}`;
  const cached = state.profileCache.get(cacheKey);
  if (cached) {
    slot.innerHTML = statsMarkup(cached.profile);
    return;
  }
  try {
    const data = await apiFetch(
      `/api/user?service=${encodeURIComponent(acc.service)}&id=${encodeURIComponent(acc.handle)}`
    );
    state.profileCache.set(cacheKey, data);
    if (!card.isConnected) return;
    slot.innerHTML = statsMarkup(data.profile);
  } catch (err) {
    if (err.message === "auth_required") return;
    if (card.isConnected) slot.innerHTML = `<span>統計を取得できませんでした</span>`;
  }
}

function renderProfilePage() {
  const auth = state.auth;
  if (!auth) return;
  const linked = Object.values(auth.linked || {});
  viewProfile.innerHTML = `
    <div class="page-head">
      <h2>プロフィール</h2>
      <p>連携している${linked.length}つのアカウント。名前とアイコンを押すと、サイト内でプロフィールを開きます。</p>
    </div>
    <div class="page-body">
      <div class="pf-grid">${linked.map(profileCard).join("")}</div>
    </div>
  `;
  linked.forEach(fillProfileCard);
}

function renderSettings() {
  const auth = state.auth;
  if (!auth) return;
  const session = auth.session || {};
  const linked = Object.keys(auth.linked || {}).length;
  const required = (auth.required || []).length;

  viewSettings.innerHTML = `
    <div class="page-head">
      <h2>設定</h2>
      <p>表示とセッションの設定です。</p>
    </div>
    <div class="page-body">
      <div class="set-group">
        <div class="set-row">
          <div><div class="k">自動更新</div><div class="d">60秒ごとに新着を読み込みます。</div></div>
          <label class="auto"><input type="checkbox" id="setAuto" ${state.autoRefresh ? "checked" : ""}><span class="box" aria-hidden="true"></span></label>
        </div>
        <div class="set-row">
          <div><div class="k">返信を除外</div><div class="d">タイムラインから返信を除いて表示します。</div></div>
          <label class="auto"><input type="checkbox" id="setHideReplies" ${state.hideReplies ? "checked" : ""}><span class="box" aria-hidden="true"></span></label>
        </div>
      </div>

      <div class="set-group" style="margin-top:22px">
        <div class="set-row">
          <div><div class="k">連携アカウント</div><div class="d">${linked} / ${required} 連携中</div></div>
          <a class="btn" href="#/accounts">管理</a>
        </div>
        <div class="set-row">
          <div><div class="k">連携を始めた日時</div></div>
          <span class="v">${session.createdAt ? esc(fullTime(session.createdAt)) : "—"}</span>
        </div>
        <div class="set-row">
          <div><div class="k">セッションの期限</div><div class="d">使うたびに30日先へ延びます。</div></div>
          <span class="v">${session.expiresAt ? esc(fullTime(session.expiresAt)) : "—"}</span>
        </div>
        <div class="set-row">
          <div><div class="k">ログアウト</div><div class="d">連携をすべて外してトップに戻ります。</div></div>
          <button class="btn" id="settingsLogout" type="button">ログアウト</button>
        </div>
      </div>

      <p class="set-note">
        Loomix 0.4 ・ パスワードは保存していません。連携を解除するとLoomix側の記録も消えます。<br>
        <a href="https://github.com/yubikirigenman2025-source/loomix" target="_blank" rel="noopener noreferrer">リポジトリ</a>
      </p>
    </div>
  `;

  const setAuto = document.getElementById("setAuto");
  if (setAuto) setAuto.addEventListener("change", () => {
    state.autoRefresh = setAuto.checked;
    localStorage.setItem("loomix.auto", state.autoRefresh ? "1" : "0");
    autoEl.checked = state.autoRefresh;
    if (state.autoRefresh) startAuto(); else stopAuto();
  });

  const setHide = document.getElementById("setHideReplies");
  if (setHide) setHide.addEventListener("change", () => {
    state.hideReplies = setHide.checked;
    hideRepliesEl.checked = setHide.checked;
    localStorage.setItem("loomix.hideReplies", state.hideReplies ? "1" : "0");
    renderAll();
    updateCounts();
    chainIfNeeded();
  });

  const out = document.getElementById("settingsLogout");
  if (out) out.addEventListener("click", logout);
}

/* ------------------------------------------------------------ post markup */

function avatarMarkup(post) {
  const a = post.author || {};
  const key = `${post.service}:${esc(a.username || a.id)}`;
  if (a.avatarUrl) {
    return `<img class="avatar" src="${esc(a.avatarUrl)}" alt="${esc(a.displayName || a.username || "")}のアイコン" loading="lazy" data-profile="${key}" data-service="${post.service}" data-handle="${esc(a.username || a.id)}" role="button" tabindex="0" aria-label="${esc(a.displayName || a.username || "")}のプロフィールを開く">`;
  }
  const initial = (a.displayName || a.username || "?").trim().slice(0, 1);
  return `<span class="avatar-fallback" data-profile="${key}" data-service="${post.service}" data-handle="${esc(a.username || a.id)}" role="button" tabindex="0" aria-label="${esc(a.displayName || a.username || "")}のプロフィールを開く">${esc(initial)}</span>`;
}

function mediaMarkup(post) {
  const media = (post.media || []).filter(Boolean).slice(0, 4);
  if (!media.length) return "";
  const urls = media.map(esc).join("|");
  const cls = media.length >= 4 ? "m4" : `m${media.length}`;
  const cells = media
    .map((url, i) => `<button type="button" data-media="${urls}" data-index="${i}" aria-label="画像を拡大">
        <img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      </button>`)
    .join("");
  return `<div class="media ${cls}">${cells}</div>`;
}

function replyMarkup(post) {
  if (!post.isReply) return "";
  const to = post.replyTo && (post.replyTo.displayName || post.replyTo.username);
  return `<div class="reply-note">返信${to ? ` <span>@${esc(String(to).replace(/^@/, ""))}</span>` : ""}</div>`;
}

function countsMarkup(post) {
  const m = post.metrics || {};
  const parts = [];
  if (m.likes) parts.push(`<span><span class="num">${num(m.likes)}</span> いいね</span>`);
  if (m.reposts) parts.push(`<span><span class="num">${num(m.reposts)}</span> リポスト</span>`);
  if (m.replies) parts.push(`<span><span class="num">${num(m.replies)}</span> 返信</span>`);
  return `<div class="counts">${parts.join("")}</div>`;
}

function postMarkup(post) {
  const a = post.author || {};
  const key = `${post.service}:${esc(a.username || a.id)}`;
  return `<article class="post" data-url="${esc(post.url)}" data-service="${post.service}">
    <div>${avatarMarkup(post)}</div>
    <div class="body-col">
      <div class="meta">
        <span class="name" role="button" tabindex="0" data-profile="${key}" data-service="${post.service}" data-handle="${esc(a.username || a.id)}">${esc(a.displayName || a.username || "不明")}</span>
        ${a.username ? `<span class="handle">@${esc(a.username)}</span>` : ""}
        <span class="svc">${esc(post.serviceLabel || post.serviceName || "")}</span>
        <a class="time" href="${esc(post.url)}" target="_blank" rel="noopener noreferrer" title="${esc(fullTime(post.createdAt))}">${esc(timeLabel(post.createdAt))}</a>
      </div>
      ${replyMarkup(post)}
      ${post.content ? `<div class="text">${linkify(post.content)}</div>` : ""}
      ${mediaMarkup(post)}
      <div class="post-foot">
        ${countsMarkup(post)}
        <a class="origin" href="${esc(post.url)}" target="_blank" rel="noopener noreferrer">${esc(post.linkLabel || "本家で開く")}</a>
      </div>
    </div>
  </article>`;
}

/* ------------------------------------------------------------ rendering */

function visiblePosts(list) {
  let out = list || state.posts;
  if (state.hideReplies) out = out.filter((p) => !p.isReply);
  return out;
}

function renderedMarkup(list) {
  return list.map(postMarkup).join("");
}

function renderAll() {
  const list = visiblePosts();
  feedEl.innerHTML = renderedMarkup(list);
  if (!list.length) {
    emptyEl.hidden = false;
    emptyEl.textContent = state.posts.length ? "該当する投稿がありません" : "";
  } else {
    emptyEl.hidden = true;
  }
  updateFooter();
}

function appendPosts(posts) {
  const list = visiblePosts(posts);
  if (!list.length) return;
  if (!state.posts.length) {
    renderAll();
    return;
  }
  feedEl.insertAdjacentHTML("beforeend", renderedMarkup(list));
}

function renderSkeleton() {
  feedEl.innerHTML = Array.from({ length: 6 })
    .map(
      () => `<div class="post skeleton">
        <div class="avatar" style="border-radius:50%"></div>
        <div class="body-col">
          <div class="sk-line w1"></div>
          <div class="sk-line w2"></div>
          <div class="sk-line w3"></div>
        </div>
      </div>`
    )
    .join("");
}

function updateCounts() {
  const total = visiblePosts().length;
  const all = document.getElementById("count-all");
  if (all) all.textContent = total ? total : "";
  ["karotter", "yudetter", "amehare", "pitter"].forEach((s) => {
    const n = state.posts.filter((p) => p.service === s).length;
    const el = document.getElementById("count-" + s);
    if (el) el.textContent = n ? n : "";
  });
}

function updateStatus() {
  const d = parseDate(state.fetchedAt);
  const time = d ? d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" }) : "--:--";
  const failed = Object.keys(state.errors || {});
  statusEl.textContent = failed.length
    ? `${time} 更新 ・ ${failed.join(", ")} 取得失敗`
    : `${time} 更新`;
}

function updateFooter() {
  const count = visiblePosts().length;
  loadingLine.hidden = !(state.isFetching || state.loadingFirst);
  moreBtn.hidden = !(state.hasMore && !state.isFetching && !state.loadingFirst && state.posts.length && state.emptyStreak >= MAX_AUTO_RETRY);
  moreBtn.textContent = "さらに読み込む";
  if (state.emptyStreak >= MAX_AUTO_RETRY && state.hasMore) {
    moreBtn.hidden = false;
    moreBtn.textContent = "読み込みを再試行";
  }
  if (state.posts.length && !state.hasMore) {
    stampEl.textContent = `全 ${count} 件 ・ これ以上はありません`;
  } else if (state.posts.length) {
    stampEl.textContent = `表示 ${count} 件`;
  } else {
    stampEl.textContent = "";
  }
  endEl.hidden = !state.posts.length;
}

function updateTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    const on = tab.dataset.service === state.filter;
    tab.classList.toggle("is-active", on);
    tab.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function resetTimeline() {
  state.posts = [];
  state.ids = new Set();
  state.nextBefore = null;
  state.hasMore = true;
  state.emptyStreak = 0;
  state.isFetching = false;
  state.loadingFirst = false;
  state.nextAllowedAt = 0;
  state.gen += 1;
  if (feedEl) feedEl.innerHTML = "";
  if (emptyEl) { emptyEl.hidden = true; emptyEl.textContent = ""; }
  if (endEl) endEl.hidden = true;
  if (stampEl) stampEl.textContent = "";
}

/* ------------------------------------------------------------ data */

function timelineUrl(before, extra) {
  const params = new URLSearchParams({ limit: String(PAGE), service: state.filter });
  if (before) params.set("before", before);
  if (extra) Object.entries(extra).forEach(([k, v]) => params.set(k, v));
  return `/api/timeline?${params.toString()}`;
}

function adoptFirstPage(data) {
  state.posts = data.posts || [];
  state.ids = new Set(state.posts.map((p) => p.id));
  state.nextBefore = data.nextBefore || null;
  state.hasMore = !!data.hasMore;
  state.emptyStreak = 0;
  state.nextAllowedAt = 0;
  state.fetchedAt = data.fetchedAt;
  state.errors = data.errors || {};
  lastLoad = Date.now();
}

async function loadFirstPage({ force = false } = {}) {
  if (state.loadingFirst) return;
  if (!state.auth || !state.auth.gateOpen) return;
  state.loadingFirst = true;
  state.gen += 1;
  const gen = state.gen;
  refreshBtn.classList.add("is-busy");
  if (!state.posts.length) renderSkeleton();
  updateFooter();

  try {
    const data = await apiFetch(timelineUrl(null, force ? { refresh: "1" } : null));
    if (gen !== state.gen) return;
    adoptFirstPage(data);
    state.bootRetries = 0;
    updateCounts();
    updateStatus();
    renderAll();
  } catch (err) {
    if (err.message === "auth_required") return;
    console.error("timeline", err);
    if (gen !== state.gen) return;
    // Render の無料プランは休止からの復帰に時間がかかるため、2回まで自動で再試行する
    if (!state.posts.length && state.bootRetries < 2) {
      state.bootRetries += 1;
      statusEl.textContent = "接続しています";
      setTimeout(() => {
        if (!state.posts.length && !state.loadingFirst) loadFirstPage({ force: true }).catch(() => {});
      }, 3000);
    } else {
      statusEl.textContent = "接続エラー";
      feedEl.innerHTML = "";
      emptyEl.hidden = false;
      emptyEl.innerHTML = `<p>タイムラインを取得できませんでした</p>
        <button class="more" id="retryBtn" type="button" style="margin-top:14px">再試行</button>`;
    }
  } finally {
    state.loadingFirst = false;
    refreshBtn.classList.remove("is-busy");
    updateFooter();
    if (state.posts.length) chainIfNeeded();
  }
}

async function loadMore() {
  if (state.isFetching || state.loadingFirst) return;
  if (!state.hasMore || !state.nextBefore) return;
  if (Date.now() < state.nextAllowedAt) return;
  if (!state.auth || !state.auth.gateOpen) return;
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }

  state.isFetching = true;
  const gen = state.gen;
  updateFooter();

  try {
    const data = await apiFetch(timelineUrl(state.nextBefore));
    if (gen !== state.gen) return;

    const fresh = (data.posts || []).filter((p) => !state.ids.has(p.id));
    fresh.forEach((p) => state.ids.add(p.id));
    if (data.nextBefore) state.nextBefore = data.nextBefore;
    state.hasMore = !!data.hasMore;
    state.errors = data.errors || {};

    if (fresh.length) {
      state.posts.push(...fresh);
      appendPosts(fresh);
      state.emptyStreak = 0;
    } else {
      state.emptyStreak += 1;
    }

    if (data.partial && state.hasMore) {
      // 相手先の都合でまだ届いていない範囲がある。少し待って続きを要求する。
      const wait = Number(data.retryAfterMs) || RETRY_BASE_MS;
      state.nextAllowedAt = Date.now() + wait * Math.min(state.emptyStreak + 1, 4);
    }
    updateStatus();
    updateCounts();
  } catch (err) {
    if (err.message === "auth_required") return;
    console.error("more", err);
    if (gen !== state.gen) return;
    state.emptyStreak += 1;
    state.nextAllowedAt = Date.now() + RETRY_BASE_MS * 2;
    statusEl.textContent = "読み込みに失敗しました";
  } finally {
    state.isFetching = false;
    updateFooter();
    if (state.emptyStreak > 0 && state.emptyStreak < MAX_AUTO_RETRY && state.hasMore) {
      scheduleRetry();
    } else {
      chainIfNeeded();
    }
  }
}

function scheduleRetry() {
  if (retryTimer) clearTimeout(retryTimer);
  const wait = Math.max(RETRY_BASE_MS, state.nextAllowedAt - Date.now());
  retryTimer = setTimeout(() => {
    retryTimer = null;
    loadMore();
  }, wait);
}

// 追加読み込みのあとも底が見えている（画面が短い）ときは続けて1回だけ繋ぐ
function chainIfNeeded() {
  if (chaining) return;
  if (!state.hasMore || state.isFetching || state.emptyStreak > 0) return;
  chaining = true;
  requestAnimationFrame(() => {
    chaining = false;
    const rect = endEl.getBoundingClientRect();
    if (rect.top < window.innerHeight + 400) loadMore();
  });
}

// 60秒ごとの自動更新。読み込み済みの古い投稿はそのまま残し、新着だけ頭に足す。
async function refreshNewest() {
  if (state.isFetching || state.loadingFirst) return;
  if (state.route !== "home" || !state.auth || !state.auth.gateOpen) return;
  try {
    const data = await apiFetch(timelineUrl(null));
    state.fetchedAt = data.fetchedAt;
    state.errors = data.errors || {};
    const fresh = (data.posts || []).filter((p) => !state.ids.has(p.id));
    if (fresh.length) {
      fresh.forEach((p) => state.ids.add(p.id));
      state.posts = fresh.concat(state.posts).sort((a, b) => {
        const da = parseDate(a.createdAt);
        const db = parseDate(b.createdAt);
        return (db ? db.getTime() : 0) - (da ? da.getTime() : 0);
      });
      const atBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 200;
      renderAll();
      if (atBottom) loadMore();
    } else {
      updateFooter();
    }
    updateStatus();
    updateCounts();
    lastLoad = Date.now();
  } catch (err) {
    if (err.message !== "auth_required") console.error("refresh", err);
  }
}

/* ------------------------------------------------------------ filters */

const filterBar = document.getElementById("filterBar");
if (filterBar) {
  filterBar.addEventListener("click", async (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    if (tab.dataset.service === state.filter) return;
    state.filter = tab.dataset.service;
    localStorage.setItem("loomix.filter", state.filter);
    updateTabs();
    resetTimeline();
    window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
    await loadFirstPage();
  });
}

hideRepliesEl.checked = state.hideReplies;
hideRepliesEl.addEventListener("change", () => {
  state.hideReplies = hideRepliesEl.checked;
  localStorage.setItem("loomix.hideReplies", state.hideReplies ? "1" : "0");
  renderAll();
  updateCounts();
  chainIfNeeded();
});

refreshBtn.addEventListener("click", async () => {
  resetTimeline();
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  await loadFirstPage({ force: true });
});

moreBtn.addEventListener("click", () => {
  state.emptyStreak = 0;
  state.nextAllowedAt = 0;
  loadMore();
});

autoEl.checked = state.autoRefresh;

function startAuto() {
  stopAuto();
  autoTimer = setInterval(() => {
    if (document.hidden) return;
    refreshNewest();
  }, 60000);
}

function stopAuto() {
  if (autoTimer) clearInterval(autoTimer);
  autoTimer = null;
}

function startAutoIfOn() {
  if (autoEl.checked) startAuto();
}

autoEl.addEventListener("change", () => {
  state.autoRefresh = autoEl.checked;
  localStorage.setItem("loomix.auto", state.autoRefresh ? "1" : "0");
  if (autoEl.checked) startAuto();
  else stopAuto();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && autoEl.checked && state.auth && state.auth.gateOpen &&
      Date.now() - lastLoad > 60000) refreshNewest();
});

/* ------------------------------------------------------------ infinite */

new IntersectionObserver(
  (entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) loadMore();
    });
  },
  { rootMargin: "800px 0px" }
).observe(endEl);

window.addEventListener("scroll", () => {
  // 底に近づいたら（IntersectionObserver の補助として）一度だけ呼ぶ
  if (state.isFetching || !state.hasMore) return;
  const remain = document.body.scrollHeight - (window.innerHeight + window.scrollY);
  if (remain < 600) loadMore();
}, { passive: true });

/* ------------------------------------------------------------ profile drawer */

function openDrawer() {
  backdrop.hidden = false;
  drawer.hidden = false;
  requestAnimationFrame(() => {
    backdrop.classList.add("on");
    drawer.classList.add("on");
  });
  document.body.style.overflow = "hidden";
}

function closeDrawer() {
  backdrop.classList.remove("on");
  drawer.classList.remove("on");
  document.body.style.overflow = "";
  setTimeout(() => {
    if (!drawer.classList.contains("on")) {
      drawer.hidden = true;
      backdrop.hidden = true;
      drawerBody.innerHTML = "";
    }
  }, 200);
}

function profileStats(profile) {
  const stats = profile.stats || {};
  const rows = [
    ["posts", "投稿"],
    ["followers", "フォロワー"],
    ["following", "フォロー中"],
    ["reactions", "リアクション"],
  ];
  const cells = rows
    .filter(([k]) => stats[k] !== undefined && stats[k] !== null)
    .map(([k, label]) => `<div><span class="v">${num(stats[k])}</span><span class="k">${label}</span></div>`)
    .join("");
  return cells ? `<div class="pf-stats">${cells}</div>` : "";
}

function renderProfile(data) {
  const p = data.profile || {};
  const posts = (data.posts || []).slice(0, 12);
  const avatar = p.avatarUrl
    ? `<img class="avatar" src="${esc(p.avatarUrl)}" alt="">`
    : `<span class="avatar-fallback">${esc((p.displayName || "?").slice(0, 1))}</span>`;

  drawerBody.innerHTML = `
    <div class="pf">
      <div class="pf-top">
        ${avatar}
        <div style="min-width:0">
          <div class="pf-name">${esc(p.displayName || p.username || "")}</div>
          <div class="pf-sub">
            ${p.username ? `<span>@${esc(p.username)}</span>` : ""}
            <span class="svc">${esc(p.serviceLabel || "")}</span>
          </div>
        </div>
      </div>
      ${p.bio ? `<div class="pf-bio">${linkify(p.bio)}</div>` : ""}
      ${profileStats(p)}
      ${p.profileUrl ? `<a class="pf-link" href="${esc(p.profileUrl)}" target="_blank" rel="noopener noreferrer">${esc(p.serviceName || "")}で開く</a>` : ""}
    </div>
    ${posts.length ? `<div class="pf-section">最近の投稿</div>${posts.map(postMarkup).join("")}` : `<div class="pf-loading">投稿が見つかりませんでした</div>`}
  `;
}

async function openProfile(service, handle) {
  if (!service || !handle) return;
  const key = `${service}:${handle}`;
  profileSeq += 1;
  const seq = profileSeq;
  openDrawer();
  drawerBody.innerHTML = `<div class="pf-loading">読み込み中</div>`;

  const cached = state.profileCache.get(key);
  if (cached) {
    renderProfile(cached);
    return;
  }

  try {
    const data = await apiFetch(
      `/api/user?service=${encodeURIComponent(service)}&id=${encodeURIComponent(handle)}`
    );
    state.profileCache.set(key, data);
    if (seq === profileSeq) renderProfile(data);
  } catch (err) {
    if (err.message === "auth_required") { closeDrawer(); return; }
    if (seq !== profileSeq) return;
    const local = state.posts.find((p) => p.service === service && (p.author.username === handle || p.author.id === handle));
    const link = local ? local.author.profileUrl : "";
    drawerBody.innerHTML = `<div class="pf-error">
      プロフィールを取得できませんでした。${link ? `<a href="${esc(link)}" target="_blank" rel="noopener noreferrer">本家で見る</a>` : ""}
    </div>`;
    console.error("profile", err);
  }
}

drawerClose.addEventListener("click", closeDrawer);
backdrop.addEventListener("click", () => {
  if (!lightboxOpen()) closeDrawer();
});

/* ------------------------------------------------------------ lightbox */

const lightbox = document.getElementById("lightbox");
const lbImg = document.getElementById("lbImg");
const lbCount = document.getElementById("lbCount");
const lbPrev = document.getElementById("lbPrev");
const lbNext = document.getElementById("lbNext");
let lbList = [];
let lbIndex = 0;

function lightboxOpen() {
  return !lightbox.hidden;
}

function showLightbox() {
  lbImg.src = lbList[lbIndex];
  lbCount.textContent = lbList.length > 1 ? `${lbIndex + 1} / ${lbList.length}` : "";
  lbPrev.hidden = lbList.length < 2;
  lbNext.hidden = lbList.length < 2;
}

function openLightbox(list, index) {
  lbList = list;
  lbIndex = Math.max(0, Math.min(index, list.length - 1));
  lightbox.hidden = false;
  document.body.style.overflow = "hidden";
  showLightbox();
}

function closeLightbox() {
  lightbox.hidden = true;
  lbImg.removeAttribute("src");
  if (drawer.classList.contains("on")) return;
  document.body.style.overflow = "";
}

lbPrev.addEventListener("click", (e) => {
  e.stopPropagation();
  lbIndex = (lbIndex - 1 + lbList.length) % lbList.length;
  showLightbox();
});

lbNext.addEventListener("click", (e) => {
  e.stopPropagation();
  lbIndex = (lbIndex + 1) % lbList.length;
  showLightbox();
});

document.getElementById("lbClose").addEventListener("click", closeLightbox);
lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox || e.target.id === "lbStage") closeLightbox();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (lightboxOpen()) closeLightbox();
    else if (drawer.classList.contains("on")) closeDrawer();
    return;
  }
  if (!lightboxOpen()) return;
  if (e.key === "ArrowLeft") {
    lbIndex = (lbIndex - 1 + lbList.length) % lbList.length;
    showLightbox();
  } else if (e.key === "ArrowRight") {
    lbIndex = (lbIndex + 1) % lbList.length;
    showLightbox();
  }
});

/* ------------------------------------------------------------ clicks */

function handleProfileTarget(target) {
  if (!target || !target.dataset || !target.dataset.service) return false;
  openProfile(target.dataset.service, target.dataset.handle);
  return true;
}

document.addEventListener("submit", (e) => {
  const form = e.target.closest("form[data-link-form]");
  if (!form) return;
  e.preventDefault();
  submitLink(form);
});

document.addEventListener("click", (e) => {
  if (e.target.id === "retryBtn") {
    state.bootRetries = 0;
    resetTimeline();
    loadFirstPage({ force: true });
    return;
  }

  const unlinkBtn = e.target.closest("[data-unlink]");
  if (unlinkBtn) {
    e.preventDefault();
    unlink(unlinkBtn.dataset.unlink);
    return;
  }

  if (handleProfileTarget(e.target.closest("[data-profile]"))) {
    e.preventDefault();
    e.stopPropagation();
    return;
  }

  const mediaBtn = e.target.closest("[data-media]");
  if (mediaBtn) {
    e.preventDefault();
    e.stopPropagation();
    const urls = mediaBtn.dataset.media.split("|");
    openLightbox(urls, Number(mediaBtn.dataset.index) || 0);
    return;
  }

  const anchor = e.target.closest("a");
  if (anchor) return; // リンクはそのまま

  const card = e.target.closest(".post");
  if (card && card.dataset.url) {
    if (String(window.getSelection && window.getSelection()) !== "") return;
    window.open(card.dataset.url, "_blank", "noopener");
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const t = e.target.closest("[data-profile]");
  if (!t) return;
  e.preventDefault();
  handleProfileTarget(t);
});

/* ------------------------------------------------------------ boot */

window.addEventListener("hashchange", () => {
  if (!state.auth || !state.auth.gateOpen) return;
  route();
});

async function boot() {
  updateTabs();
  updateFooter();
  try {
    await loadStatus();
  } catch (err) {
    console.error("status", err);
    state.auth = null;
    showGate("サーバーに接続できませんでした。少し待ってから再読み込みしてください。");
    return;
  }
  if (state.auth && state.auth.gateOpen) openApp();
  else showGate();
}

boot();
