'use strict';
// Main-process only: copy coding records; credentials never enter files or results.
const fs = require('node:fs');
const path = require('node:path');
const { createHash, randomUUID } = require('node:crypto');
const { spawn } = require('node:child_process');
const { DatabaseSync, backup } = require('node:sqlite');
const { getGumCodeRuntime, pythonEnvironment } = require('./gum-code-runtime.cjs');
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MB = 1024 * 1024;
const MARKER = 'workbench-import.json';
const safeErrors = new WeakSet();
const fail = message => { const error = Error(message); error.gumCodeSafe = true; safeErrors.add(error); throw error; };
const invalid = () => fail('原工作台编码数据无法安全迁移；请关闭工作台并检查数据目录。原数据已保留。');
const text = (s, n) => typeof s === 'string' && s.length <= n && !s.includes('\0');
const absolute = s => text(s, 4096) && path.isAbsolute(s) && path.normalize(s) === s;
const hash = s => createHash('sha256').update(s).digest('hex');
const keys = o => o && !Array.isArray(o) && Object.keys(o).sort().join(',');
function workspaceId(workspace) {
  // RFC 4122 name UUID: retries create the same scope without touching the source.
  const namespace = Buffer.from('6ba7b8119dad11d180b400c04fd430c8', 'hex');
  const bytes = createHash('sha1').update(namespace).update(`GuMi-Code imported workspace:${workspace}`).digest().subarray(0, 16);
  bytes[6] = (bytes[6] & 0x0f) | 0x50; bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex'); return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function directory(file) {
  // Refuse symlinks anywhere along selected paths, including parent directories.
  for (let current = path.resolve(file); ; current = path.dirname(current)) {
    const s = fs.lstatSync(current);
    if (!s.isDirectory() || s.isSymbolicLink()) invalid();
    if (path.dirname(current) === current) break;
  }
}
function read(file, max) {
  const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try {
    const s = fs.fstatSync(fd);
    if (!s.isFile() || s.nlink !== 1 || s.size > max) invalid();
    const contents = fs.readFileSync(fd);
    const after = fs.fstatSync(fd);
    if (contents.length !== s.size || after.size !== s.size || after.mtimeMs !== s.mtimeMs) invalid();
    return { contents, stamp: `${s.dev}:${s.ino}:${s.size}:${s.mtimeMs}:${hash(contents)}` };
  } finally { fs.closeSync(fd); }
}
function parse(file, max) { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(read(file, max).contents)); }
function catalog(file, contents, allowMissingWorkspaces = false) {
  const c = contents ? JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(contents)) : parse(file, 16 * MB);
  const shape = keys(c);
  if (shape !== 'selectedId,sessions,trusted,version,workspaces' && !(allowMissingWorkspaces && shape === 'selectedId,sessions,trusted,version')) invalid();
  if (c.workspaces === undefined) c.workspaces = [];
  if (c.version !== 1 || !Array.isArray(c.sessions) || c.sessions.length > 200 || !Array.isArray(c.workspaces) || c.workspaces.length > 200 || !Array.isArray(c.trusted) || c.trusted.length > 200 || !c.trusted.every(absolute)) invalid();
  if (!c.workspaces.every(w => keys(w) === 'id,name,path' && UUID.test(w.id) && text(w.name, 4096) && w.name && absolute(w.path)) || new Set(c.workspaces.map(w => w.id)).size !== c.workspaces.length || new Set(c.workspaces.map(w => w.path)).size !== c.workspaces.length) invalid();
  const allowed = ['id', 'projectId', 'workspaceId', 'workspace', 'title', 'updatedAt', 'pendingText', 'pendingInputs', 'providerId', 'titleSummarized'];
  for (const s of c.sessions) {
    if (!keys(s) || Object.keys(s).some(k => !allowed.includes(k)) || !UUID.test(s.id) || !absolute(s.workspace) || !text(s.title, 100) || !Number.isSafeInteger(s.updatedAt) || !(UUID.test(s.workspaceId) || (text(s.projectId, 200) && s.projectId)) || (s.workspaceId !== undefined && !UUID.test(s.workspaceId)) || (s.projectId !== undefined && !text(s.projectId, 200)) || (s.pendingText !== undefined && !text(s.pendingText, 16000)) || (s.titleSummarized !== undefined && typeof s.titleSummarized !== 'boolean') || (s.providerId !== undefined && s.providerId !== 'workbench' && !UUID.test(s.providerId))) invalid();
    if (s.pendingInputs !== undefined && (!Array.isArray(s.pendingInputs) || s.pendingInputs.length > 8 || !s.pendingInputs.every(p => keys(p) === 'id,text' && UUID.test(p.id) && text(p.text, 16000) && p.text.trim()) || new Set(s.pendingInputs.map(p => p.id)).size !== s.pendingInputs.length)) invalid();
    let w = c.workspaces.find(w => w.path === s.workspace);
    if (s.workspaceId && w && w.id !== s.workspaceId) invalid();
    // Missing canonical IDs cannot be guessed: existing memories use them as scope.
    if (!w) {
      if (!allowMissingWorkspaces || c.workspaces.length >= 200) invalid();
      w = { id: s.workspaceId || workspaceId(s.workspace), name: path.basename(s.workspace) || s.workspace, path: s.workspace }; c.workspaces.push(w);
    }
    s.workspaceId = w.id;
    delete s.projectId;
  }
  if (new Set(c.sessions.map(s => s.id)).size !== c.sessions.length || new Set(c.workspaces.map(w => w.id)).size !== c.workspaces.length || !(c.selectedId === null || c.sessions.some(s => s.id === c.selectedId))) invalid();
  c.trusted = c.trusted.filter(p => c.workspaces.some(w => w.path === p));
  return c;
}
function providers(file, contents) {
  if (!contents && !fs.existsSync(file)) return null;
  const p = contents ? JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(contents)) : parse(file, 64 * 1024);
  if (![1, 2].includes(p.version) || keys(p) !== (p.version === 1 ? 'profiles,version' : 'activeId,credentialId,profiles,version') || !Array.isArray(p.profiles) || p.profiles.length > 20) invalid();
  for (const e of p.profiles) {
    if (keys(e) !== 'baseUrl,id,model,name' || !UUID.test(e.id) || !text(e.name, 40) || !e.name.trim() || e.name !== e.name.trim() || !text(e.model, 120) || !e.model.trim() || e.model !== e.model.trim() || !text(e.baseUrl, 2048) || /[\p{Cc}\s\\]/u.test(e.baseUrl)) invalid();
    const u = new URL(e.baseUrl), local = u.hostname === 'localhost' || u.hostname === '[::1]' || /^127\.\d+\.\d+\.\d+$/.test(u.hostname);
    if (u.username || u.password || u.search || u.hash || e.baseUrl.includes('?') || e.baseUrl.includes('#') || (u.protocol !== 'https:' && !(u.protocol === 'http:' && local))) invalid();
  }
  if (new Set(p.profiles.map(e => e.id)).size !== p.profiles.length || (p.version === 2 && (!UUID.test(p.activeId) || !UUID.test(p.credentialId) || !p.profiles.some(e => e.id === p.activeId)))) invalid();
  if (p.version === 1 && p.profiles.length && p.profiles.filter(e => e.name === 'GuMi-DS').length !== 1) invalid();
  return p;
}
const memoryColumns = {
  settings: ['id', 'enabled', 'epoch', 'revision'], sources: ['seq', 'id', 'scope', 'sessionId', 'turnId', 'inputId', 'digest', 'epoch', 'proof', 'factsProcessed', 'experiencesProcessed'],
  memories: ['id', 'scope', 'kind', 'factKey', 'body', 'status', 'revision', 'sourceId', 'sourceOrder', 'reason', 'verified', 'skillRef', 'skillDigest', 'proof'],
  invalidations: ['sourceId', 'factKey', 'scope', 'cutoff'], jobs: ['id', 'sourceId', 'status', 'attempts', 'error'],
};
function inspectMemory(file, c, empty = false) {
  read(file, 256 * MB);
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    if (db.prepare('PRAGMA user_version').get().user_version !== 1 || db.prepare('PRAGMA quick_check').get().quick_check !== 'ok') invalid();
    const schema = db.prepare("SELECT type,name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").all();
    if (schema.some(s => !((s.type === 'table' && Object.hasOwn(memoryColumns, s.name)) || (s.type === 'index' && s.name === 'source_event')))) invalid();
    for (const [table, columns] of Object.entries(memoryColumns)) {
      const actual = db.prepare(`PRAGMA table_info(${table})`).all().map(r => r.name);
      if (actual.some(k => !columns.includes(k)) || !columns.filter(k => !['proof', 'skillDigest', 'factsProcessed', 'experiencesProcessed'].includes(k)).every(k => actual.includes(k))) invalid();
    }
    const settings = db.prepare('SELECT * FROM settings').all();
    if (settings.length !== 1 || settings[0].id !== 1 || ![0, 1].includes(settings[0].enabled) || !Number.isSafeInteger(settings[0].epoch) || !Number.isSafeInteger(settings[0].revision)) invalid();
    const counts = Object.fromEntries(['sources', 'memories', 'invalidations', 'jobs'].map(t => [t, Number(db.prepare(`SELECT count(*) AS n FROM ${t}`).get().n)]));
    if (empty && (Object.values(counts).some(n => n) || settings[0].enabled || settings[0].epoch || settings[0].revision)) fail('GuMi-Code 已有数据或配置，无法覆盖。请保留现有数据。');
    if (c) {
      const scopes = new Set(['global', ...c.workspaces.map(w => w.id)]), sessions = new Set(c.sessions.map(s => s.id));
      for (const row of db.prepare('SELECT DISTINCT scope FROM memories UNION SELECT DISTINCT scope FROM sources UNION SELECT DISTINCT scope FROM invalidations').all()) if (!scopes.has(row.scope)) invalid();
      for (const row of db.prepare('SELECT DISTINCT sessionId FROM sources').all()) if (!sessions.has(row.sessionId)) invalid();
    }
    return { db, memories: counts.memories };
  } catch (error) { db.close(); throw error; }
}
function emptyTarget(dir) {
  if (!fs.existsSync(dir)) return;
  directory(dir);
  const names = fs.readdirSync(dir);
  if (names.some(n => !['catalog.json', 'providers.json', 'memory.sqlite'].includes(n))) fail('GuMi-Code 已有数据或配置，无法覆盖。请保留现有数据。');
  if (names.includes('catalog.json')) { const c = catalog(path.join(dir, 'catalog.json')); if (c.sessions.length || c.workspaces.length || c.trusted.length) fail('GuMi-Code 已有数据或配置，无法覆盖。请保留现有数据。'); }
  if (names.includes('providers.json') && providers(path.join(dir, 'providers.json')).profiles.length) fail('GuMi-Code 已有 API 配置，无法覆盖。请保留现有配置。');
  if (names.includes('memory.sqlite')) inspectMemory(path.join(dir, 'memory.sqlite'), null, true).db.close();
}
function inventory(dir, c) {
  const files = new Map(), allowed = new Set(['catalog.json', 'memory.sqlite', 'providers.json']);
  for (const s of c.sessions) for (const suffix of ['.jsonl', '.jsonl.lock', '.jsonl.segments']) allowed.add(s.id + suffix);
  let total = 0;
  const add = (rel, max) => { const r = read(path.join(dir, rel), max); total += r.contents.length; if (total > 512 * MB) invalid(); files.set(rel, r.stamp); return r.contents; };
  const names = fs.readdirSync(dir).sort();
  if (names.some(n => !allowed.has(n))) invalid();
  for (const n of ['catalog.json', 'memory.sqlite', 'providers.json']) if (names.includes(n)) add(n, n === 'memory.sqlite' ? 256 * MB : n === 'catalog.json' ? 16 * MB : 64 * 1024);
  const locks = [];
  for (const s of c.sessions) {
    const base = `${s.id}.jsonl`, segments = `${base}.segments`, lock = `${base}.lock`;
    if (!names.includes(base)) { if (names.includes(segments)) invalid(); if (names.includes(lock)) { add(lock, 4096); locks.push(path.join(dir, lock)); } continue; }
    if (names.includes(lock)) { add(lock, 4096); locks.push(path.join(dir, lock)); }
    let expected = 0, headerId;
    const log = rel => {
      const content = new TextDecoder('utf-8', { fatal: true }).decode(add(rel, 5 * MB));
      if (expected === 0 && rel === base) {
        let snapshot; try { snapshot = JSON.parse(content); } catch {}
        if (snapshot && [1, 2].includes(snapshot.version)) {
          if (names.includes(segments) || snapshot.workspace !== s.workspace || !text(snapshot.model, 4096) || !snapshot.model || !Array.isArray(snapshot.messages) || !snapshot.messages.length || snapshot.messages[0]?.role !== 'system' || !snapshot.messages.every(m => keys(m) && ['system', 'user', 'assistant', 'tool'].includes(m.role)) || (snapshot.version === 2 && !text(snapshot.title, 30))) invalid();
          expected = 1; return;
        }
      }
      if (!content.endsWith('\n')) invalid();
      const lines = content.slice(0, -1).split('\n');
      for (const line of lines) {
        const event = JSON.parse(line);
        if (keys(event) !== 'data,seq,time,type' || event.seq !== expected || !text(event.time, 80) || !event.time.endsWith('Z') || !Number.isFinite(Date.parse(event.time)) || !['session.created', 'session.metadata_updated', 'session.recovered', 'turn.started', 'turn.completed', 'message.appended', 'tool.calls', 'tool.execution_started', 'tool.result', 'skill.activated', 'context.compacted', 'task.state', 'vision.state', 'self_check.state', 'plan.state'].includes(event.type) || !event.data || Array.isArray(event.data) || typeof event.data !== 'object') invalid();
        if (expected === 0) { if (event.type !== 'session.created' || event.data.version !== 3 || !UUID.test(event.data.id) || event.data.workspace !== s.workspace) invalid(); headerId = event.data.id; }
        expected++;
      }
    };
    log(base);
    if (names.includes(segments)) {
      directory(path.join(dir, segments));
      const index = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(add(`${segments}/index.json`, 4096)));
      if (keys(index) !== 'segments,session_id,version' || index.version !== 1 || index.session_id !== headerId || !Number.isSafeInteger(index.segments) || index.segments < 2 || index.segments > 10000) invalid();
      const expectedNames = ['index.json', ...Array.from({ length: index.segments - 1 }, (_, i) => `${String(i + 1).padStart(16, '0')}.jsonl`)].sort();
      if (JSON.stringify(fs.readdirSync(path.join(dir, segments)).sort()) !== JSON.stringify(expectedNames)) invalid();
      for (const n of expectedNames) if (n !== 'index.json') log(`${segments}/${n}`);
    }
  }
  return { files, locks, names };
}
// Hold existing Python SessionLease locks without creating/chmodding source files.
async function lease(paths, python) {
  if (!paths.length) return () => {};
  const code = 'import os,sys,fcntl,stat,json\nf=[]\ntry:\n for p in json.loads(sys.stdin.readline()):\n  d=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)\n  if not stat.S_ISREG(os.fstat(d).st_mode): raise ValueError()\n  fcntl.flock(d,fcntl.LOCK_EX|fcntl.LOCK_NB);f.append(d)\n print("ready",flush=True)\n sys.stdin.read()\nfinally:\n for d in f: os.close(d)\n';
  const env = { ...process.env }; for (const k of Object.keys(env)) if (k.startsWith('PYTHON') || ['VIRTUAL_ENV', 'CONDA_PREFIX', '__PYVENV_LAUNCHER__'].includes(k)) delete env[k];
  return new Promise((resolve, reject) => {
    const child = spawn(python || '/usr/bin/python3', ['-I', '-c', code], { env, stdio: ['pipe', 'pipe', 'ignore'] });
    let ready = false, output = '';
    const timer = setTimeout(() => { child.kill(); reject(Error()); }, 15000);
    child.on('error', () => { clearTimeout(timer); reject(Error()); });
    child.stdin.on('error', () => {});
    child.on('exit', () => { clearTimeout(timer); if (!ready) reject(Error()); });
    child.stdout.on('data', data => { output += data; if (output === 'ready\n' && !ready) {
      ready = true; clearTimeout(timer);
      const release = () => new Promise(done => {
        if (child.exitCode !== null || child.signalCode !== null) return done();
        const killTimer = setTimeout(() => { child.kill(); }, 2000);
        child.once('exit', () => { clearTimeout(killTimer); done(); }); child.stdin.end();
      });
      release.assertHeld = () => { if (child.exitCode !== null || child.signalCode !== null) invalid(); };
      resolve(release);
    } else if (output.length > 32) child.kill(); });
    child.stdin.write(JSON.stringify(paths) + '\n');
  });
}
function write(file, contents) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const fd = fs.openSync(file, 'wx', 0o600);
  try { fs.writeFileSync(fd, contents); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}
async function validateStagedHistory(stage, session, runtime) {
  const sessionPath = path.join(stage, `${session.id}.jsonl`);
  if (!fs.existsSync(sessionPath)) return;
  const turnId = randomUUID();
  const converted = await new Promise((resolve, reject) => {
    const child = spawn(runtime.python, ['-u', '-m', 'gum_code.desktop'], { env: pythonEnvironment(runtime.source), stdio: ['pipe', 'pipe', 'ignore'] });
    let bytes = 0, output = '';
    const timeout = setTimeout(() => { child.kill(); reject(Error()); }, 30000);
    child.on('error', () => { clearTimeout(timeout); reject(Error()); }); child.stdin.on('error', () => {});
    child.stdout.on('data', chunk => { bytes += chunk.length; if (bytes > 65536) { child.kill(); return; } output += chunk; });
    child.on('close', code => {
      clearTimeout(timeout);
      try {
        if (code !== 0 || bytes > 65536 || !output.endsWith('\n')) throw Error();
        const rows = output.trim().split('\n').map(line => JSON.parse(line));
        if (rows.length !== 2 || rows.some((r, i) => r.v !== 1 || r.sessionId !== session.id || r.turnId !== turnId || r.seq !== i + 1) || rows[0].type !== 'history_migrated' || typeof rows[0].converted !== 'boolean' || rows[1].type !== 'finished' || rows[1].status !== 'completed') throw Error();
        resolve(rows[0].converted);
      } catch { reject(Error()); }
    });
    child.stdin.end(JSON.stringify({ v: 1, action: 'migrate_history', sessionId: session.id, turnId, workspace: session.workspace, sessionPath }) + '\n');
  });
  if (converted) {
    const next = path.join(stage, `${session.id}.converted.jsonl`); read(next, 5 * MB);
    fs.renameSync(next, sessionPath);
    if (fs.existsSync(next + '.segments')) { directory(next + '.segments'); fs.renameSync(next + '.segments', sessionPath + '.segments'); }
  }
}
async function migrateWorkbench({ sourceUserData, targetDataDir, credentials, python, runtime = getGumCodeRuntime(), sourceConfirmedClosed = false }) {
  let stage, previous, release, memory, published = false;
  const slots = [];
  try {
    if (!absolute(sourceUserData) || !absolute(targetDataDir)) invalid();
    if (!sourceConfirmedClosed) fail('请先关闭原工作台，再确认迁移。原数据已保留。');
    directory(sourceUserData); directory(path.dirname(targetDataDir));
    const source = path.join(sourceUserData, 'gum-code'); directory(source);
    if (source === targetDataDir || targetDataDir.startsWith(source + path.sep) || source.startsWith(targetDataDir + path.sep)) invalid();
    const marker = path.join(targetDataDir, MARKER);
    if (fs.existsSync(targetDataDir)) directory(targetDataDir);
    if (fs.existsSync(marker)) {
      const m = parse(marker, 4096);
      if (keys(m) !== 'counts,source,version' || m.version !== 1 || m.source !== hash(source)) fail('GuMi-Code 已完成其他来源的迁移，不能覆盖。');
      if (keys(m.counts) !== 'memories,providers,sessions,workspaces' || Object.values(m.counts).some(v => !Number.isSafeInteger(v) || v < 0)) invalid();
      return { imported: false, alreadyImported: true, ...m.counts };
    }
    emptyTarget(targetDataDir);
    const catalogFile = path.join(source, 'catalog.json'), providerFile = path.join(source, 'providers.json');
    const initialCatalog = read(catalogFile, 16 * MB), initialProvider = fs.existsSync(providerFile) ? read(providerFile, 64 * 1024) : null;
    const c = catalog(catalogFile, initialCatalog.contents, !fs.existsSync(path.join(source, 'memory.sqlite'))), p = initialProvider ? providers(providerFile, initialProvider.contents) : null;
    const before = inventory(source, c);
    if (before.files.get('catalog.json') !== initialCatalog.stamp || (before.files.get('providers.json') || null) !== (initialProvider?.stamp || null)) invalid();
    release = await lease(before.locks, python);
    const stable = inventory(source, c);
    if (JSON.stringify([...before.files]) !== JSON.stringify([...stable.files])) invalid();
    stage = fs.mkdtempSync(path.join(path.dirname(targetDataDir), '.workbench-import-')); fs.chmodSync(stage, 0o700);
    for (const rel of before.files.keys()) {
      if (['catalog.json', 'providers.json', 'memory.sqlite'].includes(rel) || rel.endsWith('.lock')) continue;
      write(path.join(stage, rel), read(path.join(source, rel), 5 * MB).contents);
    }
    write(path.join(stage, 'catalog.json'), JSON.stringify(c));
    for (const session of c.sessions) await validateStagedHistory(stage, session, runtime);
    let memories = 0;
    if (before.files.has('memory.sqlite')) {
      memory = inspectMemory(path.join(source, 'memory.sqlite'), c); memories = memory.memories;
      await backup(memory.db, path.join(stage, 'memory.sqlite')); fs.chmodSync(path.join(stage, 'memory.sqlite'), 0o600);
      memory.db.close(); memory = null;
      inspectMemory(path.join(stage, 'memory.sqlite'), c).db.close();
    }
    if (p?.profiles.length) {
      if (!credentials || !['readSource', 'writeTarget', 'readTarget', 'deleteTarget'].every(k => typeof credentials[k] === 'function')) fail('无法迁移 API 凭据。原数据和配置已保留。');
      const active = p.version === 2 ? p.activeId : p.profiles.find(e => e.name === 'GuMi-DS').id;
      const needed = [p.version === 2 ? p.credentialId : active], mapping = new Map();
      for (const id of needed) {
        const key = await credentials.readSource(id);
        if (!text(key, 4096) || !key.trim() || key !== key.trim() || /[^\x20-\x7e]/.test(key)) fail('无法读取原 API 凭据，请检查钥匙串授权。原数据已保留。');
        const next = randomUUID(); slots.push(next);
        await credentials.writeTarget(next, key);
        if (await credentials.readTarget(next) !== key) fail('无法确认新 API 凭据已保存。原数据和配置已保留。');
        mapping.set(id, next);
      }
      // The source host only resolves its one shared active API; keep inactive metadata.
      write(path.join(stage, 'providers.json'), JSON.stringify({ version: 2, activeId: active, credentialId: mapping.get(p.version === 2 ? p.credentialId : active), profiles: p.profiles }));
    } else if (p) write(path.join(stage, 'providers.json'), JSON.stringify({ version: 1, profiles: [] }));
    const after = inventory(source, c);
    release.assertHeld?.();
    if (JSON.stringify(before.names) !== JSON.stringify(after.names) || JSON.stringify([...before.files]) !== JSON.stringify([...after.files])) fail('迁移期间原工作台数据发生变化。请关闭工作台后重试。原数据已保留。');
    emptyTarget(targetDataDir);
    const counts = { sessions: c.sessions.length, workspaces: c.workspaces.length, memories, providers: p?.profiles.length || 0 };
    write(path.join(stage, MARKER), JSON.stringify({ version: 1, source: hash(source), counts }));
    if (fs.existsSync(targetDataDir)) { previous = path.join(path.dirname(targetDataDir), `.workbench-empty-${randomUUID()}`); fs.renameSync(targetDataDir, previous); }
    try { fs.renameSync(stage, targetDataDir); stage = null; published = true; }
    catch (error) { if (previous) { fs.renameSync(previous, targetDataDir); previous = null; } throw error; }
    // Publication completed; cleanup failure must not report a failed import or delete keys.
    if (previous) { try { fs.rmSync(previous, { recursive: true }); } catch {} }
    return { imported: true, alreadyImported: false, ...counts };
  } catch (error) {
    if (!published && credentials) for (const id of slots) { try { await credentials.deleteTarget(id); } catch {} }
    if (safeErrors.has(error)) throw error;
    fail('迁移未完成。请关闭工作台并检查目录及钥匙串授权，原数据和配置已保留。');
  } finally {
    if (memory) memory.db.close();
    if (release) await release();
    if (stage) { try { fs.rmSync(stage, { recursive: true }); } catch {} }
  }
}
module.exports = { migrateWorkbench };
