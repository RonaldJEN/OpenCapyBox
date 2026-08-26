import type { KeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react';

interface SessionFilesSplitterProps {
  containerRef: RefObject<HTMLDivElement | null>;
  chatRatio: number;
  onRatioChange: (ratio: number) => void;
  onStartEdgeCollapse?: () => void;
}

const MIN_CHAT_RATIO = 0;
const MAX_CHAT_RATIO = 100;

function clampRatio(value: number): number {
  return Math.min(MAX_CHAT_RATIO, Math.max(MIN_CHAT_RATIO, value));
}

export function SessionFilesSplitter({
  containerRef,
  chatRatio,
  onRatioChange,
  onStartEdgeCollapse,
}: SessionFilesSplitterProps) {
  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const separator = event.currentTarget;
    separator.setPointerCapture?.(event.pointerId);
    const startsAtLeftEdge = chatRatio <= MIN_CHAT_RATIO;
    const startX = event.clientX;
    let dragOwner: 'pending' | 'sidebar' | 'files' = startsAtLeftEdge ? 'pending' : 'files';
    let sidebarCollapsed = false;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      if (dragOwner === 'pending') {
        const deltaX = moveEvent.clientX - startX;
        if (Math.abs(deltaX) < 2) return;
        dragOwner = deltaX < 0 && onStartEdgeCollapse ? 'sidebar' : 'files';
      }
      if (dragOwner === 'sidebar') {
        if (!sidebarCollapsed) {
          sidebarCollapsed = true;
          onStartEdgeCollapse?.();
        }
        return;
      }
      const bounds = containerRef.current?.getBoundingClientRect();
      if (!bounds?.width) return;
      const nextRatio = ((moveEvent.clientX - bounds.left) / bounds.width) * 100;
      onRatioChange(clampRatio(nextRatio));
    };

    const finish = () => {
      separator.releasePointerCapture?.(event.pointerId);
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
    };

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', finish, { once: true });
    window.addEventListener('pointercancel', finish, { once: true });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    let nextRatio: number;
    if (event.key === 'Home') nextRatio = MIN_CHAT_RATIO;
    else if (event.key === 'End') nextRatio = MAX_CHAT_RATIO;
    else if (event.key === 'ArrowLeft' && chatRatio <= MIN_CHAT_RATIO && onStartEdgeCollapse) {
      event.preventDefault();
      onStartEdgeCollapse();
      return;
    } else if (event.key === 'ArrowLeft') nextRatio = chatRatio - 2;
    else if (event.key === 'ArrowRight') nextRatio = chatRatio + 2;
    else return;
    event.preventDefault();
    onRatioChange(clampRatio(nextRatio));
  };

  return (
    <div
      role="separator"
      aria-label="调整聊天和文件面板宽度"
      aria-orientation="vertical"
      aria-valuemin={MIN_CHAT_RATIO}
      aria-valuemax={MAX_CHAT_RATIO}
      aria-valuenow={Math.round(chatRatio)}
      data-edge={chatRatio <= MIN_CHAT_RATIO ? 'start' : chatRatio >= MAX_CHAT_RATIO ? 'end' : 'middle'}
      tabIndex={0}
      className="session-files-splitter"
      onPointerDown={handlePointerDown}
      onKeyDown={handleKeyDown}
    >
      <span aria-hidden="true" />
    </div>
  );
}
