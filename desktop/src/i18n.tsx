import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react';
import type { Commit, State } from './model';

export type Language = 'zh' | 'en';
const LanguageContext = createContext<Language>('zh');
const common: Record<string, string> = {
  '工作间': 'Workroom', '工作台导航': 'Workbench navigation', '设置': 'Settings',
  'GuMi-Code 设置': 'GuMi-Code settings', '语言设置': 'Language settings', '语言': 'Language',
  'API 配置': 'API configuration', '记忆': 'Memory', '工作区域': 'Workspace',
  '选择工作区域': 'Select workspace', '未分配项目': 'Unassigned project', '项目': 'Project',
  '新建工作': 'New work', '编辑工作': 'Edit work', '正在做': 'In progress',
  '下一步': 'Next step', '完成标准': 'Acceptance criteria', '尚未填写': 'Not filled in',
  '保存': 'Save', '取消': 'Cancel', '关闭': 'Close', '编辑': 'Edit', '删除': 'Delete',
  '已保存': 'Saved', '加载中…': 'Loading…', '处理中…': 'Processing…', '重试': 'Retry',
  '标记完成': 'Mark complete', '详细管理': 'Manage work', '笔记': 'Note', '文件': 'File',
  '链接': 'Link', '产出': 'Output', '进展': 'Progress', '完成记录': 'Completion record',
  '已完成': 'Completed', '待开始': 'Not started', '进行中': 'In progress', '未完成': 'Incomplete',
  '关键步骤': 'Key steps', '风险与依赖': 'Risks and dependencies', '建议': 'Suggestions', '待确认': 'To confirm',
  '开发与需求修改': 'Development and feature changes', '缺陷修复与排查': 'Bug fixes and investigation',
  '技术调研与方案设计': 'Technical research and design', '其他技术工作': 'Other technical work',
  '正在启动 GuMi-Code': 'Starting GuMi-Code', '正在处理': 'Processing', '正在等待确认': 'Waiting for approval',
  '正在停止…': 'Stopping…', '本轮已结束': 'Turn ended', '已停止': 'Stopped', '连接已关闭': 'Connection closed',
  '这项工作已不存在。': 'This work no longer exists.',
  '保存失败，请重试。': 'Could not save. Please try again.',
};

export function translate(language: Language, chinese: string, english?: string) {
  return language === 'en' ? english ?? common[chinese] ?? chinese : chinese;
}
export function LanguageProvider({ language = 'zh', children }: { language?: Language; children: ReactNode }) {
  return <LanguageContext.Provider value={language}>{children}</LanguageContext.Provider>;
}
export function useI18n() {
  const language = useContext(LanguageContext);
  const t = useCallback((chinese: string, english?: string) => translate(language, chinese, english), [language]);
  return { language, t };
}

export function LanguageSettings({ state, commit }: { state: State; commit: Commit }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const pending = useRef(false);
  async function change(language: Language) {
    if (pending.current || language === (state.settings.language || 'zh')) return;
    pending.current = true; setBusy(true); setError('');
    try { await commit(next => { next.settings.language = language; }); }
    catch { setError('语言保存失败，请重试。'); }
    finally { pending.current = false; setBusy(false); }
  }
  return <section className="language-settings">
    <h2>{t('语言设置')}</h2>
    <div className="settings-card"><div className="settings-row">{t('应用界面语言', 'Application language')}
      <span className="language-options" role="group" aria-label={t('应用界面语言', 'Application language')}>
        <button type="button" aria-pressed={(state.settings.language || 'zh') === 'zh'} disabled={busy} onClick={() => void change('zh')}>中文</button>
        <button type="button" aria-pressed={state.settings.language === 'en'} disabled={busy} onClick={() => void change('en')}>English</button>
      </span>
    </div></div>
    {busy && <p role="status">{t('正在保存…', 'Saving…')}</p>}
    {error && <p className="error-line" role="alert">{t(error, 'Could not save the language. Please try again.')}</p>}
  </section>;
}
