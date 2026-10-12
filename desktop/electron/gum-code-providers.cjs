'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { createHash, randomUUID } = require('node:crypto');
const { createAICompletion } = require('./ai-plan.cjs');

const MAX_FILE = 64 * 1024;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const CONTROL = /[\p{Cc}\u2028\u2029]/u;
const PROVIDER_NAME = 'GuMi-DS';
const service = (id, namespace = "GuMi-Code") => `${namespace}.GumCode.${id}`;
const safeError = message => { const error = Error(message); error.gumCodeSafe = true; return error; };
const credentialError = () => safeError('无法读取模型凭据，请检查钥匙串授权。');
const validKey = key => typeof key === 'string' && Boolean(key.trim()) && key === key.trim() && Buffer.byteLength(key) <= 4096 && !CONTROL.test(key) && !/[^\x20-\x7e]/.test(key);

function readKey(id, namespace = "GuMi-Code") {
  return new Promise((resolve, reject) => execFile('/usr/bin/security', ['find-generic-password', '-a', namespace, '-s', service(id, namespace), '-w'], { timeout: 15000, maxBuffer: 16384 }, (error, stdout) => {
    if (error) return reject(credentialError());
    // security adds one line ending; preserve intentional whitespace inside the key.
    const key = stdout.replace(/\r?\n$/, '');
    if (!validKey(key)) return reject(credentialError());
    resolve(key);
  }));
}
function normalizedText(value, max) {
  if (typeof value !== 'string' || !value.trim() || value.trim().length > max || CONTROL.test(value)) throw Error();
  return value.trim();
}
function normalizedBase(value) {
  const input = normalizedText(value, 2048);
  if (/[\s\\]/u.test(input)) throw Error();
  const url = new URL(input);
  const loopback = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127\.\d+\.\d+\.\d+$/.test(url.hostname);
  if (url.username || url.password || url.search || url.hash || input.includes('?') || input.includes('#') || (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))) throw Error();
  return url.href.replace(/\/+$/, '');
}
function profile(request) {
  return { name: normalizedText(request?.name, 40), baseUrl: normalizedBase(request?.baseUrl), model: normalizedText(request?.model, 120) };
}

// Interactive security receives the command through a private pipe, never argv.
function writeKey(id, key) {
  return new Promise((resolve, reject) => {
    const child = execFile('/usr/bin/security', ['-i'], { timeout: 15000, maxBuffer: 16384 }, error => error ? reject(credentialError()) : resolve());
    child.stdin.on('error', () => reject(credentialError()));
    child.stdin.end(`add-generic-password -a GuMi-Code -s ${service(id)} -X ${Buffer.from(key, 'utf8').toString('hex')}\n`);
  });
}
function deleteKey(id) {
  return new Promise(resolve => execFile('/usr/bin/security', ['delete-generic-password', '-a', 'GuMi-Code', '-s', service(id)], { timeout: 15000, maxBuffer: 16384 }, () => resolve()));
}
const digest = content => createHash('sha256').update(content ?? '').digest('hex');

function createGumCodeProviders({ dataDir, readCredential = readKey, writeCredential = writeKey, deleteCredential = deleteKey, fileSystem = fs, fetchImpl = fetch, timeoutMs = 20000 }) {
  const file = path.join(dataDir, 'providers.json');
  let profiles = [], initError = null, activeId = null, credentialId = null, raw = null, saving = false;
  const checkPaths = () => {
    const directory = fileSystem.lstatSync(dataDir);
    if (directory.isSymbolicLink() || !directory.isDirectory()) throw Error();
    try {
      const stat = fileSystem.lstatSync(file);
      if (stat.isSymbolicLink() || !stat.isFile() || stat.size > MAX_FILE) throw Error();
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  };
  const diskContents = () => {
    checkPaths();
    try { return fileSystem.readFileSync(file, 'utf8'); }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  };
  try {
    fileSystem.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
    raw = diskContents(); fileSystem.chmodSync(dataDir, 0o700);
    if (raw !== null) {
      const parsed = JSON.parse(raw);
      const keys = parsed && Object.keys(parsed).sort().join(',');
      if (!parsed || ![1, 2].includes(parsed.version) || keys !== (parsed.version === 1 ? 'profiles,version' : 'activeId,credentialId,profiles,version') || !Array.isArray(parsed.profiles) || parsed.profiles.length > 20) throw Error();
      for (const entry of parsed.profiles) {
        if (!entry || !UUID.test(entry.id) || Object.keys(entry).sort().join(',') !== 'baseUrl,id,model,name') throw Error();
        const normalized = profile(entry);
        if (Object.entries(normalized).some(([key, value]) => entry[key] !== value)) throw Error();
      }
      if (new Set(parsed.profiles.map(entry => entry.id)).size !== parsed.profiles.length) throw Error();
      profiles = parsed.profiles;
      if (parsed.version === 2) {
        if (!UUID.test(parsed.activeId) || !UUID.test(parsed.credentialId) || !profiles.some(entry => entry.id === parsed.activeId)) throw Error();
        activeId = parsed.activeId; credentialId = parsed.credentialId;
      }
      fileSystem.chmodSync(file, 0o600);
    }
  } catch { initError = '模型配置列表无法读取，原文件已保留。请检查本地应用数据目录。'; }
  const check = () => { if (initError) throw safeError(initError); };
  const selected = (allowEmpty = false) => {
    check();
    if (activeId) return profiles.find(entry => entry.id === activeId);
    const matches = profiles.filter(entry => entry.name === PROVIDER_NAME);
    if (!matches.length && allowEmpty && !profiles.length) return null;
    if (!matches.length) throw safeError('API 尚未配置，请在设置中填写接口地址、模型和密钥。');
    if (matches.length !== 1) throw safeError('GuMi-DS 配置不唯一，请先修复模型配置。');
    return matches[0];
  };
  const read = async id => {
    try { const key = await readCredential(id); if (!validKey(key)) throw Error(); return key; }
    catch { throw credentialError(); }
  };
  const publicState = async () => {
    const entry = selected(true);
    // hasKey means a credential slot is configured; validity is checked on use.
    return { baseUrl: entry?.baseUrl || '', model: entry?.model || '', hasKey: Boolean(entry), revision: digest(raw) };
  };
  const prepare = async request => {
    const entry = selected(true), slot = credentialId || entry?.id;
    if (!request || typeof request !== 'object' || Array.isArray(request) || Object.keys(request).some(key => !['baseUrl', 'model', 'apiKey', 'revision'].includes(key))) throw safeError('API 配置格式无效。');
    if (request.revision !== digest(raw)) throw safeError('API 配置已更新，请重新打开设置后再试。');
    let baseUrl, model;
    try { baseUrl = normalizedBase(request.baseUrl); model = normalizedText(request.model, 120); }
    catch { throw safeError('请输入有效的 HTTPS 接口地址（本机地址可用 HTTP）和模型名称。'); }
    const hasNewKey = request.apiKey !== undefined && request.apiKey !== '';
    if (hasNewKey && !validKey(request.apiKey)) throw safeError('API Key 格式无效。');
    if (!hasNewKey && (!entry || entry.baseUrl !== baseUrl)) throw safeError('首次配置或更改接口地址时，请重新输入 API Key。');
    const apiKey = hasNewKey ? request.apiKey : await read(slot);
    return { entry, baseUrl, model, apiKey, hasNewKey, slot };
  };
  const unchanged = () => {
    try { if (diskContents() !== raw) throw Error(); }
    catch { throw safeError('本地 API 配置已变化或无法读取，请重启应用后再试。'); }
  };
  return {
    async list() { return [{ ...selected() }]; },
    getSettings: publicState,
    async saveSettings(request) {
      if (saving) throw safeError('API 配置正在保存，请稍后再试。');
      saving = true;
      let newSlot = null, temp = null;
      try {
        unchanged();
        const next = await prepare(request);
        const id = next.entry?.id || randomUUID();
        let slot = next.slot;
        if (next.hasNewKey) {
          newSlot = randomUUID(); slot = newSlot;
          try {
            await writeCredential(slot, next.apiKey);
            // security interactive mode may return zero after a failed command.
            if (await read(slot) !== next.apiKey) throw Error();
          } catch { throw safeError('无法保存 API 凭据，请检查钥匙串授权。原配置未更改。'); }
        }
        const entry = { id, name: '自定义 API', baseUrl: next.baseUrl, model: next.model };
        const nextProfiles = next.entry ? profiles.map(item => item.id === id ? entry : item) : [entry];
        const content = JSON.stringify({ version: 2, activeId: id, credentialId: slot, profiles: nextProfiles });
        unchanged();
        temp = path.join(dataDir, `.providers-${randomUUID()}.tmp`);
        try {
          fileSystem.writeFileSync(temp, content, { mode: 0o600, flag: 'wx' });
          unchanged();
          fileSystem.renameSync(temp, file);
        } catch { throw safeError('API 配置保存失败，原配置未更改。请检查本地应用数据目录。'); }
        profiles = nextProfiles; activeId = id; credentialId = slot; raw = content; newSlot = null;
        // ponytail: retain prior slots so pending credential reads keep their snapshot;
        // add reference-counted cleanup only if credential rotation becomes frequent.
        return { baseUrl: entry.baseUrl, model: entry.model, hasKey: true, revision: digest(raw) };
      } finally {
        if (temp) { try { fileSystem.unlinkSync(temp); } catch {} }
        if (newSlot) { try { await deleteCredential(newSlot); } catch {} }
        saving = false;
      }
    },
    async testSettings(request) {
      unchanged();
      const { baseUrl, model, apiKey } = await prepare(request);
      const complete = createAICompletion({ fetchImpl, timeoutMs, resolveProvider: async () => ({ baseUrl, model, apiKey }) });
      try {
        await complete([{ role: 'user', content: 'This is a connection test. Reply with OK.' }], 32);
        return { ok: true };
      } catch (error) {
        const allowed = ['AI 请求超时，请重试。', 'AI 服务连接失败，请稍后重试。', 'AI 响应读取失败或过大，请重试。', 'AI 服务暂时不可用，请稍后重试。', 'AI 服务返回内容无法识别，请重试。'];
        throw safeError(error.message === 'AI 认证失败，请检查钥匙串中的凭据。' ? 'API 认证失败，请检查 API Key。' : allowed.includes(error.message) ? error.message : 'API 连接测试失败，请检查接口与模型名称后重试。');
      }
    },
    async resolve(id) {
      const entry = selected(), slot = credentialId || entry.id;
      if (id !== 'workbench' && id !== entry.id) throw safeError('GuMi-Code 仅允许使用已配置的 API，请重新选择模型。');
      const apiKey = await read(slot);
      return { baseUrl: entry.baseUrl, model: entry.model, apiKey };
    },
  };
}
module.exports = { createGumCodeProviders, readKey, writeKey, deleteKey };
