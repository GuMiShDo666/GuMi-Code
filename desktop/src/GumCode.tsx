import { Fragment, useEffect, useRef, useState } from "react";
import type { Project } from "./model";
import type { GumCodeAttachment, GumCodeState, ModelProfile } from "./gumCodeTypes";
import { Icon } from "./ui";
import "./gum-code.css";
import { GumCodeMarkdown } from "./GumCodeMarkdown";
import { GumCodeInput, getSkillMentions } from "./GumCodeInput";
import { useI18n } from "./i18n";
import { GumCodeProgress } from "./GumCodeProgress";

// Presentation adapted from Spectrum UI AI Assistants (Apache-2.0); see third-party/Spectrum-UI-NOTICE.md.

const empty: GumCodeState = {
  progress: [], plan: [], turnStartedAt: null, turnEndedAt: null, workspaces: [], sessions: [], selectedId: null, messages: [], status: "idle",
  statusText: "", approval: null, error: null,
};

export function GumCode({ projects, visible }: { projects: Project[]; visible: boolean }) {
  const { t } = useI18n();
  const [state, setState] = useState(empty);
  const [attachments, setAttachments] = useState<Record<string, GumCodeAttachment[]>>({});
  const [ready, setReady] = useState(false);
  const [pageHidden, setPageHidden] = useState(document.hidden);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [models, setModels] = useState<ModelProfile[]>([]);
  const [providerId, setProviderId] = useState("workbench");
  const inFlight = useRef(false);
  const eventVersion = useRef(0);
  const history = useRef<HTMLDivElement>(null);
  const historyContent = useRef<HTMLDivElement>(null);
  const [reducedMotion, setReducedMotion] = useState(false);
  const nearBottom = useRef(true);
  const scrolledSession = useRef<string | null>(null);
  const api = window.gumi;
  const available = Boolean(api?.gumCodeState && api?.onGumCode);
  const active = state.status !== "idle";
  const locked = !ready || !available || active || pending;
  const selected = state.sessions.find((session) => session.id === state.selectedId);
  const selectedProvider = selected ? selected.providerId || "workbench" : providerId;

  const workspaces = state.workspaces || [];
  const currentWorkspace = workspaces.find((workspace) => workspace.path === selected?.workspace);
  const workspaceValue = currentWorkspace?.id || (selected?.projectId ? `project:${selected.projectId}` : "");
  const groups = Array.from(new Set(state.sessions.map((session) => session.workspace))).map((path) => ({
    path, name: path.split("/").filter(Boolean).pop() || path,
    sessions: state.sessions.filter((session) => session.workspace === path),
  }));
  const draftKey = state.selectedId || "new";
  const draft = drafts[draftKey] || "";
  const files = attachments[draftKey] || [];
  const skillLimitExceeded = new Set(getSkillMentions(draft).map((mention) => mention.name)).size > 3;
  const [titleEdit, setTitleEdit] = useState<{ sessionId: string; value: string } | null>(null);
  const previousInput = [...state.messages].reverse().find((message) => message.role === "user")?.content;


  useEffect(() => {
    if (!api?.gumCodeModels) return;
    let disposed = false;
    let generation = 0;
    const refresh = () => { const request = ++generation; void api.gumCodeModels().then((result) => {
      if (disposed || request !== generation) return;
      setModels(result.models);
      setError(result.error || '');
    }).catch(() => { if (!disposed && request === generation) setError("无法载入模型配置。"); }); };
    refresh();
    const off = api.onApiSettingsChanged?.(refresh);
    return () => { disposed = true; off?.(); };
  }, [api]);

  useEffect(() => {
    const sync = () => setPageHidden(document.hidden);
    document.addEventListener("visibilitychange", sync);
    return () => document.removeEventListener("visibilitychange", sync);
  }, []);

  useEffect(() => {
    if (!api?.gumCodeState || !api.onGumCode) return;
    let disposed = false;
    // Events can arrive while the initial snapshot is in flight.
    const off = api.onGumCode((next) => {
      if (disposed) return;
      eventVersion.current++;
      setState(next);
    });
    const version = eventVersion.current;
    api.gumCodeState().then((next) => {
      if (disposed) return;
      if (eventVersion.current === version) setState(next);
      setReady(true);
    }).catch(() => {
      if (!disposed) setError("无法载入 GuMi-Code 会话。请重新打开工作台后重试。");
    });
    return () => { disposed = true; off(); };
  }, [api]);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const shell = history.current?.closest(".app");
    const sync = () => setReducedMotion(preference.matches || Boolean(shell?.classList.contains("motion-reduced")));
    sync();
    preference.addEventListener("change", sync);
    const observer = new MutationObserver(sync);
    if (shell) observer.observe(shell, { attributes: true, attributeFilter: ["class"] });
    return () => { preference.removeEventListener("change", sync); observer.disconnect(); };
  }, []);

  useEffect(() => {
    const element = history.current;
    const content = historyContent.current;
    if (!visible || pageHidden || !element || !content) return;
    if (scrolledSession.current !== state.selectedId) {
      element.scrollTop = element.scrollHeight;
      nearBottom.current = true;
      scrolledSession.current = state.selectedId;
    }
    let frame = 0;
    let previous = 0;
    const follow = (now: number) => {
      frame = 0;
      if (!nearBottom.current) return;
      const gap = element.scrollHeight - element.clientHeight - element.scrollTop;
      if (gap <= 1) return;
      const elapsed = previous ? Math.min(now - previous, 64) : 16;
      previous = now;
      element.scrollTop += reducedMotion ? gap : Math.max(1, gap * (1 - Math.exp(-elapsed / 55)));
      frame = requestAnimationFrame(follow);
    };
    const schedule = () => { if (!frame && nearBottom.current) { previous = 0; frame = requestAnimationFrame(follow); } };
    const stopFollowing = () => { nearBottom.current = false; cancelAnimationFrame(frame); frame = 0; };
    const onWheel = (event: WheelEvent) => { if (event.deltaY < 0) stopFollowing(); };
    const onKey = (event: KeyboardEvent) => { if (["ArrowUp", "PageUp", "Home"].includes(event.key)) stopFollowing(); };
    // Only user scrolling changes follow mode; animation's own scroll events do not.
    const onScroll = () => {
      if (!frame) nearBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 64;
    };
    const observer = new ResizeObserver(schedule);
    observer.observe(content);
    observer.observe(element);
    element.addEventListener("wheel", onWheel, { passive: true });
    element.addEventListener("keydown", onKey);
    element.addEventListener("pointerdown", stopFollowing);
    element.addEventListener("scroll", onScroll);
    schedule();
    return () => {
      observer.disconnect(); cancelAnimationFrame(frame);
      element.removeEventListener("wheel", onWheel); element.removeEventListener("keydown", onKey);
      element.removeEventListener("pointerdown", stopFollowing); element.removeEventListener("scroll", onScroll);
    };
  }, [state.selectedId, visible, pageHidden, reducedMotion]);

  async function run(operation: () => Promise<GumCodeState>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setError("");
    const version = eventVersion.current;
    try {
      const next = await operation();
      if (eventVersion.current === version) setState(next);
      return next;
    } catch {
      // Do not render raw IPC exceptions, which can contain subprocess details.
      setError("操作未成功，请检查当前状态后重试。输入内容已保留。");
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  async function send() {
    if (!ready || !available || pending || state.status === "stopping" || !api || !state.selectedId || (!draft.trim() && !files.length) || skillLimitExceeded) return;
    const key = draftKey;
    const text = draft;
    const next = await run(() => api.gumCodeSend({ sessionId: key, text, attachmentIds: files.map(file => file.id) }));
    if (next && !next.error) {
      setDrafts((current) => current[key] === text ? { ...current, [key]: "" } : current);
      setAttachments(current => ({ ...current, [key]: (current[key] || []).filter(file => !files.some(sent => sent.id === file.id)) }));
    }
  }

  async function saveTitle() {
    if (!titleEdit || !api || pending || titleEdit.sessionId !== state.selectedId) return;
    if (!titleEdit.value.trim()) { setTitleEdit(null); return; }
    const next = await run(() => api.gumCodeRename({ sessionId: titleEdit.sessionId, title: titleEdit.value }));
    if (next?.sessions.find(session => session.id === titleEdit.sessionId)?.title === titleEdit.value.trim()) setTitleEdit(null);
  }

  async function attachFiles() {
    if (!api?.gumCodePickAttachments || !selected || locked || inFlight.current) return;
    const key = selected.id;
    inFlight.current = true; setPending(true); setError('');
    try {
      const result = await api.gumCodePickAttachments({ sessionId: key });
      if (result.error !== undefined) { setError(result.error); return; }
      if (files.length + result.files.length > 8) { setError(t('最多添加 8 个文件。', 'Add up to 8 files.')); return; }
      setAttachments(current => ({ ...current, [key]: [...(current[key] || []), ...result.files] }));
    } catch { setError(t('文件添加失败，请重新选择。', 'Could not attach files. Please select them again.')); }
    finally { inFlight.current = false; setPending(false); }
  }

  async function changeProvider(id: string) {
    if (!api || active || inFlight.current) return;
    if (selected && selectedProvider !== id) {
      const next = await run(() => api.gumCodeNew({ ...(selected.workspaceId ? { workspaceId: selected.workspaceId } : { projectId: selected.projectId }), providerId: id }));
      if (!next || next.error) return;
    }
    setProviderId(id);
  }

  async function changeWorkspace(value: string, carryDraft = true) {
    if (locked || !api) return;
    const next = await run(() => value === "__pick__"
      ? api.gumCodePickWorkspace({ providerId: selectedProvider })
      : api.gumCodeNew({ ...(value.startsWith("project:") ? { projectId: value.slice(8) } : { workspaceId: value }), providerId: selectedProvider }));
    if (carryDraft && draft && next?.selectedId && next.selectedId !== state.selectedId && !next.error) {
      const id = next.selectedId;
      setDrafts((current) => ({ ...current, [id]: current[id] || draft }));
    }
  }

  return (
    <section className="gum-code" data-paused={pageHidden || !visible} hidden={!visible} aria-label={t('GuMi-Code 工作区', 'GuMi-Code workspace')}>
      <div className="gum-code-conversation">
        <header className="gum-code-header">
          <div id="gum-code-navigator" className="gum-code-navigator" role="navigation" aria-label={t('项目与对话', 'Folders and conversations')}>
            <div className="gum-code-nav-toolbar"><span>{t('项目', 'Projects')}</span><div>
              <button type="button" disabled={locked} aria-label={t('新建会话', 'New conversation')} title={t('新建会话', 'New conversation')} onClick={() => void changeWorkspace(workspaceValue || "__pick__", false)}><Icon name="plus" size={17} /></button>
            </div></div>
            <div className="gum-code-sessions">
              {groups.map((group) => <details className="gum-code-group" key={group.path} open>
                <summary title={group.path}><Icon name="folder" size={16} /><span>{group.name}</span></summary>
                {group.sessions.map((session) => <button type="button" key={session.id} className="gum-code-session" title={session.title || t('新会话', 'New conversation')} aria-current={session.id === state.selectedId ? "true" : undefined} disabled={locked} onClick={async () => {
                  if (!api) return;
                  await run(() => api.gumCodeSelect({ sessionId: session.id }));
                }}><span>{session.title || t('新会话', 'New conversation')}</span></button>)}
              </details>)}
              {!groups.length && <p className="gum-code-nav-empty">{t('选择工作区域，开始新对话', 'Select a folder to start a conversation')}</p>}
            </div>
            <button type="button" className="gum-code-nav-folder" disabled={locked} onClick={() => void changeWorkspace("__pick__")}><Icon name="folder" size={15} />{t('选择项目文件夹…', 'Select project folder…')}</button>
          </div>
          <div>{titleEdit?.sessionId === selected?.id && titleEdit ? <input className="gum-code-title-input" autoFocus aria-label={t('对话标题', 'Conversation title')} maxLength={100} value={titleEdit.value} disabled={pending} onChange={event => setTitleEdit({ ...titleEdit, value: event.target.value })} onBlur={() => void saveTitle()} onKeyDown={event => { if (event.nativeEvent.isComposing) return; if (event.key === 'Escape') { event.preventDefault(); setTitleEdit(null); } if (event.key === 'Enter') { event.preventDefault(); void saveTitle(); } }} /> : <h2 tabIndex={selected ? 0 : undefined} title={t('双击修改标题', 'Double-click to rename')} onDoubleClick={() => selected && setTitleEdit({ sessionId: selected.id, value: selected.title })} onKeyDown={event => { if (selected && event.key === 'Enter') setTitleEdit({ sessionId: selected.id, value: selected.title }); }}>{selected?.title || t('新会话', 'New conversation')}</h2>}</div>
          {(!available || !ready) && <span className="gum-code-status" role="status">{!available ? t('不可用', 'Unavailable') : t('载入中', 'Loading')}</span>}
        </header>
        {(!available || error || state.error) && <div className="gum-code-error" role="alert">{!available ? "GuMi-Code 需要在已接入引擎的桌面工作台中使用。" : error || state.error}</div>}
        <div className="gum-code-history" ref={history} aria-label={t('对话记录', 'Conversation history')} tabIndex={0}>
          <div className="gum-code-history-content" ref={historyContent}>
          {!state.messages.length && <div className="gum-code-empty"><h3>{t('想在这个项目中做什么？', 'What would you like to do in this project?')}</h3></div>}
          {state.messages.map((message, index) => <Fragment key={`${state.selectedId}-${index}`}>
            <article className={`gum-code-message is-${message.role}`} aria-label={message.role === "user" ? t('你', 'You') : "GuMi-Code"}>
              <h3 className="sr-only">{message.role === "user" ? t('你', 'You') : "GuMi-Code"}</h3>
              {message.role === "assistant" ? <GumCodeMarkdown streaming={Boolean(message.streaming) && visible && !pageHidden && !reducedMotion}>{message.content}</GumCodeMarkdown> : <div className="gum-code-user-text">{message.content}</div>}
              {(message.delivery || message.interrupted) && <small className="gum-code-delivery">{message.interrupted ? t('输出已中断', 'Output interrupted') : message.delivery === "received" ? t('已接收', 'Received') : active ? t('待接收', 'Pending') : t('未接收', 'Not received')}</small>}
            </article>
          </Fragment>)}
          </div>
        </div>
        {state.approval && <section className="gum-code-approval" aria-labelledby="gum-code-approval-title">
          <h3 id="gum-code-approval-title"><span className="gum-code-approval-mark" aria-hidden="true">!</span>{state.approval.title}</h3><p>{state.approval.detail}</p>
          <div><button disabled={pending || state.status !== "waiting"} onClick={() => api && state.approval && void run(() => api.gumCodeApprove({ requestId: state.approval!.requestId, approved: false }))}>{t('拒绝', 'Deny')}</button><button className="gum-code-primary" disabled={pending || state.status !== "waiting"} onClick={() => api && state.approval && void run(() => api.gumCodeApprove({ requestId: state.approval!.requestId, approved: true }))}>{t('允许本次操作', 'Allow this operation')}</button></div>
        </section>}
        {!active && selected?.pendingInputs?.map(input => <div className="gum-code-pending-input" key={input.id}><span>{t("未接收", "Not received")}: {input.text}</span><button type="button" disabled={locked} onClick={() => api && void run(() => api.gumCodeSend({ sessionId: selected.id, text: input.text, pendingInputId: input.id }))}>{t('重新发送', 'Send again')}</button></div>)}
        <div className="gum-code-run-status"><GumCodeProgress state={state} visible={visible && !pageHidden} /></div>
        <form className="gum-code-composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <label className="sr-only" htmlFor="gum-code-input">{t('任务描述', 'Task description')}</label>{!active && previousInput && (error || state.error || (state.statusText && state.statusText !== "本轮已结束")) && <div className="gum-code-composer-label"><button type="button" disabled={Boolean(draft) || pending} title={draft ? t('先清空当前草稿，再取回上一条输入', 'Clear the draft before recovering the previous input') : t('取回后可编辑，不会自动发送', 'Recover for editing without sending')} onClick={() => setDrafts((current) => current[draftKey] ? current : { ...current, [draftKey]: previousInput })}>{t('取回上一条输入', 'Recover previous input')}</button></div>}
          <GumCodeInput key={draftKey} value={draft} disabled={!selected || !ready || !available}
            visible={visible && !pageHidden} loadSkills={api?.gumCodeSkills}
            onChange={(value) => setDrafts((current) => ({ ...current, [draftKey]: value }))}
            onSend={() => void send()} />
          {files.length > 0 && <div className="gum-code-attachments" aria-label={t('待发送附件', 'Attachments')}>
            {files.map(file => <span key={file.id}><Icon name="file" size={15} /><span title={file.name}>{file.name}</span><button type="button" disabled={pending} aria-label={`${t('移除附件', 'Remove attachment')} ${file.name}`} onClick={() => setAttachments(current => ({ ...current, [draftKey]: (current[draftKey] || []).filter(item => item.id !== file.id) }))}><Icon name="close" size={13} /></button></span>)}
          </div>}
          <div className="gum-code-composer-actions"><button type="button" className="gum-code-attach-button" aria-label={t('上传文件', 'Attach files')} title={t('添加文档、图片或文本文件（每个最大 20 MB）', 'Attach documents, images or text files (up to 20 MB each)')} disabled={locked || !selected || files.length >= 8} onClick={() => void attachFiles()}><Icon name="plus" size={21} /></button>
            <div className="gum-code-send-controls"><select aria-label={t('模型', 'Model')} title={selected ? t('切换模型将新建会话，保留原历史', 'Switching models starts a new conversation and keeps history') : t('选择模型', 'Choose model')} value={selectedProvider === "workbench" ? models[0]?.id || "workbench" : selectedProvider} disabled={locked || !models.length} onChange={(event) => void changeProvider(event.target.value)}>
              {!models.length && <option value="workbench">{t('请先在设置中配置 API', 'Configure API in Settings first')}</option>}
              {models.map((model) => <option key={model.id} value={model.id}>{model.model}</option>)}
              {selectedProvider !== "workbench" && !models.some((model) => model.id === selectedProvider) && <option value={selectedProvider}>{t('配置不可用', 'Configuration unavailable')}</option>}
            </select>
            {active ? <button type="button" className="gum-code-send-button" aria-label={t('停止', 'Stop')} title={state.status === "stopping" ? t('正在停止…', 'Stopping…') : t('停止不会撤销已经发生的文件修改', 'Stopping will not undo file changes')} disabled={pending || state.status === "stopping"} onClick={() => api && void run(() => api.gumCodeStop())}><svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" /></svg></button> : null}<button type="submit" className="gum-code-primary gum-code-send-button" aria-label={active ? t('追加需求', 'Add request') : t('发送', 'Send')} title={active ? t('加入当前任务', 'Add to current task') : t('发送', 'Send')} disabled={!ready || !available || pending || state.status === "stopping" || !selected || (!draft.trim() && !files.length) || skillLimitExceeded}><svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 19V5m-6 6 6-6 6 6" /></svg></button>
            </div>
          </div>
        </form>
      </div>
    </section>
  );
}
