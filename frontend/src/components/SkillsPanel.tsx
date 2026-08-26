import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Blocks,
  Loader2,
  RefreshCw,
  Search,
  X,
} from 'lucide-react';

import {
  getSkills,
  toggleSkill,
  type SkillInfo,
  type SkillInventoryState,
  type SkillScanIssue,
  type SkillSandboxStatus,
} from '../services/configApi';

type SkillStatusFilter = 'all' | 'enabled' | 'disabled';
type SkillSourceFilter = 'all' | 'official' | 'user';

function skillKey(skill: SkillInfo): string {
  return skill.key || skill.name;
}

function skillDisplayName(skill: SkillInfo): string {
  return skill.display_name || skill.name;
}

const SKILL_MARK_PALETTES = [
  'border-[#dfe3f6] bg-[#eef1ff] text-[#5968d8]',
  'border-[#dceade] bg-[#eef7f0] text-[#4d795d]',
  'border-[#f0dfc7] bg-[#fff5e7] text-[#a36b2c]',
  'border-[#e8def3] bg-[#f6f0fc] text-[#795b9e]',
  'border-[#eedddd] bg-[#fbf0f0] text-[#9a5d5d]',
] as const;

const CATEGORY_LABELS: Record<string, string> = {
  data: '数据',
  document: '文档',
  finance: '金融',
  general: '通用',
  media: '媒体',
  research: '研究',
  user: '自定义',
};

function skillMark(skill: SkillInfo): { label: string; palette: string } {
  const name = skillDisplayName(skill).trim() || 'Skill';
  const label = Array.from(name)[0]?.toLocaleUpperCase('zh-CN') || 'S';
  const hash = Array.from(skillKey(skill)).reduce((total, character) => total + character.codePointAt(0)!, 0);
  return { label, palette: SKILL_MARK_PALETTES[hash % SKILL_MARK_PALETTES.length] };
}

function categoryLabel(category: string): string {
  const normalized = category.trim().toLocaleLowerCase('zh-CN');
  return CATEGORY_LABELS[normalized] || category || '通用';
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '加载失败，请稍后重试';
}

export default function SkillsPanel() {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [sandboxStatus, setSandboxStatus] = useState<SkillSandboxStatus | null>(null);
  const [inventoryState, setInventoryState] = useState<SkillInventoryState | null>(null);
  const [issues, setIssues] = useState<SkillScanIssue[]>([]);
  const [toggling, setToggling] = useState<Set<string>>(() => new Set());
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<SkillStatusFilter>('all');
  const [sourceFilter, setSourceFilter] = useState<SkillSourceFilter>('all');
  const togglingRef = useRef<Set<string>>(new Set());
  const mutationVersionsRef = useRef<Map<string, number>>(new Map());
  const loadSequenceRef = useRef(0);
  const refreshRequestRef = useRef<Promise<void> | null>(null);

  const loadSkills = useCallback(async (options: { refresh?: boolean } = {}) => {
    if (refreshRequestRef.current) {
      await refreshRequestRef.current;
      return;
    }

    const executeLoad = async () => {
      const sequence = ++loadSequenceRef.current;
      const mutationVersionsAtStart = new Map(mutationVersionsRef.current);
      setLoading(true);
      setError('');
      try {
        const result = await getSkills({ refresh: options.refresh });
        if (sequence !== loadSequenceRef.current) return;
        setSkills((previousSkills) => {
          const previousByKey = new Map(
            previousSkills.map((skill) => [skillKey(skill), skill]),
          );
          return result.skills.map((skill) => {
            const key = skillKey(skill);
            const mutationOverlappedLoad = (
              (mutationVersionsRef.current.get(key) ?? 0)
              !== (mutationVersionsAtStart.get(key) ?? 0)
            );
            if (!mutationOverlappedLoad && !togglingRef.current.has(key)) return skill;
            const optimisticSkill = previousByKey.get(key);
            return optimisticSkill
              ? { ...skill, enabled: optimisticSkill.enabled }
              : skill;
          });
        });
        setSandboxStatus(result.sandbox_status);
        setInventoryState(result.inventory_state ?? null);
        setIssues(result.skill_issues ?? []);
      } catch (loadError) {
        if (sequence !== loadSequenceRef.current) return;
        setError(errorMessage(loadError));
      } finally {
        if (sequence === loadSequenceRef.current) setLoading(false);
      }
    };

    const request = executeLoad();
    if (!options.refresh) {
      await request;
      return;
    }

    refreshRequestRef.current = request;
    try {
      await request;
    } finally {
      if (refreshRequestRef.current === request) refreshRequestRef.current = null;
    }
  }, []);

  useEffect(() => {
    void loadSkills();
    return () => {
      loadSequenceRef.current += 1;
    };
  }, [loadSkills]);

  const handleToggle = async (key: string, enabled: boolean) => {
    if (togglingRef.current.has(key)) return;
    mutationVersionsRef.current.set(key, (mutationVersionsRef.current.get(key) ?? 0) + 1);

    const nextToggling = new Set(togglingRef.current);
    nextToggling.add(key);
    togglingRef.current = nextToggling;
    setToggling(nextToggling);
    setError('');
    setSkills((current) => current.map((skill) => (
      skillKey(skill) === key ? { ...skill, enabled: !enabled } : skill
    )));

    try {
      await toggleSkill(key, !enabled);
      // A refresh can start while this mutation is in flight and finish after
      // the server has accepted it with an older inventory snapshot. Mark the
      // successful commit as a second version boundary so that response cannot
      // roll the confirmed local value back.
      mutationVersionsRef.current.set(key, (mutationVersionsRef.current.get(key) ?? 0) + 1);
    } catch (toggleError) {
      mutationVersionsRef.current.set(key, (mutationVersionsRef.current.get(key) ?? 0) + 1);
      setSkills((current) => current.map((skill) => (
        skillKey(skill) === key ? { ...skill, enabled } : skill
      )));
      setError(errorMessage(toggleError));
    } finally {
      const remaining = new Set(togglingRef.current);
      remaining.delete(key);
      togglingRef.current = remaining;
      setToggling(remaining);
    }
  };

  const filteredSkills = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN');
    return skills.filter((skill) => {
      if (statusFilter === 'enabled' && !skill.enabled) return false;
      if (statusFilter === 'disabled' && skill.enabled) return false;
      if (sourceFilter !== 'all' && skill.source !== sourceFilter) return false;
      if (!normalizedQuery) return true;
      return [skillDisplayName(skill), skill.name, skill.description, skill.category]
        .filter(Boolean)
        .some((value) => value!.toLocaleLowerCase('zh-CN').includes(normalizedQuery));
    });
  }, [query, skills, sourceFilter, statusFilter]);

  const enabledCount = skills.filter((skill) => skill.enabled).length;
  const disabledCount = skills.length - enabledCount;
  const officialCount = skills.filter((skill) => skill.source === 'official').length;
  const userCount = skills.length - officialCount;

  const statusOptions: Array<{ value: SkillStatusFilter; label: string; count: number }> = [
    { value: 'all', label: '全部', count: skills.length },
    { value: 'enabled', label: '已启用', count: enabledCount },
    { value: 'disabled', label: '未启用', count: disabledCount },
  ];
  const sourceOptions: Array<{ value: SkillSourceFilter; label: string; count: number }> = [
    { value: 'all', label: '全部', count: skills.length },
    { value: 'official', label: '官方', count: officialCount },
    { value: 'user', label: '我的', count: userCount },
  ];

  return (
    <section aria-label="Skill 清单" className="min-w-0">
      <div className="mb-5 border-b border-[#e8e3d9] pb-5">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <label className="relative block min-w-0 flex-1 sm:max-w-[460px]">
            <span className="sr-only">搜索 Skills</span>
            <Search size={16} className="pointer-events-none absolute left-3.5 top-1/2 -translate-y-1/2 text-[#9397aa]" />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索技能名称、说明或分类"
              className="h-11 w-full rounded-[13px] border border-[#dedfdc] bg-white pl-10 pr-10 text-[13.5px] text-[#1c1a16] shadow-[0_1px_2px_rgba(30,26,20,0.03)] outline-none transition placeholder:text-[#a3a39f] focus:border-[#7b86d8] focus:ring-2 focus:ring-[#7b86d8]/15"
            />
            {query ? (
              <button
                type="button"
                aria-label="清空 Skill 搜索"
                onClick={() => setQuery('')}
                className="absolute right-2 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-lg text-[#9b9a96] hover:bg-black/[0.04] hover:text-[#5f5d58] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#7b86d8]/25"
              >
                <X size={13} />
              </button>
            ) : null}
          </label>
          <button
            type="button"
            onClick={() => void loadSkills({ refresh: true })}
            disabled={loading}
            aria-label="刷新 Skill 清单"
            className="inline-flex h-11 shrink-0 items-center justify-center gap-2 rounded-[13px] border border-[#dedfdc] bg-white px-4 text-[13px] font-semibold text-[#5f5d58] shadow-[0_1px_2px_rgba(30,26,20,0.03)] transition hover:border-[#cfd2e3] hover:bg-[#f7f7fb] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#7b86d8]/25 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            重新扫描
          </button>
        </div>

        <div className="mt-4 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
            <div className="flex min-w-0 items-center gap-2">
              <span className="shrink-0 text-[11px] font-bold uppercase tracking-[0.12em] text-[#989793]">状态</span>
              <div role="group" aria-label="按启用状态筛选 Skills" className="flex min-w-0 gap-1 overflow-x-auto rounded-xl bg-[#f0efeb] p-1">
                {statusOptions.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    aria-label={`筛选状态：${option.label}`}
                    aria-pressed={statusFilter === option.value}
                    onClick={() => setStatusFilter(option.value)}
                    className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-3 text-[12px] font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#7b86d8]/30 ${statusFilter === option.value ? 'bg-white text-[#252522] shadow-[0_1px_3px_rgba(30,26,20,0.10)]' : 'text-[#77756f] hover:text-[#393834]'}`}
                  >
                    {option.label}<span className="text-[10.5px] font-medium opacity-65">{option.count}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="flex min-w-0 items-center gap-2">
              <span className="shrink-0 text-[11px] font-bold uppercase tracking-[0.12em] text-[#989793]">来源</span>
              <div role="group" aria-label="按来源筛选 Skills" className="flex min-w-0 gap-1 overflow-x-auto rounded-xl bg-[#f0efeb] p-1">
                {sourceOptions.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    aria-label={`筛选来源：${option.label}`}
                    aria-pressed={sourceFilter === option.value}
                    onClick={() => setSourceFilter(option.value)}
                    className={`inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg px-3 text-[12px] font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#7b86d8]/30 ${sourceFilter === option.value ? 'bg-white text-[#252522] shadow-[0_1px_3px_rgba(30,26,20,0.10)]' : 'text-[#77756f] hover:text-[#393834]'}`}
                  >
                    {option.label}<span className="text-[10.5px] font-medium opacity-65">{option.count}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="flex items-center justify-between gap-3 text-xs text-[#85837d] lg:justify-end">
            <span><strong className="font-semibold text-[#4f5dbb]">{enabledCount}</strong> 项已启用</span>
            <span aria-live="polite">显示 {filteredSkills.length} / {skills.length}</span>
          </div>
        </div>
      </div>

      {error ? (
        <div role="alert" className="mb-4 flex items-center justify-between gap-3 rounded-xl border border-[#efd0ca] bg-[#fff5f3] px-4 py-3 text-xs font-medium text-[#9b473a]">
          <span>{error}</span>
          <button type="button" onClick={() => void loadSkills({ refresh: true })} disabled={loading} className="shrink-0 rounded-lg border border-current px-2.5 py-1.5 font-semibold hover:bg-[#fde9e5] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#c75c4a]/30 disabled:opacity-50">重新加载</button>
        </div>
      ) : null}

      {issues.length > 0 && !error ? (
        <details role="alert" className="group mb-4 overflow-hidden rounded-xl border border-[#ead8bd] bg-[#fff9ef] text-xs text-[#76532f]">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[#b8814a]/25 [&::-webkit-details-marker]:hidden">
            <span className="flex items-center gap-2"><AlertTriangle size={14} />{issues.length} 个 Skill 未载入，其他 Skill 可正常使用</span>
            <span className="shrink-0 text-[11px] text-[#a27647] group-open:hidden">查看原因</span>
            <span className="hidden shrink-0 text-[11px] text-[#a27647] group-open:inline">收起详情</span>
          </summary>
          <ul className="space-y-1.5 border-t border-[#ead8bd] px-4 py-3 pl-9">
            {issues.map((issue) => (
              <li key={`${issue.path}:${issue.field}`} className="list-disc break-words">
                <code>{issue.path}</code> · {issue.field}：{issue.message}。{issue.suggestion}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {inventoryState === 'stale' && !error ? (
        <div role="status" className="mb-4 rounded-xl border border-[#ead8bd] bg-[#fff8ec] px-4 py-3 text-xs font-medium text-[#8a5a2f]">刷新失败，正在显示上次成功加载的 Skill 清单。</div>
      ) : null}
      {sandboxStatus === 'unavailable' && inventoryState !== 'stale' && !error ? (
        <div role="status" className="mb-4 rounded-xl border border-[#ead8bd] bg-[#fff8ec] px-4 py-3 text-xs font-medium text-[#8a5a2f]">工作沙箱暂时不可用，目前仅显示官方技能。</div>
      ) : null}
      {sandboxStatus === 'not_created' ? (
        <div role="status" className="mb-4 rounded-xl border border-[#e8e3d9] bg-[#faf8f3] px-4 py-3 text-xs font-medium text-[#6f6960]">尚未创建工作沙箱，以下仅显示官方技能。</div>
      ) : null}

      {loading && skills.length === 0 ? (
        <div data-testid="skills-loading-grid" className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(300px, 100%), 1fr))' }}>
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <div key={index} className="h-[168px] animate-pulse rounded-2xl border border-[#e8e7e2] bg-white p-5">
              <div className="mb-5 flex gap-3"><div className="h-11 w-11 rounded-xl bg-[#efefec]" /><div className="flex-1 space-y-2 pt-1"><div className="h-3 w-2/5 rounded bg-[#ecebe7]" /><div className="h-2.5 w-1/3 rounded bg-[#f1f0ed]" /></div></div>
              <div className="space-y-2"><div className="h-2.5 w-full rounded bg-[#efeeea]" /><div className="h-2.5 w-4/5 rounded bg-[#f2f1ee]" /></div>
            </div>
          ))}
        </div>
      ) : filteredSkills.length === 0 ? (
        <div className="flex min-h-56 flex-col items-center justify-center rounded-2xl border border-dashed border-[#dcdad3] bg-white/70 px-5 py-12 text-center text-[#999791]">
          <Blocks size={34} strokeWidth={1.3} className="mb-3 opacity-60" />
          <div className="mb-1 text-sm font-semibold text-[#5f5d58]">{skills.length ? '没有匹配的 Skill' : '没有可用技能'}</div>
          <div className="max-w-[320px] text-xs leading-6">{skills.length ? '清除搜索或切回“全部”后再试。' : '安装或创建技能后，会出现在这里。'}</div>
          {skills.length ? (
            <button type="button" onClick={() => { setQuery(''); setStatusFilter('all'); setSourceFilter('all'); }} className="mt-3 rounded-lg border border-[#d8d7d1] bg-white px-3 py-1.5 text-xs font-semibold text-[#5968a8] hover:bg-[#f5f6fb] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#7b86d8]/25">清除筛选</button>
          ) : null}
        </div>
      ) : (
        <div data-testid="skills-card-grid" className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(300px, 100%), 1fr))' }}>
          {filteredSkills.map((skill) => {
            const key = skillKey(skill);
            const name = skillDisplayName(skill);
            const isToggling = toggling.has(key);
            const mark = skillMark(skill);
            return (
              <article key={key} aria-label={`Skill：${name}`} className={`group flex min-h-[168px] flex-col rounded-2xl border bg-white p-5 shadow-[0_1px_2px_rgba(30,26,20,0.035)] transition duration-200 hover:-translate-y-px hover:border-[#d5d8e7] hover:shadow-[0_8px_22px_rgba(48,52,72,0.07)] motion-reduce:transform-none ${skill.enabled ? 'border-[#e2e1dc]' : 'border-[#e8e7e2]'}`}>
                <div className="flex items-start gap-3">
                  <div aria-hidden="true" className={`flex h-11 w-11 shrink-0 items-center justify-center rounded-[13px] border text-[18px] font-bold ${mark.palette}`}>{mark.label}</div>
                  <div className="min-w-0 flex-1 pt-0.5">
                    <h2 className="truncate text-[15px] font-semibold tracking-[-0.01em] text-[#232320]">{name}</h2>
                    <div className="mt-1.5 flex min-w-0 flex-wrap items-center gap-1.5">
                      <span className={`rounded-md px-1.5 py-0.5 text-[10.5px] font-semibold ${skill.source === 'user' ? 'bg-[#eaf4ed] text-[#4d765a]' : 'bg-[#eff1f8] text-[#66709d]'}`}>{skill.source === 'user' ? '我的' : '官方'}</span>
                      <span className="truncate text-[10.5px] font-medium text-[#99968f]">{categoryLabel(skill.category)}</span>
                    </div>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={skill.enabled}
                    aria-busy={isToggling}
                    aria-label={`${skill.enabled ? '禁用' : '启用'} ${name}`}
                    disabled={isToggling}
                    onClick={() => void handleToggle(key, skill.enabled)}
                    className={`relative mt-1 h-[24px] w-[42px] shrink-0 rounded-full transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#6f7adc]/30 focus-visible:ring-offset-2 ${skill.enabled ? 'bg-[#5968d8]' : 'bg-[#d9d8d3]'} ${isToggling ? 'opacity-75' : ''}`}
                  >
                    <span className={`absolute top-[3px] flex h-[18px] w-[18px] items-center justify-center rounded-full bg-white shadow-[0_1px_3px_rgba(0,0,0,0.2)] transition-transform ${skill.enabled ? 'translate-x-[21px]' : 'translate-x-[3px]'}`}>
                      {isToggling ? <Loader2 size={11} className="animate-spin text-[#5968d8]" /> : null}
                    </span>
                  </button>
                </div>
                <p className="mt-4 line-clamp-2 min-h-10 text-[12.5px] leading-5 text-[#77756f]">{skill.description || '暂时没有说明。'}</p>
                <div className="mt-auto flex items-center gap-2 border-t border-[#f0efeb] pt-3 text-[11px] font-medium text-[#85837d]">
                  <span className={`h-1.5 w-1.5 rounded-full ${skill.enabled ? 'bg-[#5968d8]' : 'bg-[#bbb9b2]'}`} />
                  {isToggling ? '正在保存…' : skill.enabled ? '已启用' : '未启用'}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
