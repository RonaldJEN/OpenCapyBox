import {
  FileText,
  PanelRightClose,
  PanelRightOpen,
} from 'lucide-react';

const CONTROL_CLASS = [
  'inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-[4px]',
  'border border-transparent bg-transparent text-claude-muted',
  'transition-[background-color,border-color,color,transform] duration-150',
  'hover:bg-claude-hover hover:text-claude-text active:scale-95',
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45',
].join(' ');

interface SessionFilesButtonProps {
  open: boolean;
  onToggle: () => void;
}

export function SessionFilesButton({ open, onToggle }: SessionFilesButtonProps) {
  const label = '查看文件';
  return (
    <button
      type="button"
      data-session-files-trigger="true"
      aria-label={label}
      aria-expanded={open}
      aria-pressed={open}
      title={label}
      onClick={onToggle}
      className={`${CONTROL_CLASS} ${
        open
          ? 'border-claude-accent/35 bg-claude-accent/5 text-claude-accent'
          : ''
      }`}
    >
      <FileText size={16} strokeWidth={1.8} aria-hidden="true" />
    </button>
  );
}

interface ChatPaneButtonProps {
  filesOpen: boolean;
  onToggle: () => void;
}

export function ChatPaneButton({ filesOpen, onToggle }: ChatPaneButtonProps) {
  // The label describes the chat pane, matching AlphaPai: when files are
  // visible the action expands chat (closing the right pane); when files are
  // closed the action contracts chat (restoring the right pane).
  const label = filesOpen ? '展开面板' : '收起面板';
  const Icon = filesOpen ? PanelRightOpen : PanelRightClose;
  return (
    <button
      type="button"
      aria-label={label}
      aria-expanded={filesOpen}
      title={label}
      onClick={onToggle}
      className={CONTROL_CLASS}
    >
      <Icon size={15} strokeWidth={1.8} aria-hidden="true" />
    </button>
  );
}

interface SessionFilesExpandButtonProps {
  expanded: boolean;
  onToggle: () => void;
}

export function SessionFilesExpandButton({ expanded, onToggle }: SessionFilesExpandButtonProps) {
  const label = expanded ? '收起面板' : '展开面板';
  const Icon = expanded ? PanelRightOpen : PanelRightClose;
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={expanded}
      title={label}
      onClick={onToggle}
      className={CONTROL_CLASS}
    >
      <Icon size={15} strokeWidth={1.8} aria-hidden="true" />
    </button>
  );
}
