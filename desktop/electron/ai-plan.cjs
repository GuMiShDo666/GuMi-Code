'use strict';
const LIMIT = 64 * 1024;
function createAISettings({ resolveProvider }) {
  return async function loadSettings() {
    try {
      const settings = await resolveProvider('workbench');
      if (!settings || typeof settings.baseUrl !== 'string' || !settings.baseUrl ||
          typeof settings.model !== 'string' || !settings.model.trim() ||
          typeof settings.apiKey !== 'string' || !settings.apiKey.trim() || /[\p{Cc}\u2028\u2029]/u.test(settings.apiKey)) throw Error();
      return { baseUrl: settings.baseUrl, model: settings.model, apiKey: settings.apiKey };
    } catch (error) {
      if (error?.gumCodeSafe) throw error;
      throw Error('API 配置或凭据不可用，请检查设置与钥匙串授权。');
    }
  };
}
function createAICompletion({ fetchImpl = fetch, timeoutMs = 60000, disableThinking = false, ...settingsOptions }) {
  const loadSettings = createAISettings(settingsOptions);
  let running = false;
  return async function complete(messages, maxTokens, signal) {
    if (running) throw Error('上一条 AI 请求尚未结束，请稍后重试。');
    running = true;
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) controller.abort();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const { baseUrl, model, apiKey } = await loadSettings();
      if (controller.signal.aborted) throw Error(signal?.aborted ? 'AI 请求已取消。' : 'AI 请求超时，请重试。');
      let response;
      try {
        response = await fetchImpl(`${baseUrl}/chat/completions`, {
          method: 'POST', redirect: 'error', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${apiKey}` },
          body: JSON.stringify({ model, messages, max_tokens: maxTokens, ...(disableThinking ? { thinking: { type: 'disabled' } } : {}) }),
        });
      } catch { throw Error(controller.signal.aborted ? 'AI 请求超时，请重试。' : 'AI 服务连接失败，请稍后重试。'); }
      if (!response.ok) {
        try { await response.body?.cancel(); } catch {}
        throw Error([401, 403].includes(response.status) ? 'AI 认证失败，请检查钥匙串中的凭据。' : 'AI 服务暂时不可用，请稍后重试。');
      }
      let raw = '';
      try {
        if (Number(response.headers.get('content-length')) > LIMIT || !response.body) { await response.body?.cancel(); throw Error(); }
        const reader = response.body.getReader();
        const chunks = []; let size = 0;
        try {
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > LIMIT) { await reader.cancel(); throw Error(); }
            chunks.push(value);
          }
        } finally { reader.releaseLock(); }
        raw = Buffer.concat(chunks).toString('utf8');
      } catch { throw Error(controller.signal.aborted ? 'AI 请求超时，请重试。' : 'AI 响应读取失败或过大，请重试。'); }
      let choice;
      try { choice = JSON.parse(raw).choices[0]; if (!choice || !choice.message) throw Error(); }
      catch { throw Error('AI 服务返回内容无法识别，请重试。'); }
      if (choice.finish_reason === 'length') {
        const error = Error('AI 输出达到长度上限，未返回完整内容，请减少素材后重试。');
        error.code = 'AI_OUTPUT_LIMIT';
        throw error;
      }
      if (choice.message.refusal || choice.finish_reason === 'content_filter') throw Error('AI 未能生成该内容，请调整素材后重试。');
      if (choice.finish_reason !== 'stop') throw Error('AI 生成未正常结束，请重试。');
      const content = choice.message.content;
      if (typeof content !== 'string' || !content.trim()) throw Error('AI 未返回正文，请重试。');
      return content;
    } finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); running = false; }
  };
}
module.exports = {createAICompletion};
