const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {randomUUID}=require('node:crypto');
const {createMemoryStore}=require('../electron/gum-code-memory.cjs');
function setup(t){const dir=fs.mkdtempSync(path.join(os.tmpdir(),'memory-store-'));const db=createMemoryStore(dir);db.fixtureDir=dir;t.after(()=>{db.close();fs.rmSync(dir,{recursive:true,force:true});});db.action('A',{action:'enable',enabled:true,revision:0});return db;}
const proposal=(body,extra={})=>({body,evidence:body,factKey:'project.test',kind:'fact',reliable:true,...extra});
const add=(db,text,extra={})=>{const s=db.admit('A',randomUUID(),randomUUID(),null,text);db.apply('A',s.id,text,[proposal(text,extra)]);return s;};
test('memory same source event and completed extraction are idempotent',t=>{const db=setup(t),session=randomUUID(),turn=randomUUID(),text='记住项目采用 SQLite';const a=db.admit('A',session,turn,null,text),b=db.admit('A',session,turn,null,text);assert.equal(a.id,b.id);db.apply('A',a.id,text,[proposal(text)]);db.apply('A',a.id,text,[proposal(text,{factKey:'changed.key'})]);assert.equal(db.state('A').items.length,1);assert.equal(db.state('A').jobs.length,1);});
test('memory deletion invalidates changed-key replay, old cursors and physical body',t=>{const db=setup(t);const text='记住项目标记 PERMANENTLY_REMOVED_BODY';add(db,text);const row=db.state('A').items[0];const pendingText='旧队列中的 PERMANENTLY_REMOVED_BODY';const pending=db.admit('A',randomUUID(),randomUUID(),null,pendingText);db.action('A',{action:'delete',id:row.id,revision:row.revision});db.apply('A',pending.id,pendingText,[proposal(pendingText,{factKey:'alternate.key'})]);assert.equal(db.state('A').items.length,0);assert.equal(fs.readFileSync(path.join(db.fixtureDir,'memory.sqlite')).includes(Buffer.from('PERMANENTLY_REMOVED_BODY')),false);assert.equal(db.state('A').jobs.find(x=>x.id===pending.id).status,'cancelled');const fresh='记住新的 PERMANENTLY_REMOVED_BODY';add(db,fresh);assert.equal(db.state('A').items.length,1);});
test('memory evidence auto-enables, preserves body-free proof, editing invalidates proof',t=>{const db=setup(t),text='请修复超时并复测';const src=db.admit('A',randomUUID(),randomUUID(),null,text);const evidence='Ran 4 tests. OK';db.apply('A',src.id,text,[proposal('增大超时后工具复测通过；根因待复核',{kind:'experience',evidence})],[],{verified:true,toolIds:['call1'],observation:evidence});let row=db.state('A').items[0];assert.equal(row.verified,true);assert.deepEqual(row.proof.toolIds,['call1']);assert.equal(JSON.stringify(row.proof).includes(evidence),false);assert.equal(row.status,'enabled');assert.equal(db.search('A','超时').items.length,1);db.action('A',{action:'edit',id:row.id,revision:row.revision,body:'新根因未经复测'});row=db.state('A').items[0];assert.equal(row.status,'candidate');assert.equal(row.verified,false);assert.equal(row.proof,null);});
test('memory update uses literal new evidence and approving candidate removes prior same fact',t=>{const db=setup(t);add(db,'记住项目采用 SQLite');const old=db.state('A').items[0],text='项目现在采用 DuckDB';const src=db.admit('A',randomUUID(),randomUUID(),null,text);db.apply('A',src.id,text,[proposal('model paraphrase',{evidence:text,targetId:old.id,baseRevision:old.revision})]);assert.equal(db.state('A').items[0].body,text);const c='我们改用 PostgreSQL';add(db,c,{reliable:false});const candidate=db.state('A').items.find(x=>x.status==='candidate');db.action('A',{action:'approve',id:candidate.id,revision:candidate.revision});assert.deepEqual(db.state('A').items.filter(x=>x.status==='enabled').map(x=>x.body),[c]);});
test('memory pagination is revision and scope bound and candidates stay excluded',t=>{const db=setup(t);for(let i=0;i<12;i++)add(db,`记住 palette color ${i}`,{factKey:`color.${i}`});const page=db.search('A','palette');assert.equal(page.items.length,10);assert.ok(page.cursor);assert.equal(db.search('A','palette',page.cursor).items.length,2);assert.throws(()=>db.search('B','palette',page.cursor));const first=page.items[0];db.action('A',{action:'delete',id:first.id,revision:first.revision});assert.throws(()=>db.search('A','palette',page.cursor));});

test('memory post-turn experience is independent of completed fact extraction',t=>{const db=setup(t),text='记住项目采用 SQLite，修复后总结经验';const src=db.admit('A',randomUUID(),randomUUID(),null,text);db.apply('A',src.id,text,[proposal('项目采用 SQLite')]);const observation=JSON.stringify([{id:'verified-call',tool:'run_command',result:'Ran 4 tests. OK'}]);const proof={verified:true,toolIds:['verified-call'],observation};db.apply('A',src.id,text,[proposal('未经授权的新事实',{evidence:'项目采用 SQLite',factKey:'alternate.fact'}),proposal('修复后工具复测通过，因果关系需审核',{kind:'experience',factKey:'project.experience',evidence:'Ran 4 tests. OK'})],[],proof);assert.equal(db.state('A').items.length,2);const experience=db.state('A').items.find(x=>x.kind==='experience');assert.equal(experience.verified,true);db.apply('A',src.id,text,[proposal('另一经验',{kind:'experience',factKey:'alternate.experience',evidence:'Ran 4 tests. OK'})],[],proof);assert.equal(db.state('A').items.length,2);});

test('memory full capacity refuses new records visibly but permits updates',t=>{const db=setup(t);add(db,'记住项目采用 SQLite');const original=db.state('A').items[0];const {DatabaseSync}=require('node:sqlite');const seed=new DatabaseSync(path.join(db.fixtureDir,'memory.sqlite'));seed.exec("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<999) INSERT INTO memories(id,scope,kind,factKey,body,status,revision,sourceId,sourceOrder,reason,verified) SELECT 'capacity-'||i,'A','fact','capacity.'||i,'capacity fixture','candidate',1,'"+original.sourceId+"',1,'fixture',0 FROM n");seed.close();const newer='项目现在采用 DuckDB';const src=db.admit('A',randomUUID(),randomUUID(),null,newer);db.apply('A',src.id,newer,[proposal(newer,{targetId:original.id,baseRevision:original.revision})]);assert.equal(db.item('A',original.id).body,newer);const extra='记住额外事实';const next=db.admit('A',randomUUID(),randomUUID(),null,extra);assert.throws(()=>db.apply('A',next.id,extra,[proposal(extra,{factKey:'extra.fact'})]),/容量已满/);assert.equal(db.state('A').items.length,1000);});

test('memory direct writes preserve multiple distinct facts before extraction finalizes',t=>{const db=setup(t),text='记住：项目采用 SQLite。文档使用中文。';const src=db.admit('A',randomUUID(),randomUUID(),null,text);const first=proposal('项目采用 SQLite。');const second=proposal('文档使用中文。',{factKey:'project.language'});db.apply('A',src.id,text,[first],[],null,{partial:true});db.apply('A',src.id,text,[second],[],null,{partial:true});db.apply('A',src.id,text,[proposal('项目采用 SQLite。',{factKey:'alias.database'})],[],null,{partial:true});assert.equal(db.state('A').items.length,2);db.apply('A',src.id,text,[first,second]);assert.equal(db.source(src.id).factsProcessed,1);assert.equal(db.state('A').items.length,2);});

test('memory global scope needs an affirmative sharing command, not later global mention',t=>{const db=setup(t),text='Remember this locally; global sharing is forbidden. The color is blue.';const src=db.admit('A',randomUUID(),randomUUID(),null,text);db.apply('A',src.id,text,[proposal('The color is blue.',{scope:'global'})]);assert.equal(db.state('B').items.length,0);});

test('validated automatic facts and skills are recalled without human approval',t=>{
  const db=setup(t),text='项目文档使用中文';
  add(db,text,{factKey:'project.language'});
  assert.deepEqual(db.search('A','文档').items.map(x=>x.body),[text]);
  assert.equal(db.search('B','文档').items.length,0);
  const skillText='发布流程使用 release skill',src=db.admit('A',randomUUID(),randomUUID(),null,skillText);
  db.apply('A',src.id,skillText,[proposal(skillText,{kind:'skill',factKey:'project.release',skillRef:'release',skillDigest:'a'.repeat(64)})],['release']);
  assert.equal(db.search('A','release').items[0].status,'enabled');
  db.invalidateSkills('A',[db.search('A','release').items[0].id]);
  assert.equal(db.search('A','release').items.length,0);
});

test('unreliable facts, hypothetical statements and unverified experience never auto-enable',t=>{
  const db=setup(t);
  add(db,'记住未知断言',{reliable:false});
  add(db,'记住如果采用 PostgreSQL',{factKey:'hypothesis'});
  add(db,'工具成功',{kind:'experience',factKey:'experience'});
  const text='发布使用 release',src=db.admit('A',randomUUID(),randomUUID(),null,text);
  db.apply('A',src.id,text,[proposal(text,{kind:'skill',factKey:'skill',skillRef:'release'})],['release']);
  for(const query of ['未知','PostgreSQL','工具','release'])assert.equal(db.search('A',query).items.length,0);
});

test('automatic facts cannot bypass correction revisions or revive deleted sources',t=>{
  const db=setup(t);add(db,'项目采用 SQLite');const old=db.state('A').items[0];
  add(db,'项目采用 PostgreSQL');
  assert.deepEqual(db.search('A','项目').items.map(x=>x.body),['项目采用 SQLite']);
  const text='项目采用 DuckDB',src=db.admit('A',randomUUID(),randomUUID(),null,text);
  db.apply('A',src.id,text,[proposal(text,{targetId:old.id,baseRevision:old.revision+1})]);
  assert.deepEqual(db.search('A','项目').items.map(x=>x.body),['项目采用 SQLite']);
  add(db,'项目采用 MySQL');
  assert.deepEqual(db.search('A','项目').items.map(x=>x.body),['项目采用 SQLite']);
  db.action('A',{action:'delete',id:old.id,revision:old.revision});
  assert.equal(db.search('A','项目').items.length,0);
});


test('verified tools cannot validate an experience citing only user text',t=>{
  const db=setup(t),text='用户猜测根因',src=db.admit('A',randomUUID(),randomUUID(),null,text);
  db.apply('A',src.id,text,[proposal(text,{kind:'experience'})],[],{verified:true,toolIds:['test'],observation:JSON.stringify([{id:'test',tool:'run_command',result:'Ran 1 test. OK'}])});
  assert.equal(db.search('A','根因').items.length,0);
});

test('existing candidates stay inactive after reopening and enabling memory',t=>{
  const dir=fs.mkdtempSync(path.join(os.tmpdir(),'memory-legacy-'));
  let db=createMemoryStore(dir);
  t.after(()=>{db.close();fs.rmSync(dir,{recursive:true,force:true});});
  db.action('A',{action:'enable',enabled:true,revision:0});
  add(db,'历史候选保留',{reliable:false});
  const original=db.state('A').items[0];
  db.close();db=createMemoryStore(dir);
  db.action('A',{action:'enable',enabled:false,revision:db.settings().revision});
  db.action('A',{action:'enable',enabled:true,revision:db.settings().revision});
  assert.equal(db.state('A').items[0].id,original.id);
  assert.equal(db.state('A').items[0].status,'candidate');
  assert.equal(db.search('A','历史候选').items.length,0);
});
