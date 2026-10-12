import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { memo, useMemo, useState } from "react";
import { useStreamText } from "./useStreamText";

function safeLink(value: string) {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

export const GumCodeMarkdown = memo(function GumCodeMarkdown({ children, streaming = false }: { children: string; streaming?: boolean }) {
  const [error, setError] = useState("");
  const text = useStreamText(children, streaming);
  const components = useMemo<NonNullable<React.ComponentProps<typeof Markdown>["components"]>>(() => ({
      a: ({ href, children }) => href ? <a href={href} onClick={(event) => {
        event.preventDefault();
        setError("");
        window.gumi?.open({ kind: "url", target: href }).catch(() => setError("链接未能打开。"));
      }}>{children}</a> : <span>{children}</span>,
      // Do not let model-generated images silently issue network/file requests.
      img: ({ src, alt }) => src ? <a href={src} onClick={(event) => {
        event.preventDefault();
        window.gumi?.open({ kind: "url", target: src }).catch(() => setError("图片链接未能打开。"));
      }}>{alt || "查看图片"}</a> : <span>{alt || "图片"}</span>,
      table: ({ children }) => <div className="gum-code-table"><table>{children}</table></div>,
    }), []);
  return <div className="gum-code-markdown">
    <Markdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={safeLink} components={components}>{text}</Markdown>
    {error && <p role="alert">{error}</p>}
  </div>;
});
