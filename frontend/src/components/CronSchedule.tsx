import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import {
  getCronJobs,
  getCronRuns,
  triggerCronJob,
  getCronRunStatus,
  type CronTask,
  type CronJobRun,
} from '../services/configApi';

// ────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────

/** 解析 cron 字段值为数字集合，支持通配符、步进、逗号和范围(1-5) */
function parseCronField(field: string): Set<number> | null {
  if (field === '*') return null; // null 表示"所有值都匹配"
  if (field.includes('/')) return null; // */N 交给调用方处理
  const nums = new Set<number>();
  for (const part of field.split(',')) {
    if (part.includes('-')) {
      const [a, b] = part.split('-').map(Number);
      if (!Number.isNaN(a) && !Number.isNaN(b)) {
        for (let i = a; i <= b; i++) nums.add(i);
      }
    } else {
      const n = Number(part);
      if (!Number.isNaN(n)) nums.add(n);
    }
  }
  return nums;
}

/** 将 cron 5-field 表达式转成可读中文频率 */
export function cronToReadable(expr: string): string {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return expr;
  const [minute, hour, dom, mon, dow] = parts;

  const hasFixedTime = !hour.includes('*') && !hour.includes('/') && !minute.includes('/');
  const timeStr = hasFixedTime ? `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}` : '';

  // */N * * * * → 每N分钟
  if (minute.startsWith('*/') && hour === '*') return `每${minute.slice(2)}分钟`;
  // 0 */N * * * → 每N小时
  if (hour.startsWith('*/')) return `每${hour.slice(2)}小时`;

  // 有固定时间的模式
  if (hasFixedTime) {
    const hasDom = dom !== '*';
    const hasMon = mon !== '*';
    const hasDow = dow !== '*';

    // M H D Mon * → 特定月日（如 0 11 15 4 * → 每年4月15日 11:00）
    if (hasMon && hasDom && !hasDow) {
      return `每年${mon}月${dom}日 ${timeStr}`;
    }
    // M H D * * → 每月某日（如 0 9 1 * * → 每月1日 09:00）
    if (hasDom && !hasMon && !hasDow) {
      return `每月${dom}日 ${timeStr}`;
    }
    // M H * * 1-5 → 工作日
    if (!hasDom && !hasMon && dow === '1-5') {
      return `工作日 ${timeStr}`;
    }
    // M H * * 0,6 or 6,0 → 周末
    if (!hasDom && !hasMon && (dow === '0,6' || dow === '6,0')) {
      return `周末 ${timeStr}`;
    }
    // M H * * N,N,... → 每周多天
    if (!hasDom && !hasMon && hasDow) {
      const dayNames = ['日', '一', '二', '三', '四', '五', '六'];
      if (!dow.includes('-') && !dow.includes('/')) {
        const dayList = dow.split(',').map((d) => dayNames[Number(d)] ?? d).join('、');
        return `每周${dayList} ${timeStr}`;
      }
      if (dow.includes('-') && !dow.includes(',')) {
        const [a, b] = dow.split('-');
        return `每周${dayNames[Number(a)] ?? a}至${dayNames[Number(b)] ?? b} ${timeStr}`;
      }
      return `每周(${dow}) ${timeStr}`;
    }
    // M H * * * → 每天
    if (!hasDom && !hasMon && !hasDow) {
      return `每天 ${timeStr}`;
    }
  }

  return expr;
}

/** 根据 cron 表达式判断某一天是否应该显示该任务 */
function taskVisibleOnDate(expr: string, date: Date): boolean {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return true; // 无法解析就都显示
  const [, , dom, mon, dow] = parts;

  // day-of-month 检查
  const domSet = parseCronField(dom);
  if (domSet && !domSet.has(date.getDate())) return false;
  // month 检查
  const monSet = parseCronField(mon);
  if (monSet && !monSet.has(date.getMonth() + 1)) return false;
  // day-of-week 检查
  const dowSet = parseCronField(dow);
  if (dowSet && !dowSet.has(date.getDay())) return false;
  return true;
}

/** 从 cron 表达式提取 HH:MM 显示时间，若无固定时间返回 null */
function cronTime(expr: string): string | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour] = parts;
  if (hour === '*' || hour.includes('/') || minute.includes('/')) return null;
  return `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;
}

/** 获取某一周的 7 天日期数组 (周一开始) */
function getWeekDays(baseDate: Date): Date[] {
  const d = new Date(baseDate);
  const day = d.getDay();
  const diff = day === 0 ? -6 : 1 - day; // 周一开始
  d.setDate(d.getDate() + diff);
  d.setHours(0, 0, 0, 0);
  const days: Date[] = [];
  for (let i = 0; i < 7; i++) {
    const nd = new Date(d);
    nd.setDate(d.getDate() + i);
    days.push(nd);
  }
  return days;
}

function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

function formatDateRange(days: Date[]): string {
  if (days.length === 0) return '';
  const first = days[0];
  const last = days[days.length - 1];
  const y = first.getFullYear();
  const m1 = first.getMonth() + 1;
  const d1 = first.getDate();
  const m2 = last.getMonth() + 1;
  const d2 = last.getDate();
  return `${y}年${m1}月${d1}日 - ${m2 !== m1 ? `${m2}月` : ''}${d2}日`;
}

const DAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const DAY_EN = ['MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN'];

// ────────────────────────────────────────────
// Task card colors (循环分配)
// ────────────────────────────────────────────

const CARD_THEMES = [
  { bg: 'bg-amber-800/60', border: 'border-amber-700/40', text: 'text-amber-100' },
  { bg: 'bg-emerald-800/50', border: 'border-emerald-700/40', text: 'text-emerald-100' },
  { bg: 'bg-slate-600/50', border: 'border-slate-500/40', text: 'text-slate-100' },
  { bg: 'bg-rose-800/50', border: 'border-rose-700/40', text: 'text-rose-100' },
  { bg: 'bg-violet-800/50', border: 'border-violet-700/40', text: 'text-violet-100' },
  { bg: 'bg-cyan-800/50', border: 'border-cyan-700/40', text: 'text-cyan-100' },
];

// ────────────────────────────────────────────
// Status helpers
// ────────────────────────────────────────────

function statusLabel(s: string) {
  switch (s) {
    case 'success': return '执行成功';
    case 'failed': return '执行失败';
    case 'running': return '执行中';
    default: return s;
  }
}

function statusIcon(s: string) {
  switch (s) {
    case 'success': return '✓';
    case 'failed': return '✕';
    case 'running': return '⟳';
    default: return '○';
  }
}

function statusColor(s: string) {
  switch (s) {
    case 'success': return 'text-green-600';
    case 'failed': return 'text-red-500';
    case 'running': return 'text-yellow-500';
    default: return 'text-claude-muted';
  }
}

// ────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────

interface TaskCardProps {
  task: CronTask;
  themeIdx: number;
  latestRun?: CronJobRun;
  onClick: () => void;
}

const TaskCard: React.FC<TaskCardProps> = ({ task, themeIdx, latestRun, onClick }) => {
  const theme = CARD_THEMES[themeIdx % CARD_THEMES.length];
  const time = cronTime(task.cron_expr);
  const runStatus = latestRun?.status;

  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-2.5 py-2 rounded-lg border ${theme.bg} ${theme.border} ${theme.text} hover:opacity-90 transition-opacity cursor-pointer mb-1.5`}
    >
      <div className="font-medium text-xs truncate leading-tight">
        {task.description || task.name}
      </div>
      <div className="flex items-center gap-1.5 mt-1 text-[10px] opacity-80">
        {runStatus ? (
          <span className="flex items-center gap-0.5">
            <span className={statusColor(runStatus)}>{statusIcon(runStatus)}</span>
            {statusLabel(runStatus)}
          </span>
        ) : (
          <span className="flex items-center gap-0.5">
            <span>⏳</span> 待执行
          </span>
        )}
      </div>
      <div className="flex items-center gap-1 mt-0.5 text-[10px] opacity-70">
        <span>{time ? '⏰' : '🔄'}</span> {cronToReadable(task.cron_expr)}
      </div>
    </button>
  );
};

interface TaskDetailModalProps {
  task: CronTask;
  runs: CronJobRun[];
  onClose: () => void;
  onTrigger: (name: string) => void;
  triggering: boolean;
}

const TaskDetailModal: React.FC<TaskDetailModalProps> = ({ task, runs, onClose, onTrigger, triggering }) => {
  const [expandedRun, setExpandedRun] = useState<string | null>(null);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onClose}>
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/30" />
      {/* Modal */}
      <div
        className="relative bg-claude-bg rounded-2xl shadow-2xl border border-claude-border w-[480px] max-h-[80vh] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-start justify-between px-6 pt-5 pb-3">
          <h3 className="text-lg font-semibold text-claude-text leading-tight">
            {task.description || task.name}
          </h3>
          <button
            onClick={onClose}
            className="p-1 rounded-lg hover:bg-claude-hover text-claude-muted ml-4 shrink-0"
          >
            ✕
          </button>
        </div>

        {/* Info grid */}
        <div className="px-6 pb-4 space-y-3 text-sm border-b border-claude-border">
          <div className="flex">
            <span className="w-24 text-claude-secondary shrink-0">状态</span>
            <span className={`font-medium ${task.enabled ? 'text-claude-text' : 'text-claude-muted'}`}>
              {task.enabled ? '待执行' : '已暂停'}
            </span>
          </div>
          <div className="flex">
            <span className="w-24 text-claude-secondary shrink-0">频率</span>
            <span className="text-claude-text">{cronToReadable(task.cron_expr)}</span>
          </div>
          <div className="flex">
            <span className="w-24 text-claude-secondary shrink-0">Cron 表达式</span>
            <code className="text-claude-text bg-claude-surface px-1.5 py-0.5 rounded text-xs">
              {task.cron_expr}
            </code>
          </div>
          {task.description && (
            <div className="flex">
              <span className="w-24 text-claude-secondary shrink-0">描述</span>
              <span className="text-claude-text">{task.description}</span>
            </div>
          )}
        </div>

        {/* Run history */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          <div className="flex items-center justify-between mb-3">
            <h4 className="text-sm font-medium text-claude-text">执行历史</h4>
            <button
              onClick={() => onTrigger(task.name)}
              disabled={triggering}
              className="px-3 py-1 text-xs font-medium rounded-lg bg-claude-surface text-claude-accent hover:bg-claude-hover disabled:opacity-50 border border-claude-border"
            >
              {triggering ? '执行中...' : '手动执行'}
            </button>
          </div>

          {runs.length === 0 ? (
            <div className="text-center text-claude-muted py-6 text-sm">暂无执行记录</div>
          ) : (
            <div className="space-y-2">
              {runs.map((run) => (
                <div
                  key={run.id}
                  className="rounded-lg border border-claude-border overflow-hidden"
                >
                  <button
                    onClick={() => setExpandedRun(expandedRun === run.id ? null : run.id)}
                    className="w-full flex items-center justify-between px-3 py-2.5 hover:bg-claude-hover/50 transition-colors"
                  >
                    <div className="flex items-center gap-2 text-sm">
                      <span className={`font-medium ${statusColor(run.status)}`}>
                        {statusIcon(run.status)}
                      </span>
                      <span className="text-claude-text">{statusLabel(run.status)}</span>
                    </div>
                    <div className="flex items-center gap-2 text-xs text-claude-secondary">
                      {run.started_at && (
                        <span>{new Date(run.started_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</span>
                      )}
                      <span className={`transition-transform ${expandedRun === run.id ? 'rotate-180' : ''}`}>▾</span>
                    </div>
                  </button>

                  {expandedRun === run.id && (
                    <div className="border-t border-claude-border px-3 py-2.5 bg-claude-surface/50">
                      <div className="text-xs text-claude-secondary space-y-1">
                        {run.started_at && <div>开始: {new Date(run.started_at).toLocaleString('zh-CN')}</div>}
                        {run.completed_at && <div>结束: {new Date(run.completed_at).toLocaleString('zh-CN')}</div>}
                        {run.cron_expr && <div>表达式: <code className="bg-claude-surface px-1 rounded">{run.cron_expr}</code></div>}
                      </div>
                      {run.output && (
                        <pre className="mt-2 text-xs text-claude-text bg-claude-bg p-2.5 rounded-lg overflow-x-auto max-h-48 whitespace-pre-wrap break-words border border-claude-border">
                          {run.output}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-claude-border flex justify-end">
          <button
            onClick={onClose}
            className="px-5 py-1.5 text-sm rounded-lg bg-claude-surface text-claude-text hover:bg-claude-hover border border-claude-border"
          >
            我知道了
          </button>
        </div>
      </div>
    </div>
  );
};

// ────────────────────────────────────────────
// Main component
// ────────────────────────────────────────────

interface Props {
  onClose?: () => void;
}

const CronSchedule: React.FC<Props> = ({ onClose }) => {
  const [tasks, setTasks] = useState<CronTask[]>([]);
  const [allRuns, setAllRuns] = useState<CronJobRun[]>([]);
  const [weekOffset, setWeekOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [selectedTask, setSelectedTask] = useState<CronTask | null>(null);
  const [taskRuns, setTaskRuns] = useState<CronJobRun[]>([]);
  const [triggeringSet, setTriggeringSet] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<'calendar' | 'manage'>('calendar');
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const today = useMemo(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  }, []);

  const weekBase = useMemo(() => {
    const d = new Date(today);
    d.setDate(d.getDate() + weekOffset * 7);
    return d;
  }, [today, weekOffset]);

  const weekDays = useMemo(() => getWeekDays(weekBase), [weekBase]);

  // 为任务分配稳定的主题色 index
  const taskThemeMap = useMemo(() => {
    const map = new Map<string, number>();
    tasks.forEach((t, i) => map.set(t.name, i));
    return map;
  }, [tasks]);

  // 每天应显示的任务
  const dayTasks = useMemo(() => {
    return weekDays.map((day) => tasks.filter((t) => taskVisibleOnDate(t.cron_expr, day)));
  }, [weekDays, tasks]);

  // 每个任务最近一次运行
  const latestRunMap = useMemo(() => {
    const map = new Map<string, CronJobRun>();
    // allRuns 已按时间倒序
    for (const run of allRuns) {
      if (!map.has(run.job_name)) {
        map.set(run.job_name, run);
      }
    }
    return map;
  }, [allRuns]);

  // 加载数据
  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [jobs, runs] = await Promise.all([getCronJobs(), getCronRuns(undefined, 100)]);
      setTasks(jobs);
      setAllRuns(runs);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, []);

  // 卸载守卫：轮询中检查，避免 setState on unmounted
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 2500);
    return () => clearTimeout(timer);
  }, [notice]);

  // 点击任务卡片 → 加载该任务执行历史并打开弹窗
  const handleOpenTask = useCallback(async (task: CronTask) => {
    setSelectedTask(task);
    try {
      const runs = await getCronRuns(task.name, 20);
      setTaskRuns(runs);
    } catch {
      setTaskRuns([]);
    }
  }, []);

  const handleTrigger = useCallback(async (name: string) => {
    setTriggeringSet((prev) => new Set(prev).add(name));
    const clearTriggering = () => {
      if (mountedRef.current) {
        setTriggeringSet((prev) => { const next = new Set(prev); next.delete(name); return next; });
      }
    };
    try {
      const result = await triggerCronJob(name);
      setNotice({ type: 'success', text: result.message || `任务 ${name} 已提交后台执行` });
      window.dispatchEvent(new CustomEvent('cron-job-done'));

      // 轮询执行状态，直到完成或超时
      const runId = result.run_id;
      const maxAttempts = 60; // 最多 60 次，约 2 分钟
      let attempts = 0;
      const poll = async () => {
        while (attempts < maxAttempts) {
          if (!mountedRef.current) return;
          attempts++;
          await new Promise((r) => setTimeout(r, 2000));
          if (!mountedRef.current) return;
          try {
            const run = await getCronRunStatus(runId);
            if (run.status !== 'running') {
              if (!mountedRef.current) return;
              // 执行完成，刷新数据
              setNotice({
                type: run.status === 'success' ? 'success' : 'error',
                text: run.status === 'success' ? `任务 ${name} 执行成功` : `任务 ${name} 执行失败`,
              });
              const [runs, allR] = await Promise.all([getCronRuns(name, 20), getCronRuns(undefined, 100)]);
              if (!mountedRef.current) return;
              setTaskRuns(runs);
              setAllRuns(allR);
              return;
            }
          } catch {
            // 轮询失败不中断，继续重试
          }
        }
        if (!mountedRef.current) return;
        // 超时，仍然刷新一次
        const [runs, allR] = await Promise.all([getCronRuns(name, 20), getCronRuns(undefined, 100)]);
        if (!mountedRef.current) return;
        setTaskRuns(runs);
        setAllRuns(allR);
      };
      // 后台轮询，不阻塞 UI
      poll().finally(clearTriggering);
      return; // clearTriggering 由 poll finally 处理
    } catch (err) {
      const message = err instanceof Error ? err.message : '提交任务失败';
      setNotice({ type: 'error', text: message });
      clearTriggering();
    }
  }, []);

  return (
    <div className="flex flex-col h-full bg-claude-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold text-claude-text">日程</h2>
          {/* Tab switch */}
          <div className="flex text-sm">
            <button
              onClick={() => setTab('calendar')}
              className={`px-3 py-1 rounded-l-lg border border-claude-border ${
                tab === 'calendar' ? 'bg-claude-surface text-claude-text font-medium' : 'text-claude-secondary hover:bg-claude-hover'
              }`}
            >
              日历
            </button>
            <button
              onClick={() => setTab('manage')}
              className={`px-3 py-1 rounded-r-lg border border-l-0 border-claude-border ${
                tab === 'manage' ? 'bg-claude-surface text-claude-text font-medium' : 'text-claude-secondary hover:bg-claude-hover'
              }`}
            >
              日程管理
            </button>
          </div>
        </div>
        {onClose && (
          <button onClick={onClose} className="p-1 rounded hover:bg-claude-hover text-claude-muted">✕</button>
        )}
      </div>

      {notice && (
        <div className="px-4 pt-3">
          <div className={`rounded-md border px-3 py-2 text-sm ${
            notice.type === 'success' ? 'border-green-200 bg-green-50 text-green-700' : 'border-red-200 bg-red-50 text-red-700'
          }`}>
            {notice.text}
          </div>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center h-32 text-claude-muted">加载中...</div>
      ) : tab === 'calendar' ? (
        // ──── Calendar View ────
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Week nav */}
          <div className="flex items-center justify-center gap-3 py-3 border-b border-claude-border">
            <button onClick={() => setWeekOffset((o) => o - 1)} className="p-1.5 rounded-lg hover:bg-claude-hover text-claude-secondary">
              ‹
            </button>
            <span className="text-sm font-medium text-claude-text min-w-[200px] text-center">
              {formatDateRange(weekDays)}
            </span>
            <button onClick={() => setWeekOffset((o) => o + 1)} className="p-1.5 rounded-lg hover:bg-claude-hover text-claude-secondary">
              ›
            </button>
            {weekOffset !== 0 && (
              <button
                onClick={() => setWeekOffset(0)}
                className="ml-1 px-2 py-0.5 text-xs rounded bg-claude-surface text-claude-accent hover:bg-claude-hover border border-claude-border"
              >
                今天
              </button>
            )}
          </div>

          {/* Day columns */}
          <div className="flex-1 grid grid-cols-7 divide-x divide-claude-border overflow-y-auto">
            {weekDays.map((day, idx) => {
              const isToday = isSameDay(day, today);
              const tasksForDay = dayTasks[idx];
              return (
                <div key={idx} className={`flex flex-col min-h-0 ${isToday ? 'bg-claude-hover/30' : ''}`}>
                  {/* Day header */}
                  <div className={`px-2 py-2 text-center border-b border-claude-border ${isToday ? 'bg-claude-accent/10' : ''}`}>
                    <div className="text-[10px] text-claude-muted leading-tight">
                      {DAY_LABELS[idx]} / {DAY_EN[idx]}
                    </div>
                    <div className={`text-lg font-semibold leading-tight mt-0.5 ${
                      isToday ? 'text-claude-accent' : 'text-claude-text'
                    }`}>
                      {day.getDate()}
                    </div>
                  </div>
                  {/* Task cards */}
                  <div className="flex-1 p-1.5 space-y-0 overflow-y-auto">
                    {tasksForDay.map((task) => (
                      <TaskCard
                        key={task.name}
                        task={task}
                        themeIdx={taskThemeMap.get(task.name) ?? 0}
                        latestRun={latestRunMap.get(task.name)}
                        onClick={() => handleOpenTask(task)}
                      />
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
        // ──── Manage View (任务列表) ────
        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {tasks.length === 0 ? (
            <div className="text-center text-claude-muted py-8">
              <p className="mb-2">暂无日程</p>
              <p className="text-xs">让 Agent 使用 manage_cron 工具创建日程</p>
            </div>
          ) : (
            tasks.map((task) => {
              const latest = latestRunMap.get(task.name);
              return (
                <button
                  key={task.name}
                  onClick={() => handleOpenTask(task)}
                  className="w-full text-left p-3 rounded-lg border border-claude-border hover:bg-claude-hover/50 transition-colors"
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm text-claude-text">{task.description || task.name}</span>
                      <span className={`px-1.5 py-0.5 text-xs rounded ${
                        task.enabled ? 'bg-green-100 text-green-700' : 'bg-claude-surface text-claude-muted'
                      }`}>
                        {task.enabled ? '启用' : '暂停'}
                      </span>
                    </div>
                    {latest && (
                      <span className={`text-xs ${statusColor(latest.status)}`}>
                        {statusIcon(latest.status)} {statusLabel(latest.status)}
                      </span>
                    )}
                  </div>
                  <div className="text-xs text-claude-secondary mt-1.5 flex items-center gap-3">
                    <span>🔄 {cronToReadable(task.cron_expr)}</span>
                    <code className="bg-claude-surface px-1 rounded">{task.cron_expr}</code>
                    {task.name !== task.description && (
                      <span className="text-claude-muted">{task.name}</span>
                    )}
                  </div>
                </button>
              );
            })
          )}
        </div>
      )}

      {/* Task detail modal */}
      {selectedTask && (
        <TaskDetailModal
          task={selectedTask}
          runs={taskRuns}
          onClose={() => setSelectedTask(null)}
          onTrigger={handleTrigger}
          triggering={triggeringSet.has(selectedTask.name)}
        />
      )}
    </div>
  );
};

export default CronSchedule;
