import { useEffect, useRef, useState } from "react";

// Reveal only received text, catching up within a short window even after a burst.
export function advanceStreamText(shown: string, target: string, elapsed: number) {
  if (!target.startsWith(shown)) return target;
  const count = Math.max(1, Math.ceil((target.length - shown.length) * Math.min(1, elapsed / 80)));
  let end = Math.min(target.length, shown.length + count);
  if (end < target.length && /[\uD800-\uDBFF]/.test(target[end - 1])) end++;
  return target.slice(0, end);
}

export function useStreamText(text: string, animate: boolean) {
  const [shown, setShown] = useState(text);
  const current = useRef(text);
  useEffect(() => {
    if (!animate || !text.startsWith(current.current)) {
      current.current = text;
      setShown(text);
      return;
    }
    let frame = 0;
    let last = performance.now();
    const tick = (now: number) => {
      if (now - last >= 32) {
        current.current = advanceStreamText(current.current, text, now - last);
        setShown(current.current);
        last = now;
      }
      if (current.current !== text) frame = requestAnimationFrame(tick);
    };
    if (current.current !== text) frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [text, animate]);
  return animate && text.startsWith(shown) ? shown : text;
}
