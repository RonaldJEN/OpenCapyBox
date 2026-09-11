import { useEffect, useRef, useState } from 'react';

const previewTail = (text: string) => Array.from(text.slice(-800).replace(/\s+/g, ' ').trim()).slice(-180).join('');

/** Sample the live tail at a steady cadence; incoming tokens never restart a debounce. */
export function LiveThinkingPreview({ text }: { text: string }) {
  const latest = useRef(text);
  const [fragment, setFragment] = useState(() => previewTail(text));
  useEffect(() => { latest.current = text; }, [text]);
  useEffect(() => {
    const timer = window.setInterval(() => setFragment(previewTail(latest.current)), 100);
    return () => window.clearInterval(timer);
  }, []);
  return <span className="chat-thinking-preview" aria-hidden="true" data-reading-ignore>
    <span key={fragment} className="chat-thinking-fragment">{fragment}</span>
  </span>;
}
