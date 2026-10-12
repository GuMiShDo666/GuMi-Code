'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');
const { DatabaseSync } = require('node:sqlite');
const { spawn } = require('node:child_process');
const { getGumCodeRuntime, pythonEnvironment } = require('../electron/gum-code-runtime.cjs');
const { migrateWorkbench } = require('../electron/migration.cjs');
const { createMemoryStore } = require('../electron/gum-code-memory.cjs');
function put(file, value) { fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 }); fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value), { mode: 0o600 }); }
function digestTree(dir) {
  const rows = [];
  for (const name of fs.readdirSync(dir).sort()) {
    const p = path.join(dir, name), stat = fs.lstatSync(p);
    rows.push([name, stat.mode, stat.isDirectory() ? digestTree(p) : createHash('sha256').update(fs.readFileSync(p)).digest('hex')]);
  }
  return rows;
}
function desktopHistory(sessionPath, sessionId, workspace) {
  const runtime = getGumCodeRuntime();
  return new Promise((resolve, reject) => {
    const child = spawn(runtime.python, ['-u', '-m', 'gum_code.desktop'], { env: pythonEnvironment(runtime.source), stdio: ['pipe', 'pipe', 'ignore'] });
    let output = ''; const timer = setTimeout(() => { child.kill(); reject(Error('Fixture adapter timed out')); }, 30000);
    child.stdout.on('data', chunk => { output += chunk; }); child.on('error', reject);
    child.on('close', code => { clearTimeout(timer); try { assert.equal(code, 0); resolve(output.trim().split('\n').map(line => JSON.parse(line))); } catch (error) { reject(error); } });
    child.stdin.end(JSON.stringify({ v: 1, action: 'history', sessionId, turnId: randomUUID(), workspace, sessionPath }) + '\n');
  });
}
function fixture(t, version = 2) {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'gumi-import-test-')); t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const sourceUserData = path.join(root, 'source'), targetDataDir = path.join(root, 'target', 'gum-code'), source = path.join(sourceUserData, 'gum-code');
  fs.mkdirSync(source, { recursive: true, mode: 0o700 }); fs.mkdirSync(path.dirname(targetDataDir), { recursive: true, mode: 0o700 });
  const sessionId = randomUUID(), workspaceId = randomUUID(), headerId = randomUUID(), providerId = randomUUID(), sourceSlot = version === 1 ? providerId : randomUUID(), memoryId = randomUUID(), sourceId = randomUUID(), turnId = randomUUID(), inputId = randomUUID();
  const workspace = path.join(root, 'workspace'); fs.mkdirSync(workspace);
  const c = { version: 1, sessions: [{ id: sessionId, projectId: 'project-test', workspace, title: 'Test title', updatedAt: 1, providerId: 'workbench', pendingText: 'Interrupted input', pendingInputs: [{ id: inputId, text: 'Pending input' }] }], selectedId: sessionId, trusted: [workspace, path.join(root, 'unrelated')], workspaces: [{ id: workspaceId, name: 'workspace', path: workspace }] };
  put(path.join(source, 'catalog.json'), c);
  const entry = { id: providerId, name: version === 1 ? 'GuMi-DS' : '自定义 API', baseUrl: 'https://example.invalid/v1', model: 'fixture-model' };
  put(path.join(source, 'providers.json'), version === 1 ? { version: 1, profiles: [entry] } : { version: 2, profiles: [entry], activeId: providerId, credentialId: sourceSlot });
  const event = (seq, type, data) => JSON.stringify({ seq, time: '2026-10-12T00:00:00Z', type, data }) + '\n';
  put(path.join(source, `${sessionId}.jsonl`), event(0, 'session.created', { version: 3, id: headerId, workspace, model: 'fixture-model', title: 'Title', sticky_skills: [] }) + event(1, 'message.appended', { message: { role: 'system', content: 'Fixture' } }));
  put(path.join(source, `${sessionId}.jsonl.lock`), '');
  put(path.join(source, `${sessionId}.jsonl.segments/index.json`), { version: 1, session_id: headerId, segments: 2 });
  put(path.join(source, `${sessionId}.jsonl.segments/0000000000000001.jsonl`), event(2, 'message.appended', { message: { role: 'assistant', content: 'Synthetic answer' } }));
  createMemoryStore(source).close();
  const db = new DatabaseSync(path.join(source, 'memory.sqlite'));
  db.exec('UPDATE settings SET enabled=1,epoch=2,revision=3');
  db.prepare('INSERT INTO sources(id,scope,sessionId,turnId,inputId,digest,epoch) VALUES(?,?,?,?,?,?,?)').run(sourceId, workspaceId, sessionId, turnId, inputId, 'fixture-digest', 2);
  db.prepare('INSERT INTO memories(id,scope,kind,factKey,body,status,revision,sourceId,sourceOrder,reason,verified) VALUES(?,?,?,?,?,?,?,?,?,?,?)').run(memoryId, workspaceId, 'fact', 'style', 'Prefer Chinese', 'enabled', 1, sourceId, 1, '', 0);
  db.prepare('INSERT INTO jobs(id,sourceId,status) VALUES(?,?,?)').run(randomUUID(), sourceId, 'pending'); db.close();
  const slots = new Map(), writes = [], deleted = [], credential = 'fixture-only-value';
  const credentials = { readSource: async id => { assert.equal(id, sourceSlot); return credential; }, writeTarget: async (id, value) => { writes.push(id); slots.set(id, value); }, readTarget: async id => slots.get(id), deleteTarget: async id => { deleted.push(id); slots.delete(id); } };
  const options = { sourceUserData, targetDataDir, credentials, sourceConfirmedClosed: true };
  return { root, source, options, c, sessionId, workspaceId, providerId, sourceSlot, memoryId, sourceId, inputId, writes, deleted, slots, credential };
}

test('migration preserves IDs, segmented history, memory settings, pending recovery and source bytes', async t => {
  const f = fixture(t), before = digestTree(f.source);
  fs.mkdirSync(f.options.targetDataDir); createMemoryStore(f.options.targetDataDir).close();
  const result = await migrateWorkbench(f.options);
  assert.deepEqual(result, { imported: true, alreadyImported: false, sessions: 1, workspaces: 1, memories: 1, providers: 1 });
  assert.deepEqual(digestTree(f.source), before);
  const dest = f.options.targetDataDir, c = JSON.parse(fs.readFileSync(path.join(dest, 'catalog.json')));
  assert.equal(c.sessions[0].id, f.sessionId); assert.equal(c.sessions[0].workspaceId, f.workspaceId); assert.equal(c.sessions[0].projectId, undefined); assert.equal(c.selectedId, f.sessionId);
  assert.deepEqual(c.sessions[0].pendingInputs, f.c.sessions[0].pendingInputs); assert.equal(c.sessions[0].pendingText, f.c.sessions[0].pendingText); assert.deepEqual(c.trusted, [f.c.sessions[0].workspace]);
  for (const rel of [`${f.sessionId}.jsonl`, `${f.sessionId}.jsonl.segments/index.json`, `${f.sessionId}.jsonl.segments/0000000000000001.jsonl`]) {
    assert.deepEqual(fs.readFileSync(path.join(dest, rel)), fs.readFileSync(path.join(f.source, rel))); assert.equal(fs.statSync(path.join(dest, rel)).mode & 0o777, 0o600);
  }
  const db = new DatabaseSync(path.join(dest, 'memory.sqlite'), { readOnly: true });
  assert.equal(db.prepare('SELECT id FROM memories').get().id, f.memoryId); assert.equal(db.prepare('SELECT scope FROM sources').get().scope, f.workspaceId); assert.equal(db.prepare('SELECT inputId FROM sources').get().inputId, f.inputId);
  assert.equal(db.prepare('SELECT status FROM jobs').get().status, 'pending'); assert.equal(db.prepare('SELECT enabled FROM settings').get().enabled, 1); db.close();
  const p = JSON.parse(fs.readFileSync(path.join(dest, 'providers.json'))); assert.equal(p.activeId, f.providerId); assert.notEqual(p.credentialId, f.sourceSlot); assert.equal(f.slots.get(p.credentialId), f.credential);
  assert.equal(fs.readFileSync(path.join(dest, 'providers.json'), 'utf8').includes(f.credential), false);
  const restarted = await desktopHistory(path.join(dest, `${f.sessionId}.jsonl`), f.sessionId, f.c.sessions[0].workspace);
  assert.equal(restarted.find(r => r.type === 'history').messages.find(m => m.role === 'assistant').content, 'Synthetic answer');
  assert.deepEqual(await migrateWorkbench(f.options), { ...result, imported: false, alreadyImported: true }); assert.equal(f.writes.length, 1);
  // Reopening normal memory store preserves facts and marks interrupted learning recoverable.
  const reopened = createMemoryStore(dest); assert.equal(reopened.state(f.workspaceId).items[0].id, f.memoryId); assert.equal(reopened.state(f.workspaceId).jobs[0].status, 'failed'); reopened.close();
});
test('v1 provider and legacy snapshot without lock are retained', async t => {
  const f = fixture(t, 1), base = path.join(f.source, `${f.sessionId}.jsonl`);
  fs.rmSync(base + '.segments', { recursive: true }); fs.unlinkSync(base + '.lock');
  put(base, { version: 2, workspace: f.c.sessions[0].workspace, model: 'fixture-model', title: 'Legacy title', messages: [{ role: 'system', content: 'Fixture' }, { role: 'assistant', content: 'Legacy' }] });
  const sourceBefore = digestTree(f.source); await migrateWorkbench(f.options); assert.deepEqual(digestTree(f.source), sourceBefore);
  const destination = path.join(f.options.targetDataDir, `${f.sessionId}.jsonl`);
  assert.equal(JSON.parse(fs.readFileSync(destination, 'utf8').split('\n')[0]).data.version, 3);
  const restarted = await desktopHistory(destination, f.sessionId, f.c.sessions[0].workspace);
  assert.equal(restarted.find(r => r.type === 'history').messages.find(m => m.role === 'assistant').content, 'Legacy');
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.options.targetDataDir, 'providers.json'))).activeId, f.providerId);
});
test('legacy v1 migrates only the active credential and preserves inactive metadata IDs', async t => {
  const f = fixture(t, 1), providerFile = path.join(f.source, 'providers.json');
  const p = JSON.parse(fs.readFileSync(providerFile)), inactive = { id: randomUUID(), name: 'Inactive old profile', baseUrl: 'https://example.invalid/old', model: 'old-model' };
  p.profiles.push(inactive); put(providerFile, p);
  await migrateWorkbench(f.options);
  assert.equal(f.writes.length, 1); assert.deepEqual(JSON.parse(fs.readFileSync(path.join(f.options.targetDataDir, 'providers.json'))).profiles[1], inactive);
});
test('catalog without workspaces and memory gets stable scope IDs without source writes', async t => {
  const f = fixture(t), file = path.join(f.source, 'catalog.json'); delete f.c.workspaces; put(file, f.c); fs.unlinkSync(path.join(f.source, 'memory.sqlite'));
  const before = digestTree(f.source); await migrateWorkbench(f.options);
  const first = JSON.parse(fs.readFileSync(path.join(f.options.targetDataDir, 'catalog.json')));
  assert.equal(first.workspaces[0].path, f.c.sessions[0].workspace); assert.equal(first.sessions[0].workspaceId, first.workspaces[0].id); assert.equal(first.sessions[0].projectId, undefined);
  const secondTarget = path.join(f.root, 'second-target'); await migrateWorkbench({ ...f.options, targetDataDir: secondTarget });
  const second = JSON.parse(fs.readFileSync(path.join(secondTarget, 'catalog.json'))); assert.equal(second.workspaces[0].id, first.workspaces[0].id); assert.deepEqual(digestTree(f.source), before);
});
test('missing canonical workspace IDs with existing memory are refused rather than guessed', async t => {
  const f = fixture(t); delete f.c.workspaces; put(path.join(f.source, 'catalog.json'), f.c);
  await assert.rejects(migrateWorkbench(f.options)); assert.equal(f.writes.length, 0); assert.equal(fs.existsSync(f.options.targetDataDir), false);
});
test('a closed-source confirmation is required', async t => {
  const f = fixture(t); await assert.rejects(migrateWorkbench({ ...f.options, sourceConfirmedClosed: false }), /请先关闭/); assert.equal(f.writes.length, 0);
});
test('populated target memory and API are never overwritten', async t => {
  const f = fixture(t); fs.mkdirSync(f.options.targetDataDir); createMemoryStore(f.options.targetDataDir).close();
  const db = new DatabaseSync(path.join(f.options.targetDataDir, 'memory.sqlite')); db.exec('UPDATE settings SET enabled=1'); db.close();
  const before = digestTree(f.options.targetDataDir); await assert.rejects(migrateWorkbench(f.options), /已有数据/); assert.deepEqual(digestTree(f.options.targetDataDir), before); assert.equal(f.writes.length, 0);
});
test('credential failure rolls back all new slots and private staging without echoing exceptions', async t => {
  const f = fixture(t), before = digestTree(f.source); fs.mkdirSync(f.options.targetDataDir); createMemoryStore(f.options.targetDataDir).close();
  const targetBefore = digestTree(f.options.targetDataDir);
  f.options.credentials.writeTarget = async (id, key) => { f.slots.set(id, key); throw Object.assign(Error('untrusted exception content'), { gumCodeSafe: true }); };
  await assert.rejects(migrateWorkbench(f.options), e => e.gumCodeSafe && !e.message.includes('untrusted'));
  assert.deepEqual(digestTree(f.source), before); assert.deepEqual(digestTree(f.options.targetDataDir), targetBefore); assert.equal(f.slots.size, 0); assert.equal(f.deleted.length, 1); assert.deepEqual(fs.readdirSync(path.dirname(f.options.targetDataDir)), ['gum-code']);
});
test('failed keychain readback deletes newly allocated slot', async t => {
  const f = fixture(t); f.options.credentials.readTarget = async () => 'wrong-fixture-value';
  await assert.rejects(migrateWorkbench(f.options), /无法确认/); assert.equal(f.slots.size, 0); assert.equal(fs.existsSync(f.options.targetDataDir), false);
});
test('source changes during credentials abort without touching target', async t => {
  const f = fixture(t); f.options.credentials.readTarget = async id => { const c = JSON.parse(fs.readFileSync(path.join(f.source, 'catalog.json'))); c.sessions[0].title = 'Changed'; put(path.join(f.source, 'catalog.json'), c); return f.slots.get(id); };
  await assert.rejects(migrateWorkbench(f.options), /数据发生变化/); assert.equal(f.slots.size, 0); assert.equal(fs.existsSync(f.options.targetDataDir), false);
});
test('links, unknown files, oversized files, wrong segments and unknown memory schema are rejected', async t => {
  for (const mutate of [
    f => { const p = path.join(f.source, 'providers.json'); fs.renameSync(p, path.join(f.root, 'outside')); fs.symlinkSync(path.join(f.root, 'outside'), p); },
    f => put(path.join(f.source, 'unexpected.json'), '{}'),
    f => fs.truncateSync(path.join(f.source, `${f.sessionId}.jsonl`), 5 * 1024 * 1024 + 1),
    f => put(path.join(f.source, `${f.sessionId}.jsonl.segments/index.json`), { version: 1, session_id: randomUUID(), segments: 2 }),
    f => { const db = new DatabaseSync(path.join(f.source, 'memory.sqlite')); db.exec('CREATE TABLE unknown(private TEXT)'); db.close(); },
  ]) {
    const f = fixture(t); mutate(f); await assert.rejects(migrateWorkbench(f.options)); assert.equal(f.writes.length, 0); assert.equal(fs.existsSync(f.options.targetDataDir), false);
  }
});
test('held source lease rejects import without modifying source', async t => {
  const f = fixture(t), before = digestTree(f.source), lock = path.join(f.source, `${f.sessionId}.jsonl.lock`);
  const child = spawn('/usr/bin/python3', ['-I', '-c', 'import sys,fcntl\nf=open(sys.argv[1],"rb");fcntl.flock(f,fcntl.LOCK_EX);print("ready",flush=True);sys.stdin.read()', lock], { stdio: ['pipe', 'pipe', 'ignore'] });
  t.after(() => child.kill()); await new Promise((resolve, reject) => { child.stdout.once('data', resolve); child.once('error', reject); });
  await assert.rejects(migrateWorkbench(f.options)); assert.deepEqual(digestTree(f.source), before); assert.equal(f.writes.length, 0);
  await new Promise(resolve => { child.once('exit', resolve); child.stdin.end(); });
});
test('mismatched workspace binding and sidecar journal are refused', async t => {
  const f = fixture(t); f.c.sessions[0].workspaceId = randomUUID(); put(path.join(f.source, 'catalog.json'), f.c); await assert.rejects(migrateWorkbench(f.options));
  delete f.c.sessions[0].workspaceId; put(path.join(f.source, 'catalog.json'), f.c); put(path.join(f.source, 'memory.sqlite-journal'), ''); await assert.rejects(migrateWorkbench(f.options)); assert.equal(f.writes.length, 0);
});
test('semantic corruption is rejected by the real staged Python validator before credentials', async t => {
  const f = fixture(t);
  const segment = path.join(f.source, `${f.sessionId}.jsonl.segments/0000000000000001.jsonl`);
  put(segment, JSON.stringify({ seq: 2, time: '2026-10-12T00:00:00Z', type: 'task.state', data: { invalid: true } }) + '\n');
  const before = digestTree(f.source); await assert.rejects(migrateWorkbench(f.options)); assert.deepEqual(digestTree(f.source), before); assert.equal(f.writes.length, 0); assert.equal(fs.existsSync(f.options.targetDataDir), false);
});
