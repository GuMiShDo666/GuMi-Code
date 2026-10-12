const fs = require('node:fs');
const path = require('node:path');
const { getGumCodeRuntime, pythonEnvironment } = require('./gum-code-runtime.cjs');
const { randomUUID } = require('node:crypto');
const { spawn } = require('node:child_process');
const { createMemoryStore, hash, sensitive } = require('./gum-code-memory.cjs');
const { createWorkspaceFiles } = require('./gum-code-files.cjs');

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const MAX_LINE = 1024 * 1024;
const MAX_OUTPUT = 8 * MAX_LINE;
const MAX_CATALOG = 16 * 1024 * 1024;
const text = (value, limit = 200000) => typeof value === 'string' && value.length <= limit;
const validPending = p => p && UUID.test(p.id) && text(p.text, 16000) && p.text.trim();
const validSession = s => s && UUID.test(s.id) && ((text(s.projectId, 200) && s.projectId) || UUID.test(s.workspaceId)) && (s.workspaceId === undefined || UUID.test(s.workspaceId)) && text(s.workspace, 4096) && path.isAbsolute(s.workspace) && text(s.title, 100) && Number.isSafeInteger(s.updatedAt) && (s.pendingText === undefined || text(s.pendingText, 16000)) && (s.pendingInputs === undefined || (Array.isArray(s.pendingInputs) && s.pendingInputs.length <= 8 && s.pendingInputs.every(validPending) && new Set(s.pendingInputs.map(p => p.id)).size === s.pendingInputs.length)) && (s.providerId === undefined || s.providerId === 'workbench' || UUID.test(s.providerId)) && (s.titleSummarized === undefined || typeof s.titleSummarized === 'boolean');

function createGumCode({ dataDir, projects, allowed, pickDirectory, grantDirectory, askTrust, getProvider, pickFiles = async () => [], summarizeTitle, getModels = async () => [{ id: 'workbench' }], publish = () => {}, spawnChild = spawn, runtime = getGumCodeRuntime() }) {
  const catalogPath = path.join(dataDir, 'catalog.json');
  let catalog = { version: 1, sessions: [], selectedId: null, trusted: [], workspaces: [] };
  let initError = null;
  try {
    fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
    if (fs.lstatSync(dataDir).isSymbolicLink()) throw new Error();
    fs.chmodSync(dataDir, 0o700);
    if (fs.existsSync(catalogPath)) {
      if (fs.lstatSync(catalogPath).isSymbolicLink() || fs.statSync(catalogPath).size > MAX_CATALOG) throw new Error();
      const saved = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));
      if (saved.version !== 1 || !Array.isArray(saved.sessions) || saved.sessions.length > 200 || !saved.sessions.every(validSession) || new Set(saved.sessions.map(s => s.id)).size !== saved.sessions.length || !Array.isArray(saved.trusted) || !saved.trusted.every(p => text(p, 4096) && path.isAbsolute(p)) || !(saved.selectedId === null || saved.sessions.some(s => s.id === saved.selectedId))) throw new Error();
      const workspaces = saved.workspaces || [];
      if (!Array.isArray(workspaces) || workspaces.length > 200 || !workspaces.every(w => w && UUID.test(w.id) && text(w.name, 4096) && w.name && text(w.path, 4096) && path.isAbsolute(w.path)) || new Set(workspaces.map(w => w.id)).size !== workspaces.length || new Set(workspaces.map(w => w.path)).size !== workspaces.length) throw new Error();
      for (const session of saved.sessions) {
        if (session.workspaceId && !workspaces.some(w => w.id === session.workspaceId && w.path === session.workspace)) throw new Error();
        if (!workspaces.some(w => w.path === session.workspace)) workspaces.push({ id: randomUUID(), name: path.basename(session.workspace) || session.workspace, path: session.workspace });
      }
      if (workspaces.length > 200) throw new Error();
      catalog = { ...saved, workspaces };
    }
  } catch { initError = 'GuMi-Code 会话索引无法读取，原数据已保留。请检查应用数据目录。'; }
  let state = { progress: [], plan: [], turnStartedAt: null, turnEndedAt: null, workspaces: catalog.workspaces, sessions: catalog.sessions, selectedId: catalog.selectedId, messages: [], status: 'idle', statusText: '', approval: null, error: initError };
  let active = null, busy = false, hydrated = false, closing = false;
  const idleWaiters = [];
  const releaseBusy = () => { busy = false; idleWaiters.splice(0).forEach(resolve => resolve()); };
  const clearProgress = () => { state.progress = []; state.plan = []; state.turnStartedAt = null; state.turnEndedAt = null; };
  const snapshot = () => structuredClone(state);
  const notify = () => publish(snapshot());
  const fail = message => { const error = new Error(message); error.gumCodeSafe = true; throw error; };
  const idle = () => { if (initError) fail(initError); if (closing) fail('GuMi-Code 正在关闭，本次请求未启动。'); if (busy || active) fail('请先等待当前操作结束。'); };
  const sessionFile = id => path.join(dataDir, `${id}.jsonl`);
  function save(next) {
    const tmp = path.join(dataDir, `${randomUUID()}.tmp`);
    const body = JSON.stringify(next);
    if (Buffer.byteLength(body) > MAX_CATALOG) fail('会话索引容量已满，本次输入未提交，原记录已保留。');
    try {
      if (fs.lstatSync(dataDir).isSymbolicLink() || (fs.existsSync(catalogPath) && fs.lstatSync(catalogPath).isSymbolicLink())) throw new Error();
      fs.writeFileSync(tmp, body, { mode: 0o600, flag: 'wx' });
      fs.renameSync(tmp, catalogPath);
    } catch { fail('会话索引保存失败，原记录已保留。'); }
    finally { try { fs.unlinkSync(tmp); } catch {} }
    catalog = next; state.workspaces = next.workspaces; state.sessions = next.sessions; state.selectedId = next.selectedId;
  }
  function registered(projectId) {
    if (!text(projectId, 200)) fail('项目无效。');
    const project = projects().find(p => p.id === projectId && !p.archived);
    if (!project || !project.rootPath || !allowed(project.rootPath)) fail('请先在项目设置中选择并登记项目目录。');
    let workspace;
    try {
      workspace = fs.realpathSync(project.rootPath);
      if (!fs.statSync(workspace).isDirectory()) throw new Error();
    } catch { fail('项目目录不可用，请重新选择。'); }
    return { project, workspace };
  }
  function workspaceById(id) {
    const entry = catalog.workspaces.find(w => w.id === id);
    if (!entry || !allowed(entry.path)) fail('工作区域尚未授权，请重新选择文件夹。');
    try {
      if (fs.realpathSync(entry.path) !== entry.path || !fs.statSync(entry.path).isDirectory()) throw new Error();
    } catch { fail('工作区域不可用或目录已改变，请重新选择。'); }
    return entry;
  }
  function sessionWorkspace(session) {
    const workspace = session.projectId ? registered(session.projectId).workspace : workspaceById(session.workspaceId).path;
    if (workspace !== session.workspace) fail('项目目录已改变，请新建会话，原会话已保留。');
    return workspace;
  }
  const workspaceFiles = createWorkspaceFiles({ workspace: sessionWorkspace, pickFiles });
  const titles = new Map();
  function summarize(session, source, provider) {
    if (!summarizeTitle || session.titleSummarized || !source?.trim() || sensitive(source) || titles.has(session.id) || closing) return;
    const controller = new AbortController();
    const promise = Promise.resolve().then(async () => {
      try {
        const title = (await summarizeTitle(source.slice(0, 4000), session.providerId || 'workbench', controller.signal, provider)).trim().replace(/^["“「]|["”」]$/g, '');
        if (!title || title.length > 48 || /[\r\n\x00-\x1f]/.test(title) || sensitive(title) || closing || controller.signal.aborted) return;
        const current = catalog.sessions.find(s => s.id === session.id);
        if (!current || current.titleSummarized || current.title !== session.title) return;
        save({ ...catalog, sessions: catalog.sessions.map(s => s.id === session.id ? { ...s, title, titleSummarized: true } : s) }); notify();
      } catch { /* A failed title request must never interrupt or erase the conversation. */ }
      finally { titles.delete(session.id); }
    });
    titles.set(session.id, { controller, promise });
  }
  async function addSession(request, binding, workspaces = catalog.workspaces) {
    if (catalog.sessions.length >= 200) fail('当前最多保留 200 个工作台会话。');
    const models = await getModels();
    const requestedId = request?.providerId || 'workbench';
    const providerId = requestedId === 'workbench' && models.length === 1 ? models[0].id : requestedId;
    if (!models.some(model => model.id === providerId)) fail('模型配置不存在，请重新选择。');
    if (closing) fail('GuMi-Code 正在关闭，本次请求未启动。');
    const entry = { id: randomUUID(), ...binding, providerId, title: '新会话', updatedAt: Date.now() };
    save({ ...catalog, workspaces, sessions: [entry, ...catalog.sessions], selectedId: entry.id });
    state.messages = []; state.error = null; state.statusText = ''; clearProgress(); hydrated = true; notify();
    return snapshot();
  }
  function selected(id) {
    if (!UUID.test(id)) fail('会话无效。');
    const found = catalog.sessions.find(s => s.id === id);
    if (!found) fail('会话不存在。');
    return found;
  }
  function checkRuntime() {
    try {
      fs.accessSync(runtime.python, fs.constants.X_OK);
      if (!fs.statSync(path.join(runtime.source, 'gum_code/desktop.py')).isFile()) throw new Error();
    } catch { fail('GuMi-Code 桌面运行环境不可用，请检查 Python 环境与接入源码。'); }
  }
  let memory, memoryError=null;
  try { memory=createMemoryStore(dataDir); } catch { memoryError='本地记忆库无法打开，原文件已保留。'; }
  const learning = new Map(), sourceTexts = new Map(), auxiliaryChildren=new Set();
  const memoryScope = session => { const root=sessionWorkspace(session); const w=catalog.workspaces.find(w=>w.path===root); if(!w) fail('工作区不存在。'); return w.id; };
  function memoryState(request) {
    const session=selected(request?.sessionId), scope=memoryScope(session);
    return memory ? memory.state(scope) : {enabled:false,revision:0,items:[],jobs:[],error:memoryError};
  }
  function auxiliary(action, session, fields={}) {
    checkRuntime();
    return new Promise((resolve,reject)=>{
      const turnId=randomUUID(), child=spawnChild(runtime.python,['-u','-m','gum_code.desktop'],{cwd:dataDir,env:pythonEnvironment(runtime.source),stdio:['pipe','pipe','pipe'],shell:false});
      auxiliaryChildren.add(child);
      let buffer='',bytes=0,result=null,terminal=false,seq=-1,ended=false;
      const finish=(error)=>{ if(ended)return; ended=true; auxiliaryChildren.delete(child); clearTimeout(timer); if(error){child.kill('SIGKILL');reject(new Error('记忆学习或来源读取未完成，可重试。'));}else resolve(result); };
      const timer=setTimeout(()=>finish(true),45000);
      child.stdout.on('data',chunk=>{ bytes+=chunk.length;buffer+=chunk.toString();if(bytes>MAX_LINE)return finish(true); let i;while((i=buffer.indexOf('\n'))>=0){const line=buffer.slice(0,i);buffer=buffer.slice(i+1);try{const m=JSON.parse(line);if(m.v!==1||m.sessionId!==session.id||m.turnId!==turnId||!Number.isSafeInteger(m.seq)||m.seq<=seq||terminal)throw Error();seq=m.seq;if(m.type===action && !result)result=m;else if(m.type==='finished')terminal=m.status==='completed';else throw Error();}catch{return finish(true);}}});
      child.stderr.on('data',chunk=>{bytes+=chunk.length;if(bytes>MAX_LINE)finish(true);});
      child.on('error',()=>finish(true)); child.stdin.on('error',()=>finish(true));
      child.once('close',code=>finish(code!==0||!terminal||!result||!!buffer));
      child.once('spawn',()=>child.stdin.end(JSON.stringify({v:1,action,sessionId:session.id,turnId,workspace:session.workspace,sessionPath:sessionFile(session.id),...fields})+'\n'));
    });
  }
  function admitMemory(session,turnId,inputId,textValue){
    if(!memory)return null;
    try{const source=memory.admit(memoryScope(session),session.id,turnId,inputId,textValue);if(source){if(sourceTexts.size>=200)sourceTexts.delete(sourceTexts.keys().next().value);sourceTexts.set(source.id,textValue);}return source;}catch{return null;}
  }
  async function learnMemory(session,sourceId,provider,proof) {
    if(!memory||learning.has(sourceId))return;
    if(proof){try{memory.saveProof(sourceId,proof);}catch{}}
    if(learning.size>=2){memory.job(sourceId,'failed','学习队列已满，请稍后重试。');notify();return;}
    const task=Promise.resolve().then(async()=>{
      try{
        const src=memory.source(sourceId),scope=memoryScope(session);
        if(!src||src.scope!==scope||!memory.settings().enabled||src.epoch!==memory.settings().epoch)return;
        if(memory.state(scope).jobs.find(j=>j.sourceId===sourceId)?.status==='completed' && (!proof || src.experiencesProcessed))return;
        if(proof)memory.saveProof(sourceId,proof);
        if(!proof && src.proof){
          const saved=JSON.parse(src.proof),found=await auxiliary('memory_source',session,{digest:src.digest,toolIds:saved.toolIds,evidenceRefs:saved.observations});
          const evidence=found.evidence||[];
          if(saved.observations.every(x=>evidence.some(e=>e.toolCallId===x.id&&hash(e.content)===x.digest))){
            proof={verified:saved.verified,toolIds:saved.toolIds,observation:JSON.stringify(saved.observations.map(x=>({id:x.id,tool:x.tool,result:evidence.find(e=>e.toolCallId===x.id).content})))};
          }
        }
        let textValue=sourceTexts.get(sourceId);
        if(!textValue){const found=await auxiliary('memory_source',session,{digest:src.digest});textValue=found.messages?.find(m=>m.role==='user'&&hash(m.content)===src.digest)?.content;}
        if(!textValue)throw Error();
        memory.job(sourceId,'running');notify();
        const existing=memory.search(scope,textValue).items.map(({id,revision,factKey,body,scope})=>({id,revision,factKey,body,scope}));
        const config=provider || await getProvider(session.providerId||'workbench');
        if(closing||!memory.settings().enabled||src.epoch!==memory.settings().epoch)return;
        const result=await auxiliary('memory_learn',session,{provider:config,text:textValue,existing,proof});
        if(JSON.stringify(result.proposals).includes(config.apiKey))throw Error();
        memory.apply(scope,sourceId,textValue,result.proposals,[],proof);sourceTexts.delete(sourceId);
      }catch(error){try{memory.job(sourceId,'failed',error.gumCodeSafe?error.message:'学习未完成；聊天已保留，可在面板重试。');}catch{}}
      finally{learning.delete(sourceId);notify();}
    });
    learning.set(sourceId,task);await task;
  }
  async function memoryAction(request){
    if(!memory)fail(memoryError);
    const session=selected(request?.sessionId),scope=memoryScope(session);
    if(request.action==='retry'){
      const job=memory.state(scope).jobs.find(j=>j.id===request.id&&j.status==='failed');if(!job)fail('学习任务不可重试。');
      void learnMemory(session,job.sourceId);return memory.state(scope);
    }
    if(request.action==='source'){
      const row=memory.item(scope,request.id),src=row.source;
      let sourceSession;
      try { sourceSession=selected(src.sessionId); } catch { return {...memory.state(scope),sourceView:{sessionId:src.sessionId,turnId:src.turnId,available:false,messages:[]}}; }
      // Global memories intentionally share their quoted source, never the surrounding workspace history.
      try{const result=await auxiliary('memory_source',sourceSession,{digest:src.digest,toolIds:row.proof?.toolIds || [],evidenceRefs:row.proof?.observations || []});if(memory.item(scope,request.id).revision!==row.revision)throw Error();return {...memory.state(scope),sourceView:{sessionId:src.sessionId,turnId:src.turnId,available:!!result.messages?.length,messages:result.messages||[],evidence:result.evidence||[]}};}catch{return {...memory.state(scope),sourceView:{sessionId:src.sessionId,turnId:src.turnId,available:false,messages:[]}};}
    }
    const result=memory.action(scope,request);
    if(request.action==='enable'||request.action==='delete'||request.action==='reject')sourceTexts.clear();
    if(request.action==='enable'&&!request.enabled)for(const child of auxiliaryChildren)child.kill('SIGINT');
    notify();return result;
  }
  let skillsPromise = null;
  function skills() {
    if (skillsPromise) return skillsPromise;
    skillsPromise = readSkills().finally(() => { skillsPromise = null; });
    return skillsPromise;
  }
  async function readSkills() {
    const unavailable = () => ({ skills: [], error: 'Skills 暂不可用，请检查 GuMi-Code 运行环境与 Skill 目录。' });
    try { checkRuntime(); if (closing) return unavailable(); } catch { return unavailable(); }
    return new Promise(resolve => {
      let child;
      try {
        child = spawnChild(runtime.python, ['-u', '-m', 'gum_code.desktop'], {
          cwd: dataDir, env: pythonEnvironment(runtime.source),
          stdio: ['pipe', 'pipe', 'pipe'], shell: false,
        });
      } catch { resolve(unavailable()); return; }
      let output = Buffer.alloc(0), done = false;
      const finish = result => { if (!done) { done = true; clearTimeout(timer); resolve(result); } };
      const abort = () => { child.kill('SIGKILL'); finish(unavailable()); };
      const timer = setTimeout(abort, 10000);
      child.stdout.on('data', chunk => {
        if (done) return;
        if (output.length + chunk.length > MAX_LINE) return abort();
        output = Buffer.concat([output, chunk]);
      });
      child.stderr.on('data', () => {});
      child.on('error', abort);
      child.stdin.on('error', abort);
      child.on('close', code => {
        if (done) return;
        try {
          const messages = output.toString('utf8').trim().split('\n').map(line => JSON.parse(line));
          const [catalog, terminal] = messages;
          if (code !== 0 || messages.length !== 2 || !messages.every((m, i) => m.v === 1 && m.sessionId === null && m.turnId === null && m.seq === i + 1)
            || catalog.type !== 'skills' || terminal.type !== 'finished' || terminal.status !== 'completed'
            || !Array.isArray(catalog.skills) || catalog.skills.length > 200
            || !catalog.skills.every(s => s && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(s.name) && text(s.displayName, 120) && text(s.description, 500))
            || new Set(catalog.skills.map(s => s.name)).size !== catalog.skills.length) throw new Error();
          finish({ skills: catalog.skills.map(({ name, displayName, description }) => ({ name, displayName, description })) });
        } catch { finish(unavailable()); }
      });
      child.stdin.end(JSON.stringify({ v: 1, action: 'skills' }) + '\n');
    });
  }
  // ponytail: one active child for this local app; add a scheduler only if concurrent execution is required.
  function run(action, session, query, provider) {
    checkRuntime();
    const turnId = randomUUID();
    const child = spawnChild(runtime.python, ['-u', '-m', 'gum_code.desktop'], {
      cwd: dataDir, env: pythonEnvironment(runtime.source),
      stdio: ['pipe', 'pipe', 'pipe'], shell: false,
    });
    const current = { child, action, turnId, stopping: false, terminal: null, protocolError: null, lastSeq: -1, bytes: 0, line: Buffer.alloc(0), timer: null, answer: false, history: null, streamId: null, inputIds: new Set(), spawned: false, inputMessages: [] };
    current.memorySources=[];current.pendingMemory=new Map();
    if(action==='turn'){const src=admitMemory(session,turnId,null,query);if(src)current.memorySources.push(src.id);}
    active = current;
    let resolveDone;
    current.done = new Promise(resolve => { resolveDone = resolve; });
    const rejectProtocol = () => {
      if (current.protocolError) return;
      current.protocolError = 'GuMi-Code 返回了无效或过大的响应，本次运行已停止。';
      state.error = current.protocolError;
      child.kill('SIGINT');
      current.timer = setTimeout(() => child.kill('SIGKILL'), 10000);
    };
    function accept(msg) {
      if (!msg || msg.v !== 1 || msg.sessionId !== session.id || msg.turnId !== turnId || !Number.isSafeInteger(msg.seq) || msg.seq <= current.lastSeq || current.terminal) return rejectProtocol();
      current.lastSeq = msg.seq;
      switch (msg.type) {
        case 'memory_evidence':
          if(action!=='turn'||typeof msg.verified!=='boolean'||!text(msg.observation,12000)||!Array.isArray(msg.toolIds)||msg.toolIds.length>20||!msg.toolIds.every(x=>text(x,200)))return rejectProtocol();
          if(sensitive(msg.observation))break;
          current.memoryProof={verified:msg.verified,observation:msg.observation,toolIds:msg.toolIds};break;
        case 'memory_request': {
          if(action!=='turn'||!UUID.test(msg.requestId)||!msg.request||typeof msg.request!=='object')return rejectProtocol();
          let result;
          try {
            if(!memory)throw Error();
            const scope=memoryScope(session),r=msg.request;
            if(r.action==='search')result=memory.search(scope,r.query,r.cursor);
            else if(r.action==='refresh')result={enabled:!!memory.settings().enabled,enableEpoch:memory.settings().epoch,revision:memory.settings().revision,items:(Array.isArray(r.ids)?r.ids.slice(0,30):[]).flatMap(id=>{try{const x=memory.item(scope,id);return x.status==='enabled'?[x]:[];}catch{return [];}})};
            else if(r.action==='invalidate_skills'){if(!Array.isArray(r.ids)||!r.ids.every(id=>UUID.test(id)))throw Error();memory.invalidateSkills(scope,r.ids);result={ok:true};}
            else if(r.action==='write'){
              if(r.proposal?.kind==='experience'){child.stdin.write(JSON.stringify({v:1,type:'memory_response',requestId:msg.requestId,result:{deferred:true,message:'经验会在本轮结束后结合工具证据生成候选，请在 Memory 面板审核。'}})+'\n');return;}
              const sourceId=current.memorySources.find(id=>{const textValue=sourceTexts.get(id);return typeof r.evidence==='string'&&textValue?.includes(r.evidence);});
              if(!sourceId)throw Error();
              result={saved:memory.apply(scope,sourceId,sourceTexts.get(sourceId),[r.proposal],Array.isArray(r.skillNames)?r.skillNames.filter(x=>text(x,128)):[],null,{partial:true})};
            }else throw Error();
          }catch(error){result={error:error.gumCodeSafe?error.message:'记忆不可用、已关闭或引用已失效。'};}
          child.stdin.write(JSON.stringify({v:1,type:'memory_response',requestId:msg.requestId,result})+'\n');
          break;
        }
        case 'status':
          if (!text(msg.text, 4000)) return rejectProtocol();
          if (!current.stopping && state.status !== 'waiting') state.statusText = msg.text;
          break;
        case 'progress': {
          if (action !== 'turn' || !text(msg.id, 200) || !msg.id || !text(msg.label, 120) || !text(msg.detail, 500) || !['running', 'completed', 'failed'].includes(msg.status)) return rejectProtocol();
          const entry = { id: msg.id, label: msg.label, detail: msg.detail, status: msg.status };
          const index = state.progress.findIndex(p => p.id === entry.id);
          if (index < 0) state.progress = [...state.progress, entry].slice(-60);
          else state.progress = state.progress.map((p, i) => i === index ? entry : p);
          if (!current.stopping && state.status !== 'waiting') state.statusText = `${entry.label}${entry.detail ? ' · ' + entry.detail : ''}`;
          break;
        }
        case 'plan':
          if (action !== 'turn' || !Array.isArray(msg.items) || msg.items.length > 20 || !msg.items.every(p => p && text(p.id, 200) && p.id && text(p.text, 200) && ['pending', 'running', 'completed'].includes(p.status)) || new Set(msg.items.map(p => p.id)).size !== msg.items.length) return rejectProtocol();
          state.plan = msg.items.map(({ id, text, status }) => ({ id, text, status }));
          break;
        case 'stream': {
          if (action !== 'turn' || !UUID.test(msg.id) || !text(msg.text, 100100) || typeof msg.done !== 'boolean' || typeof msg.replace !== 'boolean' || current.answer) return rejectProtocol();
          let entry = state.messages.find(m => m.streamId === msg.id);
          if (!entry) {
            if (state.messages.filter(m => m.streamId).length >= 200) return rejectProtocol();
            entry = { role: 'assistant', content: '', streamId: msg.id }; state.messages.push(entry);
          } else if (entry.streaming === false) return rejectProtocol();
          const content = msg.replace ? msg.text : entry.content + msg.text;
          if (!text(content, 100100)) return rejectProtocol();
          entry.content = content; entry.streaming = !msg.done;
          current.streamId = msg.id;
          if (!current.stopping && state.status !== 'waiting' && msg.text) state.statusText = '正在输出…';
          break;
        }
        case 'input_received':
        case 'input_rejected': {
          if (action !== 'turn' || !UUID.test(msg.id) || !current.inputIds.has(msg.id)) return rejectProtocol();
          const entry = state.messages.find(m => m.inputId === msg.id);
          if (msg.type === 'input_received') {
            save({ ...catalog, sessions: catalog.sessions.map(s => s.id === session.id ? { ...s, pendingInputs: (s.pendingInputs || []).filter(p => p.id !== msg.id) } : s) });
            if (entry) { entry.delivery = 'received'; const id=current.pendingMemory.get(msg.id);if(id)current.memorySources.push(id);current.pendingMemory.delete(msg.id); }
          } else {
            if (!text(msg.message, 500)) return rejectProtocol();
            if (entry) entry.delivery = 'pending';
            state.error = msg.message;
          }
          current.inputIds.delete(msg.id);
          break;
        }
        case 'answer':
          if (action !== 'turn' || current.answer || !text(msg.text)) return rejectProtocol();
          current.answer = true;
          { const entry = state.messages.find(m => m.streamId === current.streamId);
            if (entry) { entry.content = msg.text; entry.streaming = false; }
            else state.messages.push({ role: 'assistant', content: msg.text }); }
          break;
        case 'approval':
          if (action !== 'turn' || !text(msg.requestId, 200) || !msg.requestId || !text(msg.title, 500) || !text(msg.detail, 12000) || state.approval) return rejectProtocol();
          if (current.stopping) {
            child.stdin.write(JSON.stringify({ v: 1, type: 'approval_response', requestId: msg.requestId, approved: false }) + '\n');
          } else {
            state.approval = { requestId: msg.requestId, title: msg.title, detail: msg.detail };
            state.status = 'waiting'; state.statusText = '等待你的确认';
          }
          break;
        case 'history':
          if (action !== 'history' || current.history || !Array.isArray(msg.messages) || msg.messages.length > 2000 || !msg.messages.every(m => m && ['user', 'assistant'].includes(m.role) && text(m.content) && (m.inputId === undefined || UUID.test(m.inputId)))) return rejectProtocol();
          current.history = msg.messages.map(({ role, content, inputId }) => ({ role, content, ...(inputId ? { inputId, delivery: 'received' } : {}) }));
          break;
        case 'error':
          if (!text(msg.message, 4000)) return rejectProtocol();
          state.error = msg.message;
          break;
        case 'finished':
          if (!['completed', 'incomplete', 'interrupted', 'failed'].includes(msg.status)) return rejectProtocol();
          current.terminal = msg.status;
          break;
        default: return rejectProtocol();
      }
      notify();
    }
    child.stdout.on('data', chunk => {
      if (current.protocolError) return;
      current.bytes += chunk.length;
      if (current.bytes > MAX_OUTPUT) return rejectProtocol();
      current.line = Buffer.concat([current.line, chunk]);
      let newline;
      while ((newline = current.line.indexOf(10)) !== -1) {
        if (newline > MAX_LINE) return rejectProtocol();
        const line = current.line.subarray(0, newline); current.line = current.line.subarray(newline + 1);
        try { accept(JSON.parse(line.toString('utf8'))); } catch { return rejectProtocol(); }
        if (current.protocolError) return;
      }
      if (current.line.length > MAX_LINE) rejectProtocol();
    });
    // Never forward Python diagnostics or raw subprocess errors into renderer/logs.
    child.stderr.on('data', chunk => { current.bytes += chunk.length; if (current.bytes > MAX_OUTPUT) rejectProtocol(); });
    child.stdin.on('error', () => { if (!current.terminal && !current.stopping) state.error = 'GuMi-Code 通信已断开，请重试。'; });
    child.on('error', () => { state.error = 'GuMi-Code 无法启动，请检查本地运行环境。'; });
    child.once('close', code => {
      clearTimeout(current.timer);
      if (active !== current) return resolveDone(snapshot());
      if (current.line.length) current.protocolError = 'GuMi-Code 响应未完整结束，历史记录已保留。';
      if (current.protocolError) state.error = current.protocolError;
      const success = code === 0 && current.terminal === 'completed' && !current.protocolError && !state.error && !current.stopping;
      if (action === 'history' && success && current.history) { state.messages = current.history; state.statusText = ''; }
      else if (action === 'history' && !state.error) state.error = '会话历史读取失败，原文件已保留。';
      if (current.stopping) {
        state.statusText = current.terminal === 'interrupted' ? '本轮已停止，已发生的文件修改仍保留。' : '进程已停止；请检查本轮结果及运行资源。';
      } else if (action === 'turn') {
        if (success) {
          state.statusText = '本轮已结束';
          try {
            save({ ...catalog, sessions: catalog.sessions.map(s => { if (s.id !== session.id) return s; const { pendingText, ...saved } = s; return saved; }) });
          } catch { state.error = '本轮结果已保存，但输入恢复标记未清除，请在重新打开后核对。'; }
        }
        else if (current.terminal === 'incomplete' && !state.error) state.statusText = '本轮尚未完成，可以继续说明下一步。';
        else if (!state.error) state.error = '本轮未正常结束，已有消息和文件修改已保留。';
      }
      if (action === 'turn') {
        state.turnEndedAt = Date.now();
        state.progress = state.progress.map(p => p.status === 'running' ? { ...p, status: 'failed', detail: current.stopping ? '已中断，结果未确认' : '执行结果未确认' } : p);
      }
      state.messages = state.messages.map(m => (m.streaming || (!current.answer && m.streamId === current.streamId)) ? { ...m, streaming: false, interrupted: true } : m);
      state.status = 'idle'; state.approval = null; active = null;
      notify(); resolveDone(snapshot());
      if(action==='turn')for(const id of current.memorySources)void learnMemory(session,id,provider,current.memoryProof);
    });
    child.once('spawn', () => {
      child.stdin.write(JSON.stringify({ v: 1, action, sessionId: session.id, turnId, sessionPath: sessionFile(session.id), workspace: session.workspace, text: query, trusted: true, ...(action === 'turn' ? { provider, live: true, memory: !!memory } : {}) }) + '\n');
      current.spawned = true;
      for (const line of current.inputMessages) child.stdin.write(line);
      current.inputMessages = [];
      if (current.stopping) child.kill('SIGINT');
    });
    return current.done;
  }
  async function history(session) {
    if (!fs.existsSync(sessionFile(session.id))) state.messages = [];
    else {
      state.statusText = '正在读取会话'; state.error = null;
      await run('history', session);
    }
    const received = new Set(state.messages.filter(m => m.inputId).map(m => m.inputId));
    const remaining = (session.pendingInputs || []).filter(p => !received.has(p.id));
    if (remaining.length !== (session.pendingInputs || []).length) save({ ...catalog, sessions: catalog.sessions.map(s => s.id === session.id ? { ...s, pendingInputs: remaining } : s) });
    for (const input of remaining) state.messages.push({ role: 'user', content: input.text, inputId: input.id, delivery: 'pending' });
    if (session.pendingText) {
      if (state.messages.findLast(m => m.role === 'user')?.content !== session.pendingText) state.messages.push({ role: 'user', content: session.pendingText });
      state.statusText = '上次运行未确认完成，输入已保留；可取回后继续。';
    }
    hydrated = true;
    const first = state.messages.find(m => m.role === 'user')?.content;
    if (first && (session.title === '新会话' || session.title === first.trim().slice(0, 60))) summarize(session, first);
  }
  async function settled() {
    while (busy || active) {
      if (busy) await new Promise(resolve => idleWaiters.push(resolve));
      else await active.done;
    }
    if(learning.size)await Promise.allSettled([...learning.values()]);
    if(titles.size)await Promise.allSettled([...titles.values()].map(t => t.promise));
    return snapshot();
  }
  return {
    skills, memoryState, memoryAction,
    async state() {
      if (!hydrated && !busy && !active && state.selectedId && !initError) {
        busy = true;
        try { await history(selected(state.selectedId)); }
        catch (error) { state.error = error.message; }
        finally { releaseBusy(); }
      }
      return snapshot();
    },
    async pickWorkspace(request) {
      idle(); busy = true;
      try {
        const chosen = await pickDirectory();
        if (closing) fail('GuMi-Code 正在关闭，本次请求未启动。');
        if (!chosen) return snapshot();
        let workspace;
        try { workspace = fs.realpathSync(chosen); if (!fs.statSync(workspace).isDirectory()) throw new Error(); }
        catch { fail('工作区域不可用，请重新选择文件夹。'); }
        const existing = catalog.workspaces.find(w => w.path === workspace);
        if (!existing && catalog.workspaces.length >= 200) fail('当前最多保留 200 个工作区域。');
        // Only the main-process native picker supplies this path; renderer paths never grant access.
        grantDirectory(workspace);
        if (!allowed(workspace)) fail('工作区域授权失败，请重新选择。');
        const entry = existing || { id: randomUUID(), name: path.basename(workspace) || workspace, path: workspace };
        return await addSession(request, { workspaceId: entry.id, workspace }, existing ? catalog.workspaces : [...catalog.workspaces, entry]);
      } finally { releaseBusy(); }
    },
    async newSession(request) {
      idle(); busy = true;
      try {
        if (request?.workspaceId && request?.projectId) fail('请只选择一个工作区域。');
        if (request?.workspaceId) {
          const entry = workspaceById(request.workspaceId);
          return await addSession(request, { workspaceId: entry.id, workspace: entry.path });
        }
        const { project, workspace } = registered(request?.projectId);
        const workspaces = catalog.workspaces.some(w => w.path === workspace) ? catalog.workspaces : [...catalog.workspaces, { id: randomUUID(), name: path.basename(workspace) || workspace, path: workspace }];
        if (workspaces.length > 200) fail('当前最多保留 200 个工作区域。');
        return await addSession(request, { projectId: project.id, workspace }, workspaces);
      } finally { releaseBusy(); }
    },
    async select(request) {
      idle();
      const session = selected(request?.sessionId);
      busy = true;
      try {
        save({ ...catalog, selectedId: session.id });
        state.messages = []; state.error = null; state.statusText = ''; clearProgress(); hydrated = false;
        await history(session); notify(); return snapshot();
      } finally { releaseBusy(); }
    },
    instructions(request) { return workspaceFiles.instructions(selected(request?.sessionId)); },
    saveInstructions(request) { idle(); return workspaceFiles.saveInstructions(selected(request?.sessionId), request); },
    async pickAttachments(request) {
      idle(); const session = selected(request?.sessionId);
      if (state.selectedId !== session.id) fail('请先打开此会话。');
      busy = true;
      try { const result = await workspaceFiles.pick(session); if (closing) fail('GuMi-Code 正在关闭，本次请求未启动。'); return result; }
      finally { releaseBusy(); }
    },
    async send(request) {
      if (active && active.action === 'turn' && !active.stopping && !active.terminal && !closing && !busy) {
        const session = selected(request?.sessionId);
        if (state.selectedId !== session.id || !text(request?.text, 16000) || (!request.text.trim() && !request.attachmentIds?.length)) fail('请输入当前会话的有效追加需求。');
        const pending = session.pendingInputs || [];
        if (pending.length >= 8) fail('最多保留 8 条待接收需求，请等待接收后再发送。');
        const prepared = workspaceFiles.input(session, request);
        const input = { id: randomUUID(), text: prepared.text };
        try { save({ ...catalog, sessions: catalog.sessions.map(s => s.id === session.id ? { ...s, pendingInputs: [...pending, input] } : s) }); prepared.commit(); } catch (e) { prepared.rollback(); throw e; }
        state.messages.push({ role: 'user', content: input.text, inputId: input.id, delivery: 'pending' });
        active.inputIds.add(input.id);
        const memorySource=admitMemory(session,active.turnId,input.id,input.text);
        if(memorySource)active.pendingMemory.set(input.id,memorySource.id);
        const line = JSON.stringify({ v: 1, type: 'input', sessionId: session.id, turnId: active.turnId, ...input }) + '\n';
        if (active.spawned) active.child.stdin.write(line); else active.inputMessages.push(line);
        notify(); return snapshot();
      }
      idle();
      const session = selected(request?.sessionId);
      if (state.selectedId !== session.id) fail('请先打开此会话。');
      if (!text(request?.text, 16000) || (!request.text.trim() && !request.attachmentIds?.length)) fail('请输入 1–16000 字的任务内容。');
      if (request.pendingInputId !== undefined && !(session.pendingInputs || []).some(p => p.id === request.pendingInputId && p.text === request.text)) fail('待接收需求已改变，请重新打开会话。');
      const workspace = sessionWorkspace(session);
      checkRuntime();
      busy = true;
      try {
        if (!catalog.trusted.includes(workspace)) {
          if (!await askTrust(workspace)) fail('未授权 GuMi-Code 访问此项目。');
          if (closing) fail('GuMi-Code 正在关闭，本次请求未启动。');
          // The project can change while the native dialog is open.
          if (sessionWorkspace(session) !== workspace) fail('项目目录已改变，请重新选择。');
          save({ ...catalog, trusted: [...catalog.trusted, workspace] });
        }
        let provider;
        try { provider = await getProvider(session.providerId || 'workbench'); }
        catch { fail('工作台 AI 配置或钥匙串凭据不可用，请检查工作台的 API 配置。'); }
        if (closing) fail('GuMi-Code 正在关闭，本次请求未启动。');
        if (sessionWorkspace(session) !== workspace) fail('项目目录已改变，请重新选择。');
        const prepared = workspaceFiles.input(session, request);
        const updated = { ...session, pendingInputs: (session.pendingInputs || []).filter(p => p.id !== request.pendingInputId), updatedAt: Date.now(), pendingText: prepared.text };
        try { save({ ...catalog, sessions: [updated, ...catalog.sessions.filter(s => s.id !== session.id)] }); prepared.commit(); } catch (e) { prepared.rollback(); throw e; }
        clearProgress(); state.turnStartedAt = Date.now();
        state.error = null; state.status = 'running'; state.statusText = '正在启动 GuMi-Code';
        if (request.pendingInputId) state.messages = state.messages.filter(m => m.inputId !== request.pendingInputId);
        state.messages.push({ role: 'user', content: prepared.text });
        run('turn', session, prepared.text, provider);
        if (session.title === '新会话') summarize(session, request.text.trim() || '查看上传的文件', provider);
        hydrated = true; notify();
        return snapshot();
      } finally { releaseBusy(); }
    },
    async approve(request) {
      if (!active || active.stopping || !state.approval || request?.requestId !== state.approval.requestId || typeof request.approved !== 'boolean') fail('此确认请求已失效。');
      const child = active.child;
      state.approval = null; state.status = 'running'; state.statusText = request.approved ? '已批准本次操作' : '已拒绝本次操作'; notify();
      child.stdin.write(JSON.stringify({ v: 1, type: 'approval_response', requestId: request.requestId, approved: request.approved }) + '\n');
      return snapshot();
    },
    async stop() {
      if (active && !active.stopping) {
        active.stopping = true; state.approval = null; state.status = 'stopping'; state.statusText = '正在停止并清理运行资源…';
        active.child.kill('SIGINT'); notify();
        const current = active;
        current.timer = setTimeout(() => {
          state.error = 'GuMi-Code 未及时响应停止，已强制结束进程。请检查本轮文件修改及 Docker 运行资源。';
          current.child.kill('SIGKILL');
        }, 20000);
      }
      return snapshot();
    },
    registeredWorkspaces: () => initError ? [] : structuredClone(catalog.workspaces),
    closeMemory: () => { if (memory) { memory.close(); memory = null; } },
    isRunning: () => Boolean(active || busy || learning.size),
    settled,
    async shutdown() {
      closing = true;
      for (const title of titles.values()) title.controller.abort();
      try { for(const child of auxiliaryChildren)child.kill('SIGINT');await this.stop();await Promise.allSettled([...learning.values()]);return await settled(); }
      finally { closing = false; }
    },
  };
}
module.exports = { createGumCode };
