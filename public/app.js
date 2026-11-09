// Loomix Frontend App

let currentService = "all";
let autoRefreshTimer = null;
let allPosts = [];

const timelineEl = document.getElementById("timeline");
const loadingEl = document.getElementById("loading");
const errorEl = document.getElementById("error");
const errorMsgEl = document.getElementById("errorMsg");
const refreshBtn = document.getElementById("refreshBtn");
const lastUpdatedEl = document.getElementById("lastUpdated");
const autoRefreshCheckbox = document.getElementById("autoRefresh");

// Initialize
document.addEventListener("DOMContentLoaded", () => {
  setupFilterChips();
  setupRefreshControls();
  fetchTimeline();
});

function setupFilterChips() {
  const chips = document.querySelectorAll(".chip");
  chips.forEach(chip => {
    chip.addEventListener("click", () => {
      chips.forEach(c => c.classList.remove("active"));
      chip.classList.add("active");
      currentService = chip.dataset.service;
      renderPosts();
    });
  });
}

function setupRefreshControls() {
  refreshBtn.addEventListener("click", () => {
    fetchTimeline(true);
  });

  autoRefreshCheckbox.addEventListener("change", (e) => {
    if (e.target.checked) {
      startAutoRefresh();
    } else {
      stopAutoRefresh();
    }
  });

  startAutoRefresh();
}

function startAutoRefresh() {
  stopAutoRefresh();
  autoRefreshTimer = setInterval(() => {
    fetchTimeline(false);
  }, 30000); // 30 seconds
}

function stopAutoRefresh() {
  if (autoRefreshTimer) {
    clearInterval(autoRefreshTimer);
    autoRefreshTimer = null;
  }
}

async function fetchTimeline(force = false) {
  const icon = refreshBtn.querySelector(".refresh-icon");
  icon.classList.add("spinning");

  if (allPosts.length === 0) {
    loadingEl.style.display = "flex";
    timelineEl.style.display = "none";
    errorEl.style.display = "none";
  }

  try {
    const url = `/api/timeline${force ? "?refresh=1" : ""}`;
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    allPosts = data.posts || [];
    updateBadges(data.serviceCounts, data.total);
    renderPosts();

    const date = new Date(data.fetchedAt || Date.now());
    lastUpdatedEl.textContent = `最終更新: ${date.toLocaleTimeString("ja-JP")}`;

    loadingEl.style.display = "none";
    timelineEl.style.display = "block";
    errorEl.style.display = "none";
  } catch (err) {
    console.error("Fetch error:", err);
    if (allPosts.length === 0) {
      loadingEl.style.display = "none";
      timelineEl.style.display = "none";
      errorEl.style.display = "flex";
      errorMsgEl.textContent = `データの取得に失敗しました: ${err.message}`;
    }
  } finally {
    icon.classList.remove("spinning");
  }
}

function updateBadges(counts, total) {
  document.getElementById("badge-all").textContent = total || 0;
  if (counts) {
    document.getElementById("badge-karotter").textContent = counts.karotter || 0;
    document.getElementById("badge-yudetter").textContent = counts.yudetter || 0;
    document.getElementById("badge-amehare").textContent = counts.amehare || 0;
    document.getElementById("badge-pitter").textContent = counts.pitter || 0;
  }
}

function renderPosts() {
  const filtered = currentService === "all" 
    ? allPosts 
    : allPosts.filter(p => p.service === currentService);

  if (filtered.length === 0) {
    timelineEl.innerHTML = `
      <div class="empty-state" style="padding: 60px 20px; text-align: center; color: var(--text-muted);">
        <p style="font-size: 2rem; margin-bottom: 8px;">📭</p>
        <p>該当する投稿がありません</p>
      </div>
    `;
    return;
  }

  const html = filtered.map(post => renderPostCard(post)).join("");
  timelineEl.innerHTML = html;
}

function renderPostCard(post) {
  const serviceBadgeClass = `badge-${post.service}`;
  const timeFormatted = formatRelativeTime(post.createdAt);
  const autolinkedContent = escapeAndAutolink(post.content);

  // Avatar fallback
  const avatarHtml = post.author.avatarUrl
    ? `<img src="${escapeHtml(post.author.avatarUrl)}" alt="" class="author-avatar" onerror="this.onerror=null; this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><circle cx=%2250%22 cy=%2250%22 r=%2250%22 fill=%22%23334155%22/><text x=%2250%22 y=%2258%22 font-size=%2240%22 fill=%22white%22 text-anchor=%22middle%22>${escapeHtml(post.serviceIcon)}</text></svg>';">`
    : `<div class="author-avatar">${escapeHtml(post.serviceIcon)}</div>`;

  // Media
  let mediaHtml = "";
  if (post.media && post.media.length > 0) {
    const gridClass = `grid-${Math.min(post.media.length, 3)}`;
    const images = post.media.slice(0, 4).map(url => `
      <a href="${escapeHtml(url)}" target="_blank" rel="noopener">
        <img src="${escapeHtml(url)}" alt="" class="media-image" loading="lazy">
      </a>
    `).join("");
    mediaHtml = `<div class="post-media-grid ${gridClass}">${images}</div>`;
  }

  return `
    <article class="post-card" data-id="${escapeHtml(post.id)}">
      <div class="post-header">
        <div class="post-author-info">
          <a href="${escapeHtml(post.author.profileUrl)}" target="_blank" rel="noopener">
            ${avatarHtml}
          </a>
          <div class="author-names">
            <a href="${escapeHtml(post.author.profileUrl)}" target="_blank" rel="noopener" class="display-name">
              ${escapeHtml(post.author.displayName)}
            </a>
            <span class="username">@${escapeHtml(post.author.username)}</span>
          </div>
        </div>
        <div class="post-meta-right">
          <span class="service-badge ${serviceBadgeClass}">
            <span>${post.serviceIcon}</span>
            <span>${escapeHtml(post.serviceName)}</span>
          </span>
          <a href="${escapeHtml(post.url)}" target="_blank" rel="noopener" class="post-time" title="${escapeHtml(post.createdAt || '')}">
            ${timeFormatted}
          </a>
        </div>
      </div>

      <div class="post-body">${autolinkedContent}</div>
      ${mediaHtml}

      <div class="post-footer">
        <div class="metrics-group">
          <span class="metric-item" title="いいね">
            <span>❤️</span>
            <span>${post.metrics.likes || 0}</span>
          </span>
          <span class="metric-item" title="リポスト">
            <span>🔁</span>
            <span>${post.metrics.reposts || 0}</span>
          </span>
          <span class="metric-item" title="返信">
            <span>💬</span>
            <span>${post.metrics.replies || 0}</span>
          </span>
        </div>
        <a href="${escapeHtml(post.url)}" target="_blank" rel="noopener" class="btn-origin-link">
          本家で開く ↗
        </a>
      </div>
    </article>
  `;
}

function formatRelativeTime(dateStr) {
  if (!dateStr) return "";
  try {
    const date = new Date(dateStr);
    const now = new Date();
    const diffSec = Math.floor((now - date) / 1000);

    if (diffSec < 0) return "たった今";
    if (diffSec < 60) return `${diffSec}秒前`;
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}分前`;
    const diffHour = Math.floor(diffMin / 60);
    if (diffHour < 24) return `${diffHour}時間前`;
    const diffDay = Math.floor(diffHour / 24);
    if (diffDay < 7) return `${diffDay}日前`;

    return `${date.getMonth() + 1}/${date.getDate()}`;
  } catch (e) {
    return "";
  }
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAndAutolink(text) {
  if (!text) return "";
  const escaped = escapeHtml(text);
  const urlRegex = /(https?:\/\/[^\s<]+)/g;
  return escaped.replace(urlRegex, (url) => {
    return `<a href="${url}" target="_blank" rel="noopener">${url}</a>`;
  });
}
