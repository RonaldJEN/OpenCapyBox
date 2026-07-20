import { useEffect, useId, useRef, useState } from 'react';
import { Loader2, ChevronDown, Check } from 'lucide-react';
import { getModelIcon } from '../utils/modelUtils';
import type { ModelInfo } from '../types';

interface ModelSelectorProps {
  selectedModelId: string;
  onModelChange: (modelId: string) => void;
  availableModels: ModelInfo[];
  /** 只读模式：仅显示当前模型徽章，不可点击 */
  readOnly?: boolean;
}

export function ModelSelector({ selectedModelId, onModelChange, availableModels, readOnly = false }: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const generatedId = useId();
  const listboxId = `model-selector-${generatedId}`;
  const currentModel = availableModels.find(m => m.id === selectedModelId);
  const selectedIndex = availableModels.findIndex((model) => model.id === selectedModelId);

  useEffect(() => {
    if (!isOpen) return;
    const closeOnOutside = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setIsOpen(false);
    };
    document.addEventListener('mousedown', closeOnOutside);
    return () => document.removeEventListener('mousedown', closeOnOutside);
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen || availableModels.length === 0) return;
    const nextIndex = Math.min(Math.max(activeIndex, 0), availableModels.length - 1);
    if (nextIndex !== activeIndex) {
      setActiveIndex(nextIndex);
      return;
    }
    optionRefs.current[nextIndex]?.focus();
  }, [activeIndex, availableModels.length, isOpen]);

  const openListbox = (preferredIndex = selectedIndex >= 0 ? selectedIndex : 0) => {
    setActiveIndex(preferredIndex);
    setIsOpen(true);
  };

  const closeAndRestoreFocus = () => {
    setIsOpen(false);
    triggerRef.current?.focus();
  };

  const selectModel = (index: number) => {
    const model = availableModels[index];
    if (!model) return;
    onModelChange(model.id);
    closeAndRestoreFocus();
  };

  const handleOptionKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        setActiveIndex((index + 1) % availableModels.length);
        break;
      case 'ArrowUp':
        event.preventDefault();
        setActiveIndex((index - 1 + availableModels.length) % availableModels.length);
        break;
      case 'Home':
        event.preventDefault();
        setActiveIndex(0);
        break;
      case 'End':
        event.preventDefault();
        setActiveIndex(availableModels.length - 1);
        break;
      case 'Enter':
      case ' ':
        event.preventDefault();
        selectModel(index);
        break;
      case 'Escape':
        event.preventDefault();
        closeAndRestoreFocus();
        break;
      case 'Tab':
        setIsOpen(false);
        break;
      default:
        break;
    }
  };

  // ── 只读徽章模式 ──
  if (readOnly) {
    return (
      <div className="flex items-center space-x-2 px-3 py-1.5 rounded-lg bg-claude-surface">
        {currentModel ? (
          <>
            <span className="text-claude-accent">{getModelIcon(currentModel)}</span>
            <span className="text-sm font-medium text-claude-text tracking-tight">{currentModel.name}</span>
            {currentModel.supports_thinking && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-claude-accent/10 text-claude-accent font-medium">思考</span>
            )}
            {currentModel.supports_image && (
              <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-100 text-emerald-700 font-medium">图片</span>
            )}
          </>
        ) : (
          <span className="text-sm text-claude-muted">--</span>
        )}
      </div>
    );
  }

  // ── 可交互下拉选择器模式 ──
  return (
    <div ref={rootRef} className="relative">
      <button
        ref={triggerRef}
        type="button"
        onClick={() => {
          if (isOpen) {
            setIsOpen(false);
          } else {
            openListbox();
          }
        }}
        onKeyDown={(event) => {
          if (isOpen && event.key === 'Escape') {
            event.preventDefault();
            closeAndRestoreFocus();
            return;
          }
          if (isOpen && event.key === 'Tab') {
            setIsOpen(false);
            return;
          }
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
          event.preventDefault();
          const fallbackIndex = event.key === 'ArrowUp' ? availableModels.length - 1 : 0;
          openListbox(selectedIndex >= 0 ? selectedIndex : fallbackIndex);
        }}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-controls={listboxId}
        aria-label={currentModel ? `选择模型，当前为 ${currentModel.name}` : '选择模型'}
        className="flex items-center space-x-2 hover:bg-claude-hover px-3 py-2 rounded-lg transition-[background-color,border-color,transform] active:scale-95 border border-transparent hover:border-claude-border"
      >
        {currentModel ? (
          <>
            <span className="text-claude-accent">{getModelIcon(currentModel)}</span>
            <span className="text-sm font-semibold text-claude-text tracking-tight">{currentModel.name}</span>
          </>
        ) : (
          <span className="text-sm font-semibold text-claude-muted tracking-tight">选择模型...</span>
        )}
        <ChevronDown size={14} className={`text-claude-muted transition-transform ${isOpen ? 'rotate-180' : ''}`} />
      </button>

      {isOpen && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setIsOpen(false)} aria-hidden="true" />
          <div className="absolute top-full left-0 mt-2 w-[260px] bg-white border border-claude-border rounded-xl shadow-xl z-50">
            <div className="px-4 pt-3 pb-1.5">
              <p className="text-[11px] text-claude-muted font-medium">新对话将使用选定模型</p>
            </div>
            <div id={listboxId} role="listbox" aria-label="可用模型" className="p-1.5 pt-0">
              {availableModels.length === 0 ? (
                <div role="status" className="p-3 text-center text-sm text-claude-muted">
                  <Loader2 size={16} className="animate-spin mx-auto mb-1" />
                  加载中...
                </div>
              ) : (
                availableModels.map((m, index) => (
                  <button
                    ref={(element) => { optionRefs.current[index] = element; }}
                    type="button"
                    role="option"
                    tabIndex={-1}
                    aria-selected={selectedModelId === m.id}
                    key={m.id}
                    onClick={() => selectModel(index)}
                    onFocus={() => setActiveIndex(index)}
                    onKeyDown={(event) => handleOptionKeyDown(event, index)}
                    className={`w-full flex items-center justify-between p-3 rounded-lg transition-colors outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 ${
                      selectedModelId === m.id ? 'bg-claude-surface text-claude-accent' : 'hover:bg-claude-hover text-claude-text'
                    }`}
                  >
                    <div className="flex items-center space-x-3">
                      <span className={`${selectedModelId === m.id ? 'text-claude-accent' : 'text-claude-muted'}`}>{getModelIcon(m)}</span>
                      <div className="flex flex-col items-start">
                        <span className="text-sm font-medium">{m.name}</span>
                        {m.supports_thinking && (
                          <span className="text-[10px] text-claude-muted">支持深度思考</span>
                        )}
                        {m.supports_image && (
                          <span className="text-[10px] text-claude-muted">支持图片（最多 {m.max_images} 张）</span>
                        )}
                      </div>
                    </div>
                    {selectedModelId === m.id && (
                      <Check size={14} className="text-claude-accent" strokeWidth={3} />
                    )}
                  </button>
                ))
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
