import { useI18n } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { bridge, type ApiSettingsState } from './model';

export function ApiSettings() {
  const { t } = useI18n();
  const [saved, setSaved] = useState<ApiSettingsState | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const inFlight = useRef(false);
  useEffect(() => {
    let disposed = false;
    if (!window.gumi) { setError(t("请在桌面应用中配置 API。", "Configure the API in the desktop application.")); return; }
    bridge().apiSettings().then(result => {
      if (disposed) return;
      if ('error' in result) { setError(result.error); return; }
      setSaved(result.settings); setBaseUrl(result.settings.baseUrl); setModel(result.settings.model);
    }).catch(() => { if (!disposed) setError(t("无法读取 API 配置，请重新打开设置。", "Could not load API settings. Reopen settings.")); });
    return () => { disposed = true; };
  }, [reload]);
  const keyRequired = !saved?.hasKey || baseUrl.trim().replace(/\/+$/, '') !== saved.baseUrl;
  const changed = () => { setMessage(''); setError(''); };
  async function submit(action: 'save' | 'test') {
    if (!saved || inFlight.current) return;
    inFlight.current = true; setBusy(true); setMessage(''); setError('');
    try {
      const request = { baseUrl, model, apiKey, revision: saved.revision };
      if (action === 'save') {
        const result = await bridge().apiSettingsSave(request);
        if ('error' in result) { setError(result.error); return; }
        setSaved(result.settings); setBaseUrl(result.settings.baseUrl); setModel(result.settings.model); setApiKey('');
        setMessage(t("已保存", "Saved"));
      } else {
        const result = await bridge().apiSettingsTest(request);
        if ('error' in result) { setError(result.error); return; }
        setMessage(t("连接成功", "Connected"));
      }
    } catch { setError(t("API 配置操作未完成，请重试。", "The API operation failed. Please try again.")); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <form className="settings-form api-settings" aria-label={t("API 配置", "API configuration")} onSubmit={event => {
    event.preventDefault();
    void submit((event.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'test' ? 'test' : 'save');
  }}>
    <fieldset disabled={busy || !saved}>
      <label className="field-label">{t("接口地址", "API endpoint")}<input type="url" required maxLength={2048} placeholder="https://api.example.com/v1" autoComplete="off" spellCheck={false} value={baseUrl} onChange={event => { setBaseUrl(event.target.value); changed(); }} />
      </label>
      <label className="field-label">{t("模型名称", "Model name")}<input required maxLength={120} placeholder={t("填写服务商提供的模型名称", "Enter the model name provided by your service")} autoComplete="off" spellCheck={false} value={model} onChange={event => { setModel(event.target.value); changed(); }} />
      </label>
      <label className="field-label">API Key
        <input type="password" required={keyRequired} maxLength={4096} autoComplete="new-password" spellCheck={false} placeholder={saved?.hasKey ? (keyRequired ? t("地址已变更，请输入此接口的 Key", "Endpoint changed. Enter the key for this endpoint") : t("已设置，留空保留", "Already set. Leave empty to keep")) : t("输入 API Key", "Enter API key")} value={apiKey} onChange={event => { setApiKey(event.target.value); changed(); }} />
      </label>
      <div className="action-row">
        <button className="primary" type="submit" value="save">{t("保存", "Save")}</button>
        <button className="quiet-button" type="submit" value="test">{t("测试连接", "Test connection")}</button>
        {busy && <span role="status">{t("处理中…", "Processing…")}</span>}
      </div>
    </fieldset>
    {error && <><p role="alert">{error}</p><button className="quiet-button" type="button" disabled={busy || !window.gumi} onClick={() => { setSaved(null); setApiKey(''); setError(''); setMessage(''); setReload(value => value + 1); }}>{t("放弃编辑并重新载入", "Discard edits and reload")}</button></>}
    {message && <p role="status">{message}</p>}
  </form>;
}
