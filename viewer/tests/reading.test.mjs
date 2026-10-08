import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import { mkdtemp, mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium, webkit, expect } from "@playwright/test";

for (const [engine, browserType] of [["chromium", chromium], ["webkit", webkit]]) {
  test(`${engine}: demand reading fetches only opened pages and keeps them offline`, { timeout: 90000 }, async () => {
    const repo = resolve("..");
    const evidence = resolve(repo, ".m6/reading-modes");
    await mkdir(evidence, { recursive: true });
    const root = await mkdtemp(resolve(evidence, engine + "-"));
    const python = args => execFileSync("python3", args, { cwd: repo, env: { ...process.env, PYTHONPATH: repo } });
    python(["tests/viewer_fixture.py", "--root", root]);
    let server = spawn("python3", ["-m", "replica", "--store", root, "serve", "--port", "0"], { cwd: repo, stdio: ["ignore", "pipe", "pipe"] });
    const url = await new Promise((resolve, reject) => { server.stdout.once("data", data => resolve(JSON.parse(data).url)); server.once("error", reject); });
    const browser = await browserType.launch({ headless: true });
    try {
      const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
      const page = await context.newPage();
      const requests = [], errors = [];
      page.on("request", r => { if (r.url().includes("/api/")) requests.push(new URL(r.url())); });
      page.on("pageerror", e => errors.push(e.message));
      await page.goto(url);
      await expect(page.getByLabel("同步方式")).toHaveValue("demand");
      await expect(page.locator("#connection")).toHaveText("目录已更新");
      await expect(page.locator(".sidebar-tree__session")).toHaveCount(3);
      assert.ok(requests.every(r => !r.pathname.startsWith("/api/live/") && (!r.pathname.match(/snapshot|changes/) || r.searchParams.get("scope") === "directory")));
      assert.ok(!requests.some(r => r.pathname.startsWith("/api/read/")));
      await page.screenshot({ path: resolve(evidence, engine + "-directory.png") });
      await page.getByRole("button", { name: /让每一次思考/ }).tap();
      await expect(page.getByRole("button", { name: "Detail", exact: true })).toBeVisible();
      assert.ok(!requests.some(r => r.pathname === "/api/read/page"));
      python(["tests/viewer_fixture.py", "--root", root, "--update"]);
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages")).toContainText("已补齐最新记录");
      await page.getByRole("button", { name: "会话列表", exact: true }).tap();
      python(["tests/viewer_fixture.py", "--root", root]);
      await page.getByRole("button", { name: /让每一次思考/ }).tap();
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages")).toContainText("历史已经保存好了");
      await expect(page.locator("#messages article")).toHaveCount(4);
      await page.locator(".tool-call__header").tap();
      await expect(page.locator(".tool-call__output")).toContainText("Ran 15 tests");
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: resolve(evidence, engine + "-reading.png") });
      await page.evaluate(() => navigator.serviceWorker.ready);
      await page.reload();
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages")).toContainText("历史已经保存好了");
      // Network failures must preserve an already opened page, while an unopened session stays explicit.
      server.kill("SIGTERM");
      await new Promise(resolve => server.once("exit", resolve));
      await page.reload();
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages")).toContainText("历史已经保存好了");
      await page.getByRole("button", { name: "会话列表", exact: true }).tap();
      await page.getByRole("button", { name: /周末，整理一下工作台/ }).tap();
      await expect(page.getByRole("alert")).toContainText("尚未缓存");
      await page.getByRole("button", { name: "会话列表", exact: true }).tap();
      python(["tests/viewer_fixture.py", "--root", root, "--update"]);
      server = spawn("python3", ["-m", "replica", "--store", root, "serve", "--port", new URL(url).port], { cwd: repo, stdio: ["ignore", "pipe", "pipe"] });
      await new Promise((resolve, reject) => { server.stdout.once("data", resolve); server.once("error", reject); });
      await page.getByRole("button", { name: /让每一次思考/ }).tap();
      await page.getByRole("button", { name: "Detail", exact: true }).tap();
      await expect(page.locator("#messages")).toContainText("已补齐最新记录");
      await expect(page.locator("#messages")).not.toContainText("历史已经保存好了");
      await page.getByRole("button", { name: "会话列表", exact: true }).tap();
      await page.getByLabel("同步方式").selectOption("full");
      await expect(page.locator("#connection")).toHaveText("副本已同步");
      await page.reload();
      await expect(page.getByLabel("同步方式")).toHaveValue("full");
      await page.getByRole("searchbox").fill("最新记录");
      await expect(page.locator(".sidebar-tree__session")).toHaveCount(1);
      await page.getByRole("searchbox").fill("");
      await page.getByLabel("同步方式").selectOption("demand");
      await expect(page.locator("#connection")).toHaveText("目录已更新");
      const afterSwitch = requests.length;
      await page.reload();
      await expect(page.getByLabel("同步方式")).toHaveValue("demand");
      await expect(page.locator("#connection")).toHaveText("目录已更新");
      assert.ok(requests.slice(afterSwitch).every(r => !r.pathname.match(/snapshot|changes/) || r.searchParams.get("scope") === "directory"));
      assert.deepEqual(errors, []);
    } finally {
      await browser.close();
      server.kill("SIGTERM");
    }
  });
}

test("switching stops an active full copy and resumes its cursor later", { timeout: 60000 }, async () => {
  const repo = resolve("..");
  const root = await mkdtemp(resolve(repo, ".m6/reading-modes/switch-"));
  execFileSync("python3", ["tests/viewer_fixture.py", "--root", root], { cwd: repo, env: { ...process.env, PYTHONPATH: repo } });
  const script = `
import json, sys, time
from urllib.parse import urlsplit,parse_qs
from replica.view import View
from replica.gateway import create_server
view=View(sys.argv[1])
with view.db:
    for i in range(300):
        view.set_entity('bulk-'+str(i),'item','unopened',{'position':i,'turn':'bulk','item':{'type':'agentMessage','id':str(i)},'text':'body'})
view.close()
server=create_server(sys.argv[1],0)
original=server.RequestHandlerClass.do_GET
def get(handler):
    url=urlsplit(handler.path)
    q=parse_qs(url.query)
    if url.path=='/api/snapshot' and 'scope' not in q and int(q.get('after',['0'])[0])>0:
        time.sleep(1)
    original(handler)
server.RequestHandlerClass.do_GET=get
print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}'}),flush=True)
server.serve_forever()
`;
  const server = spawn("python3", ["-c", script, root], { cwd: repo, stdio: ["ignore", "pipe", "pipe"] });
  const url = await new Promise((resolve, reject) => { server.stdout.once("data", data => resolve(JSON.parse(data).url)); server.once("error", reject); });
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const second = page.waitForRequest(r => { const u = new URL(r.url()); return u.pathname === "/api/snapshot" && Number(u.searchParams.get("after")) >= 100 && !u.searchParams.has("scope"); });
    await page.goto(url);
    await expect(page.getByLabel("同步方式")).toHaveValue("full");
    await second;
    await page.getByLabel("同步方式").selectOption("demand");
    await expect(page.locator("#connection")).toHaveText("目录已更新", { timeout: 5000 });
    const cursor = await page.evaluate(async () => {
      const db = await new Promise(resolve => { const r = indexedDB.open("codex-session-replica"); r.onsuccess = () => resolve(r.result); });
      const p = await new Promise(resolve => { const r = db.transaction("meta").objectStore("meta").get("progress"); r.onsuccess = () => resolve(r.result); });
      db.close(); return p.cursor;
    });
    assert.equal(cursor, "100");
    const resume = page.waitForRequest(r => { const u = new URL(r.url()); return u.pathname === "/api/snapshot" && !u.searchParams.has("scope"); });
    await page.getByLabel("同步方式").selectOption("full");
    assert.equal(new URL((await resume).url()).searchParams.get("after"), cursor);
    await expect(page.locator("#connection")).toHaveText("副本已同步", { timeout: 10000 });
  } finally { await browser.close(); server.kill("SIGTERM"); }
});
