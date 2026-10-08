import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { chromium, expect } from '@playwright/test';

test('trace keeps old caches, virtualizes long histories, pages ordered records and follows selected-head replacement', { timeout: 60000 }, async () => {
  const repo=resolve('..');
  await mkdir(resolve(repo,'.m5/browser-tests'),{recursive:true});
  const root=await mkdtemp(resolve(repo,'.m5/browser-tests/run-'));
  const fixture = revert => execFileSync('python3',['tests/trace_fixture.py','--root',root,...(revert?['--revert']:[])],{cwd:repo,env:{...process.env,PYTHONPATH:repo}});
  fixture();
  const server=spawn('python3',['-m','replica','--store',root,'serve','--port','0'],{cwd:repo,stdio:['ignore','pipe','pipe']});
  const url=await new Promise((resolve,reject)=>{server.stdout.once('data',data=>resolve(JSON.parse(data).url));server.once('error',reject);});
  const browser=await chromium.launch({headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1440,height:950}});
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.goto(url+'/api/status');
    await page.evaluate(async()=>{
      const db=await new Promise((resolve,reject)=>{
        const open=indexedDB.open('codex-session-replica',1);
        open.onupgradeneeded=()=>{const db=open.result;const s=db.createObjectStore('entities',{keyPath:'key'});s.createIndex('kind','kind');s.createIndex('session','session');s.createIndex('position',['session','data.position']);db.createObjectStore('meta');};
        open.onerror=()=>reject(open.error);open.onsuccess=()=>resolve(open.result);
      });
      const tx=db.transaction('meta','readwrite');tx.objectStore('meta').put('retained','upgrade-test');
      await new Promise(resolve=>tx.oncomplete=resolve);db.close();
    });
    let releaseSnapshot;
    const snapshotGate = new Promise(resolve => { releaseSnapshot = resolve; });
    let snapshotPages = 0;
    await page.route('**/api/snapshot?**', async route => {
      if (++snapshotPages === 2) await snapshotGate;
      await route.continue();
    });
    try {
      await page.goto(url);
      await expect(page.locator('#connection')).toHaveText('保存初始副本 · 本次已保存 100 条');
    } finally { releaseSnapshot(); }
    await expect(page.locator('#connection')).toHaveText('副本已同步');
    await page.unroute('**/api/snapshot?**');
    const reloadRequests = [];
    page.on('request', request => { if (request.url().includes('/api/')) reloadRequests.push(request.url()); });
    await page.reload();
    await expect(page.locator('#connection')).toHaveText('副本已同步');
    assert.ok(reloadRequests.some(url => url.includes('/api/changes?after=')));
    assert.equal(reloadRequests.some(url => url.includes('/api/snapshot')), false);
    await expect(page.locator('.sidebar-tree__session')).toHaveCount(2);
    await page.getByRole('button',{name:/长历史样本/}).click();
    await expect(page.getByRole('button',{name:'Detail',exact:true}).first()).toBeVisible();
    assert.ok(await page.locator('.turn-list__turn').count()<30);
    await page.getByRole('button',{name:'Detail',exact:true}).first().click();
    await expect(page.locator('#messages article')).toHaveCount(60);
    await expect(page.locator('.complementary-item__body').first()).toHaveText('PAGE 0');
    await expect(page.locator('.complementary-item__body')).toContainText(['PAGE 0','VISIBLE REASONING']);
    await page.locator('.tool-call__header').first().click();
    await expect(page.locator('.tool-call__output')).toContainText('UNKNOWN VISIBLE');
    await page.locator('.tool-call__header').nth(1).click();
    await expect(page.locator('.tool-call__output').last()).toHaveText('FIXTURE OUTPUT');
    await page.locator('.source summary').first().click();
    await expect(page.locator('.source').first()).toContainText('parent-prefix');
    const ids=[];
    ids.push(...await page.locator('#messages article').evaluateAll(els=>els.map(el=>el.dataset.recordId)));
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await expect(page.locator('#messages article')).toHaveCount(60);
    await expect(page.locator('.complementary-item__body').first()).toHaveText('PAGE 60');
    ids.push(...await page.locator('#messages article').evaluateAll(els=>els.map(el=>el.dataset.recordId)));
    await page.getByRole('button',{name:'下一页',exact:true}).click();
    await expect(page.locator('#messages article')).toHaveCount(4);
    ids.push(...await page.locator('#messages article').evaluateAll(els=>els.map(el=>el.dataset.recordId)));
    assert.equal(new Set(ids).size,124);
    await expect(page.getByRole('button',{name:'下一页',exact:true})).toBeDisabled();
    await page.getByRole('button',{name:'上一页',exact:true}).click();
    await expect(page.locator('.complementary-item__body').first()).toHaveText('PAGE 60');
    const cache=await page.evaluate(async()=>{
      const db=await new Promise(resolve=>{const r=indexedDB.open('codex-session-replica');r.onsuccess=()=>resolve(r.result);});
      const value=await new Promise(resolve=>{const r=db.transaction('meta').objectStore('meta').get('upgrade-test');r.onsuccess=()=>resolve(r.result);});
      const version=db.version;db.close();return {value,version};
    });
    assert.deepEqual(cache,{value:'retained',version:3});
    fixture(true);
    await expect(page.locator('#messages')).toHaveCount(0,{timeout:15000});
    await page.getByRole('button',{name:'Detail',exact:true}).click();
    await expect(page.locator('.complementary-item__body')).toHaveText('REVERTED HEAD');
    await expect(page.locator('#messages')).not.toContainText('PAGE 60');
    await expect(page.locator('.turn-list__turn')).toHaveCount(1);
    await expect(page.locator('.archive')).not.toContainText('STALE NOTIFICATION');
    assert.deepEqual(errors,[]);
  } finally {await browser.close();server.kill('SIGTERM');}
});
