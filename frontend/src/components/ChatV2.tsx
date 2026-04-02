import { useEffect, useLayoutEffect, useState, useRef } from 'react';
import { applyPatch, Operation } from 'fast-json-patch';

import { apiService } from '../services/api';
import {
  RoundData,
  FileInfo,
  AgentState,
  ModelInfo,
  ChatContentBlock,
  AttachmentInfo,
  InterruptDetails,
  AskUserQuestion,
  StreamCallbacks,
  StepData,
} from '../types';
import { compressImage } from '../utils/imageUtils';
import { toFileInfo, isImageFile } from '../utils/fileUtils';
import { Round } from './Round';
import { ArtifactsPanel } from './ArtifactsPanel';
import { FilePreview } from './FilePreview';
import { ModelSelector } from './ModelSelector';
import { ChatInput } from './ChatInput';
import { QuestionCard } from './QuestionCard';
import {
  Loader2,
  AlertCircle,
  Paperclip,
  X,
  ArrowDown,
  Folder,
} from 'lucide-react';

/** 欢迎页快捷建议 */
const WELCOME_SUGGESTIONS = [
  '分析上传的 PDF 文件',
  '帮我写一个 Python 爬虫',
  '解释这一段 React 代码',
  '生成一份周报模板',
] as const;

interface ChatV2Props {
  sessionId: string;
  onTitleUpdated?: () => void;
  onExecutionStart?: (sessionId: string) => void;
  onExecutionEnd?: () => void;
  onPanelToggle?: (isOpen: boolean) => void;
  selectedModelId: string;
  onModelChange: (modelId: string) => void;
  availableModels?: ModelInfo[];
  /** 从欢迎页触发创建会话：返回新 sessionId */
  onCreateSession?: (modelId?: string) => Promise<string>;
}

type StepUpdater = (patchOrFn: Partial<StepData> | ((step: StepData) => StepData)) => void;

interface StreamCallbacksFactoryOptions {
  tempRoundId: string;
  getCurrentRunId: () => string;
  setCurrentRunId: (runId: string) => void;
  updateLastStep: StepUpdater;
  setBusyFalse: () => void;
  onStreamSuccess: () => void;
  onStreamError: (errorMsg: string, code?: string) => void;
  shouldRefreshTitleOnFirstRound?: boolean;
  mirrorTextToFinalResponse?: boolean;
  setRoundRunningOnStart?: boolean;
}

export function ChatV2({ sessionId, onTitleUpdated, onExecutionStart, onExecutionEnd, onPanelToggle, selectedModelId, onModelChange, availableModels = [], onCreateSession }: ChatV2Props) {
  const [rounds, setRounds] = useState<RoundData[]>([]);
  const [disableInitialMotion, setDisableInitialMotion] = useState(false);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState('');

  // AG-UI 状态管理
  const [agentState, setAgentState] = useState<AgentState>({
    currentStep: 0,
    status: 'idle',
    toolLogs: [],
    lastUpdated: Date.now(),
  });

  // 暴露 agentState 供调试使用
  useEffect(() => {
    if (import.meta.env.DEV) {
      (window as any).__agentState = agentState;
    }
  }, [agentState]);

  // 流式内容累积器 (基于 messageId)
  const streamingContentRef = useRef<{
    currentTextMessageId: string | null;
    currentThinkingMessageId: string | null;
    textContent: string;
    thinkingContent: string;
    toolArgs: Record<string, string>; // toolCallId -> accumulated args
  }>({
    currentTextMessageId: null,
    currentThinkingMessageId: null,
    textContent: '',
    thinkingContent: '',
    toolArgs: {},
  });

  // Apple 风格 UI 状态
  const [isFilesOpen, setIsFilesOpen] = useState(false);

  // 监听面板状态变化并通知父组件
  useEffect(() => {
    onPanelToggle?.(isFilesOpen);
  }, [isFilesOpen, onPanelToggle]);

  const [previewFile, setPreviewFile] = useState<FileInfo | null>(null);
  const [previewSessionId, setPreviewSessionId] = useState<string>('');

  // 🆕 文件上传相关状态
  const [attachedFiles, setAttachedFiles] = useState<FileInfo[]>([]);
  const [uploading, setUploading] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const [availableFiles, setAvailableFiles] = useState<FileInfo[]>([]);
  const [showFileAutocomplete, setShowFileAutocomplete] = useState(false);

  // 🔥 智能滚动状态
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);

  // 🆕 Human-in-the-Loop 中断状态
  const [pendingInterrupt, setPendingInterrupt] = useState<InterruptDetails | null>(null);
  const [resuming, setResuming] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatAreaRef = useRef<HTMLDivElement>(null);
  const prevRoundsLengthRef = useRef<number>(0);
  const isInitialLoadRef = useRef<boolean>(true); // 🆕 标记是否是首次加载
  const subscriptionAbortRef = useRef<(() => void) | null>(null); // 🆕 保存订阅取消函数
  const scrollPosBySessionRef = useRef<Record<string, number>>({});
  const pendingRestoreScrollRef = useRef<number | null>(null);
  const suppressAutoScrollRef = useRef<boolean>(false); // 切会话期间抑制自动 smooth 滚动
  const cronRefreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const titleRefreshTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 🆕 欢迎页「输入即创建」相关状态
  const [creatingSession, setCreatingSession] = useState(false);
  const pendingMessageRef = useRef<{
    displayMessage: string;
    content: ChatContentBlock[];
    attachments: FileInfo[];
  } | null>(null);
  const historyLoadedRef = useRef(false); // 标记 loadHistory 是否已完成
  const selectedModel = availableModels.find((m) => m.id === selectedModelId);

  // readFileAsDataUrl / compressImage 已从 utils/imageUtils 导入

  const buildDisplayMessage = (text: string, files: FileInfo[]) => {
    const trimmed = text.trim();
    if (trimmed) {
      return trimmed;
    }
    return files.length > 0 ? '' : '[空消息]';
  };

  const buildContentBlocks = (text: string, files: FileInfo[]): ChatContentBlock[] => {
    const blocks: ChatContentBlock[] = [];
    const trimmed = text.trim();
    const toMimeType = (value?: string) => (value && value.includes('/') ? value : undefined);
    if (trimmed) {
      blocks.push({ type: 'text', text: trimmed });
    }

    const imageFiles = files.filter((file) => isImageFile(file));
    if (imageFiles.length > 0 && !(selectedModel?.supports_image ?? false)) {
      throw new Error(`当前模型 ${selectedModel?.name || selectedModelId} 不支持图片输入`);
    }
    if (imageFiles.length > 0 && imageFiles.length > (selectedModel?.max_images ?? 0)) {
      throw new Error(`当前模型最多支持 ${selectedModel?.max_images ?? 0} 张图片`);
    }

    for (const file of files) {
      const isImage = isImageFile(file);
      if (isImage && file.data_url && (selectedModel?.supports_image ?? false)) {
        blocks.push({
          type: 'image_url',
          image_url: {
            url: file.data_url,
          },
          file: {
            path: file.path,
            name: file.name,
            mime_type: toMimeType(file.type),
            size: file.size,
          },
        });
      } else {
        blocks.push({
          type: 'file',
          file: {
            path: file.path,
            name: file.name,
            mime_type: toMimeType(file.type),
            size: file.size,
          },
        });
      }
    }

    if (blocks.length === 0) {
      throw new Error('消息内容不能为空');
    }
    return blocks;
  };

  useEffect(() => {
    if (!sessionId) {
      if (subscriptionAbortRef.current) {
        subscriptionAbortRef.current();
        subscriptionAbortRef.current = null;
      }
      if (cronRefreshTimeoutRef.current) {
        clearTimeout(cronRefreshTimeoutRef.current);
        cronRefreshTimeoutRef.current = null;
      }
      if (titleRefreshTimeoutRef.current) {
        clearTimeout(titleRefreshTimeoutRef.current);
        titleRefreshTimeoutRef.current = null;
      }
      setRounds([]);
      setInput('');
      setSending(false);
      setError('');
      setPendingInterrupt(null);
      setResuming(false);
      setAttachedFiles([]);
      setAvailableFiles([]);
      setShowFileAutocomplete(false);
      setPreviewFile(null);
      setPreviewSessionId('');
      setIsFilesOpen(false);
      setIsDragging(false);
      setLoading(false);
      setDisableInitialMotion(false);
      isInitialLoadRef.current = true;
      suppressAutoScrollRef.current = false;
      pendingRestoreScrollRef.current = null;
      historyLoadedRef.current = false;
      return;
    }

    setDisableInitialMotion(true);

    if (titleRefreshTimeoutRef.current) {
      clearTimeout(titleRefreshTimeoutRef.current);
      titleRefreshTimeoutRef.current = null;
    }

    // 记录目标会话的滚动恢复位置（如果有）
    pendingRestoreScrollRef.current = scrollPosBySessionRef.current[sessionId] ?? null;

    // 🆕 切换会话时，先取消之前的订阅
    if (subscriptionAbortRef.current) {
      console.log('🔄 切换会话，取消之前的订阅');
      subscriptionAbortRef.current();
      subscriptionAbortRef.current = null;
    }

    isInitialLoadRef.current = true; // 🆕 切换会话时重置为首次加载
    suppressAutoScrollRef.current = true; // 切会话期间抑制自动 smooth 滚动
    setIsAtBottom(false); // 避免会话切换瞬间误触发 smooth scroll
    historyLoadedRef.current = false; // 重置历史加载标记
    prevRoundsLengthRef.current = 0;
    setPendingInterrupt(null); // 切换会话时清除旧的中断状态
    setResuming(false);
    loadHistory();

    // 🆕 cleanup 函数：组件卸载或 sessionId 变化时取消订阅
    return () => {
      if (subscriptionAbortRef.current) {
        console.log('🧹 清理订阅');
        subscriptionAbortRef.current();
        subscriptionAbortRef.current = null;
      }
      if (titleRefreshTimeoutRef.current) {
        clearTimeout(titleRefreshTimeoutRef.current);
        titleRefreshTimeoutRef.current = null;
      }
    };
  }, [sessionId]);

  // 🔄 轮询检测新消息（Cron / 系统注入的 Round）
  const knownRoundCountRef = useRef<number>(0);
  useEffect(() => {
    // 同步已知 round count
    knownRoundCountRef.current = rounds.length;
  }, [rounds.length]);

  useEffect(() => {
    if (!sessionId || sending) return;

    let timer: ReturnType<typeof setInterval> | null = null;

    const poll = async () => {
      try {
        const { round_count } = await apiService.pollSession(sessionId);
        if (round_count > knownRoundCountRef.current) {
          console.log(`🔔 检测到新 Round (${knownRoundCountRef.current} → ${round_count})，刷新历史`);
          knownRoundCountRef.current = round_count;
          loadHistory();
        }
      } catch {
        // 静默忽略轮询错误
      }
    };

    timer = setInterval(poll, 1000);

    // 监听 CronHistory 手动触发的即时刷新事件
    const handleCronDone = () => {
      // 延迟 300ms 等后端落库完成
      if (cronRefreshTimeoutRef.current) {
        clearTimeout(cronRefreshTimeoutRef.current);
      }
      cronRefreshTimeoutRef.current = setTimeout(() => {
        cronRefreshTimeoutRef.current = null;
        void poll();
      }, 300);
    };
    window.addEventListener('cron-job-done', handleCronDone);

    return () => {
      if (timer) clearInterval(timer);
      if (cronRefreshTimeoutRef.current) {
        clearTimeout(cronRefreshTimeoutRef.current);
        cronRefreshTimeoutRef.current = null;
      }
      window.removeEventListener('cron-job-done', handleCronDone);
    };
  }, [sessionId, sending]);

  useEffect(() => {
    return () => {
      if (cronRefreshTimeoutRef.current) {
        clearTimeout(cronRefreshTimeoutRef.current);
        cronRefreshTimeoutRef.current = null;
      }
      if (titleRefreshTimeoutRef.current) {
        clearTimeout(titleRefreshTimeoutRef.current);
        titleRefreshTimeoutRef.current = null;
      }
    };
  }, []);

  // 僅在文件面板打開時拉取文件，避免僅載入歷史時觸發 /files 請求
  useEffect(() => {
    if (sessionId && isFilesOpen) {
      loadAvailableFiles();
    }
  }, [sessionId, isFilesOpen]);

  // 🔥 切换会话时：useLayoutEffect 在浏览器绘制前同步恢复 scrollTop，用户看不到跳动
  useLayoutEffect(() => {
    const container = chatAreaRef.current;
    if (!isInitialLoadRef.current || !container) return;

    isInitialLoadRef.current = false;

    const restoreTop = pendingRestoreScrollRef.current;
    if (restoreTop !== null && Number.isFinite(restoreTop)) {
      container.scrollTop = restoreTop;
    }

    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    const atBottom = distanceFromBottom < 100;
    setIsAtBottom(atBottom);
    setShowScrollButton(!atBottom);

    suppressAutoScrollRef.current = false;
    pendingRestoreScrollRef.current = null;

    prevRoundsLengthRef.current = rounds.reduce((sum, r) => sum + 1 + r.steps.length, 0);
  }, [rounds, sessionId]);

  // 🔥 流式新内容时：仅在用户位于底部时平滑滚动跟随
  useEffect(() => {
    if (suppressAutoScrollRef.current) return;

    const currentLength = rounds.reduce((sum, r) => sum + 1 + r.steps.length, 0);
    const hasNewContent = currentLength > prevRoundsLengthRef.current;

    if (hasNewContent && isAtBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }

    prevRoundsLengthRef.current = currentLength;
  }, [rounds, isAtBottom]);

  // 🔥 监听滚动事件，检测用户是否在底部
  useEffect(() => {
    const container = chatAreaRef.current;
    if (!container) return;

    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight;
      const atBottom = distanceFromBottom < 100; // 100px 容差

      setIsAtBottom(atBottom);
      setShowScrollButton(!atBottom);

      if (sessionId) {
        scrollPosBySessionRef.current[sessionId] = scrollTop;
      }
    };

    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, [sessionId]);

  // 🆕 加载可用文件列表
  const loadAvailableFiles = async () => {
    try {
      const response = await apiService.getSessionFiles(sessionId);
      setAvailableFiles(response.files);
    } catch (err) {
      console.error('Failed to load files:', err);
    }
  };

  const scrollToBottom = (force: boolean = false) => {
    if (force) {
      setIsAtBottom(true);
    }
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const loadHistory = async () => {
    setLoading(true);
    setError('');
    try {
      const response = await apiService.getSessionHistoryV2(sessionId);
      setRounds(response.rounds.map((round) => ({
        ...round,
        user_attachments: (round.user_attachments || []).map((attachment) => ({
          ...attachment,
          session_id: attachment.session_id || sessionId,
        })),
      })));
      // 检查是否有运行中的轮次，如果有则订阅更新
      const runningRound = response.rounds.find(r => r.status === 'running');

      // 🆕 检查是否有中断的轮次，恢复 QuestionCard
      if (!runningRound) {
        const interruptedRound = [...response.rounds].reverse().find(
          r => r.status === 'interrupted' && r.interrupt
        );
        if (interruptedRound?.interrupt) {
          setPendingInterrupt(interruptedRound.interrupt);
          setAgentState((prev) => ({ ...prev, status: 'waiting', lastUpdated: Date.now() }));
          console.log(`⏸️ 恢复中断状态: ${interruptedRound.round_id}`);
        }
      }

      if (runningRound) {
        onExecutionStart?.(sessionId);
        setDisableInitialMotion(false);
        setSending(true);

        console.log(`🔄 检测到运行中轮次 ${runningRound.round_id}，开始订阅更新...`);

        // 使用 AG-UI 协议订阅轮次更新
        const subscription = apiService.subscribeToRound(sessionId, runningRound.round_id, {
          // 状态快照 - 恢复完整状态
          onStateSnapshot: (snapshot) => {
            setAgentState(snapshot);
          },

          // 状态增量 - JSON Patch 更新
          onStateDelta: (delta) => {
            setAgentState((prev) => {
              try {
                const result = applyPatch(prev, delta as Operation[], true, false);
                return result.newDocument;
              } catch (e) {
                console.error('Failed to apply state patch:', e);
                return prev;
              }
            });
          },

          // 消息快照 - 恢复历史消息
          onMessagesSnapshot: (messages) => {
            console.log('📥 收到消息快照:', messages.length, '条消息');
            // 消息快照用于恢复历史，通常 loadHistory 已处理
          },

          // 🆕 流式思维链事件
          onThinkingStart: (messageId) => {
            console.log('🧠 开始思维:', messageId);
            streamingContentRef.current.currentThinkingMessageId = messageId;
            streamingContentRef.current.thinkingContent = '';
          },

          onThinkingContent: (messageId, delta) => {
            if (streamingContentRef.current.currentThinkingMessageId === messageId) {
              streamingContentRef.current.thinkingContent += delta;
              // 更新当前轮次的最后一个步骤的 thinking
              setRounds((prev) =>
                prev.map((round) => {
                  if (round.round_id === runningRound.round_id && round.steps.length > 0) {
                    const steps = [...round.steps];
                    const lastStep = { ...steps[steps.length - 1] };
                    lastStep.thinking = streamingContentRef.current.thinkingContent;
                    steps[steps.length - 1] = lastStep;
                    return { ...round, steps };
                  }
                  return round;
                })
              );
            }
          },

          onThinkingEnd: (messageId) => {
            console.log('🧠 思维结束:', messageId);
            streamingContentRef.current.currentThinkingMessageId = null;
          },

          // 🆕 流式文本消息事件
          onTextMessageStart: (messageId, _role) => {
            console.log('💬 开始消息:', messageId);
            streamingContentRef.current.currentTextMessageId = messageId;
            streamingContentRef.current.textContent = '';
          },

          onTextMessageContent: (messageId, delta) => {
            if (streamingContentRef.current.currentTextMessageId === messageId) {
              streamingContentRef.current.textContent += delta;
              // 更新当前轮次的最后一个步骤的 assistant_content
              setRounds((prev) =>
                prev.map((round) => {
                  if (round.round_id === runningRound.round_id && round.steps.length > 0) {
                    const steps = [...round.steps];
                    const lastStep = { ...steps[steps.length - 1] };
                    lastStep.assistant_content = streamingContentRef.current.textContent;
                    steps[steps.length - 1] = lastStep;
                    return { ...round, steps };
                  }
                  return round;
                })
              );
            }
          },

          onTextMessageEnd: (messageId) => {
            console.log('💬 消息结束:', messageId);
            streamingContentRef.current.currentTextMessageId = null;
          },

          // 🆕 步骤事件
          onStepStarted: (stepName) => {
            console.log('📝 步骤开始:', stepName);
            const stepNumber = parseInt(stepName.replace('step_', '')) || 0;
            // 如果这个步骤不存在，添加一个新步骤
            setRounds((prev) =>
              prev.map((round) => {
                if (round.round_id === runningRound.round_id) {
                  const existingStep = round.steps.find((s) => s.step_number === stepNumber);
                  if (!existingStep) {
                    return {
                      ...round,
                      steps: [
                        ...round.steps,
                        {
                          step_number: stepNumber,
                          status: 'running',
                          thinking: '',
                          assistant_content: '',
                          tool_calls: [],
                          tool_results: [],
                        },
                      ],
                    };
                  }
                }
                return round;
              })
            );
          },

          onStepFinished: (stepName) => {
            console.log('📝 步骤完成:', stepName);
            const stepNumber = parseInt(stepName.replace('step_', '')) || 0;
            setRounds((prev) =>
              prev.map((round) => {
                if (round.round_id === runningRound.round_id) {
                  const steps = round.steps.map((s) =>
                    s.step_number === stepNumber ? { ...s, status: 'completed' as const } : s
                  );
                  return { ...round, steps };
                }
                return round;
              })
            );
          },

          // 🆕 工具调用事件
          onToolCallStart: (toolCallId, toolName, _parentMessageId) => {
            console.log('🔧 工具调用开始:', toolCallId, toolName);
            streamingContentRef.current.toolArgs[toolCallId] = '';
            // 添加工具调用到当前步骤
            setRounds((prev) =>
              prev.map((round) => {
                if (round.round_id === runningRound.round_id && round.steps.length > 0) {
                  const steps = [...round.steps];
                  const lastStep = { ...steps[steps.length - 1] };
                  lastStep.tool_calls = [
                    ...(lastStep.tool_calls || []),
                    { id: toolCallId, name: toolName, input: {} },
                  ];
                  steps[steps.length - 1] = lastStep;
                  return { ...round, steps };
                }
                return round;
              })
            );
          },

          onToolCallArgs: (toolCallId, delta) => {
            streamingContentRef.current.toolArgs[toolCallId] =
              (streamingContentRef.current.toolArgs[toolCallId] || '') + delta;
            // 尝试解析参数并更新
            try {
              const args = JSON.parse(streamingContentRef.current.toolArgs[toolCallId]);
              setRounds((prev) =>
                prev.map((round) => {
                  if (round.round_id === runningRound.round_id && round.steps.length > 0) {
                    const steps = [...round.steps];
                    const lastStep = { ...steps[steps.length - 1] };
                    lastStep.tool_calls = (lastStep.tool_calls || []).map((tc) =>
                      tc.id === toolCallId ? { ...tc, input: args } : tc
                    );
                    steps[steps.length - 1] = lastStep;
                    return { ...round, steps };
                  }
                  return round;
                })
              );
            } catch {
              // 参数还没完整，忽略解析错误
            }
          },

          onToolCallEnd: (toolCallId) => {
            console.log('🔧 工具调用结束:', toolCallId);
          },

          onToolCallResult: (_messageId, toolCallId, content) => {
            console.log('🔧 工具结果:', toolCallId);
            setRounds((prev) =>
              prev.map((round) => {
                if (round.round_id === runningRound.round_id && round.steps.length > 0) {
                  const steps = [...round.steps];
                  const lastStep = { ...steps[steps.length - 1] };
                  lastStep.tool_results = [
                    ...(lastStep.tool_results || []),
                    { tool_call_id: toolCallId, content },
                  ];
                  steps[steps.length - 1] = lastStep;
                  return { ...round, steps };
                }
                return round;
              })
            );
          },

          // 运行完成
          onRunFinished: (_threadId, runId, result, outcome, interrupt) => {
            setRounds((prev) =>
              prev.map((round) =>
                round.round_id === runId
                  ? {
                      ...round,
                      final_response: result?.finalResponse || round.final_response,
                      status: outcome === 'interrupt' ? 'interrupted' : (outcome === 'success' ? 'completed' : outcome),
                      completed_at: outcome === 'interrupt' ? undefined : new Date().toISOString(),
                    }
                  : round
              )
            );
            // 处理中断恢复
            if (outcome === 'interrupt' && interrupt) {
              setPendingInterrupt(interrupt);
              setAgentState((prev) => ({ ...prev, status: 'waiting', lastUpdated: Date.now() }));
              setSending(false);
              subscriptionAbortRef.current = null;
              console.log(`⏸️ 轮次 ${runId} 中断，等待用户输入`);
              return;
            }
            setSending(false);
            onExecutionEnd?.();
            subscriptionAbortRef.current = null;
            console.log(`✅ 轮次 ${runId} 订阅完成`);
          },

          // 错误处理
          onRunError: (errorMsg) => {
            setError(errorMsg);
            setRounds((prev) =>
              prev.map((round) =>
                round.round_id === runningRound.round_id
                  ? {
                      ...round,
                      status: 'failed',
                      completed_at: new Date().toISOString(),
                      final_response: round.final_response || errorMsg,
                    }
                  : round
              )
            );
            setSending(false);
            onExecutionEnd?.();
            subscriptionAbortRef.current = null;
            console.error(`❌ 订阅错误: ${errorMsg}`);
          },

          // 自定义事件
          onCustomEvent: (name, _value) => {
            if (name === 'title_updated') {
              onTitleUpdated?.();
            }
          },
        });

        subscriptionAbortRef.current = subscription.abort;

        subscription.promise.catch((err) => {
          console.error('订阅失败:', err);
          setSending(false);
          onExecutionEnd?.();
          subscriptionAbortRef.current = null;
        });
      }
    } catch (err) {
      console.error('Failed to load history:', err);
      setError('加载历史记录失败');
    } finally {
      setLoading(false);
      historyLoadedRef.current = true;

      // 如果有暂存消息（欢迎页「输入即创建」），历史加载完成后立即发送
      if (pendingMessageRef.current) {
        const { displayMessage, content, attachments } = pendingMessageRef.current;
        pendingMessageRef.current = null;
        setCreatingSession(false);
        sendMessageForSession(sessionId, displayMessage, content, attachments);
      }
    }
  };

  // 🆕 文件上传处理（支持欢迎页无 sessionId 场景）
  const handleFileUpload = async (files: FileList | File[] | null) => {
    if (!files || files.length === 0) return;

    setUploading(true);
    const uploadedFiles: FileInfo[] = [];

    try {
      // 如果没有 sessionId（欢迎页），先创建会话
      let targetSessionId = sessionId;
      if (!targetSessionId) {
        if (!onCreateSession) {
          setError('无法创建会话');
          setUploading(false);
          return;
        }
        setCreatingSession(true);
        try {
          targetSessionId = await onCreateSession(selectedModelId || undefined);
        } catch (err) {
          console.error('Failed to create session for file upload:', err);
          setError('创建会话失败，无法上传文件');
          setCreatingSession(false);
          setUploading(false);
          return;
        }
        // 注意：onCreateSession 会触发父组件更新 sessionId，
        // 但这里我们直接用返回的 targetSessionId 继续上传
      }

      const uploadQueue = Array.from(files as ArrayLike<File>);

      for (let i = 0; i < uploadQueue.length; i++) {
        const file = uploadQueue[i];
        const fileInfo = await apiService.uploadFile(targetSessionId, file);
        fileInfo.session_id = targetSessionId;
        if (file.type) {
          fileInfo.type = file.type;
        }
        if (isImageFile(fileInfo)) {
          // 壓縮圖片至 ≤500KB JPEG（小圖不壓縮），降低 LLM token 消耗
          fileInfo.data_url = await compressImage(file, {
            maxWidth: 2048,
            maxHeight: 2048,
            quality: 0.8,
            maxSizeKB: 500,
          });
        }
        uploadedFiles.push(fileInfo);
      }

      setAttachedFiles((prev) => [...prev, ...uploadedFiles]);
      setAvailableFiles((prev) => [...prev, ...uploadedFiles]);
      console.log(`✅ 上传成功: ${uploadedFiles.length} 个文件`);
    } catch (err) {
      console.error('Failed to upload files:', err);
      setError('文件上传失败');
    } finally {
      setUploading(false);
      setCreatingSession(false);
    }
  };

  // 🆕 拖拽处理（优化版：防止子元素触发闪烁）
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    // 确保有文件类型的拖拽才显示提示
    if (e.dataTransfer.types.includes('Files')) {
      setIsDragging(true);
    }
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    // 只有当真正离开容器时才隐藏提示（不是移动到子元素）
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    if (
      e.clientX <= rect.left ||
      e.clientX >= rect.right ||
      e.clientY <= rect.top ||
      e.clientY >= rect.bottom
    ) {
      setIsDragging(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(false);

    const files = e.dataTransfer.files;
    if (files.length > 0) {
      handleFileUpload(files);
    }
  };

  // 🆕 移除附件
  const handleRemoveAttachment = (index: number) => {
    setAttachedFiles(attachedFiles.filter((_, i) => i !== index));
  };

  const handlePreviewAttachment = async (file: AttachmentInfo | FileInfo) => {
    const targetSessionId = file.session_id || sessionId;
    if (!targetSessionId) {
      if (isImageFile(file) && file.data_url) {
        setPreviewFile(toFileInfo(file, ''));
      }
      return;
    }

    let normalizedFile = toFileInfo(file, targetSessionId);
    if (normalizedFile.size <= 0) {
      try {
        const list = await apiService.getSessionFiles(targetSessionId);
        const matched = list.files.find((f) => f.path === normalizedFile.path);
        if (matched) {
          normalizedFile = {
            ...normalizedFile,
            size: matched.size,
            modified: matched.modified || normalizedFile.modified,
            type: matched.type || normalizedFile.type,
          };
        }
      } catch (err) {
        console.warn('Failed to hydrate file metadata for preview:', err);
      }
    }

    setPreviewSessionId(targetSessionId);
    setPreviewFile(normalizedFile);
  };

  // 🆕 检测@输入,显示文件自动补全
  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const value = e.target.value;
    setInput(value);

    // 检测@符号
    const cursorPosition = e.target.selectionStart;
    const textBeforeCursor = value.substring(0, cursorPosition);
    const lastAtIndex = textBeforeCursor.lastIndexOf('@');

    if (lastAtIndex !== -1 && cursorPosition - lastAtIndex <= 20) {
      // 首次使用 @ 文件補全時，按需拉取一次文件列表
      if (sessionId && availableFiles.length === 0) {
        loadAvailableFiles();
      }

      // 在@符号后面,显示自动补全
      const searchText = textBeforeCursor.substring(lastAtIndex + 1);
      const filteredFiles = availableFiles.filter(f =>
        f.name.toLowerCase().includes(searchText.toLowerCase())
      );

      if (filteredFiles.length > 0) {
        setShowFileAutocomplete(true);
      } else {
        setShowFileAutocomplete(false);
      }
    } else {
      setShowFileAutocomplete(false);
    }
  };

  // 🆕 选择@文件 - 光标插入由 ChatInput 内部处理，这里只管附件状态
  const handleSelectFile = (file: FileInfo, _newInput: string) => {
    // 添加到附件列表(如果不存在)
    if (!attachedFiles.find(f => f.name === file.name)) {
      setAttachedFiles([...attachedFiles, file]);
    }
  };

  const markRoundFailed = (runId: string, message: string) => {
    setRounds((prev) =>
      prev.map((round) =>
        round.round_id === runId
          ? {
              ...round,
              status: 'failed',
              completed_at: new Date().toISOString(),
              final_response: round.final_response || message,
            }
          : round
      )
    );
  };

  const updateRoundFinalResponse = (runId: string, content: string) => {
    setRounds((prev) =>
      prev.map((round) =>
        round.round_id === runId
          ? { ...round, final_response: content }
          : round
      )
    );
  };

  const createUpdateLastStep = (getRunId: () => string): StepUpdater => {
    return (patchOrFn) => {
      const runId = getRunId();
      setRounds((prev) =>
        prev.map((round) => {
          if (round.round_id === runId && round.steps.length > 0) {
            const steps = [...round.steps];
            const lastStep = steps[steps.length - 1];
            steps[steps.length - 1] = typeof patchOrFn === 'function'
              ? patchOrFn(lastStep)
              : { ...lastStep, ...patchOrFn };
            return { ...round, steps };
          }
          return round;
        })
      );
    };
  };

  const createStreamCallbacks = ({
    tempRoundId,
    getCurrentRunId,
    setCurrentRunId,
    updateLastStep,
    setBusyFalse,
    onStreamSuccess,
    onStreamError,
    shouldRefreshTitleOnFirstRound = false,
    mirrorTextToFinalResponse = false,
    setRoundRunningOnStart = false,
  }: StreamCallbacksFactoryOptions): StreamCallbacks => ({
    onRunStarted: (_threadId, runId) => {
      setCurrentRunId(runId);

      setRounds((prev) =>
        prev.map((r) =>
          r.round_id === tempRoundId
            ? {
                ...r,
                round_id: runId,
                ...(setRoundRunningOnStart ? { status: 'running' } : {}),
              }
            : r
        )
      );

      setAgentState({
        currentStep: 0,
        status: 'running',
        toolLogs: [],
        lastUpdated: Date.now(),
      });
    },

    onRunFinished: (_threadId, runId, result, outcome, interrupt) => {
      const finalContent = streamingContentRef.current.textContent;
      const targetRunId = runId || getCurrentRunId();
      setCurrentRunId(targetRunId);
      let shouldScheduleTitleRefresh = false;

      setRounds((prev) => {
        const updatedRounds = prev.map((round) => {
          if (round.round_id === targetRunId) {
            const completedSteps = round.steps.map((s) => ({
              ...s,
              status: s.status === 'streaming' || s.status === 'running' ? 'completed' : s.status,
            }));

            return {
              ...round,
              steps: completedSteps,
              final_response: result?.finalResponse || finalContent || round.final_response,
              status: outcome === 'interrupt' ? 'interrupted' : (outcome === 'success' ? 'completed' : outcome),
              completed_at: outcome === 'interrupt' ? undefined : new Date().toISOString(),
            };
          }
          return round;
        });

        if (shouldRefreshTitleOnFirstRound && updatedRounds.length === 1 && outcome === 'success') {
          shouldScheduleTitleRefresh = true;
        }

        return updatedRounds;
      });

      if (shouldScheduleTitleRefresh) {
        if (titleRefreshTimeoutRef.current) {
          clearTimeout(titleRefreshTimeoutRef.current);
        }
        titleRefreshTimeoutRef.current = setTimeout(() => {
          titleRefreshTimeoutRef.current = null;
          console.log('🔄 第一轮对话完成，刷新会话列表获取标题');
          onTitleUpdated?.();
        }, 3000);
      }

      if (outcome === 'interrupt' && interrupt) {
        setPendingInterrupt(interrupt);
        setAgentState((prev) => ({
          ...prev,
          status: 'waiting',
          lastUpdated: Date.now(),
        }));
        setBusyFalse();
        return;
      }

      setAgentState((prev) => ({
        ...prev,
        status: 'completed',
        lastUpdated: Date.now(),
      }));

      setBusyFalse();
      onStreamSuccess();
    },

    onRunError: (errorMsg, code) => {
      setError(`${errorMsg}${code ? ` (${code})` : ''}`);
      markRoundFailed(getCurrentRunId(), errorMsg);
      setBusyFalse();
      setAgentState((prev) => ({ ...prev, status: 'error', lastUpdated: Date.now() }));
      onStreamError(errorMsg, code);
    },

    onStepStarted: (_stepName, timestamp) => {
      setAgentState((prev) => ({
        ...prev,
        currentStep: prev.currentStep + 1,
        lastUpdated: Date.now(),
      }));

      setRounds((prev) =>
        prev.map((round) => {
          if (round.round_id === getCurrentRunId()) {
            const newStepNumber = round.steps.length + 1;
            const newStep = {
              step_number: newStepNumber,
              thinking: '',
              assistant_content: '',
              tool_calls: [],
              tool_results: [],
              status: 'streaming',
              started_at_ts: timestamp,
            };
            return {
              ...round,
              steps: [...round.steps, newStep],
              step_count: newStepNumber,
            };
          }
          return round;
        })
      );
    },

    onStepFinished: (_stepName, timestamp) => {
      updateLastStep({ status: 'completed', finished_at_ts: timestamp });
    },

    onTextMessageStart: (messageId, _role) => {
      streamingContentRef.current.currentTextMessageId = messageId;
      streamingContentRef.current.textContent = '';
    },

    onTextMessageContent: (_messageId, delta) => {
      streamingContentRef.current.textContent += delta;
      updateLastStep({ assistant_content: streamingContentRef.current.textContent });

      if (mirrorTextToFinalResponse) {
        updateRoundFinalResponse(getCurrentRunId(), streamingContentRef.current.textContent);
      }
    },

    onTextMessageEnd: (_messageId) => {
      streamingContentRef.current.currentTextMessageId = null;
    },

    onThinkingStart: (messageId, timestamp) => {
      streamingContentRef.current.currentThinkingMessageId = messageId;
      streamingContentRef.current.thinkingContent = '';
      if (timestamp) {
        updateLastStep({ thinking_start_ts: timestamp });
      }
    },

    onThinkingContent: (_messageId, delta) => {
      streamingContentRef.current.thinkingContent += delta;
      updateLastStep({ thinking: streamingContentRef.current.thinkingContent });
    },

    onThinkingEnd: (_messageId, timestamp) => {
      streamingContentRef.current.currentThinkingMessageId = null;
      if (timestamp) {
        updateLastStep({ thinking_end_ts: timestamp });
      }
    },

    onToolCallStart: (toolCallId, toolName, _parentMessageId, timestamp) => {
      streamingContentRef.current.toolArgs[toolCallId] = '';

      setAgentState((prev) => ({
        ...prev,
        toolLogs: [
          ...prev.toolLogs,
          {
            toolCallId,
            toolName,
            status: 'running',
            startedAt: Date.now(),
          },
        ],
        lastUpdated: Date.now(),
      }));

      updateLastStep((step) => ({
        ...step,
        tool_calls: [
          ...step.tool_calls,
          { id: toolCallId, name: toolName, input: {}, started_at_ts: timestamp },
        ],
      }));
    },

    onToolCallArgs: (toolCallId, delta) => {
      streamingContentRef.current.toolArgs[toolCallId] =
        (streamingContentRef.current.toolArgs[toolCallId] || '') + delta;

      const argsString = streamingContentRef.current.toolArgs[toolCallId];
      try {
        const parsedArgs = JSON.parse(argsString);
        updateLastStep((step) => {
          const toolIndex = step.tool_calls.findIndex((toolCall) => toolCall.id === toolCallId);
          if (toolIndex < 0) return step;
          const updatedToolCalls = [...step.tool_calls];
          updatedToolCalls[toolIndex] = { ...updatedToolCalls[toolIndex], input: parsedArgs };
          return { ...step, tool_calls: updatedToolCalls };
        });
      } catch {
        // JSON 尚未完整，继续累积
      }
    },

    onToolCallEnd: (toolCallId, timestamp) => {
      setAgentState((prev) => ({
        ...prev,
        toolLogs: prev.toolLogs.map((log) =>
          log.toolCallId === toolCallId
            ? { ...log, status: 'pending' as const, args: streamingContentRef.current.toolArgs[toolCallId] }
            : log
        ),
        lastUpdated: Date.now(),
      }));

      updateLastStep((step) => {
        const toolIndex = step.tool_calls.findIndex((toolCall) => toolCall.id === toolCallId);
        if (toolIndex < 0) return step;
        const updatedToolCalls = [...step.tool_calls];
        updatedToolCalls[toolIndex] = { ...updatedToolCalls[toolIndex], ended_at_ts: timestamp };
        return { ...step, tool_calls: updatedToolCalls };
      });
    },

    onToolCallResult: (_messageId, toolCallId, content, timestamp, executionTimeMs) => {
      setAgentState((prev) => ({
        ...prev,
        toolLogs: prev.toolLogs.map((log) =>
          log.toolCallId === toolCallId
            ? { ...log, status: 'completed' as const, result: content, completedAt: Date.now() }
            : log
        ),
        lastUpdated: Date.now(),
      }));

      updateLastStep((step) => {
        let resultObj = { success: true, content, error: undefined as string | undefined };
        try {
          const parsed = JSON.parse(content);
          resultObj = {
            success: !parsed.error,
            content: parsed.output || content,
            error: parsed.error,
          };
        } catch {
          // 保持原始 content
        }

        return {
          ...step,
          tool_results: [
            ...step.tool_results,
            {
              ...resultObj,
              tool_call_id: toolCallId,
              received_at_ts: timestamp,
              execution_time_ms: executionTimeMs,
            },
          ],
        };
      });
    },

    onStateSnapshot: (snapshot) => {
      setAgentState(snapshot);
    },

    onStateDelta: (delta) => {
      setAgentState((prev) => {
        try {
          const result = applyPatch(prev, delta as Operation[], true, false);
          return result.newDocument;
        } catch (e) {
          console.error('Failed to apply state patch:', e);
          return prev;
        }
      });
    },

    onMessagesSnapshot: (messages) => {
      console.log('📥 收到消息快照:', messages.length, '条消息');
    },

    onCustomEvent: (name, value) => {
      if (name === 'title_updated') {
        console.log('✅ 会话标题已更新:', value?.title);
        onTitleUpdated?.();
      }
    },
  });

  const handleSend = async () => {
    if ((!input.trim() && attachedFiles.length === 0) || sending || creatingSession) return;

    const draftInput = input;
    const draftAttachments = [...attachedFiles];

    let contentBlocks: ChatContentBlock[] = [];
    try {
      contentBlocks = buildContentBlocks(draftInput, draftAttachments);
    } catch (err: any) {
      setError(err?.message || '消息构建失败');
      return;
    }
    const userMessage = buildDisplayMessage(draftInput, draftAttachments);

    // 🆕 如果没有 sessionId，先创建会话
    if (!sessionId) {
      if (!onCreateSession) return;
      setCreatingSession(true);
      setInput('');
      setAttachedFiles([]);
      setError('');

      // 暂存消息，等 sessionId 更新后自动发送
      pendingMessageRef.current = {
        displayMessage: userMessage,
        content: contentBlocks,
        attachments: draftAttachments,
      };
      try {
        await onCreateSession(selectedModelId || undefined);
        // sessionId 由父组件设置 → 触发 useEffect → 自动调用 sendMessageForSession
      } catch (err: any) {
        console.error('Failed to create session:', err);
        setError('创建会话失败，请重试');
        pendingMessageRef.current = null;
        // 恢复输入内容
        setInput(draftInput);
        setAttachedFiles(draftAttachments);
        setCreatingSession(false);
      }
      return;
    }

    // 正常发送逻辑
    sendMessageForSession(sessionId, userMessage, contentBlocks, draftAttachments);
  };

  /**
   * 核心发送逻辑：给定 sessionId，发送消息并处理 SSE 流
   * 从 handleSend 提取，同时也供 pendingMessage 自动发送使用
   */
  const sendMessageForSession = async (
    targetSessionId: string,
    userMessage: string,
    contentBlocks: ChatContentBlock[],
    sentAttachments: FileInfo[] = []
  ) => {

    setInput('');
    setAttachedFiles([]);
    setDisableInitialMotion(false);
    setSending(true);
    setError('');
    setPendingInterrupt(null); // 清除未回答的中断

    // 重置流式内容累积器
    streamingContentRef.current = {
      currentTextMessageId: null,
      currentThinkingMessageId: null,
      textContent: '',
      thinkingContent: '',
      toolArgs: {},
    };

    // 通知父组件开始执行
    onExecutionStart?.(targetSessionId);

    // 立即创建并显示用户消息的round（使用临时ID）
    const tempRoundId = `temp-${Date.now()}`;
    const pendingRound: RoundData = {
      round_id: tempRoundId,
      user_message: userMessage,
      user_attachments: [...sentAttachments],
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
    };
    setRounds((prev) => [...prev, pendingRound]);

    // 当前轮次 ID（会在 RUN_STARTED 时更新）
    let currentRunId = tempRoundId;
    const updateLastStep = createUpdateLastStep(() => currentRunId);
    const streamCallbacks = createStreamCallbacks({
      tempRoundId,
      getCurrentRunId: () => currentRunId,
      setCurrentRunId: (runId) => {
        currentRunId = runId;
      },
      updateLastStep,
      setBusyFalse: () => setSending(false),
      onStreamSuccess: () => onExecutionEnd?.(),
      onStreamError: () => onExecutionEnd?.(),
      shouldRefreshTitleOnFirstRound: true,
      mirrorTextToFinalResponse: false,
      setRoundRunningOnStart: false,
    });

    try {
      await apiService.sendMessageStreamV2(targetSessionId, contentBlocks, streamCallbacks);
    } catch (err: any) {
      console.error('Failed to send message:', err);

      let errorMessage = '发送消息失败';
      if (err.message) {
        errorMessage = err.message;
      }

      setError(errorMessage);
      setSending(false);
      setAgentState((prev) => ({ ...prev, status: 'error', lastUpdated: Date.now() }));
      onExecutionEnd?.();
    }
  };

  // 🆕 判断输入区是否禁用（正在创建会话 or 正在发送/恢复）
  // 注意：即使存在 pendingInterrupt，也允许用户直接发送新消息跳过中断问题。
  const inputDisabled = sending || creatingSession || resuming;

  /** 当前发送按钮的 loading 文案（空 = 非 loading） */
  const sendingLabel = creatingSession ? '创建中' : resuming ? 'Resuming' : sending ? 'Running' : '';

  /** 停止生成：调用后端 abort API，SSE 不断开，等后端推 RUN_FINISHED(interrupt) */
  const handleStop = async () => {
    if (!sessionId || !(sending || resuming)) return;
    try {
      await apiService.abortChat(sessionId);
    } catch (err) {
      console.warn('Abort request failed (Agent may have already finished):', err);
    }
  };

  /** 🆕 提交中断回答，恢复 Agent 执行 */
  const handleResumeSubmit = async (answers: Record<string, string>) => {
    const interruptSnapshot = pendingInterrupt;
    if (!sessionId || !interruptSnapshot?.id) return;

    const interruptId = interruptSnapshot.id;
    const resumeTempRoundId = `resume-temp-${Date.now()}`;
    let currentResumeRunId = resumeTempRoundId;
    const updateLastStep = createUpdateLastStep(() => currentResumeRunId);

    const resumeEntries = Object.entries(answers);
    const resumeUserMessage = resumeEntries.length > 0
      ? resumeEntries
          .map(([question, answer], index) => {
            const safeQuestion = question?.trim() || '(Untitled question)';
            const safeAnswer = answer?.trim() || '[No preference]';
            return `${index > 0 ? '\n\n' : ''}Q: ${safeQuestion}\nA: ${safeAnswer}`;
          })
          .join('')
      : 'Q: (No question)\nA: [No preference]';

    setPendingInterrupt(null);
    setResuming(true);
    setError('');

    // 追加一个新的 resume 轮次，保持旧 interrupted 轮次不变
    setRounds((prev) => [
      ...prev,
      {
        round_id: resumeTempRoundId,
        user_message: resumeUserMessage,
        user_attachments: [],
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ]);

    // 重置流式内容累积器
    streamingContentRef.current = {
      currentTextMessageId: null,
      currentThinkingMessageId: null,
      textContent: '',
      thinkingContent: '',
      toolArgs: {},
    };

    onExecutionStart?.(sessionId);

    const resumeCallbacks = createStreamCallbacks({
      tempRoundId: resumeTempRoundId,
      getCurrentRunId: () => currentResumeRunId,
      setCurrentRunId: (runId) => {
        currentResumeRunId = runId;
      },
      updateLastStep,
      setBusyFalse: () => setResuming(false),
      onStreamSuccess: () => onExecutionEnd?.(),
      onStreamError: () => {
        setPendingInterrupt(interruptSnapshot);
        onExecutionEnd?.();
      },
      shouldRefreshTitleOnFirstRound: false,
      mirrorTextToFinalResponse: true,
      setRoundRunningOnStart: true,
    });

    try {
      await apiService.resumeStream(sessionId, interruptId, answers, resumeCallbacks);
    } catch (err: any) {
      console.error('Failed to resume:', err);
      const message = err.message || '恢复执行失败';
      setError(message);
      markRoundFailed(currentResumeRunId, message);
      setPendingInterrupt(interruptSnapshot);
      setResuming(false);
      onExecutionEnd?.();
    }
  };

  return (
    <div className="flex-1 flex h-screen bg-claude-bg relative">
      {/* Main Chat Area */}
      <div
        className="flex-1 flex flex-col relative"
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {/* 拖拽提示 */}
        {isDragging && (
          <div className="absolute inset-0 flex items-center justify-center bg-claude-accent/5 backdrop-blur-sm z-50 pointer-events-none border-2 border-dashed border-claude-accent/40 rounded-2xl m-4">
            <div className="text-center">
              <Paperclip className="w-8 h-8 text-claude-accent mx-auto mb-3" />
              <p className="text-claude-text font-medium mb-1">释放以上传文件</p>
              <p className="text-claude-muted text-sm">{sessionId ? '支持拖放到对话框任意位置' : '支持拖放到输入框或页面'}</p>
            </div>
          </div>
        )}

        {/* Header */}
        <header className="h-14 flex items-center justify-between px-6 bg-claude-bg/80 backdrop-blur-sm border-b border-claude-border sticky top-0 z-20">
          <ModelSelector
            selectedModelId={selectedModelId}
            onModelChange={onModelChange}
            availableModels={availableModels}
            readOnly={!!sessionId}
          />

          {sessionId ? (
            <button
              type="button"
              onClick={() => setIsFilesOpen(!isFilesOpen)}
              className={`p-2 rounded-lg transition-all active:scale-95 flex items-center gap-2 ${
                isFilesOpen
                  ? 'bg-claude-text text-white'
                  : 'text-claude-secondary hover:bg-claude-hover'
              }`}
              title="会话资源"
            >
              <Folder size={16} />
              <span className="text-sm hidden sm:inline">Files</span>
            </button>
          ) : (
            <div className="w-[88px]" />
          )}
        </header>

        {/* Messages Area - Design System 风格 */}
        <div
          ref={chatAreaRef}
          className="flex-1 overflow-y-auto relative bg-claude-bg"
        >
          {loading ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <Loader2 className="w-6 h-6 text-claude-muted animate-spin mx-auto mb-3" />
                <p className="text-claude-muted text-sm">正在同步会话...</p>
              </div>
            </div>
          ) : rounds.length === 0 ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-center max-w-lg px-6">
                <h2 className="text-3xl font-medium text-claude-text mb-3">
                  你好，有什么可以帮你的？
                </h2>
                <p className="text-claude-secondary leading-relaxed mb-10">
                  编写代码、分析数据、处理文件，或者解答技术问题。
                </p>

                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {WELCOME_SUGGESTIONS.map((suggestion, i) => (
                    <button
                      type="button"
                      key={i}
                      onClick={() => setInput(suggestion)}
                      className="px-4 py-3 bg-white border border-claude-border rounded-2xl text-sm text-claude-secondary hover:border-claude-border-strong hover:bg-claude-hover transition-colors text-left"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="mx-auto px-4 md:px-8 py-6 space-y-6 max-w-3xl">
              {rounds.map((round, index) => (
                <Round
                  key={round.round_id}
                  round={round}
                  userAttachments={round.user_attachments || []}
                  sessionId={sessionId}
                  authSessionId={apiService.getUserId?.() || ''}
                  onPreviewAttachment={handlePreviewAttachment}
                  isStreaming={(sending || resuming) && index === rounds.length - 1}
                  disableMotion={disableInitialMotion}
                />
              ))}

              <div ref={messagesEndRef} />
            </div>
          )}

          {/* 🔥 回到底部按钮 */}
          {showScrollButton && (
            <button
              type="button"
              onClick={() => scrollToBottom(true)}
              className="fixed bottom-28 right-8 bg-white text-claude-text p-2.5 rounded-full shadow-lg border border-claude-border transition-all hover:scale-105 active:scale-95 z-10"
              aria-label="回到底部"
            >
              <ArrowDown className="w-4 h-4" />
            </button>
          )}
        </div>

        {/* Error Message */}
        {error && (
          <div className="px-6 py-3 bg-red-50 border-t border-red-100">
            <div className="mx-auto max-w-3xl">
              <div className="flex items-start gap-3">
                <AlertCircle className="w-4 h-4 text-claude-error flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0 overflow-hidden">
                  <pre className="text-xs whitespace-pre-wrap break-words font-mono text-claude-error overflow-x-auto max-w-full">
                    {error}
                  </pre>
                </div>
                <button
                  onClick={() => setError('')}
                  className="p-1 hover:bg-red-100 rounded-full transition-colors"
                  aria-label="关闭错误提示"
                >
                  <X className="w-3.5 h-3.5 text-claude-error" />
                </button>
              </div>
            </div>
          </div>
        )}

        {/* 🆕 Human-in-the-Loop 问题浮层 — 盖在输入框上方 */}
        {pendingInterrupt && pendingInterrupt.reason === 'input_required' && pendingInterrupt.payload?.questions && (pendingInterrupt.payload.questions as AskUserQuestion[]).length > 0 && (
          <div className="relative z-20 px-4 md:px-8 mb-[-3.5rem] mx-auto w-full max-w-3xl">
            <QuestionCard
              questions={pendingInterrupt.payload.questions as AskUserQuestion[]}
              onSubmit={handleResumeSubmit}
              disabled={resuming}
            />
          </div>
        )}

        {/* Input Area */}
        <ChatInput
          value={input}
          onChange={setInput}
          onSend={handleSend}
          onStop={(sending || resuming) ? handleStop : undefined}
          disabled={inputDisabled}
          sendingLabel={sendingLabel}
          placeholder={sessionId ? '输入指令...' : '输入你的问题，按 Enter 开始对话...'}
          attachedFiles={attachedFiles}
          onRemoveAttachment={handleRemoveAttachment}
          onFileUpload={handleFileUpload}
          onInputDropHandled={() => setIsDragging(false)}
          onPreviewAttachment={sessionId ? handlePreviewAttachment : undefined}
          uploading={uploading}
          availableFiles={availableFiles}
          showFileAutocomplete={showFileAutocomplete}
          onInputChangeRaw={handleInputChange}
          onFileSelected={handleSelectFile}
          onDismissAutocomplete={() => setShowFileAutocomplete(false)}
        />
      </div>

      {/* Artifacts Panel - 滑入式文件面板 */}
      {sessionId && (
        <ArtifactsPanel
          sessionId={sessionId}
          isOpen={isFilesOpen}
          onClose={() => setIsFilesOpen(false)}
          onFilePreview={(file) => {
            setPreviewSessionId(sessionId);
            setPreviewFile(file);
          }}
        />
      )}

      {/* File Preview Overlay - 全屏文件预览 */}
      {previewFile && previewSessionId && (
        <FilePreview
          sessionId={previewSessionId}
          file={previewFile}
          onClose={() => {
            setPreviewFile(null);
            setPreviewSessionId('');
          }}
        />
      )}
    </div>
  );
}
