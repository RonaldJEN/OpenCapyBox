import {
  useRef,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { ChevronRight } from 'lucide-react';

export const DEFAULT_APP_SIDEBAR_WIDTH = 220;
export const APP_SIDEBAR_RAIL_WIDTH = 64;
const RAIL_REVEAL_DRAG_DISTANCE = 24;

interface AppSidebarProps {
  children: ReactNode;
  collapsed: boolean;
  boundaryClaimed?: boolean;
  mobileOpen?: boolean;
  userId: string;
  onCollapsedChange: (collapsed: boolean) => void;
}

export function AppSidebar({
  children,
  collapsed,
  boundaryClaimed = false,
  mobileOpen = false,
  userId,
  onCollapsedChange,
}: AppSidebarProps) {
  const shellRef = useRef<HTMLDivElement>(null);
  const renderedWidth = collapsed ? APP_SIDEBAR_RAIL_WIDTH : DEFAULT_APP_SIDEBAR_WIDTH;
  const userInitial = userId.trim().charAt(0).toUpperCase() || 'U';

  const applyWidth = (nextWidth: number) => {
    const bounded = Math.min(DEFAULT_APP_SIDEBAR_WIDTH, Math.max(0, nextWidth));
    if (collapsed) {
      if (bounded <= APP_SIDEBAR_RAIL_WIDTH + RAIL_REVEAL_DRAG_DISTANCE) return;
      onCollapsedChange(false);
      return;
    }
    if (bounded < DEFAULT_APP_SIDEBAR_WIDTH) {
      onCollapsedChange(true);
      return;
    }
    onCollapsedChange(false);
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.preventDefault();
    const separator = event.currentTarget;
    const pointerId = event.pointerId;
    separator.setPointerCapture?.(pointerId);

    const handlePointerMove = (moveEvent: PointerEvent) => {
      const bounds = shellRef.current?.getBoundingClientRect();
      if (!bounds) return;
      applyWidth(moveEvent.clientX - bounds.left);
    };
    const finish = () => {
      separator.releasePointerCapture?.(pointerId);
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
    };

    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', finish, { once: true });
    window.addEventListener('pointercancel', finish, { once: true });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Home') {
      event.preventDefault();
      onCollapsedChange(true);
      return;
    }
    if (event.key === 'End') {
      event.preventDefault();
      onCollapsedChange(false);
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      onCollapsedChange(true);
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      if (collapsed) onCollapsedChange(false);
    }
  };

  const expandSidebar = () => {
    onCollapsedChange(false);
  };

  const trapMobileDialogFocus = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!mobileOpen || event.key !== 'Tab') return;
    const content = shellRef.current?.firstElementChild;
    const focusable = Array.from(content?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])',
    ) || []);
    if (focusable.length === 0) {
      event.preventDefault();
      shellRef.current?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      ref={shellRef}
      data-testid="app-sidebar-shell"
      data-collapsed={collapsed ? 'true' : 'false'}
      role={mobileOpen ? 'dialog' : undefined}
      aria-modal={mobileOpen ? 'true' : undefined}
      aria-label={mobileOpen ? '会话与工作区' : undefined}
      tabIndex={mobileOpen ? -1 : undefined}
      onKeyDown={trapMobileDialogFocus}
      className={`${mobileOpen ? 'fixed inset-0 z-[140] flex' : 'relative hidden'} h-screen shrink-0 bg-claude-surface md:relative md:inset-auto md:z-auto md:flex`}
      style={{ width: mobileOpen ? '100%' : `${renderedWidth}px` }}
    >
      <div className={`h-full min-w-0 w-full overflow-hidden opacity-100 ${collapsed ? 'md:w-0 md:opacity-0' : 'md:w-full md:opacity-100'}`}>
        {children}
      </div>
      {collapsed && (
        <div className="pointer-events-none absolute inset-0 hidden flex-col items-center justify-between py-4 md:flex">
          <button
            type="button"
            onClick={expandSidebar}
            aria-label="从 Logo 展开左侧栏"
            title="展开左侧栏"
            className="pointer-events-auto flex h-10 w-10 items-center justify-center rounded-xl transition-colors hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
          >
            <img src="/logo.jpg" alt="OpenCapyBox" className="h-9 w-9 rounded-lg object-cover" />
          </button>
          <button
            type="button"
            onClick={expandSidebar}
            aria-label={`从用户 ${userId} 展开左侧栏`}
            title={`${userId} · 展开左侧栏`}
            className="pointer-events-auto flex h-10 w-10 items-center justify-center rounded-full bg-claude-accent text-xs font-bold text-white transition-[transform,box-shadow] hover:shadow-md active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
          >
            {userInitial}
          </button>
        </div>
      )}
      {!boundaryClaimed && (
        <div
          role="separator"
          aria-label="调整左侧栏宽度"
          aria-orientation="vertical"
          aria-valuemin={0}
          aria-valuemax={DEFAULT_APP_SIDEBAR_WIDTH}
          aria-valuenow={collapsed ? 0 : DEFAULT_APP_SIDEBAR_WIDTH}
          tabIndex={0}
          onPointerDown={handlePointerDown}
          onKeyDown={handleKeyDown}
          className="group absolute right-0 inset-y-0 z-10 hidden w-2 cursor-col-resize touch-none outline-none md:block"
        >
          <span className="absolute inset-y-0 right-0 w-px bg-claude-border transition-[width,background-color] group-hover:w-0.5 group-hover:bg-claude-accent/55 group-focus-visible:w-0.5 group-focus-visible:bg-claude-accent" />
        </div>
      )}
      {collapsed && (
        <button
          type="button"
          onClick={expandSidebar}
          aria-label="展开左侧栏"
          title="展开左侧栏"
          className="absolute right-0 top-16 z-[15] hidden h-8 w-8 translate-x-1/2 items-center justify-center rounded-full border border-claude-border bg-white text-claude-secondary shadow-sm transition-[background-color,color,transform,box-shadow] hover:bg-claude-hover hover:text-claude-text active:translate-x-1/2 active:scale-95 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 md:flex"
        >
          <ChevronRight size={16} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
