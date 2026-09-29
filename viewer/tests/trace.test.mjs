import { test } from 'node:test';
import assert from 'node:assert/strict';
import { toTurn, toTool, sessionInfo, recordId, itemText } from '../src/trace.js';

const row = (position, item) => ({ turn: 'turn', position, item, text: item.text || '', source: { generation: 'prefix', start: '100', end: '200' } });
test('notification-only user messages and readable reasoning retain their text', () => {
  assert.equal(itemText({type:'userMessage', content:[{type:'text',text:'user text'},{type:'image',url:'file.png'}]}), 'user text');
  assert.equal(itemText({type:'reasoning',summary:['summary'],content:['body']}), 'summary\nbody');
});
test('projection retains every message and tool at its original position without inventing usage or dates', () => {
  const rows = [row(1, { type: 'agentMessage', id: 'one', phase: 'final_answer', text: 'first final' }), row(2, { type: 'commandExecution', id: 'tool', command: 'pwd', aggregatedOutput: '/work', status: 'completed' }), row(3, { type: 'reasoning', id: 'reason', text: 'visible summary' }), row(4, { type: 'agentMessage', id: 'two', phase: 'final_answer', text: 'second final' }), row(0, { type: 'userMessage', id: 'user', text: 'question' })];
  const turn = toTurn('turn', rows, { status: 'completed', durationMs: 0 });
  assert.deepEqual(turn.agent_messages.map(m => m.order), [0, 1, 3, 4]);
  assert.deepEqual(turn.tool_call_orders, [2]);
  assert.deepEqual(turn.agent_messages.filter(m => m.phase === 'final_answer').map(m => m.text), ['first final', 'second final']);
  assert.equal(turn.agent_messages[2].text, 'visible summary');
  assert.equal(turn.agent_messages[2].is_reasoning, true);
  assert.equal(turn.total_tokens, null);
  assert.equal(turn.started_at, null);
  assert.equal(turn.agent_messages[0].timestamp, '');
  assert.equal(turn.tool_calls[0].exit_code, null);
  assert.equal(turn.tool_calls[0].status, 'completed');
  assert.equal(turn.status, 'complete');
  assert.equal(turn.duration_ms, 0);
});
test('unmapped and legacy records stay inspectable without being mislabeled as official messages', () => {
  for (const item of [{ type: 'newFutureType', id: 'future', payload: { value: 42 } }, { type: 'rawResponse', id: 'raw', record: { type: 'function_call', arguments: 'source' } }]) {
    const tool = toTool(row(0, item));
    assert.equal(tool.kind, 'unknown');
    assert.equal(tool.name, item.type);
    assert.deepEqual(JSON.parse(tool.output), item);
  }
});
test('same native IDs in different homes remain separate including subagent parents', () => {
  const threads = ['host:/a', 'host:/b'].map(origin => ({ id: JSON.stringify([origin, 'same']), origin, title: 'child', metadata: { parent_thread_id: 'parent' }, coverage: {} }));
  const a = sessionInfo(threads[0]), b = sessionInfo(threads[1]);
  assert.notEqual(a.id, b.id);
  assert.notEqual(a.parent_session_id, b.parent_session_id);
  assert.equal(a.path, threads[0].id);
  assert.equal(a.start_time, '');
  assert.equal(recordId(row(0, { id: 'same' })), '["turn","same"]');
});
test('collaboration receivers and file diffs preserve the normalized payload', () => {
  const item = { id: 'spawn', type: 'collabAgentToolCall', tool: 'spawnAgent', receiverThreadIds: ['child'], prompt: 'work', agentsStates: { child: { status: 'running' } } };
  const tool = toTool(row(0, item));
  assert.equal(tool.kind, 'spawn_agent');
  assert.deepEqual(tool.arguments.receiverThreadIds, ['child']);
  const patch = toTool(row(1, { id: 'patch', type: 'fileChange', changes: [{ path: 'a.py', kind: { type: 'update' }, diff: '-old\n+new' }] }));
  assert.equal(patch.patch_changes['a.py'].unified_diff, '-old\n+new');
  assert.equal(patch.patch_success, null);
});

test('native subagent source metadata connects children within the same home', () => {
  const info=sessionInfo({id:'child',origin:'host:/home',coverage:{},metadata:{source:JSON.stringify({subagent:{thread_spawn:{parent_thread_id:'parent'}}})}});
  assert.equal(info.parent_session_id, '["host:/home","parent"]');
});
