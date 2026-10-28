import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { DatabaseSync } from "node:sqlite";

export class ReviewDatabase {
  constructor(path) {
    mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      CREATE TABLE IF NOT EXISTS products (
        goods_id TEXT PRIMARY KEY,
        first_seen TEXT NOT NULL,
        last_collected TEXT NOT NULL,
        review_count INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS reviews (
        review_id TEXT PRIMARY KEY,
        goods_id TEXT NOT NULL,
        comment TEXT,
        stars REAL,
        specs TEXT,
        review_time TEXT,
        username TEXT,
        anonymous INTEGER NOT NULL DEFAULT 0,
        sku_id TEXT,
        pictures_json TEXT NOT NULL DEFAULT '[]',
        replies_json TEXT NOT NULL DEFAULT '[]',
        raw_json TEXT NOT NULL,
        collected_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS idx_reviews_goods_id ON reviews(goods_id);
    `);
    this.upsertProductStmt = this.db.prepare(`
      INSERT INTO products(goods_id, first_seen, last_collected, review_count)
      VALUES (?, ?, ?, 0)
      ON CONFLICT(goods_id) DO UPDATE SET last_collected = excluded.last_collected
    `);
    this.upsertReviewStmt = this.db.prepare(`
      INSERT INTO reviews(
        review_id, goods_id, comment, stars, specs, review_time, username,
        anonymous, sku_id, pictures_json, replies_json, raw_json, collected_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(review_id) DO UPDATE SET
        comment = excluded.comment,
        stars = excluded.stars,
        specs = excluded.specs,
        review_time = excluded.review_time,
        username = excluded.username,
        anonymous = excluded.anonymous,
        sku_id = excluded.sku_id,
        pictures_json = excluded.pictures_json,
        replies_json = excluded.replies_json,
        raw_json = excluded.raw_json,
        collected_at = excluded.collected_at
    `);
  }

  saveReviews(goodsId, reviews) {
    const now = new Date().toISOString();
    this.upsertProductStmt.run(goodsId, now, now);
    this.db.exec("BEGIN");
    try {
      for (const item of reviews) {
        const reviewId = String(item.review_id ?? item.id ?? `${goodsId}-${item.time ?? ""}-${item.name ?? ""}-${item.comment ?? ""}`);
        this.upsertReviewStmt.run(
          reviewId,
          goodsId,
          item.comment ?? "",
          Number.isFinite(Number(item.stars)) ? Number(item.stars) : null,
          item.specs ?? "",
          item.time == null ? "" : String(item.time),
          item.name ?? "",
          item.anonymous ? 1 : 0,
          item.sku_id == null ? "" : String(item.sku_id),
          JSON.stringify(item.pictures ?? []),
          JSON.stringify(item.reply_list ?? []),
          JSON.stringify(item),
          now,
        );
      }
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
    this.refreshProductCount(goodsId);
  }

  refreshProductCount(goodsId) {
    this.db.prepare(`
      UPDATE products SET review_count = (
        SELECT COUNT(*) FROM reviews WHERE reviews.goods_id = products.goods_id
      ), last_collected = ? WHERE goods_id = ?
    `).run(new Date().toISOString(), goodsId);
  }

  listProducts() {
    return this.db.prepare("SELECT * FROM products ORDER BY last_collected DESC").all();
  }

  listReviews(goodsId = null, limit = 5000) {
    if (goodsId) {
      return this.db.prepare("SELECT * FROM reviews WHERE goods_id = ? ORDER BY collected_at DESC LIMIT ?").all(goodsId, limit);
    }
    return this.db.prepare("SELECT * FROM reviews ORDER BY collected_at DESC LIMIT ?").all(limit);
  }

  allReviews(goodsIds = []) {
    if (goodsIds.length) {
      const placeholders = goodsIds.map(() => "?").join(",");
      return this.db.prepare(`SELECT * FROM reviews WHERE goods_id IN (${placeholders}) ORDER BY goods_id, collected_at`).all(...goodsIds);
    }
    return this.db.prepare("SELECT * FROM reviews ORDER BY goods_id, collected_at").all();
  }

  deleteProducts(goodsIds) {
    if (!goodsIds.length) return 0;
    const placeholders = goodsIds.map(() => "?").join(",");
    this.db.exec("BEGIN");
    try {
      const result = this.db.prepare(`DELETE FROM reviews WHERE goods_id IN (${placeholders})`).run(...goodsIds);
      this.db.prepare(`DELETE FROM products WHERE goods_id IN (${placeholders})`).run(...goodsIds);
      this.db.exec("COMMIT");
      return Number(result.changes);
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  clearAll() {
    const count = Number(this.db.prepare("SELECT COUNT(*) AS count FROM reviews").get().count);
    this.db.exec("BEGIN");
    try {
      this.db.exec("DELETE FROM reviews; DELETE FROM products;");
      this.db.exec("COMMIT");
      return count;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.db.close();
  }
}
