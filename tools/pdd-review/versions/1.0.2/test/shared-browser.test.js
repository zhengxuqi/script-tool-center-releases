import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { BrowserAutomation } from "../src/browser-automation.js";

process.env.PDD_BROWSER_ENDPOINT = "http://127.0.0.1:9351";

class Page extends EventEmitter {
  constructor(url) { super(); this.address = url; this.navigations = []; }
  url() { return this.address; }
  isClosed() { return false; }
  mainFrame() { return this; }
  async goto(url) { this.navigations.push(url); this.address = url; }
}

function fixture(pages) {
  const context = new EventEmitter();
  context.pages = () => pages;
  context.newPage = async () => { const page = new Page("about:blank"); pages.push(page); context.emit("page", page); return page; };
  context.close = async () => assert.fail("The shared context must never be closed");
  const browser = new EventEmitter();
  browser.contexts = () => [context];
  browser.close = async () => { browser.emit("disconnected"); browser.disconnected = true; };
  const type = { connectOverCDP: async (endpoint) => { assert.equal(endpoint, process.env.PDD_BROWSER_ENDPOINT); return browser; } };
  return { automation: new BrowserAutomation("unused", { browserType: type }), browser, context };
}

test("reuses the PDD tab in shared Chrome and disconnects without closing other tools", async () => {
  const bi = new Page("https://aokesi.ecbis.cn/");
  const dmp = new Page("https://dmp.taobao.com/");
  const pdd = new Page("https://mobile.yangkeduo.com/goods.html?goods_id=123456789012");
  const { automation, browser, context } = fixture([bi, dmp, pdd]);
  await automation.launch();
  assert.equal(automation.snapshot().selectedGoodsId, "123456789012");
  assert.equal(automation.page, pdd);
  assert.deepEqual(bi.navigations, []);
  assert.deepEqual(dmp.navigations, []);
  await automation.close();
  assert.equal(browser.disconnected, true);
  assert.equal(context.listenerCount("page"), 0);
  for (const page of [bi, dmp, pdd]) assert.equal(page.listenerCount("response"), 0);
  assert.equal(automation.snapshot().connected, false);
});

test("creates its own blank tab when the shared browser only has other tools", async () => {
  const bi = new Page("https://aokesi.ecbis.cn/");
  const pages = [bi];
  const { automation } = fixture(pages);
  await automation.launch();
  assert.equal(pages.length, 2);
  assert.notEqual(automation.page, bi);
  assert.deepEqual(bi.navigations, []);
  assert.equal(automation.snapshot().selectedGoodsId, null);
  await automation.close();
});

test("a lookalike host is never selected as a PDD tab", async () => {
  const unrelated = new Page("https://yangkeduo.com.example.org/goods.html?goods_id=123456789012");
  const { automation } = fixture([unrelated]);
  await automation.launch();
  assert.notEqual(automation.page, unrelated);
  assert.equal(automation.snapshot().selectedGoodsId, null);
  await automation.close();
});
