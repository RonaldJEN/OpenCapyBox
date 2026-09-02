import {
  useCallback,
  useEffect,
  useRef,
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
  onDismiss: () => void;
  tone: FeedbackTone;
  autoDismissMs?: number;
}

export default function FeedbackMessage({
  children,
  className = '',
  closeButtonClassName = '',
  icon,
  onDismiss,
  tone,
  autoDismissMs,
}: FeedbackMessageProps) {
  const resolvedAutoDismissMs = autoDismissMs
    ?? (tone === 'success' || tone === 'info' ? DEFAULT_FEEDBACK_AUTO_DISMISS_MS : 0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startedAtRef = useRef(0);
  const remainingRef = useRef(resolvedAutoDismissMs);
  const dismissRef = useRef(onDismiss);

  useEffect(() => {
    dismissRef.current = onDismiss;
  }, [onDismiss]);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const startTimer = useCallback(() => {
    clearTimer();
    if (!resolvedAutoDismissMs || remainingRef.current <= 0) return;
    startedAtRef.current = Date.now();
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      dismissRef.current();
    }, remainingRef.current);
  }, [clearTimer, resolvedAutoDismissMs]);

  useEffect(() => {
    remainingRef.current = resolvedAutoDismissMs;
    startTimer();
    return clearTimer;
  }, [children, clearTimer, resolvedAutoDismissMs, startTimer]);

  const pauseTimer = () => {
    if (timerRef.current === null) return;
    remainingRef.current = Math.max(0, remainingRef.current - (Date.now() - startedAtRef.current));
    clearTimer();
  };

  const resumeTimer = () => {
    if (!resolvedAutoDismissMs || timerRef.current !== null || remainingRef.current <= 0) return;
    startTimer();
  };

  return (
    <div
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
        aria-label="关闭提示"
        onClick={onDismiss}
      >
        <X size={14} />
      </button>
    </div>
  );
}
