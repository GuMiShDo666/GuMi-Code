const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { createWorkspaceFiles } = require('../electron/gum-code-files.cjs');

function setup(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'gumi-files-test-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspace = path.join(root, 'workspace'); fs.mkdirSync(workspace);
  const session = { id: 'synthetic-session', workspace };
  let chosen = [];
  const files = createWorkspaceFiles({ workspace: s => s.workspace, pickFiles: async () => chosen });
  return { root, workspace, session, files, choose: value => { chosen = value; } };
}
test('AGENTS editing creates only the workspace file and rejects stale revisions and symlinks', t => {
  const f = setup(t), filename = path.join(f.workspace, 'AGENTS.md');
  const original = f.files.instructions(f.session);
  assert.equal(original.revision, 'missing');
  const saved = f.files.saveInstructions(f.session, { content: 'Keep edits minimal.', revision: original.revision });
  assert.equal(saved.content, 'Keep edits minimal.');
  fs.writeFileSync(filename, 'External edit.');
  assert.throws(() => f.files.saveInstructions(f.session, { content: 'Stale edit.', revision: saved.revision }), /其他位置修改/);
  assert.equal(fs.readFileSync(filename, 'utf8'), 'External edit.');
  const outside = path.join(f.root, 'outside.md'); fs.writeFileSync(outside, 'Untouched.');
  fs.unlinkSync(filename); fs.symlinkSync(outside, filename);
  assert.throws(() => f.files.instructions(f.session), /文件不可用/);
  assert.equal(fs.readFileSync(outside, 'utf8'), 'Untouched.');
});
test('native attachment capabilities are session-bound, copied only on send and never overwrite originals', async t => {
  const f = setup(t), source = path.join(f.root, 'requirements.md');
  fs.writeFileSync(source, 'Build a local page.'); f.choose([source]);
  const { files } = await f.files.pick(f.session);
  assert.deepEqual(Object.keys(files[0]).sort(), ['id', 'name', 'size']);
  assert.equal(fs.existsSync(path.join(f.workspace, '.gumi-attachments')), false);
  assert.throws(() => f.files.input({ ...f.session, id: 'another' }, { text: 'Read', attachmentIds: [files[0].id] }), /不属于当前会话/);
  const prepared = f.files.input(f.session, { text: '', attachmentIds: [files[0].id] });
  const relative = JSON.parse(prepared.text.split('\n').find(line => line.startsWith('".gumi-attachments/')));
  assert.equal(fs.readFileSync(path.join(f.workspace, relative), 'utf8'), 'Build a local page.');
  assert.equal(fs.readFileSync(source, 'utf8'), 'Build a local page.');
  prepared.commit();
  assert.throws(() => f.files.input(f.session, { text: 'Again', attachmentIds: [files[0].id] }), /已失效/);
});
test('attachments reject cancellation, changed source, invalid IDs and symlink destinations safely', async t => {
  const f = setup(t);
  assert.deepEqual(await f.files.pick(f.session), { files: [] });
  assert.throws(() => f.files.input(f.session, { text: 'Read', attachmentIds: ['made-up'] }), /失效/);
  const source = path.join(f.root, 'input.txt'); fs.writeFileSync(source, 'Original.'); f.choose([source]);
  const first = (await f.files.pick(f.session)).files[0];
  fs.writeFileSync(source, 'Changed.');
  assert.throws(() => f.files.input(f.session, { text: 'Read', attachmentIds: [first.id] }), /选择后修改/);
  const next = (await f.files.pick(f.session)).files[0];
  const outside = path.join(f.root, 'outside'); fs.mkdirSync(outside);
  fs.symlinkSync(outside, path.join(f.workspace, '.gumi-attachments'));
  assert.throws(() => f.files.input(f.session, { text: 'Read', attachmentIds: [next.id] }), /副本无法保存/);
  assert.deepEqual(fs.readdirSync(outside), []);
});
test('credential paths and excessive files are rejected before registering attachments', async t => {
  const f = setup(t);
  const sensitivePath = path.join(f.root, '.env'); fs.writeFileSync(sensitivePath, 'synthetic only');
  f.choose([sensitivePath]); await assert.rejects(f.files.pick(f.session), /敏感/);
  f.choose(Array(9).fill(path.join(f.root, 'ordinary.txt')));
  await assert.rejects(f.files.pick(f.session), /8 个文件/);
});
