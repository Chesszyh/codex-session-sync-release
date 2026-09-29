import { readFileSync, writeFileSync, appendFileSync, mkdirSync, copyFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { pageCodexRolloutStreams, readAfterCodexRolloutStreams } from '../.m0/upstream/happier/apps/cli/src/backends/codex/directSessions/codexDirectTranscriptStreamPaging';
import { collectCodexSessionRolloutFiles } from '../.m0/upstream/happier/apps/cli/src/backends/codex/directSessions/collectCodexSessionRolloutFiles';

const cases = JSON.parse(readFileSync(process.env.M0_CASES!, 'utf8'));
const results = [];
for (const c of cases) {
  const row: any = { name: c.name, messages: 0, pages: 0, text: '', discovered: 0 };
  try {
    row.discovered = (await collectCodexSessionRolloutFiles({codexHome: c.home, remoteSessionId: c.thread_id})).length;
    let cursor: string | undefined;
    for (let i = 0; i < 30; i++) {
      const page = await pageCodexRolloutStreams({codexHome: c.home, remoteSessionId: c.thread_id, direction: 'older', cursor, maxBytes: 16 * 1024 * 1024, maxItems: 2});
      row.pages++;
      row.messages += page.items.length;
      row.text += JSON.stringify(page.items);
      row.truncationReason = page.truncationReason;
      if (!page.hasMore) break;
      if (!page.nextCursor || page.nextCursor === cursor) { row.error = 'cursor did not advance'; break; }
      cursor = page.nextCursor;
      if (i === 29) row.error = 'page budget exhausted';
    }
  } catch (error) { row.error = String(error); }
  results.push(row);
}
writeFileSync(process.env.M0_OUTPUT!, JSON.stringify(results, null, 2), {mode: 0o600});

const original = cases.find((c: any) => c.name === 'legacy');
const home = join(dirname(process.env.M0_OUTPUT!), 'happier-follow-home');
const path = join(home, 'sessions', `rollout-2026-09-22T00-00-00-${original.thread_id}.jsonl`);
mkdirSync(dirname(path), {recursive: true});
copyFileSync(original.path, path);
const params = {codexHome: home, remoteSessionId: original.thread_id, maxBytes: 1024 * 1024, maxItems: 100};
const initial = await readAfterCodexRolloutStreams({...params, cursor: 'tail'});
appendFileSync(path, JSON.stringify({timestamp: '2026-09-22T00:00:01.000Z', type: 'response_item', payload: {type: 'message', role: 'assistant', content: [{type: 'output_text', text: 'M0_FOLLOW'}]}}) + '\n');
const after = await readAfterCodexRolloutStreams({...params, cursor: initial.nextCursor!});
const repeat = await readAfterCodexRolloutStreams({...params, cursor: after.nextCursor!});
const replacement = readFileSync(path).toString().replace('M0_FOLLOW', 'M0_REPLACED');
writeFileSync(path + '.new', replacement);
const {renameSync} = await import('node:fs');
renameSync(path + '.new', path);
const replaced = await readAfterCodexRolloutStreams({...params, cursor: after.nextCursor!});
writeFileSync(process.env.M0_OUTPUT!.replace('-results.json', '-follow.json'), JSON.stringify({initial, after, repeat, replaced}, null, 2), {mode: 0o600});
