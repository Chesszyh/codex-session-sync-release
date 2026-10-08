import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { mkdtemp, mkdir, writeFile, unlink, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, webkit, expect } from "@playwright/test";

for (const [engine, browserType, width, height] of [
  ["chromium", chromium, 360, 800],
  ["webkit", webkit, 390, 844],
]) {
  test(`${engine}: mobile search, detail navigation and offline reading`, { timeout: 60000 }, async () => {
    const repo = resolve("..");
    const evidence = resolve(repo, ".m6/mobile-access");
    await mkdir(evidence, { recursive: true });
    const root = await mkdtemp(resolve(evidence, `${engine}-`));
    execFileSync("python3", ["tests/viewer_fixture.py", "--root", root], {
      cwd: repo, env: { ...process.env, PYTHONPATH: repo },
    });
    const serve = `
import json, sys
from pathlib import Path
from replica.gateway import create_server
root = Path(sys.argv[1])
server = create_server(root, 0)
original_get = server.RequestHandlerClass.do_GET
def get(handler):
    if handler.path.startswith('/api/') and (root / 'login-required').exists():
        handler.send_response(302)
        handler.send_header('Location', '/sign-in')
        handler.send_header('Content-Length', '0')
        handler.send_header('Cache-Control', 'no-store')
        handler.end_headers()
    else:
        if handler.path == '/?login=1':
            (root / 'login-visited').write_text('network')
        original_get(handler)
server.RequestHandlerClass.do_GET = get
print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}'}), flush=True)
server.serve_forever()
`;
    const server = spawn("python3", ["-c", serve, root], {
      cwd: repo, stdio: ["ignore", "pipe", "pipe"],
    });
    const url = await new Promise((resolve, reject) => {
      server.stdout.once("data", data => resolve(JSON.parse(data).url));
      server.once("error", reject);
    });
    const browser = await browserType.launch({ headless: true });
    try {
      const context = await browser.newContext({ viewport: { width, height }, isMobile: true, hasTouch: true });
      await context.addInitScript(() => localStorage.setItem("replica-sync-mode", "full"));
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(error.message));
      const fits = async () => {
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
        for (const selector of [".archive", ".message-detail__header", ".record-pages"]) {
          const element = page.locator(selector);
          if (await element.isVisible()) {
            const box = await element.boundingBox();
            assert.ok(box.x >= 0 && box.x + box.width <= width + 1, `${selector} fits ${width}px`);
          }
        }
      };
      await page.goto(url);
      await expect(page.locator("#connection")).toHaveText("副本已同步");
      await expect(page.locator(".sidebar-tree__session")).toHaveCount(3);
      await fits();
      await page.screenshot({ path: resolve(evidence, `${engine}-list.png`) });
      await page.getByRole("searchbox").fill("分段归档");
      await expect(page.locator(".sidebar-tree__session")).toHaveCount(1);
      await page.getByRole("button", { name: /让每一次思考/ }).tap();
      await expect(page.locator("#messages")).toBeVisible();
      await expect(page.locator(".replica-sidebar")).toBeHidden();
      await page.locator(".tool-call__header").tap();
      await expect(page.locator(".tool-call__output")).toContainText("Ran 15 tests");
      await fits();
      await page.screenshot({ path: resolve(evidence, `${engine}-detail.png`) });
      await page.getByRole("button", { name: "View full content", exact: true }).tap();
      await expect(page.locator(".popout-modal__body")).toContainText("Ran 15 tests");
      const modal = await page.locator(".popout-modal").boundingBox();
      assert.ok(modal.x >= 0 && modal.x + modal.width <= width + 1);
      await page.locator(".popout-modal__close").tap();
      await expect(page.locator(".popout-modal")).toHaveCount(0);
      await page.getByRole("button", { name: "Back", exact: true }).tap();
      await expect(page.locator(".archive-turns")).toBeVisible();
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages article")).toHaveCount(4);
      await page.getByRole("button", { name: "会话列表", exact: true }).tap();
      await expect(page.getByRole("searchbox")).toBeVisible();
      await page.getByRole("searchbox").fill("");
      await expect(page.locator(".sidebar-tree__session")).toHaveCount(3);
      await writeFile(resolve(root, "login-required"), "");
      await expect(page.locator("#connection")).toHaveText("登录已过期 · 阅读本地副本", { timeout: 10000 });
      await expect(page.getByRole("link", { name: "重新登录" })).toHaveAttribute("href", "/?login=1");
      await expect(page.locator(".sidebar-tree__session")).toHaveCount(3);
      await unlink(resolve(root, "login-required"));
      await page.getByRole("link", { name: "重新登录" }).tap();
      await expect(page.locator("#connection")).toHaveText("副本已同步", { timeout: 10000 });
      assert.equal(await readFile(resolve(root, "login-visited"), "utf8"), "network");
      assert.equal(new URL(page.url()).search, "");
      await expect(page.getByRole("link", { name: "重新登录" })).toHaveCount(0);
      await page.evaluate(() => navigator.serviceWorker.ready);
      await page.reload();
      assert.deepEqual(errors, []);
      server.kill("SIGTERM");
      await new Promise(resolve => server.once("exit", resolve));
      await page.reload();
      await expect(page.locator("#connection")).toContainText("离线");
      await page.getByRole("button", { name: /让每一次思考/ }).tap();
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages")).toContainText("历史已经保存好了");
      await fits();
    } finally {
      await browser.close();
      server.kill("SIGTERM");
    }
  });
}
