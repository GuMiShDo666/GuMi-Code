import { useEffect, useRef, useState } from 'react';
import type { GumCodeMemoryState } from './gumCodeTypes';
import { useI18n } from './i18n';

export function GumCodeMemory({ sessionId, visible }: { sessionId: string; visible: boolean }) {
  const { t } = useI18n();
  const [state, setState] = useState<Pick<GumCodeMemoryState, 'enabled' | 'revision'>>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const mounted = useRef(true), writing = useRef(false), version = useRef(0);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; version.current++; }; }, []);
  useEffect(() => {
    if (!visible || !sessionId || !window.gumi?.gumCodeMemoryState) return;
    let disposed = false;
    const request = ++version.current;
    void window.gumi.gumCodeMemoryState({ sessionId }).then(next => {
      if (disposed || request !== version.current) return;
      if (next.error) { setError(next.error); return; }
      setState({ enabled: next.enabled, revision: next.revision }); setError('');
    }).catch(() => { if (!disposed && request === version.current) setError(t('无法读取记忆设置，请重试。', 'Could not load memory settings. Try again.')); });
    return () => { disposed = true; };
  }, [sessionId, visible, reload, t]);
  async function toggle(enabled: boolean) {
    if (!state || !sessionId || !window.gumi?.gumCodeMemoryAction || writing.current) return;
    writing.current = true; ++version.current; setBusy(true); setError('');
    try {
      const next = await window.gumi.gumCodeMemoryAction({ sessionId, action: 'enable', enabled, revision: state.revision });
      if (!mounted.current) return;
      if (next.error) { setError(next.error); return; }
      setState({ enabled: next.enabled, revision: next.revision });
    } catch { if (mounted.current) setError(t('保存失败，请重试。', 'Could not save. Try again.')); }
    finally { writing.current = false; if (mounted.current) setBusy(false); }
  }
  return <section className="gum-memory-panel" aria-label={t('记忆', 'Memory')}>
    <div className="gum-memory-toolbar"><label><input type="checkbox" role="switch" checked={state?.enabled || false} disabled={!sessionId || !state || busy} onChange={event => void toggle(event.target.checked)} />{t('启用记忆', 'Enable memory')}</label></div>
    {!sessionId && <p role="status">{t('请先打开项目会话', 'Open a project conversation first')}</p>}
    {error && <div role="alert">{error} <button type="button" disabled={busy} onClick={() => setReload(value => value + 1)}>{t('重试', 'Retry')}</button></div>}
  </section>;
}
