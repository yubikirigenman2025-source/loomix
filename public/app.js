// Loomix 0.2 — frontend

const PAGE = 40;

const state = {
  posts: [],
  filter: localStorage.getItem("loomix.filter") || "all",
  hideReplies: localStorage.getItem("loomix.hideReplies") === "1",
  shown: PAGE,
  fetchedAt: null,
  counts: {},
  errors: {},
  loading: false,
  profileCache: new Map(),
};

const feedEl = document.getElementById("feed");
const emptyEl = document.getElementById("empty");
const endEl = document.getElementById("feedEnd");
const moreBtn = document.getElementById("moreBtn");
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
let profileSeq = 0;

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

function avatarMarkup(post, size) {
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

function visiblePosts() {
  let list = state.posts;
  if (state.filter !== "all") list = list.filter((p) => p.service === state.filter);
  if (state.hideReplies) list = list.filter((p) => !p.isReply);
  return list;
}

function render() {
  const list = visiblePosts();
  if (!list.length) {
    feedEl.innerHTML = "";
    emptyEl.hidden = false;
    emptyEl.innerHTML = "該当する投稿がありません";
    endEl.hidden = true;
    return;
  }
  emptyEl.hidden = true;
  feedEl.innerHTML = list.slice(0, state.shown).map(postMarkup).join("");
  const rest = list.length - state.shown;
  endEl.hidden = false;
  moreBtn.hidden = rest <= 0;
  stampEl.textContent = rest > 0 ? `表示 ${Math.min(state.shown, list.length)} / ${list.length} 件` : `全 ${list.length} 件`;
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
  const total = state.posts.length;
  document.getElementById("count-all").textContent = total ? total : "";
  ["karotter", "yudetter", "amehare", "pitter"].forEach((s) => {
    const n = state.counts[s];
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

function updateTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    const on = tab.dataset.service === state.filter;
    tab.classList.toggle("is-active", on);
    tab.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

/* ------------------------------------------------------------ data */

async function loadTimeline(force = false, isRetry = false) {
  if (state.loading) return;
  state.loading = true;
  refreshBtn.classList.add("is-busy");
  if (!state.posts.length) renderSkeleton();

  try {
    const res = await fetch(`/api/timeline${force ? "?refresh=1" : ""}`, { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.posts = data.posts || [];
    state.counts = data.serviceCounts || {};
    state.errors = data.errors || {};
    state.fetchedAt = data.fetchedAt;
    state.shown = Math.max(state.shown, PAGE);
    updateCounts();
    updateStatus();
    render();
    lastLoad = Date.now();
  } catch (err) {
    console.error("timeline", err);
    if (!state.posts.length) {
      if (!isRetry) {
        // Render の無料プランは休止からの復帰に時間がかかるため、一度だけ自動で再試行する
        statusEl.textContent = "接続しています";
        setTimeout(() => loadTimeline(true, true), 3000);
      } else {
        feedEl.innerHTML = "";
        emptyEl.hidden = false;
        emptyEl.innerHTML = `<p>タイムラインを取得できませんでした</p>
          <button class="more" id="retryBtn" type="button" style="margin-top:14px">再試行</button>`;
        statusEl.textContent = "接続エラー";
      }
    }
  } finally {
    state.loading = false;
    refreshBtn.classList.remove("is-busy");
  }
}

/* ------------------------------------------------------------ filters */

document.getElementById("filterBar").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  state.filter = tab.dataset.service;
  state.shown = PAGE;
  localStorage.setItem("loomix.filter", state.filter);
  updateTabs();
  render();
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
});

hideRepliesEl.checked = state.hideReplies;
hideRepliesEl.addEventListener("change", () => {
  state.hideReplies = hideRepliesEl.checked;
  state.shown = PAGE;
  localStorage.setItem("loomix.hideReplies", state.hideReplies ? "1" : "0");
  render();
});

refreshBtn.addEventListener("click", () => loadTimeline(true));

function startAuto() {
  stopAuto();
  autoTimer = setInterval(() => {
    if (document.hidden) return;
    loadTimeline(false);
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
  if (!document.hidden && autoEl.checked && Date.now() - lastLoad > 60000) loadTimeline(false);
});

/* ------------------------------------------------------------ infinite */

new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (!entry.isIntersecting) return;
    const list = visiblePosts();
    if (state.shown < list.length) {
      state.shown += PAGE;
      render();
    }
  });
}, { rootMargin: "600px" }).observe(endEl);

moreBtn.addEventListener("click", () => {
  state.shown += PAGE;
  render();
});

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
    loadTimeline(true, true);
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
loadTimeline();
startAuto();
