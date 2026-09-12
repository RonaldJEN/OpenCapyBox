import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { X } from 'lucide-react';

import './FeedbackMessage.css';

export type FeedbackTone = 'success' | 'info' | 'warning' | 'error';

export const DEFAULT_FEEDBACK_AUTO_DISMISS_MS = 3000;

interface FeedbackMessageProps {
  children: ReactNode;
  className?: string;
  closeButtonClassName?: string;
  icon?: ReactNode;
  onDismiss?: () => void;
  tone: FeedbackTone;
  autoDismissMs?: number;
  /** Stable identity for feedback with rich content, or a new failed attempt. */
  messageKey?: unknown;
  id?: string;
  closeLabel?: string;
}

export default function FeedbackMessage({
  children,
  className = '',
  closeButtonClassName = '',
  icon,
  onDismiss,
  tone,
  autoDismissMs,
  messageKey,
  id,
  closeLabel = '关闭提示',
}: FeedbackMessageProps) {
  const resolvedAutoDismissMs = autoDismissMs
    ?? (tone === 'warning' ? 0 : DEFAULT_FEEDBACK_AUTO_DISMISS_MS);
  const feedbackKey = messageKey ?? children;
  const [dismissed, setDismissed] = useState<{ key: unknown } | null>(null);
  const visible = !dismissed || !Object.is(dismissed.key, feedbackKey);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startedAtRef = useRef(0);
  const remainingRef = useRef(resolvedAutoDismissMs);
  const dismissRef = useRef(() => {});

  useEffect(() => {
    setDismissed(null);
  }, [feedbackKey]);

  useEffect(() => {
    dismissRef.current = () => {
      setDismissed({ key: feedbackKey });
      onDismiss?.();
    };
  }, [feedbackKey, onDismiss]);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const startTimer = useCallback(() => {
    clearTimer();
    if (!visible || !resolvedAutoDismissMs || remainingRef.current <= 0) return;
    startedAtRef.current = Date.now();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      dismissRef.current();
    }, remainingRef.current);
  }, [clearTimer, resolvedAutoDismissMs, visible]);

  useEffect(() => {
    remainingRef.current = resolvedAutoDismissMs;
    startTimer();
    return clearTimer;
  }, [feedbackKey, clearTimer, resolvedAutoDismissMs, startTimer]);

  const pauseTimer = () => {
    if (timerRef.current === null) return;
    remainingRef.current = Math.max(0, remainingRef.current - (Date.now() - startedAtRef.current));
    clearTimer();
  };

  const resumeTimer = () => {
    if (!resolvedAutoDismissMs || timerRef.current !== null || remainingRef.current <= 0) return;
    startTimer();
  };

  if (!visible) return null;

  return (
    <div
      id={id}
      className={`feedback-message ${className}`.trim()}
      role={tone === 'error' ? 'alert' : 'status'}
      data-tone={tone}
      onMouseEnter={pauseTimer}
      onMouseLeave={resumeTimer}
      onFocusCapture={pauseTimer}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) resumeTimer();
      }}
    >
      {icon ? <span className="feedback-message__icon" aria-hidden="true">{icon}</span> : null}
      <span className="feedback-message__content">{children}</span>
      <button
        type="button"
        className={`feedback-message__close ${closeButtonClassName}`.trim()}
        aria-label={closeLabel}
        onClick={() => {
          clearTimer();
          dismissRef.current();
        }}
      >
        <X size={14} />
      </button>
    </div>
  );
}
