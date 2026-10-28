const $ = (selector) => document.querySelector(selector);
const fmt = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
const excludedProducts = new Set();
let latestProducts = [];

async function api(path, options) {
  const response = await fetch(path, options);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function render(state) {
  latestProducts = state.products;
  const existingIds = new Set(state.products.map((item) => item.goods_id));
  for (const id of excludedProducts) if (!existingIds.has(id)) excludedProducts.delete(id);
  $("#browserState").textContent = state.browserConnected ? "已启动" : "未启动";
  $("#browserMessage").textContent = state.browserMessage || "—";
  $("#selectedGoods").textContent = state.selectedGoodsId
    ? `当前选择：商品 ${state.selectedGoodsId}`
    : "尚未检测到商品页面，请在专用 Chrome 中打开商品详情";
  $("#sessionState").textContent = state.session ? "可用" : "未捕获";
  $("#sessionDetail").textContent = state.session ? `商品 ${state.session.goodsId} · ${fmt(state.session.capturedAt)}` : "请在网页中进入商品评价页";
  $("#productCount").textContent = state.products.length;
  const total = state.products.reduce((sum, item) => sum + Number(item.review_count), 0);
  $("#reviewCount").textContent = total.toLocaleString("zh-CN");
  const ready = state.browserConnected && state.session;
  $("#overallState").textContent = ready ? "已就绪" : state.browserConnected ? "等待评价请求" : "等待启动浏览器";
  $("#overallState").className = `state-pill ${ready ? "ready" : "waiting"}`;
  $("#latestFirst").checked = state.collector.settings.latestFirst;
  if (document.activeElement !== $("#maxReviews")) $("#maxReviews").value = state.collector.settings.maxReviews;
  if (document.activeElement !== $("#delayMs")) $("#delayMs").value = state.collector.settings.delayMs;

  const job = state.collector.currentJob;
  const isRunning = Boolean(state.browserBusy || job?.status === "running");
  $("#startButton").disabled = isRunning;
  $("#startButton").textContent = isRunning ? "正在采集…" : "抓取当前商品";
  $("#openBrowserButton").disabled = isRunning;
  $("#jobBadge").textContent = job ? (job.status === "done" ? "已完成" : job.status === "error" ? "已中止" : "采集中") : "空闲";
  $("#currentJob").className = job ? "job" : "empty-state";
  $("#currentJob").innerHTML = job ? `
    <span class="job-id">${job.goodsId}</span>
    <div class="job-numbers">
      <div><span>加载轮次</span><strong>${job.page}</strong></div>
      <div><span>本次采集评论</span><strong>${job.saved}</strong></div>
    </div>
    ${job.status === "error" ? `<p class="job-error">${escapeHtml(job.error || "采集失败")}</p>` : ""}` : `<div class="pulse-icon">◎</div><p>等待捕获评论请求</p>`;
  $("#queueText").textContent = state.collector.queue.length ? state.collector.queue.join("、") : "无";

  $("#productRows").innerHTML = state.products.length ? state.products.map((item) => `
    <tr><td><input class="product-check" type="checkbox" data-goods-id="${item.goods_id}" ${excludedProducts.has(item.goods_id) ? "" : "checked"}></td><td>${item.goods_id}</td><td>${Number(item.review_count).toLocaleString("zh-CN")}</td><td>${fmt(item.last_collected)}</td></tr>
  `).join("") : `<tr><td colspan="4" class="muted">暂无数据</td></tr>`;
  const selectedCount = state.products.filter((item) => !excludedProducts.has(item.goods_id)).length;
  $("#selectAllProducts").checked = state.products.length > 0 && selectedCount === state.products.length;
  $("#selectAllProducts").indeterminate = selectedCount > 0 && selectedCount < state.products.length;

}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
}

async function refresh() {
  try { render(await api("/api/status")); } catch (error) { $("#overallState").textContent = error.message; }
}

async function saveSettings() {
  await api("/api/settings", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      maxReviews: Number($("#maxReviews").value),
      delayMs: Number($("#delayMs").value),
      latestFirst: $("#latestFirst").checked,
    }),
  });
}

$("#openBrowserButton").addEventListener("click", async () => {
  const button = $("#openBrowserButton");
  const message = $("#formMessage");
  button.disabled = true;
  button.textContent = "正在打开…";
  try {
    await api("/api/browser/open", { method: "POST" });
    message.textContent = "已打开拼多多，请在专用 Chrome 中选择商品";
    await refresh();
  } catch (error) {
    message.textContent = `打开失败：${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = "打开拼多多";
  }
});

$("#startButton").addEventListener("click", async () => {
  const message = $("#formMessage");
  const button = $("#startButton");
  button.disabled = true;
  button.textContent = "正在启动…";
  try {
    await saveSettings();
    const result = await api("/api/capture-current", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        maxReviews: Number($("#maxReviews").value),
        delayMs: Number($("#delayMs").value),
        latestFirst: $("#latestFirst").checked,
      }),
    });
    message.textContent = `已开始抓取商品 ${result.accepted}`;
    await refresh();
  } catch (error) {
    message.textContent = error.message;
    button.disabled = false;
    button.textContent = "抓取当前商品";
  }
});

$("#productRows").addEventListener("change", (event) => {
  const checkbox = event.target.closest(".product-check");
  if (!checkbox) return;
  if (checkbox.checked) excludedProducts.delete(checkbox.dataset.goodsId);
  else excludedProducts.add(checkbox.dataset.goodsId);
});

$("#selectAllProducts").addEventListener("change", (event) => {
  if (event.target.checked) excludedProducts.clear();
  else for (const item of latestProducts) excludedProducts.add(item.goods_id);
  refresh();
});

function selectedGoodsIds() {
  return latestProducts.map((item) => item.goods_id).filter((id) => !excludedProducts.has(id));
}

$("#exportButton").addEventListener("click", () => {
  const selected = selectedGoodsIds();
  const message = $("#formMessage");
  if (!selected.length) { message.textContent = "请先勾选要导出的商品"; return; }
  const params = new URLSearchParams();
  for (const id of selected) params.append("goods_id", id);
  window.location.href = `/api/export.xlsx?${params}`;
  message.textContent = `正在导出 ${selected.length} 个商品`;
});

$("#clearButton").addEventListener("click", async () => {
  const message = $("#formMessage");
  if (!window.confirm("确定清空全部商品、评论和当前进度吗？此操作无法撤销。")) return;
  try {
    const result = await api("/api/products", {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ all: true }),
    });
    excludedProducts.clear();
    message.textContent = `已清空全部记录，共删除 ${result.deletedReviews} 条评论`;
    await refresh();
  } catch (error) { message.textContent = error.message; }
});

for (const id of ["maxReviews", "delayMs", "latestFirst"]) {
  $("#" + id).addEventListener("change", () => saveSettings().catch(() => {}));
}

async function initialize() {
  await refresh();
  setInterval(refresh, 1800);
}

initialize();
