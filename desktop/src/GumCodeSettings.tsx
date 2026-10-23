import { useEffect, useRef, useState } from 'react';
import { GumCodeMemory } from './GumCodeMemory';
import type { GumCodeState } from './gumCodeTypes';
import { useI18n } from './i18n';

export function GumCodeSettings({ visible, section }: { visible: boolean; section: 'memory' | 'personalization' }) {
  const { t } = useI18n();
  const [state, setState] = useState<GumCodeState>();
  const [error, setError] = useState('');
  useEffect(() => {
    if (!visible || !window.gumi?.gumCodeState) return;
    let disposed = false, changed = false;
    const update = (next: GumCodeState) => {
      if (disposed) return;
      setState(next); setError('');
    };
    const off = window.gumi.onGumCode(next => { changed = true; update(next); });
    void window.gumi.gumCodeState().then(next => { if (!changed) update(next); }).catch(() => { if (!disposed) setError(t('无法读取当前项目，请重新打开设置。', 'Could not load the current project. Reopen Settings.')); });
    return () => { disposed = true; off(); };
  }, [visible, t]);
  const selected = state?.sessions.find(session => session.id === state.selectedId);
  return <div className="gum-code-settings">
    <div hidden={section !== 'memory'}>
      <h2>{t('记忆', 'Memory')}</h2>
      <GumCodeMemory key={selected?.id || 'none'} sessionId={selected?.id || ''} visible={visible && section === 'memory'} />
    </div>
    <div hidden={section !== 'personalization'}>
      <h2>{t('个性化', 'Personalization')}</h2>
      {selected && <p className="settings-project-name">{selected.workspace.split(/[\\/]/).filter(Boolean).pop()}</p>}
      {!selected && <p role="status">{t('请先打开项目会话', 'Open a project conversation first')}</p>}
      {state?.sessions.map(session => <div key={session.id} hidden={session.id !== selected?.id}>
        <InstructionsEditor sessionId={session.id} visible={visible && section === 'personalization' && session.id === selected?.id} />
      </div>)}
    </div>
    {error && <p role="alert">{error}</p>}
  </div>;
}

function InstructionsEditor({ sessionId, visible }: { sessionId: string; visible: boolean }) {
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
