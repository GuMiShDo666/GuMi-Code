import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { GumCode } from './GumCode';
import { ApiSettings } from './ApiSettings';
import { GumCodeSettings } from './GumCodeSettings';
import { LanguageProvider, LanguageSettings, useI18n } from './i18n';
import { Icon } from './ui';
import { bridge, type State, type MigrationResult } from './model';
import './base.css';
import './settings.css';

function App() {
 const [state,setState]=useState<State>({settings:{language:'zh'}}),[error,setError]=useState('');
 useEffect(()=>{void bridge().preferences().then(setState).catch(()=>setError('无法读取本机设置。'));},[]);
 async function commit(change:(next:State)=>void){const next=structuredClone(state);change(next);setState(await bridge().savePreferences(next));}
 return <LanguageProvider language={state.settings.language}><Shell state={state} commit={commit} error={error}/></LanguageProvider>;
}
function Shell({state,commit,error}:{state:State;commit:(change:(next:State)=>void)=>Promise<void>;error:string}) {
 const {t}=useI18n(); const [settings,setSettings]=useState(false),[section,setSection]=useState<'model'|'project'|'language'>('model'),[generation,setGeneration]=useState(0);
 return <div className={`app ${settings?'settings-route':''}`}>
  <div className="titlebar">GuMi-Code</div>
  {!settings&&<aside className="navigation"><div className="brand"><img src="./brand-bird.png" alt="GuMi"/></div><button className="settings-link" aria-label={t('设置')} onClick={()=>setSettings(true)}><Icon name="settings" size={24}/><span>{t('设置')}</span></button></aside>}
  <main className="content">
   {error&&<p role="alert">{error}</p>}
   <div hidden={settings}><GumCode key={generation} projects={[]} visible={!settings}/></div>
   <section hidden={!settings} className="settings-panel settings-layout">
    <aside className="settings-sidebar">
     <button className="settings-back" onClick={()=>setSettings(false)} aria-label={t('返回','Back')}><span aria-hidden="true">←</span>{t('返回','Back')}</button>
     <h1>{t('设置')}</h1>
     <nav aria-label={t('设置导航','Settings navigation')}>
      <button aria-current={section==='model'?'page':undefined} onClick={()=>setSection('model')}><Icon name="connection"/>{t('模型','Model')}</button>
      <button aria-current={section==='project'?'page':undefined} onClick={()=>setSection('project')}><Icon name="folder"/>{t('项目','Project')}</button>
      <button aria-current={section==='language'?'page':undefined} onClick={()=>setSection('language')}><Icon name="globe"/>{t('语言')}</button>
     </nav>
    </aside>
    <div className="settings-detail">
     <div hidden={section!=='model'}><h2>{t('模型','Model')}</h2><ApiSettings key={generation}/></div>
     <div hidden={section!=='project'}><GumCodeSettings key={generation} visible={settings&&section==='project'}/><Migration onImported={()=>setGeneration(value=>value+1)}/></div>
     <div hidden={section!=='language'}><LanguageSettings state={state} commit={commit}/></div>
    </div>
   </section>
  </main>
 </div>;
}
function Migration({onImported}:{onImported:()=>void}) {
 const {t}=useI18n();const [busy,setBusy]=useState(false),[result,setResult]=useState<MigrationResult>();
 async function run(){if(busy)return;setBusy(true);setResult(undefined);try{const value=await bridge().importWorkbench();setResult(value);if(value.imported)onImported();}catch{setResult({error:t('迁移未完成，原数据已保留。','Import failed. Original data was kept.')});}finally{setBusy(false);}}
 return <details className="settings-card settings-migration"><summary>{t('导入旧数据','Import legacy data')}</summary><p>{t('先关闭工作台，再导入会话、记忆和 API 配置。原数据保留。','Close Workbench before importing chats, memory and API settings. Original data is kept.')}</p><button disabled={busy} onClick={()=>void run()}>{busy?t('处理中…'):t('从工作台迁移','Import from Workbench')}</button>{result?.error&&<p role="alert">{result.error}</p>}{result?.imported&&<p role="status">{t('迁移完成','Import complete')} · {result.sessions} {t('会话','chats')} · {result.memories} {t('记忆','memories')}</p>}{result?.alreadyImported&&<p role="status">{t('已完成迁移，不会重复覆盖。','Already imported. Existing data will not be overwritten.')}</p>}</details>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><App/></StrictMode>);
