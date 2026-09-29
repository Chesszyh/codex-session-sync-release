import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, expect } from "@playwright/test";

const repo = resolve("..");
test("generated source reaches the browser through collection, official decoding and offline storage", {
  timeout: 60000,
  skip: !existsSync(resolve(repo, ".m0/official-target/debug/examples/replica_read")) && "build the official adapter for pipeline integration",
}, async () => {
  await mkdir(resolve(repo, ".m6/pipeline"), { recursive: true });
  const parent = await mkdtemp(resolve(repo, ".m6/pipeline/run-"));
  const server = spawn("python3", ["scripts/demo.py", "--root", resolve(parent, "demo"), "--port", "0"], { cwd: repo, stdio: ["ignore", "pipe", "pipe"] });
  let browser;
  try {
    const result = await new Promise((resolve, reject) => {
      let output = "", errors = "";
      server.stderr.on("data", data => { errors += data; });
      server.stdout.on("data", data => {
        output += data;
        if (output.includes("\n")) { try { resolve(JSON.parse(output.split("\n")[0])); } catch (e) { reject(e); } }
      });
      server.once("error", reject);
      server.once("exit", code => reject(new Error(`demo exited ${code}: ${errors}`)));
    });
    assert.equal(result.capture_complete, true);
    assert.equal(result.raw_bytes_equal, true);
    assert.equal(result.threads, 1);
    assert.equal(result.items, 4);
    browser = await chromium.launch();
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(result.url);
    await expect(page.locator("#connection")).toHaveText("副本已同步");
    await page.getByRole("button", { name: /生成样本：跨设备会话归档/ }).click();
    await page.getByRole("button", { name: "Detail", exact: true }).click();
    await expect(page.locator("#messages article")).toHaveCount(4);
    await expect(page.locator("#messages")).toContainText("M0_AgentMessage_final");
    await page.locator(".tool-call__header").click();
    await expect(page.locator(".tool-call__output")).toContainText("M0_TOOL");
    await page.screenshot({ path: resolve(repo, ".m6/pipeline/demo.png"), fullPage: true });
    await page.evaluate(() => navigator.serviceWorker.ready);
    await context.setOffline(true);
    await page.reload();
    await expect(page.locator("#connection")).toContainText("离线");
    await page.getByRole("button", { name: "Detail", exact: true }).click();
    await expect(page.locator("#messages")).toContainText("M0_AgentMessage_final");
    assert.deepEqual(errors, []);
  } finally {
    if (browser) await browser.close();
    server.kill("SIGTERM");
  }
});
