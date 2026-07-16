import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  Check,
  CheckSquare,
  Info,
  Loader2,
  RefreshCw,
  Shield,
  ShieldAlert,
  Square,
  Trash2,
  X,
} from 'lucide-react';

import {
  clearPermissionSelection,
  getPermissionRules,
  getPermissionTools,
  setPermissionSelection,
  setPermissionSelectionBatch,
  type PermissionEffect,
  type PermissionTool,
  type ToolPermissionRule,
} from '../services/permissionApi';


type EffectFilter = 'all' | PermissionEffect;
type Category = 'builtin' | 'official' | 'personal';

const EFFECT_LABELS: Record<PermissionEffect, string> = {
  allow: 'ALLOW',
  ask: 'ASK',
  deny: 'DENY',
};

const EFFECT_CLASSES: Record<PermissionEffect, string> = {
  allow: 'border-[#b9d8c0] bg-[#edf7ef] text-[#327043]',
  ask: 'border-[#ead2a6] bg-[#fff7e7] text-[#9a6517]',
  deny: 'border-[#e7b7b2] bg-[#fff0ee] text-[#a33b31]',
};

const CATEGORY_ORDER: Category[] = ['builtin', 'official', 'personal'];

const CATEGORY_LABELS: Record<Category, string> = {
  builtin: '系统工具',
  official: '官方 MCP',
  personal: '个人 MCP',
};

function categoryOf(tool: PermissionTool): Category {
  if (tool.source_type === 'official') return 'official';
  if (tool.source_type === 'personal') return 'personal';
  return 'builtin';
}

function errorMessage(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  return error instanceof Error ? error.message : '权限配置失败';
}

function managedRuleForTool(
  tool: PermissionTool,
  rules: ToolPermissionRule[],
): ToolPermissionRule | undefined {
  const now = Date.now();
  const applicable = rules.filter((rule) => (
    rule.managed
    && rule.enabled
    && rule.provider === tool.provider
    && rule.server_id === tool.server_id
    && (rule.tool_name === '*' || rule.tool_name === tool.tool_name)
    && (!rule.expires_at || new Date(rule.expires_at).getTime() > now)
  ));
  return applicable.sort((left, right) => {
    const restrictiveness = { allow: 0, ask: 1, deny: 2 } as const;
    return restrictiveness[right.effect] - restrictiveness[left.effect]
      || Number(right.tool_name !== '*') - Number(left.tool_name !== '*')
      || right.priority - left.priority;
  })[0];
}

function blockedByManagedCeiling(
  managedRule: ToolPermissionRule | undefined,
  effect: PermissionEffect,
): boolean {
  if (managedRule?.effect === 'deny') return effect !== 'deny';
  if (managedRule?.effect === 'ask') return effect === 'allow';
  return false;
}

interface ToolPermissionsPanelProps {
  refreshToken?: number;
}

export default function ToolPermissionsPanel({ refreshToken = 0 }: ToolPermissionsPanelProps) {
  const [tools, setTools] = useState<PermissionTool[]>([]);
  const [rules, setRules] = useState<ToolPermissionRule[]>([]);
  const [category, setCategory] = useState<Category>('builtin');
  const [effectFilter, setEffectFilter] = useState<EffectFilter>('all');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [mutating, setMutating] = useState<Set<string>>(() => new Set());
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [batching, setBatching] = useState(false);
  const loadSequenceRef = useRef(0);

  const load = useCallback(async () => {
    const sequence = ++loadSequenceRef.current;
    setLoading(true);
    setError('');
    try {
      const [nextTools, nextRules] = await Promise.all([
        getPermissionTools(),
        getPermissionRules(),
      ]);
      if (sequence !== loadSequenceRef.current) return;
      setTools(nextTools);
      setRules(nextRules);
    } catch (loadError) {
      if (sequence !== loadSequenceRef.current) return;
      setError(errorMessage(loadError));
    } finally {
      if (sequence === loadSequenceRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  const ownRulesByToolRef = useMemo(() => {
    const map = new Map<string, ToolPermissionRule[]>();
    for (const rule of rules) {
      if (!rule.managed && rule.scope_type === 'user') {
        const current = map.get(rule.tool_ref) ?? [];
        current.push(rule);
        map.set(rule.tool_ref, current);
      }
    }
    return map;
  }, [rules]);

  const ruleById = useMemo(() => {
    const map = new Map<string, ToolPermissionRule>();
    for (const rule of rules) {
      map.set(rule.id, rule);
    }
    return map;
  }, [rules]);

  const visibleTools = useMemo(() => (
    tools.filter((tool) => (
      categoryOf(tool) === category
      && (effectFilter === 'all' || tool.effect === effectFilter)
    ))
  ), [tools, category, effectFilter]);

  const toolByRef = useMemo(() => {
    const map = new Map<string, PermissionTool>();
    for (const tool of tools) {
      map.set(tool.tool_ref, tool);
    }
    return map;
  }, [tools]);

  const categoryCounts = useMemo(() => {
    const counts: Record<Category, number> = { builtin: 0, official: 0, personal: 0 };
    for (const tool of tools) {
      counts[categoryOf(tool)] += 1;
    }
    return counts;
  }, [tools]);

  const visibleRefs = useMemo(() => visibleTools.map((tool) => tool.tool_ref), [visibleTools]);
  const allVisibleSelected = visibleRefs.length > 0 && visibleRefs.every((ref) => selected.has(ref));

  const toggleSelect = (ref: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(ref)) next.delete(ref);
      else next.add(ref);
      return next;
    });
  };

  const toggleSelectAllVisible = () => {
    setSelected((current) => {
      const next = new Set(current);
      if (allVisibleSelected) {
        for (const ref of visibleRefs) next.delete(ref);
      } else {
        for (const ref of visibleRefs) next.add(ref);
      }
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const mutate = async (tool: PermissionTool, effect: PermissionEffect) => {
    if (mutating.has(tool.tool_ref)) return;
    setMutating((current) => new Set(current).add(tool.tool_ref));
    setError('');
    setNotice('');
    try {
      // A manual choice is an atomic replacement of this exact tool's own
      // rules. The backend deletes any prior schema-bound approval grant and
      // creates one unconditional user rule, so the UI never decides between
      // create and update.
      await setPermissionSelection({
        provider: tool.provider,
        server_id: tool.server_id,
        tool_name: tool.tool_name,
        effect,
      });
      await load();
    } catch (mutationError) {
      setError(errorMessage(mutationError));
    } finally {
      setMutating((current) => {
        const next = new Set(current);
        next.delete(tool.tool_ref);
        return next;
      });
    }
  };

  const reset = async (tool: PermissionTool) => {
    const ownRules = ownRulesByToolRef.get(tool.tool_ref) ?? [];
    if (!ownRules.length || mutating.has(tool.tool_ref)) return;
    setMutating((current) => new Set(current).add(tool.tool_ref));
    setError('');
    setNotice('');
    try {
      await clearPermissionSelection({
        provider: tool.provider,
        server_id: tool.server_id,
        tool_name: tool.tool_name,
      });
      await load();
    } catch (mutationError) {
      setError(errorMessage(mutationError));
    } finally {
      setMutating((current) => {
        const next = new Set(current);
        next.delete(tool.tool_ref);
        return next;
      });
    }
  };

  const applyBatch = async (effect: PermissionEffect) => {
    if (batching || selected.size === 0) return;
    setError('');
    setNotice('');
    const selectedTools = [...selected]
      .map((ref) => toolByRef.get(ref))
      .filter((tool): tool is PermissionTool => tool !== undefined);
    const appliable = selectedTools.filter(
      (tool) => !blockedByManagedCeiling(managedRuleForTool(tool, rules), effect),
    );
    const skipped = selectedTools.length - appliable.length;
    if (appliable.length === 0) {
      setNotice('所选工具均被平台策略限制，未做变更');
      return;
    }
    setBatching(true);
    try {
      await setPermissionSelectionBatch({
        effect,
        items: appliable.map((tool) => ({
          provider: tool.provider,
          server_id: tool.server_id,
          tool_name: tool.tool_name,
        })),
      });
      await load();
      clearSelection();
      setNotice(
        `已将 ${appliable.length} 个工具设为 ${EFFECT_LABELS[effect]}`
        + (skipped > 0 ? `，${skipped} 个被平台策略跳过` : ''),
      );
    } catch (batchError) {
      setError(errorMessage(batchError));
    } finally {
      setBatching(false);
    }
  };

  if (loading && tools.length === 0) {
    return (
      <div className="flex min-h-[260px] items-center justify-center text-sm text-[#8d877c]">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
        加载权限策略...
      </div>
    );
  }

  return (
    <div className="max-w-[980px]">
      <div className="mb-5 flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-xl font-semibold text-[#1c1a16]">
            <Shield size={20} />
            权限管控
          </div>
          <p className="mt-1 text-sm leading-6 text-[#777064]">
            MCP 连接与执行权限相互独立。DENY 不向模型暴露工具，ASK 会在执行前征求确认。
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          className="inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg border border-[#e5dfd4] bg-white px-3 text-sm font-medium text-[#4f4a42] hover:bg-[#f7f4ee] disabled:opacity-60"
        >
          <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          刷新
        </button>
      </div>

      {error && (
        <div className="mb-4 flex items-start gap-2 rounded-xl border border-[#efc5c0] bg-[#fff5f3] px-4 py-3 text-sm text-[#9f3b32]">
          <AlertCircle size={16} className="mt-0.5 shrink-0" />
          {error}
        </div>
      )}

      {notice && (
        <div className="mb-4 flex items-start gap-2 rounded-xl border border-[#cfe0ef] bg-[#f2f7fb] px-4 py-3 text-sm text-[#3f6488]">
          <Info size={16} className="mt-0.5 shrink-0" />
          <span className="flex-1">{notice}</span>
          <button type="button" aria-label="关闭提示" onClick={() => setNotice('')} className="text-[#7c93a9] hover:text-[#3f6488]">
            <X size={14} />
          </button>
        </div>
      )}

      <div className="mb-4 flex gap-1 rounded-xl border border-[#e8e3d9] bg-[#f7f4ee] p-1">
        {CATEGORY_ORDER.map((value) => (
          <button
            type="button"
            key={value}
            aria-label={`来源 ${CATEGORY_LABELS[value]}`}
            onClick={() => setCategory(value)}
            className={`flex-1 rounded-lg px-4 py-2 text-sm font-semibold transition ${
              category === value ? 'bg-white text-[#1c1a16] shadow-sm' : 'text-[#777064] hover:text-[#3d3932]'
            }`}
          >
            {CATEGORY_LABELS[value]}
            <span className="ml-1.5 text-xs font-medium text-[#9a9284]">{categoryCounts[value]}</span>
          </button>
        ))}
      </div>

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex gap-1 rounded-xl border border-[#e8e3d9] bg-[#f7f4ee] p-1">
          {(['all', 'allow', 'ask', 'deny'] as EffectFilter[]).map((value) => (
            <button
              type="button"
              key={value}
              aria-label={value === 'all' ? '筛选全部策略' : `筛选 ${EFFECT_LABELS[value]}`}
              onClick={() => setEffectFilter(value)}
              className={`rounded-lg px-3.5 py-1.5 text-xs font-semibold transition ${
                effectFilter === value ? 'bg-white text-[#1c1a16] shadow-sm' : 'text-[#777064] hover:text-[#3d3932]'
              }`}
            >
              {value === 'all' ? '全部' : EFFECT_LABELS[value]}
            </button>
          ))}
        </div>
        {visibleTools.length > 0 && (
          <button
            type="button"
            onClick={toggleSelectAllVisible}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-[#e5dfd4] bg-white px-3 text-xs font-medium text-[#4f4a42] hover:bg-[#f7f4ee]"
          >
            {allVisibleSelected ? <CheckSquare size={14} /> : <Square size={14} />}
            {allVisibleSelected ? '取消全选' : '全选当前'}
          </button>
        )}
      </div>

      {selected.size > 0 && (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-[#dfe6ef] bg-[#f4f7fb] px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium text-[#3f5c7a]">
            <CheckSquare size={16} />
            已选 {selected.size} 项
            <button type="button" onClick={clearSelection} className="ml-1 text-xs font-normal text-[#7c93a9] underline-offset-2 hover:underline">
              清空
            </button>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-[#7c93a9]">批量设为</span>
            {(['allow', 'ask', 'deny'] as PermissionEffect[]).map((effect) => (
              <button
                type="button"
                key={effect}
                onClick={() => void applyBatch(effect)}
                disabled={batching}
                className={`h-8 rounded-lg border px-3 text-[11px] font-bold transition disabled:opacity-50 ${EFFECT_CLASSES[effect]}`}
              >
                {batching ? <Loader2 size={12} className="animate-spin" /> : EFFECT_LABELS[effect]}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="space-y-3">
        {visibleTools.map((tool) => {
          const ownRules = ownRulesByToolRef.get(tool.tool_ref) ?? [];
          const managedRule = managedRuleForTool(tool, rules);
          const matchedRule = tool.matched_rule_id ? ruleById.get(tool.matched_rule_id) : undefined;
          // Only surface the version-bound label when the conditional approval
          // grant is the rule that actually decided the effect. A historical
          // grant that no longer matches the current tool version is ignored.
          const hasConditionalGrant = Boolean(
            matchedRule
            && matchedRule.scope_type === 'user'
            && !matchedRule.managed
            && matchedRule.conditions != null
            && Object.keys(matchedRule.conditions).length > 0,
          );
          const busy = mutating.has(tool.tool_ref);
          const isSelected = selected.has(tool.tool_ref);
          return (
            <section
              key={tool.tool_ref}
              className={`rounded-2xl border bg-white px-4 py-4 shadow-[0_1px_2px_rgba(30,26,20,0.04)] ${
                isSelected ? 'border-[#bcd0e6] ring-1 ring-[#bcd0e6]' : 'border-[#e8e3d9]'
              }`}
            >
              <div className="flex flex-col items-stretch gap-3 md:flex-row md:flex-wrap md:items-center md:justify-between">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  <button
                    type="button"
                    aria-label={isSelected ? `取消选择 ${tool.tool_ref}` : `选择 ${tool.tool_ref}`}
                    onClick={() => toggleSelect(tool.tool_ref)}
                    className={`mt-0.5 shrink-0 ${isSelected ? 'text-[#3f6488]' : 'text-[#b7b0a4] hover:text-[#7c93a9]'}`}
                  >
                    {isSelected ? <CheckSquare size={18} /> : <Square size={18} />}
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`inline-flex rounded-md border px-2 py-1 text-[11px] font-bold ${EFFECT_CLASSES[tool.effect]}`}>
                        {EFFECT_LABELS[tool.effect]}
                      </span>
                      <code className="break-all rounded-md bg-[#f5f2ec] px-2 py-1 text-[13px] text-[#302d28]">
                        {tool.tool_ref}
                      </code>
                      {managedRule && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-[#eeeafd] px-2 py-1 text-[11px] font-medium text-[#6252a3]">
                          <ShieldAlert size={11} /> 平台策略
                        </span>
                      )}
                      {hasConditionalGrant && (
                        <span className="rounded-full bg-[#edf4fa] px-2 py-1 text-[11px] font-medium text-[#426b91]">
                          审批授权 · 绑定工具版本
                        </span>
                      )}
                    </div>
                    <p className="mt-2 text-sm font-medium text-[#302d28]">
                      {tool.server_name ? `${tool.server_name} / ` : ''}{tool.title}
                    </p>
                    {tool.description && (
                      <p className="mt-1 line-clamp-2 text-xs leading-5 text-[#8a8377]">{tool.description}</p>
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap items-center justify-end gap-1.5 md:flex-nowrap">
                  {(['allow', 'ask', 'deny'] as PermissionEffect[]).map((effect) => (
                    <button
                      type="button"
                      key={effect}
                      onClick={() => void mutate(tool, effect)}
                      disabled={busy || blockedByManagedCeiling(managedRule, effect)}
                      title={blockedByManagedCeiling(managedRule, effect) ? '平台策略不允许放宽到该级别' : undefined}
                      className={`h-8 rounded-lg border px-2.5 text-[11px] font-bold transition disabled:opacity-50 ${
                        tool.effect === effect
                          ? EFFECT_CLASSES[effect]
                          : 'border-[#e5dfd4] bg-white text-[#777064] hover:bg-[#f7f4ee]'
                      }`}
                    >
                      {busy && tool.effect !== effect ? <Loader2 size={12} className="animate-spin" /> : EFFECT_LABELS[effect]}
                    </button>
                  ))}
                  {ownRules.length > 0 && (
                    <button
                      type="button"
                      title="恢复默认策略"
                      onClick={() => void reset(tool)}
                      disabled={busy}
                      className="ml-1 inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[#e5dfd4] text-[#8a8377] hover:bg-[#fff0ee] hover:text-[#a33b31] disabled:opacity-50"
                    >
                      <Trash2 size={14} />
                    </button>
                  )}
                </div>
              </div>
            </section>
          );
        })}

        {visibleTools.length === 0 && (
          <div className="rounded-2xl border border-dashed border-[#dcd5c9] px-6 py-12 text-center text-sm text-[#8a8377]">
            <Check className="mx-auto mb-2 h-5 w-5" />
            {categoryCounts[category] === 0 ? `${CATEGORY_LABELS[category]}下暂无工具` : '当前筛选下没有工具'}
          </div>
        )}
      </div>
    </div>
  );
}
