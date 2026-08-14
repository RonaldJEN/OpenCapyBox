import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Check, ChevronDown, ChevronLeft, ChevronRight, Loader2 } from 'lucide-react';

import type { ModelInfo, TurnReasoningSelection } from '../types';
import { getModelIcon } from '../utils/modelUtils';

interface ModelSelectorProps {
  selectedModelId: string;
  onModelChange: (modelId: string) => void;
  availableModels: ModelInfo[];
  reasoningSelection?: TurnReasoningSelection | null;
  onReasoningChange?: (selection: TurnReasoningSelection) => void;
  /** 选项提交后恢复聊天输入焦点；未提供时回到触发器。 */
  onSelectionComplete?: () => void;
  /** 会话已有历史时锁定模型，但仍允许为下一轮切换推理等级。 */
  readOnly?: boolean;
}

type Panel = 'root' | 'models' | 'reasoning';

function effortLabel(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : 'On';
}

function modelDefaultLevel(model?: ModelInfo): string | null {
  if (!model) return null;
  if (model.default_reasoning_level) return model.default_reasoning_level;
  if (model.reasoning_effort) return model.reasoning_effort;
  if (model.thinking_mode === 'disabled') return 'off';
  if (model.thinking_mode === 'enabled') return 'on';
  return null;
}

function selectionForLevel(level: string): TurnReasoningSelection {
  if (level === 'off') return { mode: 'disabled', effort: null };
  if (level === 'on') return { mode: 'enabled', effort: null };
  return { mode: 'enabled', effort: level };
}

function selectionMatchesLevel(
  selection: TurnReasoningSelection | null | undefined,
  level: string,
): boolean {
  if (level === 'off') return selection?.mode === 'disabled';
  if (level === 'on') return selection?.mode === 'enabled' && !selection.effort;
  return selection?.mode === 'enabled' && selection?.effort === level;
}

function selectionLabel(selection: TurnReasoningSelection | null | undefined, model?: ModelInfo): string {
  if (selection?.mode === 'disabled') return 'Off';
  if (selection?.mode === 'provider_default') {
    const defaultLevel = selection.effort || modelDefaultLevel(model);
    return defaultLevel ? `Default (${effortLabel(defaultLevel)})` : 'Default';
  }
  if (selection?.effort) return effortLabel(selection.effort);
  if (selection?.mode === 'enabled') return 'On';
  const defaultLevel = modelDefaultLevel(model);
  return defaultLevel ? effortLabel(defaultLevel) : 'Default';
}

function reasoningOptions(model?: ModelInfo): string[] {
  const supportsControl = model?.supports_reasoning_control ?? model?.supports_thinking;
  if (!supportsControl || model?.provider !== 'openai') return [];
  return [...new Set((model.supported_reasoning_efforts || []).filter(Boolean))];
}

export function ModelSelector({
  selectedModelId,
  onModelChange,
  availableModels,
  reasoningSelection,
  onReasoningChange,
  onSelectionComplete,
  readOnly = false,
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [panel, setPanel] = useState<Panel>('root');
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const generatedId = useId();
  const menuId = `model-reasoning-selector-${generatedId}`;
  const currentModel = availableModels.find((model) => model.id === selectedModelId);
  const efforts = reasoningOptions(currentModel);
  const canChooseReasoning = Boolean(efforts.length > 0 && onReasoningChange);
  // provider_default is a distinct catalog value even when it carries a
  // concrete effort. Its display projection can collide with enabled+effort,
  // so both choices must remain representable and reversible in the menu.
  const showsProviderDefault = currentModel?.thinking_mode === 'provider_default'
    || modelDefaultLevel(currentModel) === null;
  const providerDefaultLabel = currentModel?.reasoning_effort
    ? `Default (${effortLabel(currentModel.reasoning_effort)})`
    : 'Default';
  const currentReasoningLabel = selectionLabel(reasoningSelection, currentModel);

  useEffect(() => {
    if (!isOpen) return;
    const closeOnOutside = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setIsOpen(false);
        setPanel('root');
      }
    };
    document.addEventListener('mousedown', closeOnOutside);
    return () => document.removeEventListener('mousedown', closeOnOutside);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const timer = window.setTimeout(() => {
      const menu = menuRef.current;
      if (!menu) return;
      const preferred = panel === 'models'
        ? menu.querySelector<HTMLElement>('[role="option"][aria-selected="true"]')
        : panel === 'reasoning'
          ? menu.querySelector<HTMLElement>('[role="menuitemradio"][aria-checked="true"]')
          : null;
      const first = menu.querySelector<HTMLElement>('[data-selector-item="true"]:not([disabled])');
      (preferred || first)?.focus();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [isOpen, panel]);

  const close = () => {
    setIsOpen(false);
    setPanel('root');
    triggerRef.current?.focus();
  };

  const closeAfterSelection = () => {
    setIsOpen(false);
    setPanel('root');
    if (onSelectionComplete) onSelectionComplete();
    else triggerRef.current?.focus();
  };

  const chooseReasoning = (selection: TurnReasoningSelection) => {
    onReasoningChange?.(selection);
    closeAfterSelection();
  };

  const open = () => {
    setPanel(readOnly && canChooseReasoning ? 'reasoning' : 'root');
    setIsOpen(true);
  };

  const handleMenuKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    if (event.key === 'Tab') {
      setIsOpen(false);
      setPanel('root');
      return;
    }
    if (event.key === 'ArrowLeft' && panel !== 'root' && !readOnly) {
      event.preventDefault();
      setPanel('root');
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;

    const items = Array.from(
      menuRef.current?.querySelectorAll<HTMLElement>('[data-selector-item="true"]:not([disabled])') || [],
    );
    if (items.length === 0) return;
    event.preventDefault();
    const currentIndex = items.findIndex((item) => item === document.activeElement);
    let nextIndex: number;
    if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = items.length - 1;
    else if (event.key === 'ArrowDown') nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % items.length;
    else nextIndex = currentIndex < 0 ? items.length - 1 : (currentIndex - 1 + items.length) % items.length;
    items[nextIndex]?.focus();
  };

  if (readOnly && !canChooseReasoning) {
    return (
      <div className="flex items-center gap-2 rounded-xl bg-claude-surface px-3 py-1.5">
        {currentModel ? (
          <>
            <span className="text-claude-accent">{getModelIcon(currentModel)}</span>
            <span className="text-sm font-medium tracking-tight text-claude-text">{currentModel.name}</span>
          </>
        ) : <span className="text-sm text-claude-muted">--</span>}
      </div>
    );
  }

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => { if (isOpen) close(); else open(); }}
        onKeyDown={(event) => {
          if (!isOpen && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
            event.preventDefault();
            open();
            return;
          }
          if (event.key === 'Escape' && isOpen) {
            event.preventDefault();
            close();
          }
        }}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={menuId}
        aria-label={currentModel
          ? `选择模型，当前 ${currentModel.name}，推理等级 ${currentReasoningLabel}`
          : '选择模型'}
        className="flex items-center gap-2 rounded-xl border border-transparent px-3 py-2 transition-[background-color,border-color,transform] hover:border-claude-border hover:bg-claude-hover active:scale-95"
      >
        {currentModel ? (
          <>
            <span className="text-claude-accent">{getModelIcon(currentModel)}</span>
            <span className="text-sm font-semibold tracking-tight text-claude-text">{currentModel.name}</span>
            {canChooseReasoning && (
              <span className="text-sm font-semibold text-claude-muted">{currentReasoningLabel}</span>
            )}
          </>
        ) : (
          <span className="text-sm font-semibold tracking-tight text-claude-muted">选择模型...</span>
        )}
        <ChevronDown size={14} className={`text-claude-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-40" onClick={close} aria-hidden="true" />
          <div
            ref={menuRef}
            id={menuId}
            role={panel === 'models' ? undefined : 'menu'}
            aria-label={panel === 'reasoning' ? '推理等级' : '模型与推理等级'}
            onKeyDown={handleMenuKeyDown}
            className="absolute bottom-full left-0 z-50 mb-2 w-[280px] rounded-2xl border border-claude-border bg-white p-2 shadow-xl"
          >
            {panel === 'root' && (
              <div className="space-y-1">
                <button
                  type="button"
                  role="menuitem"
                  data-selector-item="true"
                  disabled={readOnly}
                  onClick={() => setPanel('models')}
                  className="flex w-full items-center justify-between rounded-xl px-3 py-3 text-left text-sm transition-colors hover:bg-claude-hover disabled:cursor-not-allowed disabled:opacity-55"
                >
                  <span className="font-medium text-claude-text">模型</span>
                  <span className="flex items-center gap-2 text-claude-muted"><span>{currentModel?.name || '未选择'}</span><ChevronRight size={15} /></span>
                </button>
                {canChooseReasoning && (
                  <button
                    type="button"
                    role="menuitem"
                    data-selector-item="true"
                    onClick={() => setPanel('reasoning')}
                    className="flex w-full items-center justify-between rounded-xl bg-claude-surface px-3 py-3 text-left text-sm transition-colors hover:bg-claude-hover"
                  >
                    <span className="font-medium text-claude-text">推理等级</span>
                    <span className="flex items-center gap-2 text-claude-muted"><span>{currentReasoningLabel}</span><ChevronRight size={15} /></span>
                  </button>
                )}
              </div>
            )}

            {panel === 'models' && (
              <div>
                <button type="button" onClick={() => setPanel('root')} className="mb-1 flex items-center gap-1 rounded-lg px-2 py-2 text-xs font-medium text-claude-muted hover:bg-claude-hover"><ChevronLeft size={14} />模型</button>
                {availableModels.length === 0 ? (
                  <div role="status" className="p-3 text-center text-sm text-claude-muted"><Loader2 size={16} className="mx-auto mb-1 animate-spin" />加载中...</div>
                ) : (
                  <div role="listbox" aria-label="可用模型">
                    {availableModels.map((model) => (
                      <button
                        type="button"
                        role="option"
                        data-selector-item="true"
                        aria-selected={selectedModelId === model.id}
                        key={model.id}
                        onClick={() => { onModelChange(model.id); closeAfterSelection(); }}
                        className={`flex w-full items-center justify-between rounded-xl p-3 text-left transition-colors ${selectedModelId === model.id ? 'bg-claude-surface text-claude-accent' : 'text-claude-text hover:bg-claude-hover'}`}
                      >
                        <span className="flex items-center gap-3">
                          <span className="text-claude-muted">{getModelIcon(model)}</span>
                          <span className="flex flex-col items-start">
                            <span className="text-sm font-medium">{model.name}</span>
                            {(model.supports_thinking || model.supports_reasoning_control) && (
                              <span className="text-[10px] text-claude-muted">支持深度思考</span>
                            )}
                            {model.supports_image && (
                              <span className="text-[10px] text-claude-muted">支持图片（最多 {model.max_images} 张）</span>
                            )}
                          </span>
                        </span>
                        {selectedModelId === model.id && <Check size={15} strokeWidth={3} />}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {panel === 'reasoning' && (
              <div className="space-y-1">
                {!readOnly && <button type="button" onClick={() => setPanel('root')} className="mb-1 flex items-center gap-1 rounded-lg px-2 py-2 text-xs font-medium text-claude-muted hover:bg-claude-hover"><ChevronLeft size={14} />推理等级</button>}
                {showsProviderDefault && (
                  <button
                    type="button"
                    role="menuitemradio"
                    data-selector-item="true"
                    aria-checked={reasoningSelection?.mode === 'provider_default'
                      && reasoningSelection.effort === (currentModel?.reasoning_effort || null)}
                    onClick={() => chooseReasoning({
                      mode: 'provider_default',
                      effort: currentModel?.reasoning_effort || null,
                    })}
                    className="flex w-full items-center justify-between rounded-xl px-3 py-3 text-sm font-semibold text-claude-text transition-colors hover:bg-claude-hover"
                  >
                    {providerDefaultLabel}
                    {reasoningSelection?.mode === 'provider_default'
                      && reasoningSelection.effort === (currentModel?.reasoning_effort || null)
                      && <Check size={16} strokeWidth={3} />}
                  </button>
                )}
                {efforts.map((effort) => {
                  const selected = selectionMatchesLevel(reasoningSelection, effort);
                  return (
                    <button
                      type="button"
                      role="menuitemradio"
                      data-selector-item="true"
                      aria-checked={selected}
                      key={effort}
                      onClick={() => chooseReasoning(selectionForLevel(effort))}
                      className={`flex w-full items-center justify-between rounded-xl px-3 py-3 text-sm font-semibold transition-colors ${selected ? 'bg-claude-surface text-claude-text' : 'text-claude-text hover:bg-claude-hover'}`}
                    >
                      {effortLabel(effort)}
                      {selected && <Check size={16} strokeWidth={3} />}
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
