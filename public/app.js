// Loomix 0.3 — frontend
// 一覧はサーバー側のカーソル（before）で少しずつ読み込む。
// スクロールが底に近づいたら /api/timeline?before=... を1回だけ投げる。

const PAGE = 40;
const RETRY_BASE_MS = 1200;
const MAX_AUTO_RETRY = 3;

const state = {
  posts: [],
  ids: new Set(),
  nextBefore: null,
  hasMore: true,
  filter: localStorage.getItem("loomix.filter") || "all",
  hideReplies: localStorage.getItem("loomix.hideReplies") === "1",
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

let autoTimer = null;
let lastLoad = 0;
let retryTimer = null;
let profileSeq = 0;
let chaining = false;

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
  return d.toLocaleString("ja-JP", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function num(n) {
  const v = Number(n) || 0;
  if (v >= 10000) return (v / 10000).toFixed(1).replace(/\.0$/, "") + "万";
  return String(v);
}

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
  document.getElementById("count-all").textContent = total ? total : "";
  ["karotter", "yudetter", "amehare", "pitter"].forEach((s) => {
    const n = state.posts.filter((p) => p.service === s).length;
    document.getElementById("count-" + s).textContent = n ? n : "";
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
  state.loadingFirst = true;
  state.gen += 1;
  const gen = state.gen;
  refreshBtn.classList.add("is-busy");
  if (!state.posts.length) renderSkeleton();
  updateFooter();

  try {
    const res = await fetch(timelineUrl(null, force ? { refresh: "1" } : null), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "error");
    if (gen !== state.gen) return;
    adoptFirstPage(data);
    state.bootRetries = 0;
    updateCounts();
    updateStatus();
    renderAll();
  } catch (err) {
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
  if (retryTimer) {
    clearTimeout(retryTimer);
    retryTimer = null;
  }

  state.isFetching = true;
  const gen = state.gen;
  updateFooter();

  try {
    const res = await fetch(timelineUrl(state.nextBefore), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "error");
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
  try {
    const res = await fetch(timelineUrl(null), { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "error");
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
    console.error("refresh", err);
  }
}

/* ------------------------------------------------------------ filters */

document.getElementById("filterBar").addEventListener("click", async (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  if (tab.dataset.service === state.filter) return;
  state.filter = tab.dataset.service;
  localStorage.setItem("loomix.filter", state.filter);
  updateTabs();
  state.posts = [];
  state.ids = new Set();
  state.nextBefore = null;
  state.hasMore = true;
  state.emptyStreak = 0;
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  await loadFirstPage();
});

hideRepliesEl.checked = state.hideReplies;
hideRepliesEl.addEventListener("change", () => {
  state.hideReplies = hideRepliesEl.checked;
  localStorage.setItem("loomix.hideReplies", state.hideReplies ? "1" : "0");
  renderAll();
  updateCounts();
  chainIfNeeded();
});

refreshBtn.addEventListener("click", async () => {
  state.nextBefore = null;
  state.hasMore = true;
  state.posts = [];
  state.ids = new Set();
  state.emptyStreak = 0;
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
  await loadFirstPage({ force: true });
});

moreBtn.addEventListener("click", () => {
  state.emptyStreak = 0;
  state.nextAllowedAt = 0;
  loadMore();
});

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

autoEl.addEventListener("change", () => {
  if (autoEl.checked) startAuto();
  else stopAuto();
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && autoEl.checked && Date.now() - lastLoad > 60000) refreshNewest();
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

/* ------------------------------------------------------------ profile */

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
    const res = await fetch(`/api/user?service=${encodeURIComponent(service)}&id=${encodeURIComponent(handle)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "error");
    state.profileCache.set(key, data);
    if (seq === profileSeq) renderProfile(data);
  } catch (err) {
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

document.addEventListener("click", (e) => {
  if (e.target.id === "retryBtn") {
    state.bootRetries = 0;
    state.posts = [];
    state.ids = new Set();
    loadFirstPage({ force: true });
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

updateTabs();
updateFooter();
loadFirstPage();
startAuto();
