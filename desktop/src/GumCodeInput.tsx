import { useI18n } from './i18n';
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { GumCodeSkill } from "./gumCodeTypes";

type Props = {
  value: string;
  disabled: boolean;
  visible: boolean;
  loadSkills?: () => Promise<{ skills: GumCodeSkill[]; error?: string }>;
  onChange: (value: string) => void;
  onSend: () => void;
};

export function getSkillMentions(value: string) {
  return Array.from(value.matchAll(/(?<!\S)\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?![\p{L}\p{N}_.\/-])|(?<![\p{L}\p{N}_.-])\$([A-Za-z0-9][A-Za-z0-9._-]{0,127})(?![\p{L}\p{N}_.-])/gu))
    .map((match) => ({ name: match[1] ?? match[2], index: match.index, text: match[0] }));
}

export function GumCodeInput({ value, disabled, visible, loadSkills, onChange, onSend }: Props) {
  const { t } = useI18n();
  const input = useRef<HTMLTextAreaElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const composing = useRef(false);
  const [caret, setCaret] = useState(value.length);
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [skills, setSkills] = useState<GumCodeSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selectionNotice, setSelectionNotice] = useState("");
  const [retry, setRetry] = useState(0);
  const [highlight, setHighlight] = useState(0);
  const listId = useId();
  const token = value.slice(0, caret).match(/(?:^|\s)\/([^\s/]*)$/u);
  const query = token?.[1] ?? "";
  const open = focused && visible && !disabled && !dismissed && Boolean(token)
    && (!value[caret] || /\s/u.test(value[caret]));
  const matches = skills.filter((skill) => `${skill.name} ${skill.displayName} ${skill.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())).slice(0, 8);
  const selected = Math.min(highlight, Math.max(0, matches.length - 1));
  const knownNames = new Set(skills.map((skill) => skill.name));
  const allMentions = getSkillMentions(value);
  const selectedNames = Array.from(new Set(allMentions.map((mention) => mention.name)));
  const mentions = allMentions.filter((mention) => knownNames.has(mention.name));
  const selectedSkills = Array.from(new Set(mentions.map((mention) => mention.name)));
  const overLimit = selectedNames.length > 3;
  let textOffset = 0;
  const highlightedText = mentions.map((match) => {
    const start = match.index;
    const before = value.slice(textOffset, start);
    textOffset = start + match.text.length;
    return <span key={start}>{before}<mark className="gum-code-skill-mention">{match.text}</mark></span>;
  });

  function syncBackdrop() {
    const field = input.current;
    const layer = backdrop.current;
    if (!field || !layer) return;
    layer.style.width = `${field.clientWidth}px`;
    layer.style.height = `${field.clientHeight}px`;
    layer.scrollTop = field.scrollTop;
    layer.scrollLeft = field.scrollLeft;
  }

  useLayoutEffect(() => {
    syncBackdrop();
    const observer = new ResizeObserver(syncBackdrop);
    if (input.current) observer.observe(input.current);
    return () => observer.disconnect();
  }, []);
  useLayoutEffect(syncBackdrop, [value, skills, visible]);

  useEffect(() => {
    if (!visible) return;
    let stale = false;
    setLoading(true);
    setError("");
    Promise.resolve().then(() => {
      if (!loadSkills) throw new Error("unavailable");
      return loadSkills();
    }).then((result) => {
      if (!stale) {
        if (!result.error) setSkills(result.skills);
        setError(result.error ? t("技能列表读取失败", "Could not load skills") : "");
      }
    }).catch(() => { if (!stale) setError(t("技能列表读取失败", "Could not load skills")); })
      .finally(() => { if (!stale) setLoading(false); });
    return () => { stale = true; };
  }, [visible, open, loadSkills, retry]);

  useEffect(() => { setHighlight(0); }, [query]);
  useEffect(() => {
    if (open) document.getElementById(`${listId}-${selected}`)?.scrollIntoView({ block: "nearest" });
  }, [open, selected, listId]);

  useEffect(() => { setSelectionNotice(""); }, [value]);

  function removeSkill(name: string) {
    let next = "";
    let offset = 0;
    let position = caret;
    for (const mention of allMentions.filter((entry) => entry.name === name)) {
      next += value.slice(offset, mention.index);
      offset = mention.index + mention.text.length;
      position -= Math.max(0, Math.min(caret - mention.index, mention.text.length));
    }
    next += value.slice(offset);
    onChange(next);
    setCaret(position);
    setDismissed(true);
    requestAnimationFrame(() => {
      input.current?.focus();
      input.current?.setSelectionRange(position, position);
    });
  }

  function choose(skill: GumCodeSkill) {
    // The /query under the caret will be replaced, so it is not another selection.
    const otherNames = new Set(allMentions.filter((mention) => mention.index !== caret - query.length - 1).map((mention) => mention.name));
    otherNames.add(skill.name);
    if (otherNames.size > 3) { setSelectionNotice(t("每条消息最多使用 3 个技能，请先移除一个。", "Use up to 3 skills per message. Remove one first.")); return; }
    const start = caret - query.length - 1;
    const insertion = `/${skill.name} `;
    const next = value.slice(0, start) + insertion + value.slice(caret);
    if (next.length > 16000) { setSelectionNotice(t("输入已达长度上限", "Message length limit reached")); return; }
    onChange(next);
    setDismissed(true);
    const position = start + insertion.length;
    setCaret(position);
    requestAnimationFrame(() => {
      input.current?.focus();
      input.current?.setSelectionRange(position, position);
    });
  }

  return <div className="gum-code-input-wrap">
    {open && <div className="gum-code-skill-menu">
      <div className="gum-code-skill-heading"><span>{t("技能", "Skills")}</span><span>{t("↑↓ 选择 · Enter 确认", "↑↓ Select · Enter Confirm")}</span></div>
      <div id={listId} role="listbox" aria-label={t("技能选择", "Select a skill")} className="gum-code-skill-list">
        {!loading && !error && matches.map((skill, index) => <button type="button" role="option"
          key={skill.name} id={`${listId}-${index}`} aria-selected={index === selected} tabIndex={-1}
          onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setHighlight(index)} onClick={() => choose(skill)}>
          <span className="gum-code-skill-mark" aria-hidden="true">/</span>
          <span><strong>{skill.displayName}</strong><small>{skill.description}</small></span>
          {skill.displayName !== skill.name && <code>{skill.name}</code>}
        </button>)}
      </div>
      {(loading || error || !matches.length) && <div className="gum-code-skill-empty" role="status">
        {loading ? t("正在读取技能…", "Loading skills…") : error || (skills.length ? t("没有匹配的技能", "No matching skills") : t("暂无可用技能", "No available skills"))}
        {error && <button type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => setRetry((count) => count + 1)}>{t("重试", "Retry")}</button>}
      </div>}
    </div>}
    <div ref={backdrop} className="gum-code-input-backdrop" aria-hidden="true">{highlightedText}{value.slice(textOffset)}{"\u200b"}</div>
    <textarea ref={input} id="gum-code-input" rows={3} maxLength={16000} value={value}
      onScroll={syncBackdrop}
      placeholder={disabled ? t("先新建或选择会话", "Create or select a conversation first") : t("描述任务，输入 / 选择技能…", "Describe a task, type / to choose skills…")} disabled={disabled}
      role="combobox" aria-autocomplete="list" aria-expanded={open} aria-controls={open ? listId : undefined}
      aria-activedescendant={open && !loading && !error && matches.length ? `${listId}-${selected}` : undefined}
      onFocus={() => { setFocused(true); setDismissed(false); }} onBlur={() => setFocused(false)}
      onSelect={(event) => setCaret(event.currentTarget.selectionStart)}
      onChange={(event) => { onChange(event.target.value); setCaret(event.target.selectionStart); setDismissed(false); }}
      onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
      onKeyDown={(event) => {
        if (composing.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
        if (open) {
          if (event.key === "Escape") { event.preventDefault(); setDismissed(true); return; }
          if ((event.key === "ArrowDown" || event.key === "ArrowUp") && matches.length) {
            event.preventDefault(); setHighlight((selected + (event.key === "ArrowDown" ? 1 : -1) + matches.length) % matches.length); return;
          }
          if ((event.key === "Enter" || event.key === "Tab") && !event.metaKey && !event.ctrlKey && !event.shiftKey && !loading && !error && matches.length) {
            event.preventDefault(); choose(matches[selected]); return;
          }
        }
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
          event.preventDefault();
          const field = event.currentTarget;
          const next = value.slice(0, field.selectionStart) + "\n" + value.slice(field.selectionEnd);
          if (next.length > field.maxLength) return;
          const position = field.selectionStart + 1;
          onChange(next);
          setCaret(position);
          setDismissed(true);
          requestAnimationFrame(() => input.current?.setSelectionRange(position, position));
        } else if (event.key === "Enter" && !event.shiftKey && !event.altKey) {
          event.preventDefault();
          if (!overLimit && !event.repeat) onSend();
        }
      }} />
    {selectedSkills.length > 0 && <div className="gum-code-selected-skills" aria-label={t("本轮技能", "Skills for this message")}>
      <span>{t("本轮技能", "Skills for this message")}</span>{selectedSkills.map((name) => <button type="button" key={name}
        aria-label={`${t('移除技能', 'Remove skill')} ${name}`} title={t("仅对当前消息生效，点击移除", "Applies to this message only. Click to remove")} disabled={disabled}
        onClick={() => removeSkill(name)}><span>/{name}</span><span aria-hidden="true">×</span></button>)}
    </div>}
    {(overLimit || selectionNotice) && <div className="gum-code-skill-limit" role="alert">{overLimit ? t("每条消息最多使用 3 个技能，请移除一个后发送。", "Use up to 3 skills per message. Remove one before sending.") : selectionNotice}</div>}
  </div>;
}
