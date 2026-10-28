import http from "node:http";
import { readFileSync } from "node:fs";
import { extname, join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import ExcelJS from "exceljs";
import { ReviewDatabase } from "./database.js";
import { Collector } from "./collector.js";
import { BrowserAutomation } from "./browser-automation.js";
import { productModel } from "./formatters.js";
import { timingSafeEqual } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, "..");
const dataDir = process.env.PDD_DATA_DIR || join(root, "data");
const serviceToken = process.env.PDD_HUB_TOKEN;
if (!serviceToken) throw new Error("请从脚本工具中心启动此工具");
const db = new ReviewDatabase(join(dataDir, "comments.db"));
const collector = new Collector(db);
const browser = new BrowserAutomation(root);
const launcherPid = process.ppid;
let browserLaunchPromise = null;
const state = {
  browserConnected: false,
  browserMessage: "浏览器尚未启动",
  browserUrl: null,
  browserPageInfo: null,
  lastCaptureAt: null,
  session: null,
};

function addLog(level, message) {
  const time = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  const line = `[${time}] [${level.toUpperCase()}] ${message}`;
  if (level === "error") console.error(line);
  else console.log(line);
}

async function ensureBrowser() {
  if (browser.snapshot().connected) return;
  if (browserLaunchPromise) return browserLaunchPromise;
  browserLaunchPromise = (async () => {
    await browser.launch();
    if (!browser.snapshot().url?.includes("yangkeduo.com")) await browser.openHome();
    addLog("success", "专用 Chrome 已打开");
  })().finally(() => { browserLaunchPromise = null; });
  return browserLaunchPromise;
}

collector.on("log", ({ level, message }) => addLog(level, message));
collector.on("session", (session) => { state.session = session; });
browser.on("state", ({ connected, message, url, pageInfo }) => {
  state.browserConnected = connected;
  state.browserMessage = message;
  state.browserUrl = url ?? null;
  state.browserPageInfo = pageInfo ?? state.browserPageInfo;
});
browser.on("ready", ({ goodsId, capturedAt }) => {
  state.session = { goodsId, capturedAt };
  state.lastCaptureAt = capturedAt;
  addLog("success", `Chrome 已进入商品 ${goodsId} 的评价页`);
});
browser.on("reviews", ({ goodsId, items }) => db.saveReviews(goodsId, items));
browser.on("log", ({ level, message }) => addLog(level, message));

function json(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(body) });
  res.end(body);
}

async function requestBody(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function exportRows(goodsIds = []) {
  return db.allReviews(goodsIds).map((row) => ({
    用户名称: row.username,
    商品型号: productModel(row.specs),
    评论: row.comment,
  }));
}

async function api(req, res, url) {
  if (req.method === "GET" && url.pathname === "/api/status") {
    const browserState = browser.snapshot();
    return json(res, 200, {
      ...state,
      selectedGoodsId: browserState.selectedGoodsId,
      browserBusy: browserState.busy,
      collector: {
        ...collector.snapshot(),
        currentJob: browserState.currentJob,
        sessionReady: Boolean(state.session),
      },
      products: db.listProducts(),
    });
  }
  if (req.method === "GET" && url.pathname === "/api/reviews") {
    return json(res, 200, db.listReviews(url.searchParams.get("goods_id"), 1000));
  }
  if (req.method === "POST" && url.pathname === "/api/browser/open") {
    try {
      if (browser.snapshot().busy) return json(res, 409, { error: "正在采集评论，请等待当前任务完成" });
      await browser.openHome();
      addLog("success", "已打开拼多多首页，请手动选择商品");
      return json(res, 200, { opened: true, url: browser.snapshot().url });
    } catch (error) {
      addLog("error", `打开拼多多失败：${error.message}`);
      return json(res, 500, { error: error.message });
    }
  }
  if (req.method === "POST" && url.pathname === "/api/capture-current") {
    try {
      const body = await requestBody(req);
      const browserState = browser.snapshot();
      if (browserState.busy) return json(res, 409, { error: "已有商品正在采集，请等待完成" });
      const goodsId = browserState.selectedGoodsId;
      if (!goodsId) return json(res, 400, { error: "请先在专用 Chrome 中打开一个商品详情页" });
      void browser.captureCurrent({
        maxReviews: body.maxReviews ?? collector.settings.maxReviews,
        delayMs: body.delayMs ?? collector.settings.delayMs,
        latestFirst: body.latestFirst ?? collector.settings.latestFirst,
      }).catch((error) => addLog("error", `当前商品采集失败：${error.message}`));
      return json(res, 200, { accepted: goodsId });
    } catch (error) {
      return json(res, 400, { error: error.message });
    }
  }
  if (req.method === "POST" && url.pathname === "/api/settings") {
    try {
      const body = await requestBody(req);
      if (body.delayMs != null) collector.settings.delayMs = Math.max(500, Math.min(30_000, Number(body.delayMs)));
      if (body.maxReviews != null) collector.settings.maxReviews = Math.max(1, Math.min(10_000, Number(body.maxReviews)));
      if (body.latestFirst != null) collector.settings.latestFirst = Boolean(body.latestFirst);
      return json(res, 200, collector.settings);
    } catch (error) {
      return json(res, 400, { error: error.message });
    }
  }
  if (req.method === "POST" && url.pathname === "/api/shutdown") {
    json(res, 200, { stopping: true });
    setImmediate(() => void shutdown());
    return;
  }
  if (req.method === "DELETE" && url.pathname === "/api/products") {
    try {
      const body = await requestBody(req);
      if (body.all === true) {
        if (browser.snapshot().busy) return json(res, 409, { error: "正在采集评论，请等待完成后再清空" });
        const deletedReviews = db.clearAll();
        browser.clearProgress();
        state.session = null;
        state.lastCaptureAt = null;
        state.browserPageInfo = null;
        addLog("success", `已清空全部记录，共删除 ${deletedReviews} 条评论`);
        return json(res, 200, { deletedProducts: "all", deletedReviews });
      }
      const goodsIds = [...new Set((body.goodsIds ?? []).map(String).filter((id) => /^\d{6,20}$/.test(id)))];
      if (!goodsIds.length) return json(res, 400, { error: "请先选择要清除的商品" });
      const deletedReviews = db.deleteProducts(goodsIds);
      browser.clearProgress(goodsIds);
      if (state.session && goodsIds.includes(state.session.goodsId)) {
        state.session = null;
        state.lastCaptureAt = null;
      }
      addLog("success", `已清除 ${goodsIds.length} 个商品、${deletedReviews} 条评论`);
      return json(res, 200, { deletedProducts: goodsIds.length, deletedReviews });
    } catch (error) {
      return json(res, 400, { error: error.message });
    }
  }
  if (req.method === "GET" && url.pathname === "/api/export.xlsx") {
    const goodsIds = url.searchParams.getAll("goods_id").filter((id) => /^\d{6,20}$/.test(id));
    const rows = exportRows(goodsIds);
    const workbook = new ExcelJS.Workbook();
    const sheet = workbook.addWorksheet("评论记录");
    const columns = rows.length ? Object.keys(rows[0]) : ["用户名称", "商品型号", "评论"];
    sheet.columns = columns.map((name) => ({ header: name, key: name, width: name === "评论" ? 60 : name === "商品型号" ? 36 : 20 }));
    sheet.addRows(rows);
    sheet.getRow(1).font = { bold: true };
    sheet.views = [{ state: "frozen", ySplit: 1 }];
    const buffer = await workbook.xlsx.writeBuffer();
    res.writeHead(200, {
      "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "content-disposition": `attachment; filename="pdd-reviews-${Date.now()}.xlsx"`,
      "content-length": buffer.byteLength,
    });
    return res.end(Buffer.from(buffer));
  }
  return json(res, 404, { error: "Not found" });
}

const mime = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8" };
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    const supplied = req.headers["x-tool-service-token"];
    if (typeof supplied !== "string" || Buffer.byteLength(supplied) !== Buffer.byteLength(serviceToken) ||
        !timingSafeEqual(Buffer.from(supplied), Buffer.from(serviceToken)))
      return json(res, 403, { error: "请从脚本工具中心访问" });
    if (url.pathname === "/" && process.env.PDD_NO_AUTO_BROWSER !== "1") void ensureBrowser().catch((error) => addLog("error", `打开拼多多失败：${error.message}`));
    if (url.pathname.startsWith("/api/")) return await api(req, res, url);
    const relative = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
    if (relative.includes("..")) return json(res, 400, { error: "Invalid path" });
    const path = join(root, "public", relative);
    const content = readFileSync(path);
    res.writeHead(200, { "content-type": mime[extname(path)] ?? "application/octet-stream" });
    res.end(content);
  } catch (error) {
    if (error.code === "ENOENT") return json(res, 404, { error: "Not found" });
    console.error(error);
    json(res, 500, { error: error.message });
  }
});

const port = Number(process.env.COLLECTOR_PORT ?? 3789);
server.listen(port, "127.0.0.1", () => {
  addLog("info", `本地采集工具已启动：http://localhost:${port}，由工具中心管理`);
  if (process.env.PDD_NO_AUTO_BROWSER !== "1")
    void ensureBrowser().catch((error) => addLog("error", `打开拼多多失败：${error.message}`));
});
server.on("error", (error) => {
  if (error.code === "EADDRINUSE") {
    console.error(`端口 ${port} 已被占用，工具可能已经启动。请打开 http://localhost:${port}`);
  } else {
    console.error(error);
  }
  process.exitCode = 1;
});

let shuttingDown = false;
async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log("[info] 正在停止采集服务并释放浏览器资源...");
  await browser.close();
  db.close();
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
}

process.on("SIGINT", () => void shutdown());
process.on("SIGTERM", () => void shutdown());

const parentWatch = setInterval(() => {
  try {
    process.kill(launcherPid, 0);
  } catch {
    console.log("[info] 启动终端已关闭，正在自动停止服务...");
    void shutdown();
  }
}, 1000);
parentWatch.unref();
