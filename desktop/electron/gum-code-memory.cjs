// Main-process-only local memory. No transcript or provider configuration lives here.
const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');
const { randomUUID, createHash } = require('node:crypto');
const hash = value => createHash('sha256').update(value).digest('hex');
const clean = (s, n) => typeof s === 'string' && s.trim() && s.length <= n && !/[\x00-\x08\x0b-\x1f]/.test(s);
const sensitive = s => /(?:-----BEGIN .*PRIVATE KEY|\bsk-[\w-]{8,}|\beyJ[\w-]+\.[\w-]+\.[\w-]+|\bBearer\s+\S+|(?:password|api[_ -]?key|token|secret|cookie|authorization|密码|密钥)\s*[:=：]\s*\S+|\w+:\/\/[^\s/@]+:[^\s/@]+@)/i.test(s);
const fail = (message='记忆操作已失效或内容不符合要求，请刷新后重试。') => { const e = new Error(message); e.gumCodeSafe = true; throw e; };
const tokens = s => new Set((s.toLowerCase().normalize('NFKC').match(/[a-z0-9_]+|[\u3400-\u9fff]/g) || []).slice(0, 1000));
function createMemoryStore(dataDir) {
  fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 });
  const file = path.join(dataDir, 'memory.sqlite');
  if (fs.lstatSync(dataDir).isSymbolicLink() || (fs.existsSync(file) && fs.lstatSync(file).isSymbolicLink())) fail();
  const db = new DatabaseSync(file); fs.chmodSync(file, 0o600);
  const version = db.prepare('PRAGMA user_version').get().user_version;
  if (version !== 0 && version !== 1) { db.close(); fail(); }
  db.exec(`PRAGMA secure_delete=ON; PRAGMA journal_mode=DELETE;
    CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,epoch INTEGER NOT NULL,revision INTEGER NOT NULL);
    INSERT OR IGNORE INTO settings VALUES(1,0,0,0);
    CREATE TABLE IF NOT EXISTS sources(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,sessionId TEXT NOT NULL,turnId TEXT NOT NULL,inputId TEXT,digest TEXT NOT NULL,epoch INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,scope TEXT NOT NULL,kind TEXT NOT NULL,factKey TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,sourceId TEXT NOT NULL,sourceOrder INTEGER NOT NULL,reason TEXT NOT NULL,verified INTEGER NOT NULL DEFAULT 0,skillRef TEXT);
    CREATE TABLE IF NOT EXISTS invalidations(sourceId TEXT NOT NULL,factKey TEXT NOT NULL,scope TEXT NOT NULL,cutoff INTEGER NOT NULL,PRIMARY KEY(sourceId,factKey,scope));
    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,sourceId TEXT NOT NULL UNIQUE,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT);
    PRAGMA user_version=1;`);
  if(!db.prepare('PRAGMA table_info(memories)').all().some(c=>c.name==='skillDigest'))db.exec('ALTER TABLE memories ADD COLUMN skillDigest TEXT');
  db.prepare("UPDATE jobs SET status='failed',error='学习被中断，可重试。' WHERE status IN ('running','pending')").run();
  db.exec("CREATE UNIQUE INDEX IF NOT EXISTS source_event ON sources(sessionId,turnId,COALESCE(inputId,''))");
  if(!db.prepare('PRAGMA table_info(memories)').all().some(c=>c.name==='proof'))db.exec('ALTER TABLE memories ADD COLUMN proof TEXT');
  if(!db.prepare('PRAGMA table_info(sources)').all().some(c=>c.name==='proof'))db.exec('ALTER TABLE sources ADD COLUMN proof TEXT');
  for(const column of ['factsProcessed','experiencesProcessed'])if(!db.prepare('PRAGMA table_info(sources)').all().some(c=>c.name===column))db.exec(`ALTER TABLE sources ADD COLUMN ${column} INTEGER NOT NULL DEFAULT 0`);
  db.prepare("UPDATE sources SET factsProcessed=1 WHERE id IN (SELECT sourceId FROM jobs WHERE status='completed')").run();
  const settings = () => db.prepare('SELECT * FROM settings WHERE id=1').get();
  const bump = () => db.prepare('UPDATE settings SET revision=revision+1 WHERE id=1').run();
  const transaction = fn => { db.exec('BEGIN IMMEDIATE'); try { const v = fn(); db.exec('COMMIT'); return v; } catch(e) { db.exec('ROLLBACK'); throw e; } };
  const source = id => db.prepare('SELECT * FROM sources WHERE id=?').get(id);
  const item = (scope, id) => { const row = db.prepare('SELECT * FROM memories WHERE id=? AND (scope=? OR scope=?)').get(id, scope, 'global'); if (!row) fail(); return row; };
  const project = (r, scope) => ({ ...r, proof:r.proof?JSON.parse(r.proof):null, scope: r.scope === 'global' ? 'global' : 'workspace', verified: !!r.verified, source: source(r.sourceId) });
  function state(scope) {
    const s = settings();
    return { enabled: !!s.enabled, revision:s.revision, items:db.prepare('SELECT * FROM memories WHERE scope=? OR scope=? ORDER BY sourceOrder DESC LIMIT 1000').all(scope,'global').map(r=>project(r,scope)), jobs:db.prepare('SELECT j.* FROM jobs j JOIN sources s ON s.id=j.sourceId WHERE s.scope=? ORDER BY s.seq DESC LIMIT 50').all(scope) };
  }
  function admit(scope, sessionId, turnId, inputId, text) {
    if (!settings().enabled || !clean(text,16000) || sensitive(text) || /不要(?:记住|记录|保存)|别(?:记住|记录|保存)|(?:don't|do not)\s+(?:remember|store|save)/i.test(text)) return null;
    return transaction(() => {
      const s=settings(), id=randomUUID();
      const previous=db.prepare("SELECT * FROM sources WHERE sessionId=? AND turnId=? AND COALESCE(inputId,'')=?").get(sessionId,turnId,inputId||'');
      if(previous){if(previous.digest!==hash(text))fail();return previous.epoch===s.epoch?previous:null;}
      db.prepare('INSERT INTO sources(id,scope,sessionId,turnId,inputId,digest,epoch) VALUES(?,?,?,?,?,?,?)').run(id,scope,sessionId,turnId,inputId || null,hash(text),s.epoch);
      db.prepare("INSERT INTO jobs(id,sourceId,status) VALUES(?,?,'pending')").run(id,id);
      return source(id);
    });
  }
  function apply(scope, sourceId, text, proposals, skillNames = [], proof = null, {partial=false} = {}) {
    if (!Array.isArray(proposals) || proposals.length>10 || !clean(text,16000)) fail();
    return transaction(() => {
      const src=source(sourceId), s=settings();
      if (!src || src.scope!==scope || src.digest!==hash(text) || !s.enabled || src.epoch!==s.epoch) fail();
      if (db.prepare("SELECT 1 FROM invalidations WHERE sourceId=? OR (scope=? AND factKey='*' AND cutoff>=?)").get(src.id,scope,src.seq)) {db.prepare("UPDATE jobs SET status='cancelled',error='删除操作使旧学习任务失效；如仍需要，请发送新的记住请求。' WHERE sourceId=?").run(src.id);return [];}

      const saved=[];let invalidated=false;
      for (const p of proposals) {
        if(p?.kind==='experience'?src.experiencesProcessed:src.factsProcessed)continue;
        if (!p || !clean(p.body,2000) || sensitive(p.body) || !clean(p.factKey,160) || sensitive(p.factKey) || !['fact','experience','skill'].includes(p.kind) || !clean(p.evidence,4000) || !(text.includes(p.evidence) || (p.kind==='experience' && (proof?.observation?.includes(p.evidence) || (()=>{try{return JSON.parse(proof.observation).some(x=>typeof x.result==='string'&&x.result.includes(p.evidence));}catch{return false;}})())))) continue;
        // Evidence must be a literal new user excerpt; model paraphrases cannot establish authority.
        const scopeWanted=p.scope==='global'?'global':scope;
        const explicit=/^(?:请)?(?:帮我)?(?:全局)?(?:记住|记下|牢记)|^(?:please\s+)?(?:globally\s+)?remember\b/i.test(text.trim());
        const globalExplicit=explicit && /^(?:请)?(?:帮我)?全局(?:记住|记下|牢记)|^(?:请)?(?:帮我)?(?:记住|记下|牢记)(?:并|，)?(?:全局共享|到全局|为全局记忆)|^(?:please\s+)?(?:remember\s+globally|globally\s+remember|remember\s+(?:this\s+)?(?:for|across|in)\s+all\s+(?:workspaces|projects))/i.test(text.trim()) && !/(?:不|别|禁止|勿|不要).{0,12}(?:全局|共享|所有)|(?:not|never|don't|do not).{0,20}(?:global|share|all workspaces|all projects)|如果|假如|假设|\bif\b|suppose/i.test(text);
        if (scopeWanted==='global' && !globalExplicit) continue;
        if(p.kind!=='experience')p.body=p.evidence.trim();
        const factKey=p.factKey.trim().toLowerCase();
        if (db.prepare("SELECT 1 FROM invalidations WHERE scope=? AND (sourceId=? OR ((factKey=? OR factKey='*') AND cutoff>=?))").get(scopeWanted,src.id,factKey,src.seq)) {invalidated=true;continue;}
        if(db.prepare('SELECT 1 FROM memories WHERE sourceId=? AND body=? AND scope=?').get(src.id,p.body,scopeWanted))continue;
        const existing=p.targetId ? db.prepare('SELECT * FROM memories WHERE id=? AND scope=?').get(p.targetId,scopeWanted) : null;
        const old=existing || db.prepare('SELECT * FROM memories WHERE scope=? AND factKey=? ORDER BY sourceOrder DESC LIMIT 1').get(scopeWanted,factKey);
        if (old && old.sourceOrder>=src.seq) continue;
        const reliable=p.reliable===true && p.body===p.evidence.trim() && !/[“”"「」]|(?:如果|假如|假设|引用|据说|例如|比如|测试一下|临时|if\b|suppose|hypothetic|quote|for example|temporar)/i.test(text);
        const canUpdate=old && old.status==='enabled' && old.kind==='fact' && p.kind==='fact' && p.targetId===old.id && Number.isSafeInteger(p.baseRevision) && p.baseRevision>=1 && p.baseRevision<=old.revision && old.factKey===factKey && reliable;
        const skillRef=p.kind==='skill' && skillNames.includes(p.skillRef)?p.skillRef:null;
        if (p.kind==='skill' && !skillRef) continue;
        const status=p.kind==='experience'?'candidate':explicit?'enabled':'candidate';
        const verified=p.kind==='experience' && proof?.verified===true && Array.isArray(proof.toolIds) && proof.toolIds.length>0;
        if (canUpdate) {
          db.prepare('UPDATE memories SET body=?,revision=revision+1,sourceId=?,sourceOrder=?,reason=? WHERE id=?').run(p.body,src.id,src.seq,'已按较新的用户事实自动更新',old.id); saved.push({id:old.id,status:'enabled',updated:true});
        } else {
          if(db.prepare('SELECT COUNT(*) n FROM memories').get().n>=1000)fail('记忆容量已满（1000 条），请先在面板删除不需要的记忆。');
          const id=randomUUID();
          db.prepare('INSERT INTO memories(id,scope,kind,factKey,body,status,revision,sourceId,sourceOrder,reason,skillRef,verified) VALUES(?,?,?,?,?,?,1,?,?,?,?,?)').run(id,scopeWanted,p.kind,factKey,p.body,status,src.id,src.seq,p.kind==='experience'?(verified?'当前回合有工具复测证据；根因仍需人工审核':'经验推断，尚未经工具复测验证'):status==='enabled'?'用户明确要求记住':'待审核的新记忆',skillRef,+verified);
          if(verified)db.prepare('UPDATE memories SET proof=? WHERE id=?').run(JSON.stringify({toolIds:proof.toolIds,observationDigest:hash(proof.observation),observations:(()=>{try{return JSON.parse(proof.observation).map(x=>({id:x.id,tool:x.tool,digest:hash(x.result)}));}catch{return [];}})()}),id);
          if(skillRef&&/^[a-f0-9]{64}$/.test(p.skillDigest||''))db.prepare('UPDATE memories SET skillDigest=? WHERE id=?').run(p.skillDigest,id);
          saved.push({id,status});
        }
      }
      db.prepare('UPDATE sources SET factsProcessed=MAX(factsProcessed,?),experiencesProcessed=MAX(experiencesProcessed,?) WHERE id=?').run(partial?0:1,proof?1:0,src.id);
      if(invalidated&&!saved.length)db.prepare("UPDATE jobs SET status='cancelled',error='删除操作使旧学习任务失效；如仍需要，请发送新的记住请求。' WHERE sourceId=?").run(src.id);
      else if(!partial)db.prepare("UPDATE jobs SET status='completed',error=NULL WHERE sourceId=?").run(src.id); bump(); return saved;
    });
  }
  function action(scope, r) {
    return transaction(()=>{
      if(r.action==='enable') { if(typeof r.enabled!=='boolean'||r.revision!==settings().revision) fail(); db.prepare('UPDATE settings SET enabled=?,epoch=epoch+1 WHERE id=1').run(+r.enabled); db.prepare("UPDATE jobs SET status='cancelled',error=NULL WHERE status IN ('pending','running','failed')").run(); }
      else {
        const row=item(scope,r.id); if(r.revision!==row.revision) fail();
        if(['delete','reject'].includes(r.action)) {
          const cutoff=db.prepare('SELECT COALESCE(MAX(seq),0) n FROM sources').get().n;
          db.prepare('INSERT OR REPLACE INTO invalidations VALUES(?,?,?,?)').run(row.sourceId,row.factKey,row.scope,cutoff);
          db.prepare('INSERT OR REPLACE INTO invalidations VALUES(?,?,?,?)').run(row.sourceId,'*',scope,cutoff);
          if(row.scope==='global')db.prepare('INSERT OR REPLACE INTO invalidations VALUES(?,?,?,?)').run(row.sourceId,'*','global',cutoff);
          db.prepare("UPDATE jobs SET status='cancelled',error='删除操作使旧学习任务失效；如仍需要，请发送新的记住请求。' WHERE sourceId IN (SELECT id FROM sources WHERE scope=? AND seq<=?) AND status IN ('pending','running','failed')").run(scope,cutoff);
          db.prepare('DELETE FROM memories WHERE id=? OR (scope=? AND factKey=? AND sourceOrder<=?)').run(row.id,row.scope,row.factKey,cutoff);
        } else if(['approve','edit'].includes(r.action)) {
          if(r.action==='approve' && row.kind==='experience' && !row.verified) fail();
          const body=r.body===undefined?row.body:r.body;
          if(!clean(body,2000)||sensitive(body)||!['workspace','global',undefined].includes(r.scope)) fail();
          if (row.kind==='experience' && body!==row.body) { db.prepare("UPDATE memories SET verified=0,proof=NULL,status='candidate' WHERE id=?").run(row.id); if(r.action==='approve')fail(); row.status='candidate'; }
          const nextScope=r.scope===undefined?row.scope:r.scope==='global'?'global':scope;
          if(r.action==='approve')db.prepare("DELETE FROM memories WHERE scope=? AND factKey=? AND status='enabled' AND id<>?").run(nextScope,row.factKey,row.id);
          db.prepare('UPDATE memories SET body=?,scope=?,status=?,revision=revision+1,reason=?,sourceOrder=(SELECT COALESCE(MAX(seq),0) FROM sources) WHERE id=?').run(body,nextScope,r.action==='approve'?'enabled':row.status,'用户审核或编辑',row.id);
        } else fail();
      }
      bump(); return state(scope);
    });
  }
  function search(scope, query, cursor=null) {
    if(!clean(query,4000)) fail();
    const s=settings(); if(!s.enabled) return {revision:s.revision,items:[],cursor:null};
    let offset=0;
    if(cursor) { try { const c=JSON.parse(Buffer.from(cursor,'base64url').toString()); if(c.scope!==scope||c.query!==hash(query)||c.revision!==s.revision||!Number.isSafeInteger(c.offset)||c.offset<0||c.offset>1000) fail(); offset=c.offset; } catch { fail(); } }
    const words=tokens(query);
    // ponytail: bounded 1000-record lexical scan; add embeddings only after measured recall gaps.
    const ranked=db.prepare("SELECT * FROM memories WHERE status='enabled' AND (scope=? OR scope=?) ORDER BY sourceOrder DESC LIMIT 1000").all(scope,'global').map(r=>({r,score:[...tokens(r.body+' '+r.factKey)].filter(w=>words.has(w)).length})).filter(x=>x.score>0).sort((a,b)=>b.score-a.score||b.r.sourceOrder-a.r.sourceOrder);
    const rows=ranked.slice(offset,offset+10).map(x=>project(x.r,scope));
    return {revision:s.revision,items:rows,cursor:offset+10<ranked.length?Buffer.from(JSON.stringify({scope,query:hash(query),revision:s.revision,offset:offset+10})).toString('base64url'):null};
  }
  function job(id,status,error=null) { const row=db.prepare('SELECT * FROM jobs WHERE id=?').get(id); if(!row) fail(); if(row.status==='cancelled')return; if(status==='running'&&row.attempts>=3) fail(); db.prepare('UPDATE jobs SET status=?,error=?,attempts=attempts+? WHERE id=?').run(status,error,status==='running'?1:0,id); }
  const saveProof=(id,proof)=>{ if(!proof)return; let observations;try{observations=JSON.parse(proof.observation);}catch{return;} if(!Array.isArray(observations))return; db.prepare('UPDATE sources SET proof=? WHERE id=?').run(JSON.stringify({verified:proof.verified===true,toolIds:proof.toolIds,observations:observations.map(x=>({id:x.id,tool:x.tool,digest:hash(x.result)}))}),id); };
  const invalidateSkills=(scope,ids)=>{for(const id of ids.slice(0,30))db.prepare("UPDATE memories SET status='candidate',revision=revision+1,reason='Skill 已改变或不可用，请重新关联。' WHERE id=? AND kind='skill' AND (scope=? OR scope='global')").run(id,scope);if(ids.length)bump();};
  return {state,admit,apply,action,search,source,job,settings,invalidateSkills,saveProof,item:(scope,id)=>project(item(scope,id),scope),close:()=>db.close()};
}
module.exports={createMemoryStore,sensitive,hash};
