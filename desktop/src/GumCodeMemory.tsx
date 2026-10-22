import { useEffect, useRef, useState } from "react";
import type { GumCodeMemoryAction, GumCodeMemoryItem, GumCodeMemoryState } from "./gumCodeTypes";
import { Modal } from "./ui";
import { useI18n } from "./i18n";

export function GumCodeMemory({ sessionId, visible, inline = false, onDirtyChange }: { sessionId: string; visible: boolean; inline?: boolean; onDirtyChange?: (dirty: boolean) => void }) {
  const { t } = useI18n();
  const api = window.gumi;
  const [open, setOpen] = useState(false);
  const [updated, setUpdated] = useState(false);
  const autoUpdates = useRef<string | undefined>(undefined);
  const [state, setState] = useState<GumCodeMemoryState>();
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [filter, setFilter] = useState("candidate");
  const [scope, setScope] = useState("all");
  const [edit, setEdit] = useState<{ item: GumCodeMemoryItem; body: string; scope: "workspace" | "global" }>();
  const [source, setSource] = useState<{ id: string; revision: number; view: NonNullable<GumCodeMemoryState["sourceView"]> }>();
  const version = useRef(0);
  const mounted = useRef(true);
  const writing = useRef(false);
  const editor = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { if (edit) editor.current?.focus(); }, [edit?.item.id]);
  useEffect(() => { onDirtyChange?.(Boolean(edit && (edit.body !== edit.item.body || edit.scope !== edit.item.scope))); }, [edit, onDirtyChange]);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; version.current++; }; }, []);
  useEffect(() => { if (!visible) setOpen(false); }, [visible]);
  useEffect(() => {
    if (!visible || !api?.gumCodeMemoryState) return;
    let disposed = false;
    let refreshing = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      if (writing.current || refreshing) return;
      refreshing = true;
      const request = ++version.current;
      try {
        const next = await api!.gumCodeMemoryState({ sessionId });
        if (disposed || request !== version.current) return;
        if (next.error) { setLoadError(next.error); return; }
        setLoadError("");
        const signature = next.items.filter(item => item.status === "enabled" && item.reason === "已按较新的用户事实自动更新").map(item => `${item.id}:${item.revision}`).sort().join("|");
        if (autoUpdates.current !== undefined && signature && autoUpdates.current !== signature) setUpdated(true);
        autoUpdates.current = signature;
        setState(next);
      } catch { if (!disposed && request === version.current) setLoadError(t('记忆列表暂时无法读取，请重试。', 'Could not load memories. Try again.')); }
      finally { refreshing = false; }
    }
    void refresh();
    // Coalesce streamed conversation events; never read the DB for every token.
    const off = api.onGumCode(() => {
      if (!timer) timer = setTimeout(() => { timer = undefined; void refresh(); }, 1500);
    });
    const poll = open || inline ? setInterval(() => void refresh(), 3000) : undefined;
    return () => { disposed = true; off(); clearTimeout(timer); clearInterval(poll); };
  }, [api, sessionId, visible, open, inline, reload, t]);

  async function act(request: Omit<GumCodeMemoryAction, "sessionId">) {
    if (!api?.gumCodeMemoryAction || writing.current) return false;
    writing.current = true; ++version.current; setBusy(true); setError(""); setNotice("");
    try {
      const next = await api.gumCodeMemoryAction({ ...request, sessionId });
      if (!mounted.current) return false;
      if (next.error) { setError(next.error); return false; }
      setState(next);
      setLoadError("");
      if (request.action === "source") {
        const item = next.items.find(item => item.id === request.id);
        setSource(item && next.sourceView ? { id: item.id, revision: item.revision, view: next.sourceView } : undefined);
      }
      else { setSource(undefined); setNotice(request.action === "delete" ? t('记忆已删除，原聊天保留。', 'Memory deleted. The original conversation is kept.') : request.action === "reject" ? t('已拒绝该候选。', 'Candidate rejected.') : t('已保存。', 'Saved.')); }
      return true;
    } catch { if (mounted.current) setError(t('操作未完成，编辑内容已保留。请重试。', 'Operation failed. Your edits have been kept. Try again.')); return false; }
    finally { writing.current = false; if (mounted.current) setBusy(false); }
  }

  const sourceView = source && state?.items.some(item => item.id === source.id && item.revision === source.revision) ? source.view : undefined;
  const currentEdit = edit && state?.items.find(item => item.id === edit.item.id);
  const editStale = edit && (!currentEdit || currentEdit.revision !== edit.item.revision);
  const cancelledJobs = state?.jobs.filter(job => job.status === "cancelled" && job.error) || [];
  const pending = state?.items.filter(item => item.status === "candidate").length || 0;
  const items = state?.items.filter(item => item.status === filter && (scope === "all" || item.scope === scope)) || [];
  const panel = <section className="gum-memory-panel" aria-label={t('记忆管理', 'Memory management')}>
        <div className="gum-memory-toolbar">
          <label><input type="checkbox" role="switch" checked={state?.enabled || false} disabled={!state || busy} onChange={event => void act({ action: "enable", enabled: event.target.checked, revision: state?.revision })} />{t('启用记忆', 'Enable memory')}</label>
        </div>
        {loadError && <div className="gum-memory-error" role="alert">{loadError} <button type="button" disabled={busy} onClick={() => setReload(value => value + 1)}>{t('重新读取', 'Reload')}</button></div>}
        {error && <p className="gum-memory-error" role="alert">{error}</p>}
        {notice && <p className="gum-memory-notice" role="status">{notice}</p>}
        <div className="gum-memory-filters">
          <div role="group" aria-label={t('记忆状态', 'Memory status')}>
            <button type="button" aria-pressed={filter === "candidate"} onClick={() => setFilter("candidate")}>{t('待审核', 'Pending review')} {pending}</button>
            <button type="button" aria-pressed={filter === "enabled"} onClick={() => setFilter("enabled")}>{t('已启用', 'Enabled')}</button>
          </div>
          <label>{t('范围', 'Scope')} <select value={scope} onChange={event => setScope(event.target.value)}><option value="all">{t('全部', 'All')}</option><option value="workspace">{t('当前工作区', 'Current workspace')}</option><option value="global">{t('全局共享', 'Shared globally')}</option></select></label>
        </div>
        {edit && <form className="gum-memory-edit" onSubmit={async event => {
          event.preventDefault();
          if (await act({ action: "edit", id: edit.item.id, revision: edit.item.revision, body: edit.body, scope: edit.scope })) setEdit(undefined);
        }}>
          <label htmlFor="gum-memory-body">{t('编辑记忆', 'Edit memory')}</label>
          {editStale && <p className="gum-memory-error" role="status">{currentEdit ? t('这条记忆已有更新，当前草稿已保留。请先核对下方最新内容。', 'This memory has changed. Your draft has been kept. Review the latest content below.') : t('这条记忆已被删除，当前草稿仅保留供查看。', 'This memory was deleted. Your draft is kept for reference.')}</p>}
          <textarea ref={editor} id="gum-memory-body" value={edit.body} maxLength={2000} disabled={busy} onChange={event => setEdit({ ...edit, body: event.target.value })} rows={4} />
          <label>{t('保存范围', 'Save scope')} <select value={edit.scope} disabled={busy} onChange={event => setEdit({ ...edit, scope: event.target.value as "workspace" | "global" })}><option value="workspace">{t('当前工作区', 'Current workspace')}</option><option value="global">{t('全局共享', 'Shared globally')}</option></select></label>
          {edit.scope === "global" && <p className="gum-memory-hint">{t('全局记忆可在其他 GuMi-Code 工作区召回。', 'Global memories can be recalled in other GuMi-Code workspaces.')}</p>}
          <div className="gum-memory-actions"><button type="submit" disabled={busy || !currentEdit || !edit.body.trim()}>{t('保存编辑', 'Save changes')}</button><button type="button" disabled={busy} onClick={() => setEdit(undefined)}>{t('取消编辑', 'Cancel editing')}</button></div>
        </form>}
        <div className="gum-memory-list" aria-busy={busy}>
          {!state && !loadError && <p role="status">{t('正在读取记忆…', 'Loading memories…')}</p>}
          {state && !items.length && <p className="gum-memory-empty">{filter === "candidate" ? t('暂无待审核记忆', 'No memories awaiting review') : t('暂无已启用记忆', 'No enabled memories')}</p>}
          {items.map(item => <article className="gum-memory-item" key={item.id}>
            <div className="gum-memory-meta"><span>{item.scope === "global" ? t('全局共享', 'Shared globally') : t('当前工作区', 'Current workspace')}</span><span>{item.kind === "experience" ? t('经验', 'Experience') : item.kind === "skill" ? t('技能关联', 'Skill reference') : t('事实 / 偏好', 'Fact / preference')}</span>{item.kind === "experience" && <span>{item.verified ? t('有验证依据', 'Verified') : t('尚未验证', 'Unverified')}</span>}</div>
            <p className="gum-memory-body">{item.body}</p>
            {item.reason && <p className="gum-memory-hint">{item.reason}</p>}
            {item.skillRef && <p className="gum-memory-hint">{t('关联技能：', 'Related skill: ')}{item.skillRef}</p>}
            <div className="gum-memory-actions">
              {item.status === "candidate" && <button type="button" disabled={busy || (item.kind === "experience" && !item.verified)} onClick={() => void act({ action: "approve", id: item.id, revision: item.revision })}>{t('确认启用', 'Approve')}</button>}
              <button type="button" disabled={busy} onClick={() => { setEdit({ item, body: item.body, scope: item.scope }); setSource(undefined); }}>{t('编辑', 'Edit')}</button>
              <button type="button" disabled={busy} onClick={() => void act({ action: "source", id: item.id })}>{t('查看来源', 'View source')}</button>
              <button type="button" disabled={busy} onClick={() => void act({ action: item.status === "candidate" ? "reject" : "delete", id: item.id, revision: item.revision })}>{item.status === "candidate" ? t('拒绝', 'Reject') : t('删除', 'Delete')}</button>
            </div>
          </article>)}
        </div>
        {sourceView && <section className="gum-memory-source" aria-label={t('记忆来源', 'Memory source')}><div className="gum-memory-actions"><h3>{t('来源记录', 'Source record')}</h3><button type="button" onClick={() => setSource(undefined)}>{t('收起来源', 'Hide source')}</button></div>{!sourceView.available ? <p>{t('原始来源已不可用，无法核对。', 'The original source is no longer available.')}</p> : sourceView.messages.map((message, index) => <p key={index}><strong>{message.role === "user" ? t('你', 'You') : "GuMi-Code"}：</strong>{message.content}</p>)}{sourceView.evidence?.map(proof => <details key={proof.toolCallId}><summary>{proof.label}</summary><p>{proof.content}</p></details>)}</section>}
        {state?.jobs.filter(job => job.status === "failed" || job.status === "running").map(job => <div className="gum-memory-job" key={job.id}><span>{job.status === "running" ? t('正在整理记忆…', 'Organizing memories…') : job.error || t('记忆整理未完成', 'Memory processing failed')}</span>{job.status === "failed" && <button type="button" disabled={busy || !state.enabled} onClick={() => void act({ action: "retry", id: job.id, revision: state.revision })}>{t('重试', 'Retry')}</button>}</div>)}
        {cancelledJobs.length > 0 && <details className="gum-memory-history"><summary>{t('已取消', 'Cancelled')} {cancelledJobs.length} {t('项历史整理任务', 'history jobs')}</summary>{[...new Set(cancelledJobs.map(job => job.error))].map(message => <p key={message}>{message}</p>)}</details>}
      </section>;
  return <>
    {!inline && <button type="button" className="gum-memory-trigger" onClick={() => { setOpen(true); setUpdated(false); }} aria-label={`${t('记忆', 'Memory')}${pending ? ` · ${pending} ${t('待审核', 'pending review')}` : ''}${updated ? ` · ${t('已自动更新', 'updated')}` : ''}`}>
      {t('记忆', 'Memory')}{pending > 0 && <span className="gum-memory-badge">{pending}</span>}{updated && <span className="gum-memory-badge" role="status">{t('已更新', 'Updated')}</span>}
    </button>}
    {inline ? <div hidden={!visible}>{panel}</div> : open && visible && <Modal title={t("GuMi-Code 记忆", "GuMi-Code memory")} onClose={() => setOpen(false)}>{panel}</Modal>}
  </>;
}
