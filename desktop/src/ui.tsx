import React, { useEffect, useRef } from 'react';
import { useI18n } from './i18n';
export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const shapes: Record<string, React.ReactNode> = {
    folder: <path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v2M3 7h17a2 2 0 0 1 2 2l-3 10H3a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1Z" />,
    connection: <><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M12 10v4"/></>,
    memory: <><rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/><path d="M10 10h4v4h-4z"/></>,
    person: <><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/></>,
    globe: <><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></>,
    code: <><path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-15-2 18" /></>,
    sun: (
      <>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12l1 1M5 19l1-1M18 6l1-1" />
      </>
    ),
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="2" />
        <rect x="14" y="3" width="7" height="7" rx="2" />
        <rect x="3" y="14" width="7" height="7" rx="2" />
        <rect x="14" y="14" width="7" height="7" rx="2" />
      </>
    ),
    inbox: (
      <>
        <path d="m5 4-3 11v5h20v-5L19 4Z" />
        <path d="M2 15h6l2 3h4l2-3h6" />
      </>
    ),
    history: (
      <>
        <path d="M4 8a9 9 0 1 1-1 8M4 3v5h5" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    settings: (
      <>
        <path d="m9 3 1-1h4l1 1 .5 2 2 1 2-.5 2 3-.8 2v3l.8 2-2 3-2-.5-2 1-.5 2-1 1h-4l-1-1-.5-2-2-1-2 .5-2-3 .8-2v-3L2 8.5l2-3 2 .5 2-1Z" />
        <circle cx="12" cy="12" r="3" />
      </>
    ),
    search: (
      <>
        <circle cx="10" cy="10" r="6" />
        <path d="m15 15 5 5" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    arrow: <path d="m9 5 7 7-7 7" />,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    file: (
      <>
        <path d="M6 3h8l4 4v14H6Z" />
        <path d="M14 3v5h4M9 12h6M9 16h4" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    board: (
      <>
        <rect x="3" y="4" width="5" height="16" rx="1" />
        <rect x="10" y="4" width="5" height="11" rx="1" />
        <path d="M18 4h3v16h-3" />
      </>
    ),
    list: (
      <>
        <path d="M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {shapes[name] || shapes.file}
    </svg>
  );
}
export function Modal({
  title,
  onClose,
  children,
  initialFocus,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  initialFocus?: string;
}) {
  const { t } = useI18n();
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current;
    dialog?.showModal();
    dialog?.querySelector<HTMLElement>(initialFocus || "input, textarea, select")?.focus();
    return () => dialog?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-label={title}
      onCancel={event => { event.preventDefault(); onClose(); }}
      onClick={(e) => {
        if (e.target === ref.current) {
          const r = ref.current.getBoundingClientRect();
          if (
            e.clientX < r.left ||
            e.clientX > r.right ||
            e.clientY < r.top ||
            e.clientY > r.bottom
          )
            onClose();
        }
      }}
    >
      <div className="modal-head">
        <h2>{title}</h2>
        <button aria-label={t("关闭", "Close")} className="icon-button" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      {children}
    </dialog>
  );
}
