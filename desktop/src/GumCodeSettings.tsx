import { useEffect, useRef, useState } from 'react';
import { GumCodeMemory } from './GumCodeMemory';
import type { GumCodeState } from './gumCodeTypes';
import { useI18n } from './i18n';

export function GumCodeSettings({ visible }: { visible: boolean }) {
  const { t } = useI18n();
  const [state, setState] = useState<GumCodeState>();
  const [sessionId, setSessionId] = useState('');
  const [error, setError] = useState('');
  const [memoryDirty, setMemoryDirty] = useState(false);
  const [instructionsDirty, setInstructionsDirty] = useState(false);
  useEffect(() => {
    if (!visible || !window.gumi?.gumCodeState) return;
    let disposed = false, changed = false;
    const update = (next: GumCodeState) => {
      if (disposed) return;
      setState(next); setError('');
      setSessionId(current => current || next.selectedId || next.sessions[0]?.id || '');
    };
    const off = window.gumi.onGumCode(next => { changed = true; update(next); });
    void window.gumi.gumCodeState().then(next => { if (!changed) update(next); }).catch(() => { if (!disposed) setError(t('无法读取工作区，请重新打开设置。', 'Could not load workspaces. Reopen Settings.')); });
    return () => { disposed = true; off(); };
  }, [visible, t]);
  const sessions = state?.sessions.filter((session, index, all) => all.findIndex(other => other.workspace === session.workspace) === index) || [];
  const selected = state?.sessions.find(session => session.id === sessionId);
  return <div className="gum-code-settings">
    <h2>{t('项目', 'Project')}</h2>
    <section className="settings-card">
      <label className="settings-row">{t('当前项目', 'Current project')}
        <select aria-label={t('设置工作区', 'Settings workspace')} value={selected?.workspace || ''} disabled={!sessions.length} onChange={event => {
          if ((memoryDirty || instructionsDirty) && !window.confirm(t('切换工作区会放弃未保存的编辑，是否继续？', 'Switch workspace and discard unsaved edits?'))) return;
          setMemoryDirty(false); setInstructionsDirty(false);
          setSessionId(sessions.find(session => session.workspace === event.target.value)?.id || '');
        }}>
          {!sessions.length && <option value="">{t('尚未选择工作区', 'No workspace selected')}</option>}
          {sessions.map(session => <option key={session.id} value={session.workspace}>{session.workspace.split(/[\\/]/).filter(Boolean).pop()}</option>)}
        </select>
      </label>
      {selected && <p className="settings-path">{selected.workspace}</p>}
      {error && <p role="alert">{error}</p>}
      {!sessions.length && !error && <p>{t('先选择项目文件夹，再管理记忆和指令。', 'Select a project folder to manage memory and instructions.')}</p>}
    </section>
    {selected && <>
      <section className="settings-card"><h3>{t('记忆', 'Memory')}</h3><GumCodeMemory key={sessionId} sessionId={sessionId} visible={visible} inline onDirtyChange={setMemoryDirty} /></section>
      <InstructionsEditor key={`instructions:${sessionId}`} sessionId={sessionId} visible={visible} onDirtyChange={setInstructionsDirty} />
    </>}
  </div>;
}

function InstructionsEditor({ sessionId, visible, onDirtyChange }: { sessionId: string; visible: boolean; onDirtyChange: (dirty: boolean) => void }) {
  const { t } = useI18n();
  const [saved, setSaved] = useState<{ content: string; revision: string; path: string }>();
  const [content, setContent] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const mounted = useRef(true), writing = useRef(false);
  const dirty = Boolean(saved && content !== saved.content);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { onDirtyChange(dirty); }, [dirty, onDirtyChange]);
  useEffect(() => {
    if (!visible || saved || !window.gumi?.gumCodeInstructions) return;
    let disposed = false;
    void window.gumi.gumCodeInstructions({ sessionId }).then(result => {
      if (disposed) return;
      if ('error' in result && result.error) { setError(result.error); return; }
      if ('content' in result) { setSaved(result); setContent(result.content); setError(''); }
    }).catch(() => { if (!disposed) setError(t('无法读取 AGENTS.md，请重试。', 'Could not read AGENTS.md. Try again.')); });
    return () => { disposed = true; };
  }, [sessionId, visible, reload, saved, t]);
  async function save() {
    if (!saved || writing.current) return;
    writing.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const result = await window.gumi!.gumCodeSaveInstructions({ sessionId, content, revision: saved.revision });
      if (!mounted.current) return;
      if ('error' in result && result.error) { setError(result.error); return; }
      if ('content' in result) { setSaved(result); setContent(result.content); setNotice(t('已保存', 'Saved')); }
    } catch { if (mounted.current) setError(t('保存未完成，编辑内容已保留。', 'Save failed. Your edits have been kept.')); }
    finally { writing.current = false; if (mounted.current) setBusy(false); }
  }
  return <section className="settings-card">
    <h3>{t('项目指令', 'Project instructions')} <span className="settings-caption">AGENTS.md</span></h3>
    {saved && <p className="settings-path">{saved.path}</p>}
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      <textarea aria-label={t('编辑 AGENTS.md', 'Edit AGENTS.md')} className="settings-instructions" value={content} disabled={!saved || busy} maxLength={65536} rows={6} spellCheck={false} onChange={event => { setContent(event.target.value); setNotice(''); }} />
      <div className="action-row"><button className="primary" type="submit" disabled={!saved || busy || !dirty}>{busy ? t('保存中…', 'Saving…') : t('保存', 'Save')}</button>
        {error && <button type="button" className="quiet-button" disabled={busy} onClick={() => {
          if (dirty && !window.confirm(t('重新载入会放弃未保存的编辑，是否继续？', 'Reload and discard unsaved edits?'))) return;
          setSaved(undefined); setError(''); setReload(value => value + 1);
        }}>{t('重新载入', 'Reload')}</button>}
      </div>
    </form>
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </section>;
}
