import test from "node:test";
import assert from "node:assert/strict";
import { goodsIdFromText, sessionFromRecord } from "../src/collector.js";
import { productModel } from "../src/formatters.js";

test("extracts goods id from supported inputs", () => {
  assert.equal(goodsIdFromText("996956197005"), "996956197005");
  assert.equal(goodsIdFromText("https://mobile.yangkeduo.com/goods.html?goods_id=946460263996"), "946460263996");
  assert.equal(goodsIdFromText("https://mobile.yangkeduo.com/proxy/api/reviews/123456789/list"), "123456789");
  assert.equal(goodsIdFromText("#小程序://拼多多/Ju38Pin81qJ57BI"), null);
});

test("extracts the model value from review specs", () => {
  const specs = JSON.stringify([
    { spec_key: "型号", spec_value: "中兴巡天AX3000T" },
    { spec_key: "组合", spec_value: "满血WIFI6" },
  ]);
  assert.equal(productModel(specs), "中兴巡天AX3000T");
  assert.equal(productModel("白色 XL"), "白色 XL");
});

test("builds an in-memory session without exposing it elsewhere", () => {
  const session = sessionFromRecord({
    id: 7,
    uid: "abc",
    url: "https://mobile.yangkeduo.com/proxy/api/reviews/996956197005/list?page=1",
    request: {
      method: "POST",
      headers: [
        { name: "Cookie", value: "PDDAccessToken=secret" },
        { name: "Anti-Content", value: "dynamic-token" },
        { name: "Content-Length", value: "100" },
      ],
      body: { text: JSON.stringify({ name: "goodsCommentListAxios", anti_content: "dynamic-token" }) },
    },
    response: { code: 200 },
  });
  assert.equal(session.sourceGoodsId, "996956197005");
  assert.equal(session.headers.cookie, "PDDAccessToken=secret");
  assert.equal(session.headers["content-length"], undefined);
  assert.equal(session.body.anti_content, "dynamic-token");
});
