'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createGumCodeProviders } = require('../electron/gum-code-providers.cjs');
const ds = { id: '11111111-1111-4111-8111-111111111111', name: 'GuMi-DS', baseUrl: 'https://gumi-ds.example.invalid/v1', model: 'synthetic-ds' };
const other = { ...ds, id: '22222222-2222-4222-8222-222222222222', name: 'Other' };
const key = 'SYNTHETIC_TEST_VALUE';
function setup(t, profiles = [ds], overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gumi-providers-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const dataDir = path.join(root, 'data'); fs.mkdirSync(dataDir);
  const file = path.join(dataDir, 'providers.json');
  if (profiles !== null) fs.writeFileSync(file, JSON.stringify({ version: 1, profiles }));
  const calls = [];
  const options = { dataDir, readCredential: async id => { calls.push(id); return key; }, ...overrides };
  return { root, file, options, calls, api: createGumCodeProviders(options) };
}
const safe = error => error.gumCodeSafe === true && !error.message.includes('PRIVATE_SENTINEL');

test('listing returns only GuMi-DS metadata; old alias and saved id use its key lazily with no writes', async t => {
  const f = setup(t, [other, ds]);
  const before = fs.readFileSync(f.file, 'utf8');
  assert.deepEqual(await f.api.list(), [ds]);
  assert.deepEqual(f.calls, []);
  for (const id of ['workbench', ds.id]) assert.deepEqual(await f.api.resolve(id), { baseUrl: ds.baseUrl, model: ds.model, apiKey: key });
  assert.deepEqual(f.calls, [ds.id, ds.id]);
  assert.equal(fs.readFileSync(f.file, 'utf8'), before);
  assert.equal(before.includes(key), false);
  assert.equal(fs.statSync(f.file).mode & 0o777, 0o600);
  assert.equal(fs.statSync(f.options.dataDir).mode & 0o777, 0o700);
  assert.equal(f.api.save, undefined, 'model creation and credential writer are removed');
  assert.deepEqual(await createGumCodeProviders(f.options).list(), [ds]);
});

test('missing, ambiguous GuMi-DS and stale nonDS ids fail before reading any credential', async t => {
  for (const profiles of [null, [], [other], [ds, { ...other, name: 'GuMi-DS' }]]) {
    const f = setup(t, profiles);
    await assert.rejects(f.api.list(), /GuMi-DS|API 尚未配置/);
    for (const id of ['workbench', ds.id, other.id]) await assert.rejects(f.api.resolve(id), safe);
    assert.deepEqual(f.calls, []);
    if (profiles === null) assert.equal(fs.existsSync(f.file), false);
  }
  const f = setup(t, [ds, other]);
  for (const id of [other.id, 'missing', undefined, {}, 'GuMi-DS']) await assert.rejects(f.api.resolve(id), /仅允许使用已配置的 API/);
  assert.deepEqual(f.calls, []);
});

test('metadata validation blocks invalid fields and insecure endpoints without credentials or overwrite', async t => {
  const bad = [{ name: '' }, { name: 'x'.repeat(41) }, { name: 'hello\nworld' }, { model: '' }, { model: 'x'.repeat(121) }, { model: 'hello\u0000world' }, { id: 'invalid' }, { apiKey: 'PRIVATE_SENTINEL' },
    ...['http://example.invalid/v1', 'file:///tmp/a', 'https://user:pass@example.invalid', 'https://example.invalid/v1?query', 'https://example.invalid/v1#fragment', 'https://example.invalid/?', 'invalid'].map(baseUrl => ({ baseUrl }))];
  for (const fields of bad) {
    const f = setup(t, [{ ...ds, ...fields }]); const before = fs.readFileSync(f.file, 'utf8');
    await assert.rejects(f.api.list(), safe); await assert.rejects(f.api.resolve('workbench'), safe);
    assert.deepEqual(f.calls, []); assert.equal(fs.readFileSync(f.file, 'utf8'), before);
  }
});

test('loopback HTTP and quoted keys remain exact through injected credential reader', async t => {
  const apiKey = 'SYNTHETIC_"quoted"_\\backslash_\'single\'_$(literal)`literal`';
  for (const baseUrl of ['http://localhost:8080/v1', 'http://127.0.0.1:8000/v1', 'http://[::1]:8080/v1']) {
    const f = setup(t, [{ ...ds, baseUrl }], { readCredential: async () => apiKey });
    assert.equal((await f.api.resolve('workbench')).apiKey, apiKey);
    assert.equal(fs.readFileSync(f.file, 'utf8').includes(apiKey), false);
  }
});

test('credential errors and invalid keys are sanitized and leave metadata unchanged', async t => {
  for (const readCredential of [async () => { throw Error('PRIVATE_SENTINEL'); }, ...[undefined, null, 123, '', ' ', 'x'.repeat(4097), 'bad\nkey', 'bad\u2028key'].map(value => async () => value)]) {
    const f = setup(t, [ds], { readCredential }); const before = fs.readFileSync(f.file, 'utf8');
    for (const id of [ds.id, 'workbench']) await assert.rejects(f.api.resolve(id), safe);
    assert.equal(fs.readFileSync(f.file, 'utf8'), before);
    assert.deepEqual(fs.readdirSync(f.options.dataDir), ['providers.json']);
  }
});

test('corrupt, oversized, duplicate-id, excessive-count and symlink metadata are preserved', async t => {
  const f = setup(t);
  for (const content of ['not-json', 'x'.repeat(65537), JSON.stringify({ version: 1, profiles: [ds, ds] }), JSON.stringify({ version: 1, profiles: Array(21).fill(ds) })]) {
    fs.writeFileSync(f.file, content); const bad = createGumCodeProviders(f.options);
    await assert.rejects(bad.list(), safe); await assert.rejects(bad.resolve('workbench'), safe);
    assert.equal(fs.readFileSync(f.file, 'utf8'), content);
  }
  fs.unlinkSync(f.file);
  const outside = path.join(f.root, 'outside.json'); fs.writeFileSync(outside, 'untouched'); fs.symlinkSync(outside, f.file);
  await assert.rejects(createGumCodeProviders(f.options).resolve('workbench'), safe);
  assert.equal(fs.readFileSync(outside, 'utf8'), 'untouched');
  const link = path.join(f.root, 'linked-data'); fs.symlinkSync(f.options.dataDir, link);
  await assert.rejects(createGumCodeProviders({ ...f.options, dataDir: link }).list(), safe);
  assert.deepEqual(f.calls, []);
});

test('native credential reader accesses only saved GuMi-DS service and sanitizes subprocess failures', async t => {
  const childProcess = require('node:child_process');
  let invocation, fail = false;
  t.mock.method(childProcess, 'execFile', (command, args, options, callback) => {
    invocation = { command, args, options };
    callback(fail ? Error('PRIVATE_SENTINEL') : null, key + '\n');
  });
  const modulePath = require.resolve('../electron/gum-code-providers.cjs');
  const cached = require.cache[modulePath]; delete require.cache[modulePath];
  const fresh = require(modulePath); require.cache[modulePath] = cached;
  const f = setup(t); const api = fresh.createGumCodeProviders({ dataDir: f.options.dataDir });
  assert.equal((await api.resolve('workbench')).apiKey, key);
  assert.deepEqual(invocation, { command: '/usr/bin/security', args: ['find-generic-password', '-a', 'GuMi-Code', '-s', `GuMi-Code.GumCode.${ds.id}`, '-w'], options: { timeout: 15000, maxBuffer: 16384 } });
  fail = true; await assert.rejects(api.resolve(ds.id), safe);
});

function writable(t, profiles = [ds], overrides = {}) {
  const secrets = new Map([[ds.id, key]]), deleted = [];
  const f = setup(t, profiles, {
    readCredential: async id => secrets.get(id),
    writeCredential: async (id, value) => { secrets.set(id, value); },
    deleteCredential: async id => { deleted.push(id); secrets.delete(id); },
    ...overrides,
  });
  return { ...f, secrets, deleted };
}
async function draft(api, values = {}) {
  const { revision, baseUrl, model } = await api.getSettings();
  return { revision, baseUrl, model, ...values };
}

test('settings saves one shared API, preserves legacy IDs and survives restart without plaintext metadata', async t => {
  const f = writable(t, [other, ds]);
  assert.deepEqual(await f.api.getSettings(), { baseUrl: ds.baseUrl, model: ds.model, hasKey: true, revision: (await f.api.getSettings()).revision });
  const result = await f.api.saveSettings(await draft(f.api, { baseUrl: 'https://shared.example.invalid/v1/', model: 'shared-model', apiKey: 'SYNTHETIC_NEW_KEY' }));
  assert.equal(result.hasKey, true);
  const stored = fs.readFileSync(f.file, 'utf8');
  assert.equal(stored.includes('SYNTHETIC_NEW_KEY'), false);
  assert.equal(stored.includes(key), false);
  assert.equal(JSON.stringify(result).includes('SYNTHETIC_NEW_KEY'), false);
  assert.equal(fs.statSync(f.file).mode & 0o777, 0o600);
  const restarted = createGumCodeProviders(f.options);
  assert.deepEqual(await restarted.getSettings(), result);
  assert.deepEqual((await restarted.list()).map(item => item.id), [ds.id]);
  for (const id of [ds.id, 'workbench']) assert.deepEqual(await restarted.resolve(id), { baseUrl: 'https://shared.example.invalid/v1', model: 'shared-model', apiKey: 'SYNTHETIC_NEW_KEY' });
  await assert.rejects(restarted.resolve(other.id), safe);
  assert.equal(JSON.parse(stored).profiles.find(item => item.id === other.id).name, other.name);
});

test('empty state can initialize, empty key retains same endpoint only, stale/extra fields reject', async t => {
  const f = writable(t, null);
  const initial = await f.api.getSettings();
  assert.equal(initial.hasKey, false); assert.equal(initial.baseUrl, '');
  await assert.rejects(f.api.saveSettings(await draft(f.api, { baseUrl: ds.baseUrl, model: ds.model })), /重新输入/);
  await f.api.saveSettings(await draft(f.api, { baseUrl: ds.baseUrl, model: ds.model, apiKey: key }));
  const old = await draft(f.api), before = fs.readFileSync(f.file, 'utf8');
  await assert.rejects(f.api.saveSettings({ ...old, baseUrl: 'https://elsewhere.invalid/v1', apiKey: '' }), /重新输入/);
  assert.equal(fs.readFileSync(f.file, 'utf8'), before);
  await f.api.saveSettings({ ...old, model: 'second', apiKey: '' });
  assert.equal((await f.api.resolve('workbench')).apiKey, key);
  await assert.rejects(f.api.saveSettings(old), /已更新/);
  await assert.rejects(f.api.saveSettings({ ...(await draft(f.api)), unexpected: true }), /格式无效/);
});

test('credential write/readback and atomic file failures retain usable previous configuration', async t => {
  for (const failure of ['write', 'readback', 'file', 'rename']) {
    const fakeFs = Object.create(fs);
    if (failure === 'file') fakeFs.writeFileSync = () => { throw Error('PRIVATE_SENTINEL'); };
    if (failure === 'rename') fakeFs.renameSync = () => { throw Error('PRIVATE_SENTINEL'); };
    const f = writable(t, [ds], { fileSystem: fakeFs });
    if (failure === 'write') f.options.writeCredential = async () => { throw Error('PRIVATE_SENTINEL'); };
    if (failure === 'readback') f.options.writeCredential = async () => {};
    const api = createGumCodeProviders(f.options), before = fs.readFileSync(f.file, 'utf8');
    await assert.rejects(api.saveSettings(await draft(api, { apiKey: 'SYNTHETIC_REPLACEMENT', model: 'new' })), safe);
    assert.equal(fs.readFileSync(f.file, 'utf8'), before);
    assert.deepEqual(await api.resolve('workbench'), { baseUrl: ds.baseUrl, model: ds.model, apiKey: key });
    assert.equal(f.deleted.length, 1); assert.equal(f.secrets.size, 1);
    assert.deepEqual(fs.readdirSync(f.options.dataDir), ['providers.json']);
  }
});

test('concurrent saves reject and in-flight resolves keep old endpoint/model/key snapshot', async t => {
  let releaseWrite, startedWrite;
  const started = new Promise(resolve => { startedWrite = resolve; });
  const f = writable(t);
  f.options.writeCredential = async (id, value) => { startedWrite(); await new Promise(resolve => { releaseWrite = resolve; }); f.secrets.set(id, value); };
  const api = createGumCodeProviders(f.options), request = await draft(api, { apiKey: 'SYNTHETIC_NEW_KEY', model: 'new-model' });
  const pending = api.saveSettings(request); await started;
  await assert.rejects(api.saveSettings(request), /正在保存/);
  assert.equal((await api.resolve('workbench')).model, ds.model);
  releaseWrite(); await pending;
  assert.equal((await api.resolve('workbench')).model, 'new-model');

  let releaseRead, blockOld = false;
  const f2 = writable(t);
  f2.options.readCredential = async id => { if (blockOld && id === ds.id) await new Promise(resolve => { releaseRead = resolve; }); return f2.secrets.get(id); };
  const snapshotApi = createGumCodeProviders(f2.options);
  const replacement = await draft(snapshotApi, { apiKey: 'SYNTHETIC_NEXT', model: 'next', baseUrl: 'https://next.invalid/v1' });
  blockOld = true;
  const snapshot = snapshotApi.resolve('workbench');
  await snapshotApi.saveSettings(replacement);
  releaseRead();
  assert.deepEqual(await snapshot, { baseUrl: ds.baseUrl, model: ds.model, apiKey: key });
  assert.deepEqual(await snapshotApi.resolve('workbench'), { baseUrl: 'https://next.invalid/v1', model: 'next', apiKey: 'SYNTHETIC_NEXT' });
});

test('corrupt, ambiguous and externally changed files are never overwritten by settings', async t => {
  const f = writable(t);
  const request = await draft(f.api, { apiKey: key });
  fs.writeFileSync(f.file, 'PRIVATE_SENTINEL');
  await assert.rejects(f.api.saveSettings(request), safe);
  assert.equal(fs.readFileSync(f.file, 'utf8'), 'PRIVATE_SENTINEL');
  const bad = createGumCodeProviders(f.options);
  await assert.rejects(bad.getSettings(), safe);
  await assert.rejects(bad.saveSettings(request), safe);
  const ambiguous = writable(t, [ds, { ...other, name: 'GuMi-DS' }]);
  await assert.rejects(ambiguous.api.getSettings(), /不唯一/);
  await assert.rejects(ambiguous.api.saveSettings(request), /不唯一/);
});

const completionResponse = () => new Response(JSON.stringify({ choices: [{ finish_reason: 'stop', message: { content: 'OK' } }] }));
test('connection test is synthetic, bounded, non-persistent and never sends an old key to changed endpoint', async t => {
  const calls = [];
  const f = writable(t, [ds], { fetchImpl: async (url, options) => { calls.push({ url, options }); return completionResponse(); } });
  const before = fs.readFileSync(f.file, 'utf8');
  assert.deepEqual(await f.api.testSettings(await draft(f.api)), { ok: true });
  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(calls[0].url, `${ds.baseUrl}/chat/completions`);
  assert.deepEqual(JSON.parse(calls[0].options.body).messages, [{ role: 'user', content: 'This is a connection test. Reply with OK.' }]);
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${key}`);
  await assert.rejects(f.api.testSettings(await draft(f.api, { baseUrl: 'https://other.invalid' })), /重新输入/);
  assert.equal(calls.length, 1);
  assert.equal(fs.readFileSync(f.file, 'utf8'), before);
});

test('test errors sanitize authentication, malformed/oversized response, network and timeout failures', async t => {
  const cases = [
    [async () => new Response('PRIVATE_SENTINEL', { status: 401 }), /认证失败/],
    [async () => new Response('PRIVATE_SENTINEL'), /无法识别/],
    [async () => new Response('x'.repeat(65537)), /过大/],
    [async () => { throw Error('PRIVATE_SENTINEL'); }, /连接失败/],
    [async (_, options) => new Promise((resolve, reject) => options.signal.addEventListener('abort', () => reject(Error('PRIVATE_SENTINEL')))), /超时/],
  ];
  for (const [fetchImpl, expected] of cases) {
    const f = writable(t, [ds], { fetchImpl, timeoutMs: 10 });
    await assert.rejects(f.api.testSettings(await draft(f.api)), error => safe(error) && expected.test(error.message));
  }
});


test('settings metadata never reads a secret; API fields reject ambiguous URL and key encodings', async t => {
  const f = writable(t, [ds], { readCredential: async () => { throw Error('must not be called'); } });
  assert.equal((await f.api.getSettings()).hasKey, true);
  for (const baseUrl of ['https://example.invalid/with space', 'https://example.invalid/with\\slash']) {
    await assert.rejects(f.api.saveSettings(await draft(f.api, { baseUrl, apiKey: key })), /有效/);
  }
  for (const apiKey of [' leading', 'trailing ', 'SYNTHETIC_中文', 'SYNTHETIC_\ncontrol']) {
    await assert.rejects(f.api.saveSettings(await draft(f.api, { apiKey })), /格式无效/);
  }
});

test('native writer sends only hexadecimal synthetic secret through stdin, with no credential in argv', async t => {
  const childProcess = require('node:child_process');
  const invocations = [], secrets = new Map([[`GuMi-Code.GumCode.${ds.id}`, key]]);
  t.mock.method(childProcess, 'execFile', (command, args, options, callback) => {
    invocations.push({ command, args });
    if (args[0] === 'find-generic-password') {
      queueMicrotask(() => callback(null, secrets.get(args[4]) + '\n'));
      return {};
    }
    assert.deepEqual(args, ['-i']);
    return { stdin: { on() {}, end(input) {
      const match = /^add-generic-password -a GuMi-Code -s (GuMi-Code\.GumCode\.[a-f0-9-]+) -X ([0-9a-f]+)\n$/.exec(input);
      assert.ok(match); secrets.set(match[1], Buffer.from(match[2], 'hex').toString('utf8'));
      queueMicrotask(() => callback(null, ''));
    } } };
  });
  const modulePath = require.resolve('../electron/gum-code-providers.cjs');
  const cached = require.cache[modulePath]; delete require.cache[modulePath];
  const fresh = require(modulePath); require.cache[modulePath] = cached;
  const f = setup(t), api = fresh.createGumCodeProviders({ dataDir: f.options.dataDir });
  const synthetic = 'SYNTHETIC_"quoted"_\\backslash_$(literal)`literal`';
  await api.saveSettings(await draft(api, { apiKey: synthetic }));
  assert.equal((await api.resolve('workbench')).apiKey, synthetic);
  assert.equal(JSON.stringify(invocations).includes(synthetic), false);
  assert.equal(JSON.stringify(invocations).includes(Buffer.from(synthetic).toString('hex')), false);
});
