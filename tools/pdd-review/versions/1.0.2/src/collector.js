import { EventEmitter } from "node:events";

const REVIEW_RE = /\/proxy\/api\/reviews\/(\d+)\/list/i;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function headersFromRecord(record) {
  const output = {};
  const cookies = [];
  for (const header of record.request?.headers ?? []) {
    const name = String(header.name).toLowerCase();
    if (["host", "content-length", "connection", "accept-encoding", "priority"].includes(name)) continue;
    if (name === "cookie") cookies.push(header.value);
    else output[name] = header.value;
  }
  if (cookies.length) output.cookie = cookies.join("; ");
  return output;
}

export function goodsIdFromText(value) {
  const text = String(value ?? "").trim();
  const patterns = [
    /\/reviews\/(\d+)\//i,
    /[?&]goods_id=(\d+)/i,
    /\bgoods_id\s*[=:]\s*["']?(\d+)/i,
    /^\d{6,20}$/,
  ];
  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match) return match[1] ?? match[0];
  }
  return null;
}

export function sessionFromRecord(record) {
  const match = String(record.url ?? "").match(REVIEW_RE);
  if (!match || record.request?.method !== "POST" || record.response?.code !== 200) return null;
  let body = {};
  try {
    body = JSON.parse(record.request?.body?.text ?? "{}");
  } catch {
    return null;
  }
  const headers = headersFromRecord(record);
  const antiContent = body.anti_content ?? headers["anti-content"];
  if (!antiContent || !headers.cookie) return null;
  return {
    sourceGoodsId: match[1],
    sourceUrl: record.url,
    headers,
    body: { ...body, anti_content: antiContent },
    capturedAt: new Date().toISOString(),
    recordUid: record.uid ?? String(record.id),
  };
}

export class Collector extends EventEmitter {
  constructor(database) {
    super();
    this.database = database;
    this.session = null;
    this.requester = null;
    this.queue = [];
    this.queued = new Set();
    this.running = false;
    this.currentJob = null;
    this.settings = { delayMs: 1400, maxReviews: 100, latestFirst: true };
  }

  setSession(session) {
    this.session = session;
    this.emit("session", { goodsId: session.sourceGoodsId, capturedAt: session.capturedAt });
    void this.#drain();
  }

  setRequester(requester) {
    this.requester = requester;
  }

  enqueue(goodsId, options = {}) {
    const id = goodsIdFromText(goodsId);
    if (!id) throw new Error(`无法识别商品 ID：${goodsId}`);
    if (this.queued.has(id) || this.currentJob?.goodsId === id) return false;
    this.queue.push({ goodsId: id, maxReviews: Number(options.maxReviews) || this.settings.maxReviews });
    this.queued.add(id);
    this.emit("queue", this.snapshot());
    void this.#drain();
    return true;
  }

  snapshot() {
    return {
      currentJob: this.currentJob,
      queue: this.queue.map((item) => item.goodsId),
      settings: this.settings,
      sessionReady: Boolean(this.session),
    };
  }

  async #drain() {
    if (this.running) return;
    this.running = true;
    try {
      while (this.queue.length) {
        if (!this.session) break;
        const job = this.queue.shift();
        this.queued.delete(job.goodsId);
        try {
          await this.#collect(job);
        } catch (error) {
          this.emit("log", { level: "error", message: `商品 ${job.goodsId} 采集失败：${error.message}` });
        } finally {
          this.currentJob = null;
          this.emit("queue", this.snapshot());
        }
      }
    } finally {
      this.running = false;
    }
  }

  async #collect(job) {
    let total = 0;
    let emptyPages = 0;
    this.currentJob = { goodsId: job.goodsId, page: 0, saved: 0, status: "running" };
    this.emit("log", { level: "info", message: `开始采集商品 ${job.goodsId}` });
    for (let page = 1; page <= 1000 && total < job.maxReviews; page += 1) {
      this.currentJob = { goodsId: job.goodsId, page, saved: total, status: "running" };
      this.emit("queue", this.snapshot());
      const payload = await this.#requestPage(job.goodsId, page);
      if (!Array.isArray(payload.data)) {
        const message = payload.error_msg ?? payload.message ?? payload.msg ?? "响应中没有 data";
        throw new Error(message);
      }
      if (payload.data.length === 0) {
        emptyPages += 1;
        if (emptyPages >= 1) break;
      } else {
        emptyPages = 0;
        const remaining = job.maxReviews - total;
        const reviews = payload.data.slice(0, remaining);
        this.database.saveReviews(job.goodsId, reviews);
        total += reviews.length;
        this.currentJob.saved = total;
        this.emit("log", { level: "info", message: `商品 ${job.goodsId}：第 ${page} 页返回 ${payload.data.length} 条` });
      }
      if (payload.data.length < 20) break;
      await sleep(this.settings.delayMs + Math.floor(Math.random() * 500));
    }
    this.database.refreshProductCount(job.goodsId);
    this.currentJob = { goodsId: job.goodsId, page: this.currentJob.page, saved: total, status: "done" };
    this.emit("log", { level: "success", message: `商品 ${job.goodsId} 采集完成，本次采集 ${total} 条` });
  }

  async #requestPage(goodsId, page) {
    const session = this.session;
    if (this.requester) return this.requester(session, goodsId, page);
    const sourceId = session.sourceGoodsId;
    const url = new URL(session.sourceUrl);
    url.pathname = url.pathname.replace(REVIEW_RE, `/proxy/api/reviews/${goodsId}/list`);
    url.searchParams.set("label_id", "0");
    url.searchParams.set("page", String(page));
    url.searchParams.set("size", "20");
    const headers = { ...session.headers };
    if (headers.referer) headers.referer = headers.referer.replaceAll(sourceId, goodsId);
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(session.body),
      redirect: "follow",
      signal: AbortSignal.timeout(30_000),
    });
    const text = await response.text();
    let payload;
    try {
      payload = JSON.parse(text);
    } catch {
      throw new Error(`HTTP ${response.status}，响应不是 JSON`);
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}：${payload.message ?? payload.msg ?? "请求失败"}`);
    return payload;
  }
}
