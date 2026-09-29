import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { mkdtemp, mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, expect } from "@playwright/test";

test("event replay persists deltas, replaces completion and recovers an expired cursor", { timeout: 60000 }, async () => {
  const repo = resolve("..");
  await mkdir(resolve(repo, ".m3/browser-tests"), { recursive: true });
  const root = await mkdtemp(resolve(repo, ".m3/browser-tests/run-"));
  const archive = resolve(root, "archive");
  const fixture = phase => execFileSync("python3", ["tests/viewer_fixture.py", "--root", archive, ...(phase ? ["--live-phase", phase] : [])], { cwd: repo, env: { ...process.env, PYTHONPATH: repo } });
  fixture();
  const server = spawn("python3", ["-m", "replica", "--store", archive, "serve", "--port", "0"], { cwd: repo, stdio: ["ignore", "pipe", "pipe"] });
  const url = await new Promise((resolve, reject) => { server.stdout.once("data", data => resolve(JSON.parse(data.toString()).url)); server.once("error", reject); });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(url);
    await expect(page.locator(".sidebar-tree__session")).toHaveCount(3);
    await page.getByRole("button", { name: /让每一次思考/ }).click();
    fixture("start");
    await expect(page.getByRole("button", { name: "Detail", exact: true })).toHaveCount(2);
    await page.getByRole("button", { name: "Detail", exact: true }).last().click();
    await expect(page.locator("#messages")).toContainText("STREAM");
    await expect(page.locator("#archive-status")).toHaveText("归档来源 2 个 · 最近采集成功 2 个");
    await expect(page.locator("#live-status")).toHaveText("通知入口已连接 1 个");
    fixture("delta");
    await expect(page.locator("#messages")).toContainText("STREAM one two");
    fixture("complete");
    await expect(page.locator("#messages")).toContainText("STREAM one two DONE");
    await expect(page.locator("#messages article")).toHaveCount(1);
    fixture("gap");
    await expect(page.locator("#messages")).toContainText("内容可能不连续");
    await page.reload();
    await page.getByRole("button", { name: "Detail", exact: true }).last().click();
    await expect(page.locator("#messages")).toContainText("STREAM one two DONE");
    await expect.poll(() => { try { fixture("prune"); return true; } catch { return false; } }).toBe(true);
    await page.evaluate(async () => {
      const opening = indexedDB.open("codex-session-replica");
      const db = await new Promise(resolve => { opening.onsuccess = () => resolve(opening.result); });
      const tx = db.transaction("meta", "readwrite"); tx.objectStore("meta").put("0", "live_cursor");
      await new Promise(resolve => { tx.oncomplete = resolve; });
    });
    await page.reload();
    await page.getByRole("button", { name: "Detail", exact: true }).last().click();
    await expect(page.locator("#messages")).toContainText("STREAM one two DONE");
    await expect(page.locator("#messages article")).toHaveCount(2);
    await page.getByRole("button", { name: "Back", exact: true }).click();
    await page.getByRole("button", { name: "Detail", exact: true }).first().click();
    await expect(page.locator("#messages")).toContainText("历史已经保存好了");
    assert.equal((await page.request.get(url + "/api/live/changes?after=0")).status(), 200);
  } finally { await browser.close(); server.kill("SIGTERM"); }
});
