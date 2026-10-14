const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { EventEmitter } = require('node:events');
const { randomUUID } = require('node:crypto');
const { PassThrough } = require('node:stream');
const { createGumCode } = require('../electron/gum-code.cjs');

function setup(t, behavior) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gum-code-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspace = path.join(root, 'workspace'); fs.mkdirSync(workspace);
  const source = path.join(root, 'source'); fs.mkdirSync(path.join(source, 'gum_code'), { recursive: true });
  fs.writeFileSync(path.join(source, 'gum_code/desktop.py'), '');
  const fixture = { root, workspace, grants: true, trusted: true, trustCalls: 0, project: { id: 'p', rootPath: workspace }, starts: 0, children: [], publications: [] };
  function spawnChild(executable, args, options) {
    fixture.starts++;
    assert.equal(options.shell, false);
    assert.deepEqual(args, ['-u', '-m', 'gum_code.desktop']);
    const child = new EventEmitter(); child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
    let request, seq = 0, closed = false;
    const emit = msg => child.stdout.write(JSON.stringify({ v: 1, sessionId: request.sessionId, turnId: request.turnId, seq: seq++, ...msg }) + '\n');
    const close = code => { if (!closed) { closed = true; child.emit('close', code); } };
    child.kill = signal => { child.signal = signal; if (request && behavior.onKill) behavior.onKill({ child, request, emit, close, signal }); return true; };
    child.stdin.on('data', data => {
      const msg = JSON.parse(data.toString());
      if (!request) { request = msg; child.request = request; setImmediate(() => behavior.start({ child, request, emit, close })); }
      else behavior.reply?.({ msg, emit, close, child, request });
    });
    fixture.children.push(child);
    setImmediate(() => child.emit('spawn'));
    return child;
  }
  fixture.options = { dataDir: path.join(root, 'data'), projects: () => [fixture.project], allowed: () => fixture.grants, askTrust: async () => { fixture.trustCalls++; return fixture.trusted; }, getProvider: async () => ({ baseUrl: 'http://127.0.0.1/v1', model: 'synthetic-model', apiKey: 'YOUR_TOKEN_HERE' }), publish: s => fixture.publications.push(s), spawnChild, runtime: { python: process.execPath, source } };
  fixture.api = createGumCode(fixture.options);
  return fixture;
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function begin(f, content = '检查这个项目') {
  const s = await f.api.newSession({ projectId: 'p' });
  await f.api.send({ sessionId: s.selectedId, text: content });
  await tick(); await tick();
  return s.selectedId;
}

test('Gum-Code project grants/trust stay authoritative, new sessions do not call model', async t => {
  const f = setup(t, { start() {} });
  f.grants = false;
  await assert.rejects(f.api.newSession({ projectId: 'p' }), /登记/);
  f.grants = true;
  const s = await f.api.newSession({ projectId: 'p' });
  assert.equal(f.starts, 0);
  f.trusted = false;
  await assert.rejects(f.api.send({ sessionId: s.selectedId, text: 'hello' }), /未授权/);
  assert.equal(f.starts, 0);
  f.trusted = true; f.project.rootPath = f.root;
  await assert.rejects(f.api.send({ sessionId: s.selectedId, text: 'hello' }), /目录已改变/);
  assert.equal((await f.api.state()).messages.length, 0);
});

test('Gum-Code one-turn approval replies bind exact request and refuse stale/repeated decisions', async t => {
  const replies = [];
  const f = setup(t, {
    start({ emit }) { emit({ type: 'approval', requestId: 'operation-1', title: '删除文件', detail: 'synthetic.txt' }); },
    reply({ msg, emit, close }) { replies.push(msg); emit({ type: 'answer', text: '已拒绝操作，文件未删除。' }); emit({ type: 'finished', status: 'completed' }); close(0); },
  });
  const id = await begin(f);
  assert.equal((await f.api.state()).status, 'waiting');
  await assert.rejects(f.api.send({ sessionId: id, text: '' }), /有效/);
  await assert.rejects(f.api.approve({ requestId: 'wrong', approved: true }), /失效/);
  await f.api.approve({ requestId: 'operation-1', approved: false });
  await tick();
  assert.equal(replies.length, 1); assert.equal(replies[0].approved, false);
  await assert.rejects(f.api.approve({ requestId: 'operation-1', approved: true }), /失效/);
  const s = await f.api.state();
  assert.equal(s.status, 'idle'); assert.equal(s.messages.length, 2);
  assert.equal(f.trustCalls, 1);
});

test('Gum-Code cancel waits for child cleanup, interruption is never success', async t => {
  const f = setup(t, {
    start({ emit }) { emit({ type: 'status', text: '正在读取' }); },
    onKill({ emit, close }) { setImmediate(() => { emit({ type: 'finished', status: 'interrupted' }); close(130); }); },
  });
  await begin(f);
  const stopping = await f.api.stop();
  assert.equal(stopping.status, 'stopping');
  assert.equal(f.children[0].signal, 'SIGINT');
  await f.api.settled();
  const state = await f.api.state();
  assert.equal(state.status, 'idle'); assert.match(state.statusText, /已停止/);
  assert.equal(state.messages.length, 1);
});

test('Gum-Code malformed protocol and nonzero exit cannot report completed', async t => {
  const f = setup(t, {
    start({ emit, close }) { emit({ type: 'answer', text: 'partial' }); emit({ type: 'finished', status: 'completed' }); close(1); },
  });
  await begin(f);
  assert.match((await f.api.state()).error, /未正常结束/);
  const bad = setup(t, {
    start({ child }) { child.stdout.write('not json\n'); },
    onKill({ close }) { setImmediate(() => close(130)); },
  });
  await begin(bad); await tick();
  assert.match((await bad.api.state()).error, /无效或过大/);
});

test('Gum-Code durable catalog reload reads bounded history via Python, corrupt catalog never resets', async t => {
  const f = setup(t, {
    start({ request, emit, close }) {
      assert.equal(request.action, 'history');
      emit({ type: 'history', messages: [{ role: 'user', content: '之前的任务' }, { role: 'assistant', content: '之前的结果' }] });
      emit({ type: 'finished', status: 'completed' }); close(0);
    },
  });
  const initial = await f.api.newSession({ projectId: 'p' });
  fs.writeFileSync(path.join(f.options.dataDir, `${initial.selectedId}.jsonl`), 'fixture only');
  const restarted = createGumCode(f.options);
  const recovered = await restarted.state();
  assert.equal(recovered.selectedId, initial.selectedId); assert.equal(recovered.messages.length, 2);
  const catalog = path.join(f.options.dataDir, 'catalog.json'); fs.writeFileSync(catalog, '{broken');
  const broken = createGumCode(f.options);
  await assert.rejects(broken.newSession({ projectId: 'p' }), /原数据已保留/);
  assert.equal(fs.readFileSync(catalog, 'utf8'), '{broken');
});

test('Gum-Code shutdown waits for trust dialog and cannot start work after close was requested', async t => {
  const f = setup(t, { start() { assert.fail('shutdown must not launch Python'); } });
  let approveTrust;
  f.options.askTrust = () => new Promise(resolve => { approveTrust = resolve; });
  f.api = createGumCode(f.options);
  const s = await f.api.newSession({ projectId: 'p' });
  const sending = f.api.send({ sessionId: s.selectedId, text: 'not started' });
  const rejected = assert.rejects(sending, /关闭/);
  let done = false;
  const shutdown = f.api.shutdown().then(() => { done = true; });
  await tick(); assert.equal(done, false);
  approveTrust(true);
  await rejected; await shutdown;
  assert.equal(f.starts, 0); assert.equal(f.api.isRunning(), false);
});

test('Gum-Code startup failure keeps complete input recoverable after app restart', async t => {
  const f = setup(t, {
    start({ emit, close }) { emit({ type: 'error', message: '模型配置不可用' }); emit({ type: 'finished', status: 'failed' }); close(1); },
  });
  const content = '配置缺失时，也要保留这一条完整的输入，而不只是标题。'.repeat(8);
  await begin(f, content);
  const restarted = createGumCode(f.options);
  const state = await restarted.state();
  assert.equal(state.messages[0].content, content);
  assert.match(state.statusText, /输入已保留/);
  assert.equal(f.starts, 1, 'recovery must not repeat the model request');
});

test('Gum-Code catalog can round-trip multiple long pending inputs beyond a protocol line size', async t => {
  const f = setup(t, { start() { assert.fail('catalog restore should not execute'); } });
  const pendingText = '保留中文'.repeat(4000);
  const sessions = Array.from({ length: 30 }, () => ({ id: randomUUID(), projectId: 'p', workspace: f.workspace, title: '尚未完成', updatedAt: 1, pendingText }));
  const catalog = path.join(f.options.dataDir, 'catalog.json');
  fs.writeFileSync(catalog, JSON.stringify({ version: 1, sessions, selectedId: sessions[0].id, trusted: [] }));
  assert.ok(fs.statSync(catalog).size > 1024 * 1024);
  const restarted = createGumCode(f.options);
  assert.equal((await restarted.state()).messages[0].content, pendingText);
  await restarted.newSession({ projectId: 'p' });
  assert.equal((await createGumCode(f.options).state()).sessions.length, 31);
});

test('Gum-Code native workspaces persist independently and reuse canonical directory IDs', async t => {
  const f = setup(t, { start({ emit, close }) { emit({ type: 'answer', text: 'done' }); emit({ type: 'finished', status: 'completed' }); close(0); } });
  const alias = path.join(f.root, 'workspace-alias'); fs.symlinkSync(f.workspace, alias);
  const granted = [];
  f.options.pickDirectory = async () => alias;
  f.options.grantDirectory = p => { granted.push(p); f.grants = true; };
  f.grants = false; f.api = createGumCode(f.options);
  const first = await f.api.pickWorkspace();
  assert.equal(first.workspaces.length, 1);
  assert.equal(first.workspaces[0].path, fs.realpathSync(f.workspace));
  assert.equal(first.sessions[0].projectId, undefined);
  assert.equal(first.sessions[0].workspaceId, first.workspaces[0].id);
  assert.deepEqual(granted, [fs.realpathSync(f.workspace)]);
  f.project.archived = true;
  await f.api.send({ sessionId: first.selectedId, text: 'independent' });
  await tick(); await tick(); await f.api.settled();
  const second = await f.api.pickWorkspace();
  assert.equal(second.workspaces.length, 1); assert.notEqual(second.selectedId, first.selectedId);
  assert.equal(second.sessions[0].workspaceId, first.workspaces[0].id);
  const restarted = createGumCode(f.options);
  assert.deepEqual((await restarted.state()).workspaces, first.workspaces);
  f.grants = false;
  await assert.rejects(restarted.newSession({ workspaceId: first.workspaces[0].id }), /未授权/);
  await assert.rejects(restarted.send({ sessionId: second.selectedId, text: 'denied' }), /未授权/);
  f.grants = true;
  await assert.rejects(restarted.newSession({ workspaceId: f.workspace }), /未授权/);
  assert.equal(f.starts, 1);
});

test('Gum-Code picker cancellation and shutdown do not grant access or create sessions', async t => {
  const f = setup(t, { start() { assert.fail('must not run'); } });
  let grants = 0;
  f.options.grantDirectory = () => { grants++; };
  f.options.pickDirectory = async () => null;
  f.api = createGumCode(f.options);
  const original = await f.api.newSession({ projectId: 'p' });
  assert.deepEqual(await f.api.pickWorkspace(), original);
  assert.equal(grants, 0);
  let choose;
  f.options.pickDirectory = () => new Promise(resolve => { choose = resolve; });
  f.api = createGumCode(f.options);
  const picking = f.api.pickWorkspace();
  const rejected = assert.rejects(picking, /关闭/);
  const shutdown = f.api.shutdown();
  await tick(); choose(f.workspace); await rejected; await shutdown;
  assert.equal(grants, 0);
  assert.equal((await f.api.state()).sessions.length, 1);
});

test('Gum-Code directory changes while trust is open prevent independent workspace execution', async t => {
  const f = setup(t, { start() { assert.fail('must not run changed path'); } });
  f.options.pickDirectory = async () => f.workspace;
  f.options.grantDirectory = () => {};
  f.options.askTrust = async () => {
    fs.renameSync(f.workspace, f.workspace + '-old');
    fs.symlinkSync(f.root, f.workspace);
    return true;
  };
  f.api = createGumCode(f.options);
  const state = await f.api.pickWorkspace();
  await assert.rejects(f.api.send({ sessionId: state.selectedId, text: 'do not run' }), /目录已改变/);
  assert.equal(f.starts, 0);
});

test('Gum-Code publishes bounded correlated progress and plan with real turn duration', async t => {
  const f = setup(t, { start({ emit, close }) {
    emit({ type: 'plan', items: [{ id: '1', text: 'Implement data layer', status: 'running' }] });
    emit({ type: 'progress', id: 'tool-1', label: 'Read file', detail: 'store.py', status: 'running' });
    emit({ type: 'progress', id: 'tool-1', label: 'Read file', detail: 'store.py', status: 'completed' });
    emit({ type: 'progress', id: 'tool-2', label: 'Run tests', detail: 'not available', status: 'failed' });
    emit({ type: 'finished', status: 'incomplete' }); close(0);
  } });
  const id = await begin(f); await f.api.settled();
  const s = await f.api.state();
  assert.equal(s.progress.length, 2); assert.equal(s.progress[0].status, 'completed'); assert.equal(s.progress[1].status, 'failed');
  assert.equal(s.plan[0].status, 'running');
  assert.ok(s.turnStartedAt > 0 && s.turnEndedAt >= s.turnStartedAt);
  assert.ok(f.publications.some(s => s.progress[0]?.status === 'running'));
  await f.api.newSession({projectId:'p'}); assert.equal((await f.api.state()).progress.length,0);
});

test('Gum-Code rejects malformed progress rather than forwarding arbitrary child fields', async t => {
  const f = setup(t, { start({emit}) { emit({type:'progress',id:'1',label:'read',detail:'x'.repeat(501),status:'completed'}); }, onKill({close}) {setImmediate(()=>close(130));} });
  await begin(f); await f.api.settled();
  const s=await f.api.state(); assert.equal(s.progress.length,0); assert.match(s.error,/无效或过大/);
});

test('Skill catalog is read-only, bounded metadata and independent of active turn', async t => {
  const f = setup(t, { start({ request, child, close }) {
    if (request.action !== 'skills') return;
    assert.deepEqual(request, { v: 1, action: 'skills' });
    child.stdout.write(JSON.stringify({ v: 1, sessionId: null, turnId: null, seq: 1, type: 'skills', skills: [{ name: 'demo', displayName: 'Demo', description: 'Testing', directory: '/omitted' }] }) + '\n');
    child.stdout.write(JSON.stringify({ v: 1, sessionId: null, turnId: null, seq: 2, type: 'finished', status: 'completed' }) + '\n'); close(0);
  } });
  await begin(f);
  const before = await f.api.state();
  const first = f.api.skills(), second = f.api.skills();
  assert.equal(first, second);
  assert.deepEqual(await first, { skills: [{ name: 'demo', displayName: 'Demo', description: 'Testing' }] });
  assert.deepEqual(await f.api.state(), before);
  assert.equal(f.starts, 2);
});

test('Skill catalog rejects malformed metadata without changing conversation state', async t => {
  const f = setup(t, { start({ child, close }) {
    child.stdout.write(JSON.stringify({ v: 1, sessionId: null, turnId: null, seq: 1, type: 'skills', skills: [{ name: 'bad name', displayName: 'x', description: 'x' }] }) + '\n');
    child.stdout.write(JSON.stringify({ v: 1, sessionId: null, turnId: null, seq: 2, type: 'finished', status: 'completed' }) + '\n'); close(0);
  } });
  const before = await f.api.state();
  assert.match((await f.api.skills()).error, /暂不可用/);
  assert.deepEqual(await f.api.state(), before);
});


test('stream chunks update one message; final answer replaces it without duplication', async t => {
  let wire;
  const f = setup(t, { start(w) { wire = w; } });
  await begin(f);
  const streamId = randomUUID();
  wire.emit({ type: 'stream', id: streamId, text: '# Hello', replace: false, done: false });
  assert.equal((await f.api.state()).messages.at(-1).streaming, true);
  wire.emit({ type: 'stream', id: streamId, text: '\n**world**', replace: false, done: true });
  wire.emit({ type: 'answer', text: '# Hello\n**world**' });
  wire.emit({ type: 'finished', status: 'completed' }); wire.close(0);
  const s = await f.api.state();
  assert.equal(s.messages.length, 2); assert.equal(s.messages[1].content, '# Hello\n**world**');
  assert.equal(s.messages[1].streaming, false);
});

test('live inputs persist until receipt, coexist with approval and survive interruption', async t => {
  let wire; const inputs = [];
  const f = setup(t, { start(w) { wire = w; w.emit({ type: 'approval', requestId: 'op', title: '确认', detail: 'test' }); },
    reply({ msg }) { inputs.push(msg); }, onKill(w) { setImmediate(() => { w.emit({ type: 'finished', status: 'interrupted' }); w.close(130); }); } });
  const id = await begin(f);
  await f.api.send({ sessionId: id, text: '改成蓝色' });
  await f.api.send({ sessionId: id, text: '补一份说明' });
  assert.equal(inputs[0].type, 'input'); assert.equal(inputs[1].type, 'input');
  assert.equal((await f.api.state()).approval.requestId, 'op');
  wire.emit({ type: 'input_received', id: inputs[0].id });
  assert.equal((await f.api.state()).sessions[0].pendingInputs.length, 1);
  await f.api.approve({ requestId: 'op', approved: false });
  assert.equal(inputs[2].type, 'approval_response');
  await f.api.stop();
  const saved = JSON.parse(fs.readFileSync(path.join(f.root, 'data/catalog.json')));
  assert.deepEqual(saved.sessions[0].pendingInputs, [{id: inputs[1].id, text: '补一份说明'}]);
  const restored = createGumCode(f.options);
  assert.equal((await restored.state()).sessions[0].pendingInputs[0].text, '补一份说明');
});

test('eight pending inputs bound and post-stop sending do not lose drafts', async t => {
  const f = setup(t, { start() {}, onKill(w) { setImmediate(() => { w.emit({ type:'finished', status:'interrupted' }); w.close(130); }); } });
  const id = await begin(f);
  for (let n=0;n<8;n++) await f.api.send({sessionId:id,text:'addition '+n});
  await assert.rejects(f.api.send({sessionId:id,text:'overflow'}), /8/);
  assert.equal((await f.api.state()).sessions[0].pendingInputs.length,8);
  await f.api.stop();
});


test('retrying preserved input replaces its pending bubble and is saved as recoverable input', async t => {
  const f = setup(t, { start() {}, onKill(w) { setImmediate(() => { w.emit({ type:'finished', status:'interrupted' }); w.close(130); }); } });
  const id = await begin(f);
  await f.api.send({sessionId:id,text:'keep this'});
  const input=(await f.api.state()).sessions[0].pendingInputs[0];
  await f.api.stop();
  await f.api.settled();
  await f.api.send({sessionId:id,text:input.text,pendingInputId:input.id});
  await tick();
  const s=await f.api.state();
  assert.equal(s.messages.filter(m=>m.content==='keep this').length,1);
  assert.equal(s.messages.at(-1).delivery,undefined);
  assert.equal(s.sessions[0].pendingInputs.length,0);
  assert.equal(s.sessions[0].pendingText,'keep this');
  await f.api.stop();
});

test('memory RPC save is source-bound and independent of tool approval', async t => {
  const replies=[];
  const f=setup(t,{start({request,emit,close}){
    if(request.action!=='turn'){emit({type:request.action,proposals:[]});emit({type:'finished',status:'completed'});close(0);return;}
    emit({type:'memory_request',requestId:'dddddddd-dddd-4ddd-dddd-dddddddddddd',request:{action:'write',evidence:'项目数据库采用 SQLite',proposal:{body:'项目数据库采用 SQLite',evidence:'项目数据库采用 SQLite',factKey:'project.database',kind:'fact',scope:'workspace',reliable:true}}});
  },reply({msg,emit,close}){replies.push(msg);assert.equal(msg.type,'memory_response');setImmediate(()=>{emit({type:'answer',text:'已记住。'});emit({type:'finished',status:'completed'});close(0);});}});
  const state=await f.api.newSession({projectId:'p'});
  const m=f.api.memoryState({sessionId:state.selectedId});
  await f.api.memoryAction({sessionId:state.selectedId,action:'enable',enabled:true,revision:m.revision});
  await f.api.send({sessionId:state.selectedId,text:'记住：项目数据库采用 SQLite'});
  await f.api.settled();
  assert.equal(replies[0].result.saved[0].status,'enabled');
  assert.equal(f.api.memoryState({sessionId:state.selectedId}).items.length,1);
  assert.equal((await f.api.state()).error,null);
});

test('memory eligibility uses live input submission time, not acknowledgement time', async t => {
  let finish;
  const f=setup(t,{start({emit,close}){finish=()=>{emit({type:'finished',status:'completed'});close(0);};},reply({msg,emit}){if(msg.type==='input')f.ack=()=>emit({type:'input_received',id:msg.id});}});
  const id=await begin(f,'普通任务');
  await f.api.send({sessionId:id,text:'记住启用前排队的输入'});
  const m=f.api.memoryState({sessionId:id});
  await f.api.memoryAction({sessionId:id,action:'enable',enabled:true,revision:m.revision});
  f.ack();finish();await f.api.settled();
  assert.equal(f.api.memoryState({sessionId:id}).jobs.length,0);
});

test('credential-like memory observation is discarded without failing completed chat', async t => {
  const f=setup(t,{start({emit,close}){emit({type:'memory_evidence',verified:false,toolIds:['synthetic-tool'],observation:'fixture token=YOUR_TOKEN_HERE'});emit({type:'answer',text:'完成普通任务'});emit({type:'finished',status:'completed'});close(0);}});
  await begin(f);await f.api.settled();
  const state=await f.api.state();assert.equal(state.error,null);assert.equal(state.messages.at(-1).content,'完成普通任务');
});

test('early skipped memory jobs release their concurrency slot',async t=>{let finish;const f=setup(t,{start({emit,close}){finish=()=>{emit({type:'finished',status:'completed'});close(0);};}});const s=await f.api.newSession({projectId:'p'});for(let i=0;i<3;i++){let m=f.api.memoryState({sessionId:s.selectedId});await f.api.memoryAction({sessionId:s.selectedId,action:'enable',enabled:true,revision:m.revision});await f.api.send({sessionId:s.selectedId,text:`普通任务 ${i}`});await tick();await tick();m=f.api.memoryState({sessionId:s.selectedId});await f.api.memoryAction({sessionId:s.selectedId,action:'enable',enabled:false,revision:m.revision});finish();await f.api.settled();assert.equal(f.api.isRunning(),false);}});


test('GuMi-DS-only listing normalizes default and legacy aliases for project and picked sessions', async t => {
  const f = setup(t, { start() {} });
  const { createGumCodeProviders } = require('../electron/gum-code-providers.cjs');
  const providerId = randomUUID();
  const profile = { id: providerId, name: 'GuMi-DS', baseUrl: 'https://gumi-ds.example.invalid/v1', model: 'synthetic-ds' };
  fs.writeFileSync(path.join(f.options.dataDir, 'providers.json'), JSON.stringify({ version: 1, profiles: [profile] }));
  let credentialReads = 0;
  const providers = createGumCodeProviders({ dataDir: f.options.dataDir, readCredential: async () => { credentialReads++; return 'SYNTHETIC_TEST_VALUE'; } });
  f.api = createGumCode({ ...f.options, getModels: providers.list, getProvider: providers.resolve, pickDirectory: async () => f.workspace, grantDirectory() {} });
  for (const requested of [undefined, 'workbench', providerId]) {
    const project = await f.api.newSession({ projectId: 'p', providerId: requested });
    assert.equal(project.sessions.find(session => session.id === project.selectedId).providerId, providerId);
    const picked = await f.api.pickWorkspace(requested === undefined ? undefined : { providerId: requested });
    assert.equal(picked.sessions.find(session => session.id === picked.selectedId).providerId, providerId);
    const existing = await f.api.newSession({ workspaceId: picked.workspaces[0].id, providerId: requested });
    assert.equal(existing.sessions.find(session => session.id === existing.selectedId).providerId, providerId);
  }
  const before = fs.readFileSync(path.join(f.options.dataDir, 'catalog.json'), 'utf8');
  const otherId = randomUUID();
  await assert.rejects(f.api.newSession({ projectId: 'p', providerId: otherId }), /模型配置不存在/);
  await assert.rejects(f.api.pickWorkspace({ providerId: otherId }), /模型配置不存在/);
  assert.equal(fs.readFileSync(path.join(f.options.dataDir, 'catalog.json'), 'utf8'), before);
  assert.equal(credentialReads, 0);
  assert.equal(f.starts, 0);
});

test('shared API updates keep GumCode turn snapshots and apply to the next request', async t => {
  let finish;
  const f = setup(t, { start({ emit, close }) { finish = () => { emit({ type: 'finished', status: 'completed' }); close(0); }; } });
  const { createGumCodeProviders } = require('../electron/gum-code-providers.cjs');
  const { createAICompletion } = require('../electron/ai-plan.cjs');
  const keys = new Map();
  const providers = createGumCodeProviders({ dataDir: f.options.dataDir, readCredential: async id => keys.get(id), writeCredential: async (id, value) => keys.set(id, value), deleteCredential: async id => keys.delete(id) });
  const save = async (model, apiKey) => providers.saveSettings({ baseUrl: 'https://synthetic.example.invalid/v1', model, apiKey, revision: (await providers.getSettings()).revision });
  await save('model-first', 'SYNTHETIC_FIRST');
  f.api = createGumCode({ ...f.options, getModels: providers.list, getProvider: providers.resolve });
  const sid = await begin(f);
  assert.equal(f.children[0].request.provider.model, 'model-first');
  const id = (await providers.list())[0].id;
  await save('model-next', 'SYNTHETIC_NEXT');
  assert.equal((await providers.list())[0].id, id);
  assert.equal(f.children[0].request.provider.model, 'model-first');
  assert.equal(f.children[0].request.provider.apiKey, 'SYNTHETIC_FIRST');
  finish(); await f.api.settled();
  let sent;
  const complete = createAICompletion({ resolveProvider: providers.resolve, fetchImpl: async (url, request) => {
    sent = { url, request };
    return new Response(JSON.stringify({ choices: [{ finish_reason: 'stop', message: { content: 'OK' } }] }));
  } });
  assert.equal(await complete([{ role: 'user', content: 'Synthetic check' }], 32), 'OK');
  assert.equal(JSON.parse(sent.request.body).model, 'model-next');
  assert.equal(sent.request.headers.Authorization, 'Bearer SYNTHETIC_NEXT');
  await f.api.send({ sessionId: sid, text: '继续合成检查' }); await tick(); await tick();
  assert.equal(f.children[1].request.provider.model, 'model-next');
  assert.equal(f.children[1].request.provider.apiKey, 'SYNTHETIC_NEXT');
  finish(); await f.api.settled();
  assert.equal((await f.api.state()).selectedId, sid);
});

test('semantic titles summarize once, preserve history and ignore failed or late results', async t => {
  const f = setup(t, { start({ emit, close }) { emit({ type: 'answer', text: 'Original answer.' }); emit({ type: 'finished', status: 'completed' }); close(0); } });
  const calls = [];
  f.options.summarizeTitle = async (source, providerId) => { calls.push({ source, providerId }); return '开发部门工作规划助手'; };
  f.api = createGumCode(f.options);
  const input = '你是一名全栈开发工程师，请从零开发一个技术部门工作规划助手，交付可以实际运行的完整系统。';
  const id = await begin(f, input); await f.api.settled();
  const state = await f.api.state();
  assert.equal(state.sessions[0].title, '开发部门工作规划助手');
  assert.equal(state.messages[0].content, input);
  assert.equal(calls.length, 1);
  assert.ok(calls[0].source.includes(input));
  assert.ok(calls[0].source.includes('Original answer.'));
  await f.api.send({ sessionId: id, text: '继续完善' }); await f.api.settled();
  assert.equal(calls.length, 1);
  const restored = createGumCode(f.options);
  assert.equal((await restored.state()).sessions[0].title, '开发部门工作规划助手');

  const failure = setup(t, { start({ emit, close }) { emit({ type: 'answer', text: 'Works.' }); emit({ type: 'finished', status: 'completed' }); close(0); } });
  failure.options.summarizeTitle = async () => { throw Error('Synthetic failure'); };
  failure.api = createGumCode(failure.options); await begin(failure); await failure.api.settled();
  assert.equal((await failure.api.state()).error, null);
  assert.equal((await failure.api.state()).sessions[0].title, '新会话');
});

test('manual titles persist, validate before writing and survive a late summary', async t => {
  const f = setup(t, { start({ emit, close }) { emit({ type: 'answer', text: 'Original answer.' }); emit({ type: 'finished', status: 'completed' }); close(0); } });
  let finishTitle;
  f.options.summarizeTitle = () => new Promise(resolve => { finishTitle = resolve; });
  f.api = createGumCode(f.options);
  const id = await begin(f);
  await f.api.rename({ sessionId: id, title: '  用户指定标题  ' });
  finishTitle('迟到的自动标题');
  await f.api.settled();
  assert.equal((await f.api.state()).sessions[0].title, '用户指定标题');
  const file = path.join(f.options.dataDir, 'catalog.json');
  const before = fs.readFileSync(file, 'utf8');
  for (const title of ['', ' ', 'x'.repeat(101), 'hello\nworld', 'hello\u001bworld']) await assert.rejects(f.api.rename({ sessionId: id, title }));
  await assert.rejects(f.api.rename({ sessionId: randomUUID(), title: 'Wrong session' }));
  assert.equal(fs.readFileSync(file, 'utf8'), before);
  const restored = await createGumCode(f.options).state();
  assert.equal(restored.sessions[0].title, '用户指定标题');
  assert.equal((await f.api.state()).messages.find(m => m.role === 'assistant').content, 'Original answer.');
});

test('initial title waits for the first completed question and answer', async t => {
  let wire;
  const f = setup(t, { start(value) { wire = value; } });
  const sources = [];
  f.options.summarizeTitle = async source => { sources.push(source); return '首轮问答标题'; };
  f.api = createGumCode(f.options);
  await begin(f, 'First question');
  assert.deepEqual(sources, []);
  assert.equal((await f.api.state()).sessions[0].title, '新会话');
  wire.emit({ type: 'answer', text: 'First answer' });
  wire.emit({ type: 'finished', status: 'completed' }); wire.close(0);
  await f.api.settled();
  assert.equal(sources.length, 1);
  assert.match(sources[0], /First question/); assert.match(sources[0], /First answer/);
});
