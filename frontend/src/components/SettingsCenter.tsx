import React, { useCallback, useEffect, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Blocks,
  Cable,
  Check,
  Layers,
  Loader2,
  Pencil,
  Shield,
  Sparkles,
  UserRound,
  X,
} from 'lucide-react';
import {
  getAgentFile,
  getSkills,
  toggleSkill,
  updateAgentFile,
  type AgentFileDetail,
  type SkillInfo,
  type SkillSandboxStatus,
} from '../services/configApi';

const LazyMcpConnectionsPanel = React.lazy(() => import('./McpConnectionsPanel'));
const LazyToolPermissionsPanel = React.lazy(() => import('./ToolPermissionsPanel'));

type AgentFileName = 'memory' | 'user' | 'soul';
type SettingsSection = 'memory' | 'soul' | 'connections' | 'permissions';
type MemoryTab = 'main' | 'user';
type SoulTab = 'role' | 'skills';

interface SettingsCenterProps {
  onClose?: () => void;
  onUnsavedChangesChange?: (hasUnsavedChanges: boolean) => void;
  initialSection?: SettingsSection;
  initialMemoryTab?: MemoryTab;
  initialSoulTab?: SoulTab;
}

interface FileState {
  content: string;
  original: string;
  version: number;
  loading: boolean;
  saving: boolean;
  editing: boolean;
  error: string;
}

interface LoadFileOptions {
  enterEditing?: boolean;
  skipIfDirty?: boolean;
}

const FILE_TITLES: Record<AgentFileName, { title: string; desc: string }> = {
  memory: {
    title: '主记忆 · MEMORY.md',
    desc: '所有对话共享，跨会话生效',
  },
  user: {
    title: '用户画像 · USER.md',
    desc: '了解你在帮的人，边走边更新',
  },
  soul: {
    title: 'Soul · 灵魂设定',
    desc: '影响 OpenCapyBox 的语气、视角和判断边界',
  },
};

const emptyFileState = (): FileState => ({
  content: '',
  original: '',
  version: 0,
  loading: true,
  saving: false,
  editing: false,
  error: '',
});

function fileErrorMessage(err: unknown): string {
  if (err instanceof Error) return err.message;
  return '加载失败，请稍后重试';
}

function hasDirtyContent(state: FileState): boolean {
  return state.editing && state.content !== state.original;
}

function fileForMemoryTab(tab: MemoryTab): AgentFileName {
  return tab === 'main' ? 'memory' : 'user';
}

function MarkdownView({ content }: { content: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      className="prose max-w-none text-[13.5px] leading-7 text-[#1c1a16] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0 [&_h1]:text-xl [&_h2]:text-lg [&_h3]:text-base [&_li]:my-1 [&_p]:my-3"
    >
      {content}
    </ReactMarkdown>
  );
}

function getInitialState(
  initialSection?: SettingsSection,
  initialMemoryTab?: MemoryTab,
  initialSoulTab?: SoulTab,
) {
  const section = initialSection ?? 'memory';
  return {
    section,
    memoryTab: initialMemoryTab ?? 'user',
    soulTab: initialSoulTab ?? 'role',
  };
}

const SettingsCenter: React.FC<SettingsCenterProps> = ({
  onClose,
  onUnsavedChangesChange,
  initialSection,
  initialMemoryTab,
  initialSoulTab,
}) => {
  const initial = getInitialState(initialSection, initialMemoryTab, initialSoulTab);
  const [activeSection, setActiveSection] = useState<SettingsSection>(initial.section);
  const [connectionsVisited, setConnectionsVisited] = useState(initial.section === 'connections');
  const [permissionsVisited, setPermissionsVisited] = useState(initial.section === 'permissions');
  const [mcpDirty, setMcpDirty] = useState(false);
  const [permissionsRefreshToken, setPermissionsRefreshToken] = useState(0);
  const [memoryTab, setMemoryTab] = useState<MemoryTab>(initial.memoryTab);
  const [soulTab, setSoulTab] = useState<SoulTab>(initial.soulTab);
  const [files, setFiles] = useState<Record<AgentFileName, FileState>>({
    memory: emptyFileState(),
    user: emptyFileState(),
    soul: emptyFileState(),
  });
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [skillsError, setSkillsError] = useState('');
  const [skillSandboxStatus, setSkillSandboxStatus] = useState<SkillSandboxStatus | null>(null);
  const [togglingSkills, setTogglingSkills] = useState<Set<string>>(() => new Set());
  const [savedFlash, setSavedFlash] = useState<AgentFileName | ''>('');
  const filesRef = useRef(files);
  const togglingSkillsRef = useRef<Set<string>>(new Set());
  const skillMutationVersionsRef = useRef<Map<string, number>>(new Map());
  const savedTimerRef = useRef<number | null>(null);
  const fileLoadSeqRef = useRef<Record<AgentFileName, number>>({
    memory: 0,
    user: 0,
    soul: 0,
  });
  const skillsLoadSeqRef = useRef(0);
  const initialPropsAppliedRef = useRef(false);

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  const loadFile = useCallback(async (name: AgentFileName, options: LoadFileOptions = {}) => {
    if (options.skipIfDirty && hasDirtyContent(filesRef.current[name])) {
      return;
    }

    const seq = ++fileLoadSeqRef.current[name];

    setFiles((prev) => {
      const current = prev[name];
      if (options.skipIfDirty && hasDirtyContent(current)) {
        return prev;
      }

      return {
        ...prev,
        [name]: { ...current, loading: true, error: '' },
      };
    });

    try {
      const detail: AgentFileDetail = await getAgentFile(name);
      setFiles((prev) => ({
        ...prev,
        [name]: (() => {
          const current = prev[name];
          if (seq !== fileLoadSeqRef.current[name]) return current;
          if (options.skipIfDirty && hasDirtyContent(current)) {
            return { ...current, loading: false };
          }

          return {
            ...current,
            content: detail.content,
            original: detail.content,
            version: detail.version,
            loading: false,
            editing: options.enterEditing ? true : current.editing,
            error: '',
          };
        })(),
      }));
    } catch (err) {
      setFiles((prev) => ({
        ...prev,
        [name]: (() => {
          const current = prev[name];
          if (seq !== fileLoadSeqRef.current[name]) return current;

          return {
            ...current,
            content: options.enterEditing ? current.content : '',
            original: options.enterEditing ? current.original : '',
            loading: false,
            editing: options.enterEditing ? false : current.editing,
            error: fileErrorMessage(err),
          };
        })(),
      }));
    }
  }, []);

  const loadSkills = useCallback(async () => {
    const seq = ++skillsLoadSeqRef.current;
    const mutationVersionsAtStart = new Map(skillMutationVersionsRef.current);
    setSkillsLoading(true);
    setSkillsError('');
    setSkillSandboxStatus(null);
    try {
      const result = await getSkills();
      if (seq !== skillsLoadSeqRef.current) return;
      setSkills((previousSkills) => {
        const previousByName = new Map(
          previousSkills.map((skill) => [skill.name, skill]),
        );
        return result.skills.map((skill) => {
          const mutationOverlappedLoad = (
            (skillMutationVersionsRef.current.get(skill.name) ?? 0)
            !== (mutationVersionsAtStart.get(skill.name) ?? 0)
          );
          if (
            !mutationOverlappedLoad
            && !togglingSkillsRef.current.has(skill.name)
          ) return skill;
          const optimisticSkill = previousByName.get(skill.name);
          return optimisticSkill
            ? { ...skill, enabled: optimisticSkill.enabled }
            : skill;
        });
      });
      setSkillSandboxStatus(result.sandbox_status);
    } catch (err) {
      if (seq !== skillsLoadSeqRef.current) return;
      setSkillsError(fileErrorMessage(err));
    } finally {
      if (seq === skillsLoadSeqRef.current) {
        setSkillsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    const next = getInitialState(initialSection, initialMemoryTab, initialSoulTab);
    setActiveSection(next.section);
    setMemoryTab(next.memoryTab);
    setSoulTab(next.soulTab);

    if (next.section === 'connections') setConnectionsVisited(true);
    if (next.section === 'permissions') setPermissionsVisited(true);
    const fileToRefresh = next.section === 'memory'
      ? fileForMemoryTab(next.memoryTab)
      : next.section === 'soul' && next.soulTab === 'role'
        ? 'soul'
        : null;
    if (initialPropsAppliedRef.current && fileToRefresh) {
      void loadFile(fileToRefresh, { skipIfDirty: true });
    }
    initialPropsAppliedRef.current = true;
  }, [initialMemoryTab, initialSection, initialSoulTab, loadFile]);

  useEffect(() => {
    void Promise.all((['memory', 'user', 'soul'] as AgentFileName[]).map((name) => loadFile(name)));
  }, [loadFile]);

  useEffect(() => {
    if (activeSection === 'soul' && soulTab === 'skills') {
      void loadSkills();
    }
  }, [activeSection, loadSkills, soulTab]);

  useEffect(() => () => {
    if (savedTimerRef.current !== null) {
      window.clearTimeout(savedTimerRef.current);
    }
  }, []);

  const enabledSkillCount = skills.filter((skill) => skill.enabled).length;
  const hasUnsavedChanges = mcpDirty || Object.values(files).some(
    (state) => state.editing && state.content !== state.original,
  );

  useEffect(() => {
    onUnsavedChangesChange?.(hasUnsavedChanges);
  }, [hasUnsavedChanges, onUnsavedChangesChange]);

  const startEdit = (name: AgentFileName) => {
    setSavedFlash('');
    void loadFile(name, { enterEditing: true, skipIfDirty: true });
  };

  const activateSection = (section: SettingsSection) => {
    setActiveSection(section);
    if (section === 'connections') setConnectionsVisited(true);
    if (section === 'permissions') setPermissionsVisited(true);
    const fileToRefresh = section === 'memory'
      ? fileForMemoryTab(memoryTab)
      : section === 'soul' && soulTab === 'role'
        ? 'soul'
        : null;
    if (fileToRefresh) {
      void loadFile(fileToRefresh, { skipIfDirty: true });
    }
  };

  const activateMemoryTab = (tab: MemoryTab) => {
    void loadFile(fileForMemoryTab(tab), { skipIfDirty: true });
    setMemoryTab(tab);
  };

  const activateSoulTab = (tab: SoulTab) => {
    if (tab === 'role') {
      void loadFile('soul', { skipIfDirty: true });
    }
    setSoulTab(tab);
  };

  const cancelEdit = (name: AgentFileName) => {
    setFiles((prev) => ({
      ...prev,
      [name]: {
        ...prev[name],
        content: prev[name].original,
        editing: false,
        error: '',
      },
    }));
  };

  const updateContent = (name: AgentFileName, content: string) => {
    setFiles((prev) => ({
      ...prev,
      [name]: { ...prev[name], content },
    }));
  };

  const saveFile = async (name: AgentFileName) => {
    const current = files[name];
    if (current.saving || current.content === current.original) return;
    const savedContent = current.content;

    setFiles((prev) => ({
      ...prev,
      [name]: { ...prev[name], saving: true, error: '' },
    }));

    try {
      const result = await updateAgentFile(name, savedContent);
      setFiles((prev) => ({
        ...prev,
        [name]: {
          ...prev[name],
          original: savedContent,
          version: result.version,
          saving: false,
          editing: false,
          error: '',
        },
      }));
      setSavedFlash(name);
      if (savedTimerRef.current !== null) {
        window.clearTimeout(savedTimerRef.current);
      }
      savedTimerRef.current = window.setTimeout(() => setSavedFlash(''), 1800);
    } catch (err) {
      setFiles((prev) => ({
        ...prev,
        [name]: {
          ...prev[name],
          saving: false,
          error: fileErrorMessage(err),
        },
      }));
    }
  };

  const handleSkillToggle = async (skillName: string, currentEnabled: boolean) => {
    if (togglingSkillsRef.current.has(skillName)) return;
    const bumpMutationVersion = () => {
      const versions = skillMutationVersionsRef.current;
      versions.set(skillName, (versions.get(skillName) ?? 0) + 1);
    };
    const setSkillToggling = (isToggling: boolean) => {
      const next = new Set(togglingSkillsRef.current);
      if (isToggling) {
        next.add(skillName);
      } else {
        next.delete(skillName);
      }
      togglingSkillsRef.current = next;
      setTogglingSkills(next);
    };

    bumpMutationVersion();
    setSkillToggling(true);
    setSkillsError('');
    setSkills((prev) =>
      prev.map((skill) =>
        skill.name === skillName ? { ...skill, enabled: !currentEnabled } : skill,
      ),
    );

    try {
      await toggleSkill(skillName, !currentEnabled);
    } catch (err) {
      setSkills((prev) =>
        prev.map((skill) =>
          skill.name === skillName ? { ...skill, enabled: currentEnabled } : skill,
        ),
      );
      setSkillsError(fileErrorMessage(err));
    } finally {
      bumpMutationVersion();
      setSkillToggling(false);
    }
  };

  const renderHeaderAction = (name: AgentFileName, emptyLabel = '编辑') => {
    const state = files[name];
    if (savedFlash === name) {
      return (
        <span className="ml-auto inline-flex items-center gap-1 text-xs font-semibold text-[#3f7a4f]">
          <Check size={13} strokeWidth={2.2} />
          已保存
        </span>
      );
    }

    if (state.editing) return null;

    return (
      <button
        type="button"
        onClick={() => startEdit(name)}
        disabled={state.loading || state.saving}
        className="ml-auto inline-flex h-8 shrink-0 items-center gap-1.5 rounded-[9px] border border-[#e8e3d9] bg-white px-3 text-[13px] font-semibold text-[#1c1a16] transition hover:bg-[#f6f2ea] focus:outline-none focus:ring-2 focus:ring-[#b8814a]/25"
      >
        <Pencil size={13} />
        <span>{emptyLabel}</span>
      </button>
    );
  };

  const renderEditableBody = (
    name: AgentFileName,
    placeholder: string,
    view: React.ReactNode,
  ) => {
    const state = files[name];

    if (state.loading) {
      return (
        <div className="flex min-h-[180px] items-center justify-center text-sm text-[#a39c8e]">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          加载中...
        </div>
      );
    }

    if (state.editing) {
      const dirty = state.content !== state.original;
      return (
        <>
          <textarea
            value={state.content}
            onChange={(event) => updateContent(name, event.target.value)}
            placeholder={placeholder}
            spellCheck={false}
            disabled={state.saving}
            className="min-h-[220px] w-full resize-y rounded-xl border border-[#e8e3d9] bg-[#fdfcfa] px-3.5 py-3 font-mono text-[13.5px] leading-7 text-[#1c1a16] outline-none transition focus:border-[#b8814a] focus:ring-2 focus:ring-[#b8814a]/20 disabled:cursor-not-allowed disabled:opacity-70"
          />
          {state.error && (
            <p className="mt-2 text-xs font-medium text-claude-error">{state.error}</p>
          )}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={() => saveFile(name)}
              disabled={state.saving || !dirty}
              className={`inline-flex h-8 items-center gap-1.5 rounded-[9px] px-3.5 text-[13px] font-semibold transition focus:outline-none focus:ring-2 focus:ring-[#b8814a]/25 ${
                dirty
                  ? 'bg-[#b8814a] text-white hover:bg-[#8a5a2f]'
                  : 'border border-[#e8e3d9] bg-[#f6f2ea] text-[#a39c8e] cursor-not-allowed'
              }`}
            >
              {state.saving && <Loader2 size={13} className="animate-spin" />}
              保存
            </button>
            <button
              type="button"
              onClick={() => cancelEdit(name)}
              disabled={state.saving}
              className="h-8 rounded-[9px] border border-[#e8e3d9] bg-white px-3.5 text-[13px] font-semibold text-[#1c1a16] transition hover:bg-[#f6f2ea] focus:outline-none focus:ring-2 focus:ring-[#b8814a]/25 disabled:cursor-not-allowed disabled:opacity-60"
            >
              取消
            </button>
          </div>
        </>
      );
    }

    if (state.error) {
      return <p className="text-sm text-claude-error">{state.error}</p>;
    }

    return view;
  };

  const renderCardShell = (
    name: AgentFileName,
    icon: React.ReactNode,
    actionLabel: string,
    children: React.ReactNode,
  ) => (
    <section className="mb-4 max-w-[680px] rounded-2xl border border-[#e8e3d9] bg-white px-6 py-6 shadow-[0_1px_3px_rgba(30,26,20,0.05)]">
      <div className="mb-4 flex items-start gap-3">
        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-[10px] bg-[#f5ece2] text-[#8a5a2f]">
          {icon}
        </div>
        <div className="min-w-0">
          <div className="text-[15px] font-bold text-[#1c1a16]">{FILE_TITLES[name].title}</div>
          <div className="mt-0.5 text-xs text-[#a39c8e]">{FILE_TITLES[name].desc}</div>
        </div>
        {renderHeaderAction(name, actionLabel)}
      </div>
      {children}
    </section>
  );

  const renderMemory = () => {
    const state = files.memory;
    return renderCardShell(
      'memory',
      <Layers size={18} />,
      state.content.trim() ? '编辑' : '添加',
      renderEditableBody(
        'memory',
        '记录一些值得长期记住的信息...',
        state.content.trim() ? (
          <MarkdownView content={state.content} />
        ) : (
          <div className="flex flex-col items-center justify-center px-5 py-11 text-center text-[#a39c8e]">
            <Layers size={34} strokeWidth={1.3} className="mb-3 opacity-60" />
            <div className="mb-1 text-sm font-semibold text-[#6f6960]">还没有记忆内容</div>
            <div className="max-w-[280px] text-xs leading-6">
              继续对话，OpenCapyBox 会自动记录值得记住的信息，你也可以现在手动添加。
            </div>
          </div>
        ),
      ),
    );
  };

  const renderUserProfile = () => {
    const state = files.user;

    return renderCardShell(
      'user',
      <UserRound size={18} />,
      state.content.trim() ? '编辑' : '添加',
      renderEditableBody(
        'user',
        '记录用户画像、偏好和背景信息...',
        state.content.trim() ? (
          <MarkdownView content={state.content} />
        ) : (
          <div className="flex flex-col items-center justify-center px-5 py-11 text-center text-[#a39c8e]">
            <UserRound size={34} strokeWidth={1.3} className="mb-3 opacity-60" />
            <div className="mb-1 text-sm font-semibold text-[#6f6960]">还没有用户画像</div>
            <div className="max-w-[280px] text-xs leading-6">添加背景、偏好和称呼方式，让后续对话更顺手。</div>
          </div>
        ),
      ),
    );
  };

  const renderSoul = () => {
    const state = files.soul;
    return renderCardShell(
      'soul',
      <Sparkles size={18} />,
      state.content.trim() ? '编辑' : '添加',
      renderEditableBody(
        'soul',
        '定义智能体的语气、视角和判断边界...',
        state.content.trim() ? (
          <MarkdownView content={state.content} />
        ) : (
          <div className="flex flex-col items-center justify-center px-5 py-11 text-center text-[#a39c8e]">
            <Sparkles size={34} strokeWidth={1.3} className="mb-3 opacity-60" />
            <div className="mb-1 text-sm font-semibold text-[#6f6960]">还没有能力设定</div>
            <div className="max-w-[280px] text-xs leading-6">写下回复风格、工作边界和判断偏好。</div>
          </div>
        ),
      ),
    );
  };

  const renderSkills = () => (
    <section className="mb-4 max-w-[680px] rounded-2xl border border-[#e8e3d9] bg-white px-6 py-6 shadow-[0_1px_3px_rgba(30,26,20,0.05)]">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-[#a39c8e]">
          {skillsLoading ? '正在加载技能...' : `${enabledSkillCount} / ${skills.length} 项技能已启用`}
        </div>
        <button
          type="button"
          disabled
          className="inline-flex h-8 cursor-default items-center gap-2 rounded-[9px] border border-[#e8e3d9] bg-white px-3.5 text-[13px] font-semibold text-[#1c1a16] opacity-60"
        >
          浏览更多技能
          <span className="rounded-md bg-[#f5ece2] px-1.5 py-0.5 text-[10px] font-bold text-[#8a5a2f]">
            即将上线
          </span>
        </button>
      </div>

      {skillsError && <p className="mb-3 text-xs font-medium text-claude-error">{skillsError}</p>}

      {skillSandboxStatus === 'unavailable' && (
        <p
          role="status"
          className="mb-3 rounded-lg border border-[#ead8bd] bg-[#fff8ec] px-3 py-2 text-xs font-medium text-[#8a5a2f]"
        >
          用户技能暂时无法读取，以下仅显示官方技能。
        </p>
      )}

      {skillSandboxStatus === 'not_created' && (
        <p
          role="status"
          className="mb-3 rounded-lg border border-[#e8e3d9] bg-[#faf8f3] px-3 py-2 text-xs font-medium text-[#6f6960]"
        >
          尚未创建工作沙箱，以下仅显示官方技能。
        </p>
      )}

      {skillsLoading ? (
        <div className="flex h-32 items-center justify-center text-sm text-[#a39c8e]">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          加载中...
        </div>
      ) : skills.length === 0 ? (
        <div className="flex flex-col items-center justify-center px-5 py-11 text-center text-[#a39c8e]">
          <Blocks size={34} strokeWidth={1.3} className="mb-3 opacity-60" />
          <div className="mb-1 text-sm font-semibold text-[#6f6960]">没有可用技能</div>
          <div className="max-w-[280px] text-xs leading-6">安装或创建技能后，会出现在这里。</div>
        </div>
      ) : (
        <div>
          {skills.map((skill) => {
            const isToggling = togglingSkills.has(skill.name);
            return (
              <div
                key={skill.name}
                className="flex items-center gap-3.5 border-b border-[#f1ede4] px-1 py-3.5 last:border-b-0"
              >
                <div className="flex h-[34px] w-[34px] shrink-0 items-center justify-center rounded-[9px] bg-[#f4f0e9] text-[#6f6960]">
                  <Blocks size={16} />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="truncate font-mono text-[13.5px] font-bold text-[#1c1a16]">
                      {skill.name}
                    </span>
                    <span className={`shrink-0 rounded-md px-1.5 py-0.5 text-[10.5px] font-semibold ${
                      skill.source === 'user'
                        ? 'bg-[#e7f2eb] text-[#477057]'
                        : 'bg-[#f0ece3] text-[#8a8377]'
                    }`}>
                      {skill.source === 'user' ? '用户' : '官方'}
                    </span>
                    {skill.category && skill.category !== 'user' && (
                      <span className="shrink-0 rounded-md bg-[#f0ece3] px-1.5 py-0.5 text-[10.5px] font-semibold text-[#8a8377]">
                        {skill.category}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-[#a39c8e]">
                    {skill.description || '无描述'}
                  </div>
                </div>
                <button
                  type="button"
                  role="switch"
                  aria-checked={skill.enabled}
                  aria-label={`${skill.enabled ? '禁用' : '启用'} ${skill.name}`}
                  disabled={isToggling}
                  onClick={() => handleSkillToggle(skill.name, skill.enabled)}
                  className={`relative h-[22px] w-[38px] shrink-0 rounded-full transition focus:outline-none focus:ring-2 focus:ring-[#b8814a]/30 ${
                    skill.enabled ? 'bg-[#b8814a]' : 'bg-[#e3ddd0]'
                  } ${isToggling ? 'opacity-60' : ''}`}
                >
                  <span
                    className={`absolute top-0.5 h-[18px] w-[18px] rounded-full bg-white shadow-[0_1px_3px_rgba(0,0,0,0.2)] transition ${
                      skill.enabled ? 'left-[18px]' : 'left-0.5'
                    }`}
                  />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );

  const navItems = [
    { id: 'memory' as const, label: '我的记忆', icon: <Layers size={17} /> },
    { id: 'soul' as const, label: '能力设定', icon: <Sparkles size={17} /> },
    { id: 'connections' as const, label: '数据连接', icon: <Cable size={17} /> },
    { id: 'permissions' as const, label: '权限管控', icon: <Shield size={17} /> },
  ];

  return (
    <div className="flex h-full flex-col overflow-hidden rounded-[22px] bg-[#fdfbf8] text-[#1c1a16]">
      <header className="flex shrink-0 items-center justify-between px-4 py-3 sm:px-5 sm:py-5">
        <span className="text-[13px] font-semibold text-[#a39c8e]">设置</span>
        {onClose && (
          <button
            type="button"
            aria-label="关闭设置"
            onClick={onClose}
            className="flex h-[30px] w-[30px] items-center justify-center rounded-lg text-[#6f6960] transition hover:bg-black/[0.04] focus:outline-none focus:ring-2 focus:ring-[#b8814a]/25"
          >
            <X size={16} />
          </button>
        )}
      </header>

      <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
        <nav
          aria-label="设置分区"
          className="flex w-full shrink-0 flex-row gap-1 overflow-x-auto border-b border-[#e8e3d9] px-3 pb-3 pt-1 sm:w-[180px] sm:flex-col sm:gap-0.5 sm:overflow-y-auto sm:border-b-0 sm:px-2 sm:pb-5"
        >
          {navItems.map((item) => {
            const active = activeSection === item.id;
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => activateSection(item.id)}
                className={`flex shrink-0 items-center gap-2.5 whitespace-nowrap rounded-[10px] px-3 py-2.5 text-[14.5px] font-medium transition focus:outline-none focus:ring-2 focus:ring-[#b8814a]/20 ${
                  active
                    ? 'bg-white font-bold text-[#8a5a2f] shadow-[0_1px_3px_rgba(20,16,10,0.08)]'
                    : 'text-[#6f6960] hover:bg-black/[0.035]'
                }`}
              >
                <span className={active ? 'opacity-100' : 'opacity-75'}>{item.icon}</span>
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <main className="min-h-0 min-w-0 flex-1 overflow-y-auto px-4 pb-8 pt-5 sm:px-8 sm:pb-10 sm:pt-8">
          {activeSection === 'memory' ? (
            <>
              <h2 className="mb-1.5 text-2xl font-bold tracking-[-0.01em] text-[#1c1a16]">我的记忆</h2>
              <p className="mb-6 max-w-[640px] text-sm leading-7 text-[#6f6960]">
                这是属于你的长期记忆，跨会话保留。AI 会在对话中自动更新，你也可以随时手动编辑。
              </p>
              <div className="mb-5 flex gap-5 border-b border-[#e8e3d9]">
                <button
                  type="button"
                  onClick={() => activateMemoryTab('user')}
                  className={`mb-[-1px] border-b-2 px-0.5 pb-2.5 text-[14.5px] font-semibold transition ${
                    memoryTab === 'user'
                      ? 'border-[#b8814a] text-[#1c1a16]'
                      : 'border-transparent text-[#a39c8e] hover:text-[#6f6960]'
                  }`}
                >
                  用户画像
                </button>
                <button
                  type="button"
                  onClick={() => activateMemoryTab('main')}
                  className={`mb-[-1px] border-b-2 px-0.5 pb-2.5 text-[14.5px] font-semibold transition ${
                    memoryTab === 'main'
                      ? 'border-[#b8814a] text-[#1c1a16]'
                      : 'border-transparent text-[#a39c8e] hover:text-[#6f6960]'
                  }`}
                >
                  主记忆
                </button>
              </div>
              {memoryTab === 'main' ? renderMemory() : renderUserProfile()}
            </>
          ) : activeSection === 'soul' ? (
            <>
              <h2 className="mb-1.5 text-2xl font-bold tracking-[-0.01em] text-[#1c1a16]">能力设定</h2>
              <p className="mb-6 max-w-[640px] text-sm leading-7 text-[#6f6960]">
                定义 OpenCapyBox 的语气、视角和判断边界，以及可以调用的技能。
              </p>
              <div className="mb-5 flex gap-5 border-b border-[#e8e3d9]">
                <button
                  type="button"
                  onClick={() => activateSoulTab('role')}
                  className={`mb-[-1px] border-b-2 px-0.5 pb-2.5 text-[14.5px] font-semibold transition ${
                    soulTab === 'role'
                      ? 'border-[#b8814a] text-[#1c1a16]'
                      : 'border-transparent text-[#a39c8e] hover:text-[#6f6960]'
                  }`}
                >
                  角色设定
                </button>
                <button
                  type="button"
                  onClick={() => activateSoulTab('skills')}
                  className={`mb-[-1px] border-b-2 px-0.5 pb-2.5 text-[14.5px] font-semibold transition ${
                    soulTab === 'skills'
                      ? 'border-[#b8814a] text-[#1c1a16]'
                      : 'border-transparent text-[#a39c8e] hover:text-[#6f6960]'
                  }`}
                >
                  技能
                </button>
              </div>
              {soulTab === 'role' ? renderSoul() : renderSkills()}
            </>
          ) : null}
          {connectionsVisited ? (
            <div hidden={activeSection !== 'connections'}>
              <h2 className="mb-1.5 text-2xl font-bold tracking-[-0.01em] text-[#1c1a16]">数据连接</h2>
              <p className="mb-6 max-w-[680px] text-sm leading-7 text-[#6f6960]">
                连接官方或个人 MCP 服务，为 OpenCapyBox 增加外部工具。工具执行权限与连接配置独立管理。
              </p>
              <React.Suspense
                fallback={(
                  <div className="flex h-40 items-center justify-center text-sm text-[#a39c8e]">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />加载数据连接...
                  </div>
                )}
              >
                <LazyMcpConnectionsPanel
                  onDirtyChange={setMcpDirty}
                  onPermissionsInvalidated={() => setPermissionsRefreshToken((token) => token + 1)}
                />
              </React.Suspense>
            </div>
          ) : null}
          {permissionsVisited ? (
            <div hidden={activeSection !== 'permissions'}>
              <React.Suspense
                fallback={(
                  <div className="flex h-40 items-center justify-center text-sm text-[#a39c8e]">
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />加载权限策略...
                  </div>
                )}
              >
                <LazyToolPermissionsPanel refreshToken={permissionsRefreshToken} />
              </React.Suspense>
            </div>
          ) : null}
        </main>
      </div>
    </div>
  );
};

export default SettingsCenter;
