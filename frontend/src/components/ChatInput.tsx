import {
  useCallback,
  useRef,
  useEffect,
  useMemo,
  useState,
  type MutableRefObject,
  type ReactNode,
} from 'react';
import {
  ArrowLeft,
  ArrowUp,
  BookOpenCheck,
  Check,
  ChevronRight,
  Database,
  FileUp,
  FolderTree,
  Loader2,
  Plus,
  Search,
  Square,
  X,
} from 'lucide-react';
import { FileInfo, type PreferredMcpConnectionSnapshot } from '../types';
import type { WorkspaceEntry } from '../types/workspace';
import { getFileIcon, getFileExtLabel, getFileBadgeClass, getFileIconClass, isImageFile } from '../utils/fileUtils';
import {
  getSkills,
  type SkillInfo,
  type SkillInventoryState,
} from '../services/configApi';
import { getMcpServers, type McpServer } from '../services/mcpApi';
import {
  MAX_SELECTED_MCP_SERVERS,
  MAX_SELECTED_SKILLS,
} from '../utils/turnPreferenceDrafts';
import { WorkspaceFilePicker } from './workspace/WorkspaceFilePicker';

const MAX_TEXTAREA_HEIGHT = 200;
type AddMenuPanel = null | 'root' | 'workspace' | 'skills' | 'mcp';

const skillKey = (skill: SkillInfo) => skill.key || skill.name;
const skillDisplayName = (skill: SkillInfo) => skill.display_name || skill.name || skillKey(skill);
const normalizedSkillLabel = (value: string) => value.trim().replace(/\s+/g, ' ').toLowerCase();
const shouldShowSkillKey = (skill: SkillInfo) => (
  normalizedSkillLabel(skillDisplayName(skill)) !== normalizedSkillLabel(skillKey(skill))
);

interface ChatInputProps {
  /** 当前输入文本 */
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  /** 停止生成回调，传入后发送中按钮变为可点击的 Stop */
  onStop?: () => void;

  /** 是否禁用（发送中 / 创建会话中） */
  disabled?: boolean;
  /** 仅禁用发送动作，输入框仍保持可编辑 */
  sendDisabled?: boolean;
  /** 发送按钮 loading 文案，为空时显示箭头 */
  sendingLabel?: string;

  placeholder?: string;
  /** 进入欢迎页时将键盘焦点放到输入框 */
  autoFocus?: boolean;
  /** 允许组合控件在完成选择后把焦点交还给输入框。 */
  textareaRef?: MutableRefObject<HTMLTextAreaElement | null>;

  // ---- 文件上传 ----
  attachedFiles?: FileInfo[];
  onRemoveAttachment?: (index: number) => void;
  onFileUpload?: (files: FileList | File[] | null) => void;
  onWorkspaceFilesSelected?: (entries: WorkspaceEntry[]) => void;
  onInputDropHandled?: () => void;
  onPreviewAttachment?: (file: FileInfo) => void;
  uploading?: boolean;

  // ---- 本轮 Skill 偏好 ----
  selectedSkillKeys?: string[];
  onSelectedSkillKeysChange?: (keys: string[]) => void;

  // ---- 本轮 MCP 数据连接偏好 ----
  selectedMcpConnections?: PreferredMcpConnectionSnapshot[];
  onSelectedMcpConnectionsChange?: (
    connections: PreferredMcpConnectionSnapshot[]
  ) => void;

  // ---- 模型与本轮推理等级 ----
  modelControl?: ReactNode;

  // ---- 输入代理 ----
  onInputChangeRaw?: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  onFileSelected?: (file: FileInfo, newInputValue: string) => void;
}

/**
 * Claude-style chat input — capsule shape, arrow send button
 */
export function ChatInput({
  value,
  onChange,
  onSend,
  onStop,
  disabled = false,
  sendDisabled = false,
  sendingLabel,
  placeholder = '输入消息...',
  autoFocus = false,
  textareaRef: externalTextareaRef,
  attachedFiles = [],
  onRemoveAttachment,
  onFileUpload,
  onWorkspaceFilesSelected,
  onInputDropHandled,
  onPreviewAttachment,
  uploading = false,
  selectedSkillKeys = [],
  onSelectedSkillKeysChange,
  selectedMcpConnections = [],
  onSelectedMcpConnectionsChange,
  modelControl,
  onInputChangeRaw,
}: ChatInputProps) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const setTextareaRef = useCallback((node: HTMLTextAreaElement | null) => {
    textareaRef.current = node;
    if (externalTextareaRef) externalTextareaRef.current = node;
  }, [externalTextareaRef]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [previewImage, setPreviewImage] = useState<{ src: string; name: string } | null>(null);
  const [isInputDragging, setIsInputDragging] = useState(false);
  const [addMenuPanel, setAddMenuPanel] = useState<AddMenuPanel>(null);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [skillsError, setSkillsError] = useState('');
  const [skillsInventoryState, setSkillsInventoryState] = useState<SkillInventoryState | null>(null);
  const [skillsLoadRevision, setSkillsLoadRevision] = useState(0);
  const [skillQuery, setSkillQuery] = useState('');
  const skillsLoadedRef = useRef(false);
  const skillsRequestRef = useRef<ReturnType<typeof getSkills> | null>(null);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpLoading, setMcpLoading] = useState(false);
  const [mcpLoaded, setMcpLoaded] = useState(false);
  const [mcpError, setMcpError] = useState('');
  const [mcpLoadRevision, setMcpLoadRevision] = useState(0);
  const [mcpQuery, setMcpQuery] = useState('');
  const mcpLoadedRef = useRef(false);
  const mcpRequestRef = useRef<ReturnType<typeof getMcpServers> | null>(null);
  const addMenuRef = useRef<HTMLDivElement>(null);
  const addMenuTriggerRef = useRef<HTMLButtonElement>(null);
  const rootMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    if (addMenuPanel !== 'skills') return;
    let active = true;
    const hadLoadedSkills = skillsLoadedRef.current;
    setSkillsLoading(true);
    setSkillsError('');
    const request = skillsRequestRef.current ?? getSkills();
    skillsRequestRef.current = request;
    void request
      .then((response) => {
        if (active) {
          setSkills(response.skills.filter((skill) => skill.enabled));
          setSkillsInventoryState(response.inventory_state ?? null);
          skillsLoadedRef.current = true;
          setSkillsLoaded(true);
        }
      })
      .catch(() => {
        if (active) {
          setSkillsError(hadLoadedSkills
            ? 'Skill 列表刷新失败，已显示上次结果'
            : 'Skill 列表加载失败');
        }
      })
      .finally(() => {
        if (skillsRequestRef.current === request) skillsRequestRef.current = null;
        if (active) setSkillsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [addMenuPanel, skillsLoadRevision]);

  useEffect(() => {
    if (addMenuPanel !== 'mcp') return;
    let active = true;
    const hadLoadedServers = mcpLoadedRef.current;
    setMcpLoading(true);
    setMcpError('');
    const request = mcpRequestRef.current ?? getMcpServers();
    mcpRequestRef.current = request;
    void request
      .then((servers) => {
        if (!active) return;
        setMcpServers(servers.filter((server) => (
          server.enabled
          && server.installation_id !== null
          && server.enabled_tools_count > 0
        )));
        mcpLoadedRef.current = true;
        setMcpLoaded(true);
      })
      .catch(() => {
        if (!active) return;
        setMcpError(hadLoadedServers
          ? '数据连接刷新失败，已显示上次结果'
          : '数据连接加载失败');
      })
      .finally(() => {
        if (mcpRequestRef.current === request) mcpRequestRef.current = null;
        if (active) setMcpLoading(false);
      });
    return () => {
      active = false;
    };
  }, [addMenuPanel, mcpLoadRevision]);

  useEffect(() => {
    if (!addMenuPanel) return;
    const closeOnOutside = (event: MouseEvent) => {
      if (!addMenuRef.current?.contains(event.target as Node)) setAddMenuPanel(null);
    };
    const closeOnFocusOutside = (event: FocusEvent) => {
      if (!addMenuRef.current?.contains(event.target as Node)) setAddMenuPanel(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setAddMenuPanel(null);
      addMenuTriggerRef.current?.focus();
    };
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('focusin', closeOnFocusOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutside);
      document.removeEventListener('focusin', closeOnFocusOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [addMenuPanel]);

  useEffect(() => {
    if (disabled) setAddMenuPanel(null);
  }, [disabled]);

  useEffect(() => {
    if (addMenuPanel !== 'root') return;
    rootMenuRef.current
      ?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')
      ?.focus();
  }, [addMenuPanel]);

  const filteredSkills = useMemo(() => {
    const query = skillQuery.trim().toLocaleLowerCase();
    if (!query) return skills;
    return skills.filter((skill) => [
      skillDisplayName(skill),
      skill.name,
      skillKey(skill),
      skill.description,
    ].some((value) => value.toLocaleLowerCase().includes(query)));
  }, [skillQuery, skills]);

  const skillByKey = useMemo(
    () => new Map(skills.map((skill) => [skillKey(skill), skill])),
    [skills],
  );

  const filteredMcpServers = useMemo(() => {
    const query = mcpQuery.trim().toLocaleLowerCase();
    if (!query) return mcpServers;
    return mcpServers.filter((server) => [
      server.name,
      server.description,
      server.id,
    ].some((value) => value.toLocaleLowerCase().includes(query)));
  }, [mcpQuery, mcpServers]);

  const mcpById = useMemo(
    () => new Map(mcpServers.map((server) => [server.id, server])),
    [mcpServers],
  );
  const selectedMcpServerIds = useMemo(
    () => selectedMcpConnections.map((connection) => connection.server_id),
    [selectedMcpConnections],
  );

  const toggleSkill = (key: string) => {
    if (disabled || !onSelectedSkillKeysChange) return;
    if (selectedSkillKeys.includes(key)) {
      onSelectedSkillKeysChange(selectedSkillKeys.filter((item) => item !== key));
      return;
    }
    if (selectedSkillKeys.length >= MAX_SELECTED_SKILLS) return;
    onSelectedSkillKeysChange([...selectedSkillKeys, key]);
  };

  const toggleMcpServer = (serverId: string) => {
    if (disabled || !onSelectedMcpConnectionsChange) return;
    if (selectedMcpServerIds.includes(serverId)) {
      onSelectedMcpConnectionsChange(
        selectedMcpConnections.filter((item) => item.server_id !== serverId),
      );
      return;
    }
    if (selectedMcpServerIds.length >= MAX_SELECTED_MCP_SERVERS) return;
    const server = mcpById.get(serverId);
    if (!server) return;
    onSelectedMcpConnectionsChange([
      ...selectedMcpConnections,
      { server_id: server.id, display_name: server.name },
    ]);
  };

  const handleRootMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    const items = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ),
    );
    if (items.length === 0) return;
    event.preventDefault();
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    const nextIndex = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? items.length - 1
        : event.key === 'ArrowUp'
          ? (currentIndex <= 0 ? items.length - 1 : currentIndex - 1)
          : (currentIndex + 1) % items.length;
    items[nextIndex].focus();
  };

  // 自动调整 textarea 高度
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    textarea.style.height = 'auto';
    const scrollHeight = textarea.scrollHeight;
    const hasOverflow = scrollHeight > MAX_TEXTAREA_HEIGHT;
    textarea.style.height = `${Math.min(scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
    textarea.style.overflowY = hasOverflow ? 'auto' : 'hidden';

    if (!hasOverflow) {
      textarea.scrollTop = 0;
    }
  }, [value]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (sendDisabled) return;
      onSend();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    if (onInputChangeRaw) {
      onInputChangeRaw(e);
    } else {
      onChange(e.target.value);
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!onFileUpload) return;

    const items = Array.from(e.clipboardData.items || []);
    const files = items
      .filter((item) => item.kind === 'file')
      .map((item) => item.getAsFile())
      .filter((file): file is File => !!file);

    if (files.length > 0) {
      e.preventDefault();
      onFileUpload(files);
    }
  };

  const handleDragOverInput = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.types.includes('Files')) {
      setIsInputDragging(true);
    }
  };

  const handleDragLeaveInput = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    const rect = (e.currentTarget as HTMLDivElement).getBoundingClientRect();
    if (
      e.clientX <= rect.left ||
      e.clientX >= rect.right ||
      e.clientY <= rect.top ||
      e.clientY >= rect.bottom
    ) {
      setIsInputDragging(false);
    }
  };

  const handleDropInput = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    setIsInputDragging(false);
    onInputDropHandled?.();
    if (!onFileUpload) return;

    const files = e.dataTransfer.files;
    if (files && files.length > 0) {
      onFileUpload(files);
    }
  };

  const hasContent = value.trim().length > 0 || attachedFiles.length > 0;
  const canSend = hasContent && !disabled && !sendDisabled;

  return (
    <div className="bg-claude-bg px-4 pb-5 pt-3 md:px-8">
      <div data-testid="chat-input-column" className="mx-auto w-full max-w-5xl">
        {/* 附件列表 */}
        {attachedFiles.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {attachedFiles.map((file, index) => (
              <div key={index} className="relative">
                <button
                  type="button"
                  onClick={() => {
                    if (onPreviewAttachment) {
                      onPreviewAttachment(file);
                      return;
                    }
                    if (isImageFile(file) && file.data_url) {
                      setPreviewImage({ src: file.data_url, name: file.name });
                    }
                  }}
                  className="group relative w-24 h-20 rounded-xl overflow-hidden border border-claude-border bg-white hover:border-claude-border-strong transition-colors"
                  title={`预览 ${file.name}`}
                >
                  <div className={`absolute top-1.5 right-1.5 text-[9px] px-1.5 py-0.5 rounded-md uppercase tracking-wide z-10 ${getFileBadgeClass(file)}`}>
                    {getFileExtLabel(file)}
                  </div>
                  {isImageFile(file) && file.data_url ? (
                    <img
                      src={file.data_url}
                      alt={file.name}
                      className="w-full h-full object-cover transition-transform group-hover:scale-105"
                    />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center bg-claude-surface">
                      {(() => {
                        const Icon = getFileIcon(file);
                        return <Icon className={`w-6 h-6 ${getFileIconClass(file)}`} />;
                      })()}
                    </div>
                  )}
                  <div className="absolute inset-x-0 bottom-0 bg-black/55 text-white text-[10px] px-1.5 py-1 truncate">
                    {file.name}
                  </div>
                </button>

                {onRemoveAttachment && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRemoveAttachment(index);
                    }}
                    className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-white border border-claude-border text-claude-muted hover:text-claude-error flex items-center justify-center shadow-sm"
                    aria-label={`移除 ${file.name}`}
                  >
                    <X className="w-3 h-3" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* 输入框容器 — 膠囊形 */}
        <div className="relative">
          <div
            onDragOver={handleDragOverInput}
            onDragLeave={handleDragLeaveInput}
            onDrop={handleDropInput}
            className={`flex flex-col bg-white rounded-3xl border transition-[border-color,box-shadow,background-color] duration-200 ${
            hasContent
              ? 'border-claude-border-strong shadow-md ring-2 ring-claude-accent/10'
              : 'border-claude-border shadow-sm hover:border-claude-border-strong'
          } ${isInputDragging ? 'ring-2 ring-claude-accent/25 border-claude-accent/50 bg-claude-accent/5' : ''}`}
          >
            {(selectedSkillKeys.length > 0 || selectedMcpServerIds.length > 0) && (
              <div className="flex flex-wrap gap-1.5 px-3 pt-3" aria-label="已选择本轮偏好">
                {selectedSkillKeys.map((key) => (
                  <span
                    key={key}
                    className="inline-flex max-w-full items-center gap-1 rounded-full border border-claude-accent/30 bg-claude-accent/10 px-2.5 py-1 text-xs text-claude-secondary"
                  >
                    <span className="truncate">{skillByKey.get(key) ? skillDisplayName(skillByKey.get(key)!) : key}</span>
                    <button
                      type="button"
                      onClick={() => toggleSkill(key)}
                      disabled={disabled}
                      className="rounded-full p-0.5 hover:bg-claude-accent/15 disabled:opacity-50"
                      aria-label={`移除 Skill ${skillByKey.get(key) ? skillDisplayName(skillByKey.get(key)!) : key}`}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
                {selectedMcpConnections.map((connection) => {
                  const serverId = connection.server_id;
                  const label = connection.display_name || serverId;
                  return (
                    <span
                      key={serverId}
                      className="inline-flex max-w-full items-center gap-1 rounded-full border border-[#cfe0d2] bg-[#eef7f0] px-2.5 py-1 text-xs text-[#4d795d]"
                    >
                      <span className="truncate">{label}</span>
                      <button
                        type="button"
                        onClick={() => toggleMcpServer(serverId)}
                        disabled={disabled}
                        className="rounded-full p-0.5 hover:bg-[#dceade] disabled:opacity-50"
                        aria-label={`移除数据连接 ${label}`}
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  );
                })}
              </div>
            )}

            {/* textarea */}
            <textarea
              ref={setTextareaRef}
              value={value}
              onChange={handleChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              placeholder={placeholder}
              disabled={disabled}
              rows={1}
              className="w-full bg-transparent border-none focus:ring-0 text-[15px] py-3.5 pl-4 pr-14 resize-none max-h-[200px] placeholder:text-claude-muted disabled:opacity-50 disabled:cursor-not-allowed outline-none text-claude-text"
            />

            {/* 底部工具栏 */}
            <div className="flex items-center justify-between px-3 pb-2">
              <div className="flex items-center gap-1">
                {(onFileUpload
                  || onWorkspaceFilesSelected
                  || onSelectedSkillKeysChange
                  || onSelectedMcpConnectionsChange) && (
                  <div ref={addMenuRef} className="relative">
                    {onFileUpload && (
                      <input
                        ref={fileInputRef}
                        type="file"
                        multiple
                        className="hidden"
                        onChange={(event) => {
                          onFileUpload(event.target.files);
                          event.target.value = '';
                        }}
                      />
                    )}
                    <button
                      ref={addMenuTriggerRef}
                      type="button"
                      onClick={() => setAddMenuPanel((panel) => (panel ? null : 'root'))}
                      disabled={disabled}
                      className={`flex h-8 w-8 items-center justify-center rounded-lg transition-[color,background-color,opacity] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-not-allowed disabled:opacity-50 ${
                        addMenuPanel
                          ? 'bg-claude-hover text-claude-secondary'
                          : 'text-claude-muted hover:bg-claude-hover hover:text-claude-secondary'
                      }`}
                      aria-label="添加内容"
                      aria-haspopup="menu"
                      aria-expanded={Boolean(addMenuPanel)}
                      aria-controls={addMenuPanel ? 'composer-add-menu' : undefined}
                      title="添加内容"
                    >
                      <Plus className="h-5 w-5" />
                    </button>

                    {addMenuPanel && (
                      <div
                        id="composer-add-menu"
                        role={addMenuPanel === 'root' ? undefined : 'dialog'}
                        aria-labelledby={addMenuPanel === 'skills'
                          ? 'composer-skill-picker-title'
                          : addMenuPanel === 'mcp'
                            ? 'composer-mcp-picker-title'
                            : addMenuPanel === 'workspace'
                              ? 'composer-workspace-picker-title'
                            : undefined}
                        className={`fixed inset-x-3 bottom-3 z-[120] max-h-[70vh] overflow-hidden rounded-2xl border border-claude-border bg-white shadow-2xl md:absolute md:inset-x-auto md:bottom-full md:left-0 md:mb-2 ${
                          addMenuPanel === 'root' ? 'md:w-[17rem]' : 'md:w-[24rem]'
                        }`}
                      >
                        {addMenuPanel === 'root' && (
                          <div
                            ref={rootMenuRef}
                            role="menu"
                            aria-label="添加内容"
                            className="p-2"
                            onKeyDown={handleRootMenuKeyDown}
                          >
                            {onWorkspaceFilesSelected && (
                              <button
                                type="button"
                                role="menuitem"
                                onClick={() => setAddMenuPanel('workspace')}
                                className="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
                              >
                                <FolderTree className="h-5 w-5 text-[#35658c]" />
                                <span className="flex-1">工作区文件</span>
                                <ChevronRight className="h-4 w-4 text-claude-muted" />
                              </button>
                            )}
                            {onFileUpload && (
                              <button
                                type="button"
                                role="menuitem"
                                disabled={uploading}
                                onClick={() => {
                                  setAddMenuPanel(null);
                                  fileInputRef.current?.click();
                                }}
                                className="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                {uploading
                                  ? <Loader2 className="h-5 w-5 animate-spin text-claude-secondary" />
                                  : <FileUp className="h-5 w-5 text-claude-secondary" />}
                                <span className="flex-1">上传文件</span>
                              </button>
                            )}
                            {onSelectedSkillKeysChange && (
                              <button
                                type="button"
                                role="menuitem"
                                onClick={() => {
                                  setSkillQuery('');
                                  setAddMenuPanel('skills');
                                }}
                                className="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
                              >
                                <BookOpenCheck className="h-5 w-5 text-claude-secondary" />
                                <span className="flex-1">专家 Skills</span>
                                {selectedSkillKeys.length > 0 && (
                                  <span className="text-xs text-claude-muted">{selectedSkillKeys.length}</span>
                                )}
                                <ChevronRight className="h-4 w-4 text-claude-muted" />
                              </button>
                            )}
                            {onSelectedMcpConnectionsChange && (
                              <button
                                type="button"
                                role="menuitem"
                                onClick={() => {
                                  setMcpQuery('');
                                  setAddMenuPanel('mcp');
                                }}
                                className="flex min-h-11 w-full items-center gap-3 rounded-xl px-3 py-2 text-left text-sm text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
                              >
                                <Database className="h-5 w-5 text-[#4d795d]" />
                                <span className="flex-1">数据连接</span>
                                {selectedMcpServerIds.length > 0 && (
                                  <span className="text-xs text-claude-muted">{selectedMcpServerIds.length}</span>
                                )}
                                <ChevronRight className="h-4 w-4 text-claude-muted" />
                              </button>
                            )}
                          </div>
                        )}

                        {addMenuPanel === 'workspace' && onWorkspaceFilesSelected && (
                          <WorkspaceFilePicker
                            onBack={() => setAddMenuPanel('root')}
                            onClose={() => {
                              setAddMenuPanel(null);
                              addMenuTriggerRef.current?.focus();
                            }}
                            onConfirm={(entries) => {
                              onWorkspaceFilesSelected(entries);
                              setAddMenuPanel(null);
                              textareaRef.current?.focus();
                            }}
                          />
                        )}

                        {addMenuPanel === 'skills' && (
                          <>
                            <div className="border-b border-claude-border p-3">
                              <div className="mb-2 flex items-center gap-2">
                                <button
                                  type="button"
                                  onClick={() => setAddMenuPanel('root')}
                                  className="rounded-lg p-1.5 text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
                                  aria-label="返回添加内容"
                                >
                                  <ArrowLeft className="h-4 w-4" />
                                </button>
                                <div className="min-w-0 flex-1">
                                  <div id="composer-skill-picker-title" className="text-sm font-medium text-claude-text">本轮优先 Skill</div>
                                  <div className="text-[11px] text-claude-muted">相关时优先考虑，不强制调用</div>
                                </div>
                                <button
                                  type="button"
                                  onClick={() => {
                                    setAddMenuPanel(null);
                                    addMenuTriggerRef.current?.focus();
                                  }}
                                  className="rounded-lg p-1.5 text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 md:hidden"
                                  aria-label="关闭 Skill 选择器"
                                >
                                  <X className="h-4 w-4" />
                                </button>
                              </div>
                              <label className="flex items-center gap-2 rounded-xl border border-claude-border px-3 py-2 focus-within:border-claude-border-strong focus-within:ring-2 focus-within:ring-claude-accent/10">
                                <Search className="h-4 w-4 text-claude-muted" />
                                <input
                                  value={skillQuery}
                                  aria-label="搜索 Skill"
                                  onChange={(event) => setSkillQuery(event.target.value)}
                                  placeholder="搜索名称、key 或描述"
                                  className="min-w-0 flex-1 border-0 bg-transparent p-0 text-sm text-claude-text outline-none placeholder:text-claude-muted focus:ring-0"
                                  autoFocus
                                />
                              </label>
                            </div>
                            <div role="group" aria-label="可选 Skills" className="max-h-[50vh] overflow-y-auto p-2">
                              {skillsLoading && !skillsLoaded && <div className="flex items-center justify-center gap-2 p-6 text-sm text-claude-muted"><Loader2 className="h-4 w-4 animate-spin" />加载中</div>}
                              {skillsError && !skillsLoaded && (
                                <div className="flex flex-col items-center gap-2 p-6 text-center text-sm text-claude-error">
                                  <span>{skillsError}</span>
                                  <button type="button" onClick={() => setSkillsLoadRevision((revision) => revision + 1)} className="rounded-lg border border-claude-border px-3 py-1.5 text-xs text-claude-secondary hover:bg-claude-hover">重新加载</button>
                                </div>
                              )}
                              {skillsLoaded && skillsLoading && <div aria-label="正在刷新 Skill 列表" className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-claude-muted"><Loader2 className="h-3 w-3 animate-spin" />正在刷新</div>}
                              {skillsLoaded && skillsError && <div className="mx-2 mb-1 flex items-center justify-between gap-2 rounded-lg bg-claude-error/5 px-2.5 py-2 text-xs text-claude-error"><span>{skillsError}</span><button type="button" onClick={() => setSkillsLoadRevision((revision) => revision + 1)} className="shrink-0 rounded-md border border-claude-border bg-white px-2 py-1 text-[11px] text-claude-secondary hover:bg-claude-hover">重试</button></div>}
                              {skillsLoaded && skillsInventoryState === 'stale' && !skillsError && <div role="status" className="mx-2 mb-1 rounded-lg bg-[#fff8ec] px-2.5 py-2 text-xs text-[#8a5a2f]">刷新失败，正在显示上次成功加载的 Skill 清单。</div>}
                              {skillsLoaded && filteredSkills.length === 0 && <div className="p-6 text-center text-sm text-claude-muted">没有匹配的 Skill</div>}
                              {skillsLoaded && filteredSkills.map((skill) => {
                                const key = skillKey(skill);
                                const selected = selectedSkillKeys.includes(key);
                                const limitReached = !selected && selectedSkillKeys.length >= MAX_SELECTED_SKILLS;
                                return (
                                  <button key={key} type="button" onClick={() => toggleSkill(key)} disabled={disabled || limitReached} aria-pressed={selected} className="flex min-h-11 w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-not-allowed disabled:opacity-40">
                                    <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${selected ? 'border-claude-accent bg-claude-accent text-white' : 'border-claude-border'}`}>{selected && <Check className="h-3.5 w-3.5" />}</span>
                                    <span className="min-w-0"><span className="block truncate text-sm font-medium text-claude-text">{skillDisplayName(skill)}</span>{shouldShowSkillKey(skill) && <span className="block truncate text-[11px] text-claude-muted">{key}</span>}<span className="mt-0.5 block line-clamp-2 text-xs text-claude-muted">{skill.description}</span></span>
                                  </button>
                                );
                              })}
                            </div>
                          </>
                        )}

                        {addMenuPanel === 'mcp' && (
                          <>
                            <div className="border-b border-claude-border p-3">
                              <div className="mb-2 flex items-center gap-2">
                                <button type="button" onClick={() => setAddMenuPanel('root')} className="rounded-lg p-1.5 text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label="返回添加内容"><ArrowLeft className="h-4 w-4" /></button>
                                <div className="min-w-0 flex-1"><div id="composer-mcp-picker-title" className="text-sm font-medium text-claude-text">本轮优先数据连接</div><div className="text-[11px] text-claude-muted">相关时优先检索，无匹配会自动回退</div></div>
                                <button type="button" onClick={() => { setAddMenuPanel(null); addMenuTriggerRef.current?.focus(); }} className="rounded-lg p-1.5 text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 md:hidden" aria-label="关闭数据连接选择器"><X className="h-4 w-4" /></button>
                              </div>
                              <label className="flex items-center gap-2 rounded-xl border border-claude-border px-3 py-2 focus-within:border-claude-border-strong focus-within:ring-2 focus-within:ring-claude-accent/10">
                                <Search className="h-4 w-4 text-claude-muted" />
                                <input value={mcpQuery} onChange={(event) => setMcpQuery(event.target.value)} aria-label="搜索数据连接" placeholder="搜索连接名称或说明" className="min-w-0 flex-1 border-0 bg-transparent p-0 text-sm text-claude-text outline-none placeholder:text-claude-muted focus:ring-0" autoFocus />
                              </label>
                            </div>
                            <div role="group" aria-label="可选数据连接" className="max-h-[50vh] overflow-y-auto p-2">
                              {mcpLoading && !mcpLoaded && <div className="flex items-center justify-center gap-2 p-6 text-sm text-claude-muted"><Loader2 className="h-4 w-4 animate-spin" />加载中</div>}
                              {mcpError && !mcpLoaded && <div className="flex flex-col items-center gap-2 p-6 text-center text-sm text-claude-error"><span>{mcpError}</span><button type="button" onClick={() => setMcpLoadRevision((revision) => revision + 1)} className="rounded-lg border border-claude-border px-3 py-1.5 text-xs text-claude-secondary hover:bg-claude-hover">重新加载</button></div>}
                              {mcpLoaded && mcpLoading && <div aria-label="正在刷新数据连接列表" className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-claude-muted"><Loader2 className="h-3 w-3 animate-spin" />正在刷新</div>}
                              {mcpLoaded && mcpError && <div className="mx-2 mb-1 flex items-center justify-between gap-2 rounded-lg bg-claude-error/5 px-2.5 py-2 text-xs text-claude-error"><span>{mcpError}</span><button type="button" onClick={() => setMcpLoadRevision((revision) => revision + 1)} className="shrink-0 rounded-md border border-claude-border bg-white px-2 py-1 text-[11px] text-claude-secondary hover:bg-claude-hover">重试</button></div>}
                              {mcpLoaded && filteredMcpServers.length === 0 && <div className="p-6 text-center text-sm text-claude-muted">没有可用的数据连接</div>}
                              {mcpLoaded && filteredMcpServers.map((server) => {
                                const selected = selectedMcpServerIds.includes(server.id);
                                const limitReached = !selected && selectedMcpServerIds.length >= MAX_SELECTED_MCP_SERVERS;
                                return (
                                  <button key={server.id} type="button" onClick={() => toggleMcpServer(server.id)} disabled={disabled || limitReached} aria-pressed={selected} className="flex min-h-11 w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-not-allowed disabled:opacity-40">
                                    <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${selected ? 'border-[#5d876a] bg-[#5d876a] text-white' : 'border-claude-border'}`}>{selected && <Check className="h-3.5 w-3.5" />}</span>
                                    <span className="min-w-0 flex-1"><span className="flex items-center gap-2"><span className="truncate text-sm font-medium text-claude-text">{server.name}</span><span className="shrink-0 rounded-full bg-claude-surface px-1.5 py-0.5 text-[10px] text-claude-muted">{server.source === 'official' ? '官方' : '个人'}</span></span>{server.description && <span className="mt-0.5 block line-clamp-2 text-xs text-claude-muted">{server.description}</span>}</span>
                                  </button>
                                );
                              })}
                            </div>
                          </>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {modelControl}
              </div>

              {/* 发送/停止按钮 — 圆形 */}
              {sendingLabel && onStop ? (
                <button
                  type="button"
                  onClick={onStop}
                  className="w-8 h-8 rounded-full flex items-center justify-center transition-[opacity,transform] bg-claude-error text-white hover:opacity-80 active:scale-95"
                  aria-label="停止生成"
                  title="停止生成"
                >
                  <Square className="w-3.5 h-3.5 fill-current" />
                </button>
              ) : (
                <button
                  type="button"
                  onClick={onSend}
                  disabled={!canSend}
                  aria-label="发送消息"
                  title="发送消息"
                  className={`w-8 h-8 rounded-full flex items-center justify-center transition-[background-color,color,opacity,transform] ${
                    canSend
                      ? 'bg-claude-text text-white hover:opacity-80 active:scale-95'
                      : 'bg-claude-border text-claude-muted cursor-not-allowed'
                  }`}
                >
                  {sendingLabel ? (
                    <Loader2 className="w-4 h-4 animate-spin" />
                  ) : (
                    <ArrowUp className="w-4 h-4" />
                  )}
                </button>
              )}
            </div>
          </div>

        </div>

        <p className="text-[10px] text-claude-muted mt-2 text-center">
          OpenCapyBox · 内容由 AI 生成，请仔细甄别
        </p>
      </div>

      {previewImage && (
        <div
          className="fixed inset-0 z-[110] flex items-center justify-center bg-black/60 backdrop-blur-sm p-6"
          onClick={() => setPreviewImage(null)}
        >
          <div
            className="relative max-w-[90vw] max-h-[90vh] bg-white rounded-2xl p-3"
            onClick={(e) => e.stopPropagation()}
          >
            <img
              src={previewImage.src}
              alt={previewImage.name}
              className="max-w-[88vw] max-h-[82vh] object-contain rounded-xl"
            />
            <button
              type="button"
              onClick={() => setPreviewImage(null)}
              className="absolute -top-2 -right-2 w-8 h-8 rounded-full bg-black text-white flex items-center justify-center"
              aria-label="关闭图片预览"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
