import {
  useCallback,
  useRef,
  useEffect,
  useMemo,
  useState,
  type MutableRefObject,
  type ReactNode,
} from 'react';
import { ArrowUp, BookOpenCheck, Check, Loader2, Paperclip, Search, Square, X } from 'lucide-react';
import { FileInfo } from '../types';
import { getFileIcon, getFileExtLabel, getFileBadgeClass, getFileIconClass, isImageFile } from '../utils/fileUtils';
import {
  getSkills,
  type SkillInfo,
  type SkillInventoryState,
} from '../services/configApi';
import { MAX_SELECTED_SKILLS } from '../utils/skillDrafts';

const MAX_TEXTAREA_HEIGHT = 200;

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
  onInputDropHandled?: () => void;
  onPreviewAttachment?: (file: FileInfo) => void;
  uploading?: boolean;

  // ---- 本轮 Skill 偏好 ----
  selectedSkillKeys?: string[];
  onSelectedSkillKeysChange?: (keys: string[]) => void;

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
  onInputDropHandled,
  onPreviewAttachment,
  uploading = false,
  selectedSkillKeys = [],
  onSelectedSkillKeysChange,
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
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [skillsError, setSkillsError] = useState('');
  const [skillsInventoryState, setSkillsInventoryState] = useState<SkillInventoryState | null>(null);
  const [skillsLoadRevision, setSkillsLoadRevision] = useState(0);
  const [skillQuery, setSkillQuery] = useState('');
  const skillsLoadedRef = useRef(false);
  const skillsRequestRef = useRef<ReturnType<typeof getSkills> | null>(null);
  const skillPickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (autoFocus) textareaRef.current?.focus();
  }, [autoFocus]);

  useEffect(() => {
    if (!skillsOpen) return;
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
  }, [skillsLoadRevision, skillsOpen]);

  useEffect(() => {
    if (!skillsOpen) return;
    const closeOnOutside = (event: MouseEvent) => {
      if (!skillPickerRef.current?.contains(event.target as Node)) setSkillsOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSkillsOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', closeOnOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [skillsOpen]);

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

  const toggleSkill = (key: string) => {
    if (!onSelectedSkillKeysChange) return;
    if (selectedSkillKeys.includes(key)) {
      onSelectedSkillKeysChange(selectedSkillKeys.filter((item) => item !== key));
      return;
    }
    if (selectedSkillKeys.length >= MAX_SELECTED_SKILLS) return;
    onSelectedSkillKeysChange([...selectedSkillKeys, key]);
  };

  const toggleSkillsOpen = () => {
    if (skillsOpen) {
      setSkillsOpen(false);
      return;
    }
    setSkillQuery('');
    setSkillsOpen(true);
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
    <div className="px-4 pb-5 pt-3 bg-claude-bg">
      <div className="mx-auto max-w-3xl">
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
            {selectedSkillKeys.length > 0 && (
              <div className="flex flex-wrap gap-1.5 px-3 pt-3" aria-label="已选择 Skill">
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
                {/* 文件上传 */}
                {onFileUpload && (
                  <>
                    <input
                      ref={fileInputRef}
                      type="file"
                      multiple
                      className="hidden"
                      onChange={(e) => onFileUpload(e.target.files)}
                    />
                    <button
                      type="button"
                      onClick={() => fileInputRef.current?.click()}
                      disabled={uploading || disabled}
                      className="p-1.5 text-claude-muted hover:text-claude-secondary hover:bg-claude-hover rounded-lg transition-[color,background-color,opacity] disabled:opacity-50 disabled:cursor-not-allowed"
                      aria-label="上传文件"
                      title="上传文件"
                    >
                      {uploading ? (
                        <Loader2 className="w-4.5 h-4.5 animate-spin text-claude-secondary" />
                      ) : (
                        <Paperclip className="w-4.5 h-4.5" />
                      )}
                    </button>
                  </>
                )}

                {onSelectedSkillKeysChange && (
                  <div ref={skillPickerRef} className="relative">
                    <button
                      type="button"
                      onClick={toggleSkillsOpen}
                      disabled={disabled}
                      className={`flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs transition-[color,background-color,opacity] disabled:cursor-not-allowed disabled:opacity-50 ${
                        selectedSkillKeys.length > 0
                          ? 'bg-claude-accent/10 text-claude-secondary'
                          : 'text-claude-muted hover:bg-claude-hover hover:text-claude-secondary'
                      }`}
                      aria-expanded={skillsOpen}
                      aria-label="选择本轮 Skill"
                      title="选择本轮优先考虑的 Skill"
                    >
                      <BookOpenCheck className="h-4 w-4" />
                      <span>Skill{selectedSkillKeys.length > 0 ? ` ${selectedSkillKeys.length}` : ''}</span>
                    </button>

                    {skillsOpen && (
                      <div className="fixed inset-x-3 bottom-3 z-[120] max-h-[70vh] overflow-hidden rounded-2xl border border-claude-border bg-white shadow-2xl md:absolute md:inset-x-auto md:bottom-full md:left-0 md:mb-2 md:w-[24rem]">
                        <div className="border-b border-claude-border p-3">
                          <div className="mb-2 flex items-center justify-between">
                            <div>
                              <div className="text-sm font-medium text-claude-text">本轮优先 Skill</div>
                              <div className="text-[11px] text-claude-muted">相关时 Agent 会优先考虑，不强制调用</div>
                            </div>
                            <button type="button" onClick={() => setSkillsOpen(false)} className="rounded-lg p-1 text-claude-muted hover:bg-claude-hover md:hidden" aria-label="关闭 Skill 选择器">
                              <X className="h-4 w-4" />
                            </button>
                          </div>
                          <label className="flex items-center gap-2 rounded-xl border border-claude-border px-3 py-2 focus-within:border-claude-border-strong">
                            <Search className="h-4 w-4 text-claude-muted" />
                            <input
                              value={skillQuery}
                              onChange={(event) => setSkillQuery(event.target.value)}
                              placeholder="搜索名称、key 或描述"
                              className="min-w-0 flex-1 border-0 bg-transparent p-0 text-sm text-claude-text outline-none placeholder:text-claude-muted focus:ring-0"
                              autoFocus
                            />
                          </label>
                        </div>
                        <div className="max-h-[50vh] overflow-y-auto p-2">
                          {skillsLoading && !skillsLoaded && <div className="flex items-center justify-center gap-2 p-6 text-sm text-claude-muted"><Loader2 className="h-4 w-4 animate-spin" />加载中</div>}
                          {skillsError && !skillsLoaded && (
                            <div className="flex flex-col items-center gap-2 p-6 text-center text-sm text-claude-error">
                              <span>{skillsError}</span>
                              <button
                                type="button"
                                onClick={() => setSkillsLoadRevision((revision) => revision + 1)}
                                className="rounded-lg border border-claude-border px-3 py-1.5 text-xs text-claude-secondary hover:bg-claude-hover"
                              >
                                重新加载
                              </button>
                            </div>
                          )}
                          {skillsLoaded && skillsLoading && (
                            <div className="flex items-center gap-1.5 px-3 py-1.5 text-[11px] text-claude-muted" aria-label="正在刷新 Skill 列表">
                              <Loader2 className="h-3 w-3 animate-spin" />
                              正在刷新
                            </div>
                          )}
                          {skillsLoaded && skillsError && (
                            <div className="mx-2 mb-1 flex items-center justify-between gap-2 rounded-lg bg-claude-error/5 px-2.5 py-2 text-xs text-claude-error">
                              <span>{skillsError}</span>
                              <button
                                type="button"
                                onClick={() => setSkillsLoadRevision((revision) => revision + 1)}
                                className="shrink-0 rounded-md border border-claude-border bg-white px-2 py-1 text-[11px] text-claude-secondary hover:bg-claude-hover"
                              >
                                重试
                              </button>
                            </div>
                          )}
                          {skillsLoaded && skillsInventoryState === 'stale' && !skillsError && (
                            <div
                              role="status"
                              className="mx-2 mb-1 rounded-lg bg-[#fff8ec] px-2.5 py-2 text-xs text-[#8a5a2f]"
                            >
                              刷新失败，正在显示上次成功加载的 Skill 清单。
                            </div>
                          )}
                          {skillsLoaded && filteredSkills.length === 0 && <div className="p-6 text-center text-sm text-claude-muted">没有匹配的 Skill</div>}
                          {skillsLoaded && filteredSkills.map((skill) => {
                            const key = skillKey(skill);
                            const selected = selectedSkillKeys.includes(key);
                            const limitReached = !selected && selectedSkillKeys.length >= MAX_SELECTED_SKILLS;
                            return (
                              <button
                                key={key}
                                type="button"
                                onClick={() => toggleSkill(key)}
                                disabled={limitReached}
                                aria-pressed={selected}
                                className="flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left hover:bg-claude-hover disabled:cursor-not-allowed disabled:opacity-40"
                              >
                                <span className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border ${selected ? 'border-claude-accent bg-claude-accent text-white' : 'border-claude-border'}`}>
                                  {selected && <Check className="h-3.5 w-3.5" />}
                                </span>
                                <span className="min-w-0">
                                  <span className="block truncate text-sm font-medium text-claude-text">{skillDisplayName(skill)}</span>
                                  {shouldShowSkillKey(skill) && (
                                    <span className="block truncate text-[11px] text-claude-muted">{key}</span>
                                  )}
                                  <span className="mt-0.5 block line-clamp-2 text-xs text-claude-muted">{skill.description}</span>
                                </span>
                              </button>
                            );
                          })}
                        </div>
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
