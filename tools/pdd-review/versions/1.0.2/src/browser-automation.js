import { EventEmitter } from "node:events";
import { chromium } from "playwright-core";
import { goodsIdFromText } from "./collector.js";

const REVIEW_RE = /\/proxy\/api\/(?:api\/)?reviews\/(\d+)\/list/i;
const isPddPage = (page) => {
  if (page.isClosed()) return false;
  try {
    const host = new URL(page.url()).hostname;
    return ["yangkeduo.com", "pinduoduo.com"].some((domain) => host === domain || host.endsWith("." + domain));
  } catch { return false; }
};

export class BrowserAutomation extends EventEmitter {
  constructor(root, { browserType = chromium } = {}) {
    super();
    this.root = root;
    this.browserType = browserType;
    this.browser = null;
    this.pageListeners = new Map();
    this.contextPageListener = null;
    this.disconnectListener = null;
    this.context = null;
    this.page = null;
    this.connected = false;
    this.message = "浏览器尚未启动";
    this.pageInfo = null;
    this.reviewNetworkUrls = [];
    this.apiNetworkUrls = [];
    this.currentJob = null;
    this.captureInProgress = false;
  }

  async launch() {
    if (this.context) return;
    const endpoint = process.env.PDD_BROWSER_ENDPOINT;
    if (!endpoint || !/^http:\/\/127\.0\.0\.1:\d+$/.test(endpoint))
      throw new Error("请从工具中心连接平台共享 Chrome");
    this.message = "正在连接共享 Chrome";
    this.emit("state", this.snapshot());
    this.browser = await this.browserType.connectOverCDP(endpoint, { timeout: 15000 });
    this.context = this.browser.contexts()[0];
    if (!this.context) {
      await this.browser.close();
      this.browser = null;
      throw new Error("共享 Chrome 没有可用窗口，请正常打开 Chrome 后重试");
    }
    this.contextPageListener = (page) => this.#attachPage(page);
    this.context.on("page", this.contextPageListener);
    this.disconnectListener = () => {
      this.#detachListeners();
      this.browser = null;
      this.context = null;
      this.page = null;
      this.connected = false;
      this.message = "浏览器已关闭";
      this.emit("state", this.snapshot());
    };
    this.browser.on("disconnected", this.disconnectListener);
    const pddPages = this.context.pages().filter(isPddPage);
    this.page = [...pddPages].reverse().find((page) => goodsIdFromText(page.url()))
      ?? pddPages.at(-1) ?? await this.context.newPage();
    for (const page of this.context.pages()) this.#attachPage(page);
    this.connected = true;
    this.message = "已连接平台共享 Chrome";
    this.emit("state", this.snapshot());
  }

  #attachPage(page) {
    if (this.pageListeners.has(page)) return;
    // Observe reviews only on PDD pages; other tools' tabs are never selected or navigated.
    const response = (value) => {
      if (isPddPage(page)) void this.#handleResponse(value);
    };
    const navigated = (frame) => {
      if (frame !== page.mainFrame() || !isPddPage(page)) return;
      this.page = page;
      this.message = goodsIdFromText(page.url())
        ? "已检测到商品页面，可以点击抓取"
        : "请在共享 Chrome 中手动选择商品";
      this.emit("state", this.snapshot());
    };
    const closed = () => { if (this.page === page) this.page = null; this.pageListeners.delete(page); };
    this.pageListeners.set(page, { response, navigated, closed });
    page.on("response", response);
    page.on("framenavigated", navigated);
    page.on("close", closed);
  }

  #detachListeners() {
    for (const [page, handlers] of this.pageListeners) {
      page.off("response", handlers.response);
      page.off("framenavigated", handlers.navigated);
      page.off("close", handlers.closed);
    }
    this.pageListeners.clear();
    if (this.context && this.contextPageListener) this.context.off("page", this.contextPageListener);
    if (this.browser && this.disconnectListener) this.browser.off("disconnected", this.disconnectListener);
    this.contextPageListener = null;
    this.disconnectListener = null;
  }

  #activePage() {
    const pages = (this.context?.pages() ?? []).filter(isPddPage);
    return [...pages].reverse().find((page) => goodsIdFromText(page.url()))
      ?? (this.page && isPddPage(this.page) ? this.page : pages.at(-1));
  }

  async openHome() {
    if (this.captureInProgress) throw new Error("正在采集评论，请等待当前任务完成");
    await this.launch();
    this.page = this.#activePage() ?? (this.page && !this.page.isClosed() ? this.page : await this.context.newPage());
    this.#attachPage(this.page);
    this.message = "正在打开拼多多，请手动选择商品";
    this.emit("state", this.snapshot());
    await this.page.goto("https://mobile.yangkeduo.com/index.html", {
      waitUntil: "domcontentloaded",
      timeout: 45_000,
    });
    await this.page.waitForTimeout(1000);
    this.message = this.page.url().includes("/login.html")
      ? "请先在共享 Chrome 中登录，然后手动选择商品"
      : "请在共享 Chrome 中手动选择商品，选好后点击抓取";
    await this.#refreshPageInfo();
    this.emit("state", this.snapshot());
  }

  async close() {
    const browser = this.browser;
    this.#detachListeners();
    // For connectOverCDP, browser.close disconnects the transport; never close the shared context.
    if (browser) await browser.close().catch(() => {});
    this.browser = null;
    this.context = null;
    this.page = null;
    this.connected = false;
    this.captureInProgress = false;
  }

  async #handleResponse(response) {
    const responseUrl = response.url();
    const resourceType = response.request().resourceType();
    if (["xhr", "fetch"].includes(resourceType) && responseUrl.includes("yangkeduo.com")) {
      this.apiNetworkUrls.push(responseUrl);
      if (this.apiNetworkUrls.length > 80) this.apiNetworkUrls.shift();
    }
    if (/review/i.test(responseUrl)) {
      this.reviewNetworkUrls.push(responseUrl);
      if (this.reviewNetworkUrls.length > 30) this.reviewNetworkUrls.shift();
      this.emit("state", this.snapshot());
    }
    const match = responseUrl.match(REVIEW_RE);
    if (!match || response.status() !== 200) return;
    const request = response.request();
    let body = {};
    try { body = JSON.parse(request.postData() ?? "{}"); } catch { return; }
    const headers = await request.allHeaders();
    const antiContent = body.anti_content ?? headers["anti-content"];
    if (!antiContent) return;
    this.emit("network-session", {
      sourceGoodsId: match[1], sourceUrl: responseUrl, headers,
      body: { ...body, anti_content: antiContent }, capturedAt: new Date().toISOString(),
    });
  }

  async openGoods(input, options = {}) {
    const goodsId = goodsIdFromText(input);
    if (!goodsId) throw new Error(`无法从输入中识别商品 ID：${input}`);
    await this.launch();
    if (!this.page || this.page.isClosed()) {
      this.page = await this.context.newPage();
      this.#attachPage(this.page);
    }
    const source = String(input);
    const goodsUrl = /^https?:\/\//i.test(source)
      ? source
      : `https://mobile.yangkeduo.com/goods.html?goods_id=${goodsId}`;
    this.message = `正在打开商品 ${goodsId}`;
    this.emit("state", this.snapshot());
    await this.page.goto(goodsUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
    await this.page.waitForTimeout(1800);
    await this.#refreshPageInfo();
    return this.#openReviewsAndScrape(goodsId, options);
  }

  async captureCurrent(options = {}) {
    if (this.captureInProgress || this.currentJob?.status === "running") {
      throw new Error("已有商品正在采集，请等待完成");
    }
    this.captureInProgress = true;
    this.emit("state", this.snapshot());
    try {
      await this.launch();
      const page = this.#activePage();
      if (!page) throw new Error("没有可用的 Chrome 页面");
      this.page = page;
      const goodsId = goodsIdFromText(page.url());
      if (!goodsId) throw new Error("当前页面不是商品详情页，请先在共享 Chrome 中打开一个商品");
      return await this.#openReviewsAndScrape(goodsId, options);
    } catch (error) {
      const message = /Execution context was destroyed|navigation/i.test(error.message)
        ? "采集过程中页面被切换，请在目标商品页重新点击抓取"
        : error.message;
      if (this.currentJob?.status === "running") {
        this.currentJob = { ...this.currentJob, status: "error", error: message };
      }
      this.message = message;
      this.emit("state", this.snapshot());
      throw new Error(message);
    } finally {
      this.captureInProgress = false;
      this.emit("state", this.snapshot());
    }
  }

  async #openReviewsAndScrape(goodsId, options = {}) {
    if (this.currentJob?.status === "running") throw new Error("已有商品正在采集，请等待完成");
    await this.#refreshPageInfo();
    const reviewLinks = this.page.getByText(/查看全部评价|商品评价|全部评价/);
    const count = await reviewLinks.count();
    for (let index = 0; index < Math.min(count, 12); index += 1) {
      const candidate = reviewLinks.nth(index);
      if (!await candidate.isVisible().catch(() => false)) continue;
      await candidate.click({ timeout: 5000 }).catch(() => {});
      break;
    }
    await this.page.waitForTimeout(3000);
    if (!this.page.url().includes("goods_comments.html")) {
      const commentsUrl = `https://mobile.yangkeduo.com/goods_comments.html?goods_id=${goodsId}&refer_page_name=goods_detail&refer_page_sn=10014`;
      await this.page.goto(commentsUrl, { waitUntil: "domcontentloaded", timeout: 45_000 });
    }
    await this.page.waitForTimeout(1500);
    await this.#refreshPageInfo();
    if (this.page.url().includes("/login.html")) {
      this.message = "请在共享 Chrome 中完成登录，登录后会自动继续";
      this.emit("state", this.snapshot());
      await this.page.waitForURL(/goods_comments\.html/i, { timeout: 10 * 60_000, waitUntil: "domcontentloaded" });
      await this.page.waitForTimeout(2000);
      await this.#refreshPageInfo();
    }
    if (!this.page.url().includes("goods_comments.html")) throw new Error("未能进入商品评价页");
    if (options.latestFirst !== false) await this.#selectLatestReviews();
    else this.emit("log", { level: "info", message: "未勾选“最新”，按页面默认顺序采集" });
    this.message = `正在读取商品 ${goodsId} 的页面评论`;
    this.emit("state", this.snapshot());
    this.emit("ready", { goodsId, capturedAt: new Date().toISOString() });
    return this.#scrapeReviews(goodsId, {
      maxReviews: Math.max(1, Math.min(10_000, Number(options.maxReviews) || 100)),
      delayMs: Math.max(500, Number(options.delayMs) || 1400),
    });
  }

  async #selectLatestReviews() {
    await this.page.waitForFunction(() => [...document.querySelectorAll("li")]
      .some((element) => (element.textContent || "").includes("最新")), null, { timeout: 8000 }).catch(() => {});
    const candidates = this.page.locator("li").filter({ hasText: "最新" });
    const count = await candidates.count().catch(() => 0);
    for (let index = 0; index < count; index += 1) {
      const candidate = candidates.nth(index);
      const clicked = await candidate.evaluate((element) => {
        element.scrollIntoView({ block: "center" });
        element.click();
        return true;
      }).catch(() => false);
      if (!clicked) continue;
      await this.page.waitForTimeout(1500);
      await this.page.evaluate(() => {
        window.scrollTo(0, 0);
        const list = document.querySelector(".pdd-list-view");
        if (list) list.scrollTop = 0;
      }).catch(() => {});
      this.emit("log", { level: "info", message: "已选择“最新”评论" });
      return true;
    }
    this.emit("log", { level: "info", message: "评价页未显示“最新”筛选，按当前顺序采集" });
    return false;
  }

  async #scrapeReviews(goodsId, { maxReviews, delayMs }) {
    const seen = new Set();
    let stagnantRounds = 0;
    this.currentJob = { goodsId, page: 0, saved: 0, status: "running" };
    this.emit("log", { level: "info", message: `开始从 Chrome 页面采集商品 ${goodsId}` });
    const hasNoReviews = await this.page.evaluate(() => {
      const text = document.body?.innerText || "";
      return /暂无(?:相关)?(?:评价|评论)|还没有.{0,8}(?:评价|评论)/.test(text);
    }).catch(() => false);
    if (hasNoReviews) {
      this.currentJob = { goodsId, page: 0, saved: 0, status: "done" };
      this.message = `商品 ${goodsId} 暂无评论`;
      this.emit("log", { level: "success", message: this.message });
      this.emit("state", this.snapshot());
      return { goodsId, count: 0 };
    }
    for (let round = 1; round <= 1000; round += 1) {
      if (!this.page.url().includes("goods_comments.html") || goodsIdFromText(this.page.url()) !== goodsId) {
        throw new Error("采集过程中页面被切换，请在目标商品页重新点击抓取");
      }
      const items = await this.page.evaluate(() => {
        const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
        const specPattern = /^(款式|型号|规格|颜色|版本|尺寸|尺码|类型|套餐|口味|容量|净含量|数量)[：:]/;
        const specLeaves = [...document.querySelectorAll("*")].filter((element) => element.children.length === 0 && specPattern.test(normalize(element.textContent)));
        const cards = [];
        for (const specElement of specLeaves) {
          let card = specElement;
          while (card && card.parentElement && card.parentElement.classList.contains("pdd-list-container") === false) {
            if (card.parentElement.classList.contains("pdd-list-container")) break;
            card = card.parentElement;
            if (card.classList.contains("pdd-list-container")) break;
          }
          if (!card || card.classList.contains("pdd-list-container")) continue;
          const lines = (card.innerText || "").split("\n").map(normalize).filter(Boolean);
          const specText = normalize(specElement.textContent);
          const specIndex = lines.indexOf(specText);
          if (specIndex < 1 || specIndex >= lines.length - 1) continue;
          const username = lines[0];
          const comment = lines[specIndex + 1];
          const separator = specText.indexOf("：") >= 0 ? "：" : ":";
          const [specKey, ...specParts] = specText.split(separator);
          if (!username || !comment) continue;
          cards.push({
            name: username,
            comment,
            specs: JSON.stringify([{ spec_key: specKey, spec_value: specParts.join(separator) }]),
            review_id: `${username}|${specText}|${comment}`,
          });
        }
        return cards;
      });
      const fresh = [];
      for (const item of items) {
        if (seen.has(item.review_id)) continue;
        if (seen.size >= maxReviews) break;
        seen.add(item.review_id);
        fresh.push(item);
      }
      if (fresh.length) {
        stagnantRounds = 0;
        this.emit("reviews", { goodsId, items: fresh });
      } else {
        stagnantRounds += 1;
      }
      this.currentJob = { goodsId, page: round, saved: seen.size, status: "running" };
      this.emit("state", this.snapshot());
      if (seen.size >= maxReviews) break;
      if (stagnantRounds >= 3) break;
      await this.page.evaluate(() => {
        window.scrollTo(0, document.documentElement.scrollHeight);
        const list = document.querySelector(".pdd-list-view");
        if (list) { list.scrollTop = list.scrollHeight; list.dispatchEvent(new Event("scroll", { bubbles: true })); }
      });
      await this.page.waitForTimeout(delayMs);
    }
    this.currentJob = { goodsId, page: this.currentJob.page, saved: seen.size, status: "done" };
    this.message = `商品 ${goodsId} 页面采集完成，共识别 ${seen.size} 条`;
    this.emit("log", { level: "success", message: this.message });
    this.emit("state", this.snapshot());
    return { goodsId, count: seen.size };
  }

  async #refreshPageInfo() {
    if (!this.page || this.page.isClosed()) return;
    this.pageInfo = {
      url: this.page.url(),
      title: await this.page.title().catch(() => ""),
      text: await this.page.locator("body").innerText({ timeout: 5000 }).catch(() => ""),
      resources: await this.page.evaluate(() => performance.getEntriesByType("resource").map((entry) => entry.name).filter((name) => name.includes("yangkeduo.com")).slice(-80)).catch(() => []),
      specSamples: await this.page.evaluate(() => {
        const leaves = [...document.querySelectorAll("*")].filter((element) => element.children.length === 0 && /^(款式|型号|规格|颜色|版本|尺寸|尺码|类型|套餐|口味|容量|净含量|数量)[：:]/.test((element.textContent || "").trim()));
        return leaves.slice(0, 3).map((element) => {
          const ancestors = [];
          let current = element;
          for (let depth = 0; depth < 7 && current; depth += 1, current = current.parentElement) {
            ancestors.push({ depth, tag: current.tagName, className: current.className, text: (current.innerText || "").slice(0, 1200), html: current.outerHTML.slice(0, 4500) });
          }
          return { text: element.textContent, ancestors };
        });
      }).catch(() => []),
    };
    this.emit("state", this.snapshot());
  }

  clearProgress(goodsIds = null) {
    const shouldClear = !goodsIds || (this.currentJob && goodsIds.includes(this.currentJob.goodsId));
    if (shouldClear) this.currentJob = null;
    this.message = goodsIdFromText(this.#activePage()?.url() ?? "")
      ? "已检测到商品页面，可以点击抓取"
      : "请在共享 Chrome 中手动选择商品";
    this.emit("state", this.snapshot());
  }

  snapshot() {
    return {
      connected: this.connected,
      message: this.message,
      url: this.#activePage()?.url() ?? null,
      pageInfo: this.pageInfo ? { ...this.pageInfo, text: this.pageInfo.text.slice(0, 1200) } : null,
      reviewNetworkUrls: this.reviewNetworkUrls,
      apiNetworkUrls: this.apiNetworkUrls,
      currentJob: this.currentJob,
      selectedGoodsId: goodsIdFromText(this.#activePage()?.url() ?? ""),
      busy: this.captureInProgress || this.currentJob?.status === "running",
    };
  }
}
