import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { ArrowLeft, CalendarX2, History, Trash2 } from 'lucide-react';
import {
  getCronJobs,
  getCronRuns,
  triggerCronJob,
  getCronRunStatus,
  deleteCronJob,
  updateCronJob,
  type CronTask,
  type CronJobRun,
} from '../services/configApi';
import CronMessageCenter from './CronMessageCenter';
import TaskFormDrawer from './cron/TaskFormDrawer';
import WeekAgenda from './cron/WeekAgenda';
import ScheduleList from './cron/ScheduleList';
import { ConfirmDialog } from './ConfirmDialog';
import FeedbackMessage from './FeedbackMessage';

// ────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────

/** 解析 cron 字段值为数字集合，支持通配符、步进、逗号和范围(1-5) */
function parseCronField(
  field: string,
  minimum: number,
  maximum: number,
): Set<number> | null {
  if (field === '*') return null; // null 表示"所有值都匹配"
  const nums = new Set<number>();
  for (const part of field.split(',')) {
    const [base, stepText, extra] = part.split('/');
    const step = stepText === undefined ? 1 : Number(stepText);
    if (extra !== undefined || !Number.isInteger(step) || step <= 0) return null;

    let start: number;
    let end: number;
    if (base === '*') {
      start = minimum;
      end = maximum;
    } else if (base.includes('-')) {
      const range = base.split('-').map(Number);
      if (
        range.length !== 2
        || range.some((value) => !Number.isInteger(value))
      ) return null;
      [start, end] = range;
    } else {
      start = Number(base);
      end = start;
    }

    if (
      !Number.isInteger(start)
      || !Number.isInteger(end)
      || start < minimum
      || end > maximum
      || start > end
    ) return null;
    for (let value = start; value <= end; value += step) nums.add(value);
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
    // Linux/Vixie Cron：1=周一..5=周五
    if (!hasDom && !hasMon && dow === '1-5') {
      return `工作日 ${timeStr}`;
    }
    // 周末：0/7=周日，6=周六
    if (!hasDom && !hasMon && ['0,6', '6,0', '7,6', '6,7'].includes(dow)) {
      return `周末 ${timeStr}`;
    }
    // M H * * N,N,... → 每周多天
    if (!hasDom && !hasMon && hasDow) {
      const dayNames = ['日', '一', '二', '三', '四', '五', '六', '日'];
      if (dow === '2-6,0' || dow === '2-6,7') {
        return `每周二至周日 ${timeStr}`;
      }
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
export function taskVisibleOnDate(expr: string, date: Date): boolean {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return true; // 无法解析就都显示
  const [, , dom, mon, dow] = parts;

  // month 始终为 AND 条件
  const monSet = parseCronField(mon, 1, 12);
  if (monSet && !monSet.has(date.getMonth() + 1)) return false;

  // Linux/Vixie Cron：日与星期都受限时使用 OR，否则匹配受限的一方。
  const domSet = parseCronField(dom, 1, 31);
  const dowSet = parseCronField(dow, 0, 7);
  if (dowSet?.has(7)) {
    dowSet.add(0);
    dowSet.delete(7);
  }
  const domMatches = domSet ? domSet.has(date.getDate()) : true;
  const dowMatches = dowSet ? dowSet.has(date.getDay()) : true;
  if (domSet && dowSet) return domMatches || dowMatches;
  if (domSet) return domMatches;
  if (dowSet) return dowMatches;
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

// ────────────────────────────────────────────
// Legacy TaskCard / TaskDetailModal removed —
// 使用 WeekAgenda / ScheduleList 替代。
// status helpers / 主题色 / DAY_LABELS / isSameDay 已迁入对应子组件。
// ────────────────────────────────────────────

// ────────────────────────────────────────────
// Main component
// ────────────────────────────────────────────

interface Props {
  onClose?: () => void;
  unreadCount?: number;
  onUnreadChange?: (count: number) => void;
  variant?: 'panel' | 'page';
}

const CronSchedule: React.FC<Props> = ({
  onClose,
  unreadCount = 0,
  onUnreadChange,
  variant = 'panel',
}) => {
  const [tasks, setTasks] = useState<CronTask[]>([]);
  const [allRuns, setAllRuns] = useState<CronJobRun[]>([]);
  const [weekOffset, setWeekOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [triggeringSet, setTriggeringSet] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<'calendar' | 'manage'>('calendar');
  const [showMessages, setShowMessages] = useState(false);
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CronTask | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const deleteReturnFocusTaskRef = useRef<string | null>(null);
  // 表单抽屉：null = 关闭；'new' = 新建；CronTask = 编辑
  const [formMode, setFormMode] = useState<'new' | CronTask | null>(null);

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
    // 日历视图只展示已启用任务；已暂停任务仍在列表视图中可见、可编辑、可启用。
    return weekDays.map((day) => tasks.filter((t) => t.enabled && taskVisibleOnDate(t.cron_expr, day)));
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
      const [jobs, runsResp] = await Promise.all([getCronJobs(), getCronRuns(undefined, 100)]);
      setTasks(jobs);
      setAllRuns(runsResp.runs);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, []);

  // 卸载守卫：轮询中检查，避免 setState on unmounted
  const mountedRef = useRef(true);
  useEffect(() => {
    // React StrictMode(dev) 会额外执行一次 setup->cleanup->setup，
    // 这里在 setup 里显式置 true，避免 cleanup 把 ref 留在 false 导致 finally 不清理。
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleTrigger = useCallback(async (name: string) => {
    setTriggeringSet((prev) => new Set(prev).add(name));
    const clearTriggering = () => {
      if (mountedRef.current) {
        setTriggeringSet((prev) => { const next = new Set(prev); next.delete(name); return next; });
      }
    };
    try {
      const result = await triggerCronJob(name);

      // queued/running 都是执行中；只在明确终态后停止轮询。
      const runId = result.run_id;
      const poll = async () => {
        while (mountedRef.current) {
          await new Promise((r) => setTimeout(r, 2000));
          if (!mountedRef.current) return;
          try {
            const run = await getCronRunStatus(runId);
            if (run.status !== 'queued' && run.status !== 'running') {
              if (!mountedRef.current) return;
              // 执行完成后：成功静默，失败才提示。
              if (run.status !== 'success') {
                setNotice({ type: 'error', text: `任务 ${name} 执行${run.status === 'conflict' ? '发生冲突' : run.status === 'unknown' ? '状态未知' : '失败'}` });
              }
              const allResp = await getCronRuns(undefined, 100);
              if (!mountedRef.current) return;
              setAllRuns(allResp.runs);
              return;
            }
          } catch {
            // 轮询失败不中断，继续重试
          }
        }
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

  const restoreDeleteTriggerFocus = useCallback(() => {
    const taskName = deleteReturnFocusTaskRef.current;
    deleteReturnFocusTaskRef.current = null;
    if (!taskName) return;
    requestAnimationFrame(() => {
      const root = Array.from(document.querySelectorAll<HTMLElement>('[data-schedule-menu-root]'))
        .find((element) => element.dataset.scheduleMenuRoot === taskName);
      root?.querySelector<HTMLButtonElement>('button[aria-label="更多操作"]')?.focus();
    });
  }, []);

  const handleDeleteRequest = useCallback((task: CronTask) => {
    deleteReturnFocusTaskRef.current = task.name;
    setDeleteError(null);
    setDeleteTarget(task);
  }, []);

  const handleDeleteCancel = useCallback(() => {
    if (deletePending) return;
    setDeleteTarget(null);
    setDeleteError(null);
    restoreDeleteTriggerFocus();
  }, [deletePending, restoreDeleteTriggerFocus]);

  const handleDeleteConfirm = useCallback(async () => {
    if (!deleteTarget || deletePending) return;
    const name = deleteTarget.name;
    setDeletePending(true);
    setDeleteError(null);
    try {
      await deleteCronJob(name);
      deleteReturnFocusTaskRef.current = null;
      setDeleteTarget(null);
      setNotice({ type: 'success', text: `已删除任务 ${name}` });
      await loadData();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : '删除失败，请稍后重试');
    } finally {
      if (mountedRef.current) setDeletePending(false);
    }
  }, [deletePending, deleteTarget, loadData]);

  const [togglingSet, setTogglingSet] = useState<Set<string>>(new Set());
  const handleToggleEnabled = useCallback(async (task: CronTask) => {
    setTogglingSet((prev) => new Set(prev).add(task.name));
    try {
      const next = !task.enabled;
      const updated = await updateCronJob(task.name, { enabled: next });
      // 局部更新，避免全量重拉闪烁
      setTasks((prev) => prev.map((t) => (t.name === task.name ? { ...t, enabled: updated.enabled } : t)));
    } catch (e) {
      setNotice({ type: 'error', text: e instanceof Error ? e.message : '状态切换失败' });
    } finally {
      if (mountedRef.current) {
        setTogglingSet((prev) => { const n = new Set(prev); n.delete(task.name); return n; });
      }
    }
  }, []);

  const handleFormSaved = useCallback(async (saved: CronTask) => {
    setFormMode(null);
    setNotice({ type: 'success', text: `已保存任务 ${saved.name}` });
    await loadData();
  }, [loadData]);

  return (
    <div
      className={`relative flex h-full min-h-0 flex-col ${variant === 'page' ? 'bg-transparent' : 'bg-claude-bg'}`}
      data-testid="cron-schedule"
      data-variant={variant}
    >
      {/* Header */}
      {!showMessages && (
        <div className={`flex shrink-0 flex-wrap items-center justify-between gap-3 border-b ${
          variant === 'page'
            ? 'border-[#e8e3d9] pb-4'
            : 'border-claude-border px-4 py-3'
        }`}>
        <div className="flex items-center gap-3">
          {variant === 'panel' && <h2 className="text-lg font-semibold text-claude-text">日程</h2>}
          {/* View switcher（去 messages tab，messages 改为顶栏按钮覆盖式弹出） */}
          <div className={`flex text-sm ${variant === 'page' ? 'rounded-[10px] bg-[#f0ede7] p-1' : ''}`}>
            <button
              type="button"
              onClick={() => setTab('calendar')}
              aria-pressed={tab === 'calendar'}
              className={`${variant === 'page' ? 'rounded-[7px] border-0 px-3.5 py-1.5' : 'rounded-l-lg border border-claude-border px-3 py-1'} ${
                tab === 'calendar'
                  ? variant === 'page'
                    ? 'bg-white font-semibold text-[#1c1a16] shadow-sm'
                    : 'bg-claude-surface font-medium text-claude-text'
                  : 'text-claude-secondary hover:bg-white/70'
              }`}
            >
              日历
            </button>
            <button
              type="button"
              onClick={() => setTab('manage')}
              aria-pressed={tab === 'manage'}
              className={`${variant === 'page' ? 'rounded-[7px] border-0 px-3.5 py-1.5' : 'rounded-r-lg border border-l-0 border-claude-border px-3 py-1'} ${
                tab === 'manage'
                  ? variant === 'page'
                    ? 'bg-white font-semibold text-[#1c1a16] shadow-sm'
                    : 'bg-claude-surface font-medium text-claude-text'
                  : 'text-claude-secondary hover:bg-white/70'
              }`}
            >
              列表
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setFormMode('new')}
            className={`${variant === 'page' ? 'h-9 rounded-[10px] px-3.5 text-[13px]' : 'rounded px-2.5 py-1 text-xs'} bg-claude-accent font-semibold text-white transition hover:opacity-90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/35`}
          >
            + 新建任务
          </button>
          <button
            type="button"
            onClick={() => setShowMessages(true)}
            aria-label={unreadCount > 0 ? `执行记录，${unreadCount} 条未读` : '执行记录'}
            className={`${variant === 'page' ? 'h-9 rounded-[10px] px-3.5 text-[13px]' : 'rounded px-2.5 py-1 text-xs'} relative border border-claude-border bg-white font-semibold text-claude-text transition hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/25`}
          >
            执行记录
            {unreadCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 min-w-[18px] h-[18px] flex items-center justify-center px-1 text-[10px] font-bold text-white bg-red-500 rounded-full">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </button>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="p-1 rounded hover:bg-claude-hover text-claude-muted"
              aria-label="关闭日程"
              title="关闭日程"
            >
              ✕
            </button>
          )}
        </div>
        </div>
      )}

      {!showMessages && notice && (
        <div className="px-4 pt-3">
          <FeedbackMessage
            className={`rounded-md border px-3 py-2 text-sm ${
              notice.type === 'success' ? 'border-green-200 bg-green-50 text-green-700' : 'border-red-200 bg-red-50 text-red-700'
            }`}
            tone={notice.type}
            onDismiss={() => setNotice(null)}
          >
            {notice.text}
          </FeedbackMessage>
        </div>
      )}

      {showMessages ? (
        <div className="flex min-h-0 flex-1 flex-col" data-testid="schedule-history-view">
          <div className={`flex shrink-0 items-center gap-3 border-b ${
            variant === 'page'
              ? 'border-[#e8e3d9] pb-4'
              : 'border-claude-border px-4 py-3'
          }`}>
            <button
              type="button"
              onClick={() => setShowMessages(false)}
              className="inline-flex h-9 items-center gap-2 rounded-[10px] border border-claude-border bg-white px-3 text-[13px] font-semibold text-claude-text transition hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/25"
            >
              <ArrowLeft size={15} aria-hidden="true" />
              返回日程
            </button>
            <h2 className="text-[15px] font-semibold text-claude-text">执行记录</h2>
          </div>
          <CronMessageCenter
            unreadCount={unreadCount}
            onUnreadChange={onUnreadChange || (() => undefined)}
          />
        </div>
      ) : loading ? (
        <div className="flex items-center justify-center h-32 text-claude-muted">加载中...</div>
      ) : tab === 'calendar' ? (
        // ──── Calendar View ────
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Week nav */}
          <div className="flex items-center justify-center gap-3 py-3 border-b border-claude-border">
            <button
              type="button"
              onClick={() => setWeekOffset((o) => o - 1)}
              className="p-1.5 rounded-lg hover:bg-claude-hover text-claude-secondary"
              aria-label="上一周"
              title="上一周"
            >
              ‹
            </button>
            <span className="text-sm font-medium text-claude-text min-w-[200px] text-center">
              {formatDateRange(weekDays)}
            </span>
            <button
              type="button"
              onClick={() => setWeekOffset((o) => o + 1)}
              className="p-1.5 rounded-lg hover:bg-claude-hover text-claude-secondary"
              aria-label="下一周"
              title="下一周"
            >
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

          {/* Day columns (Agenda) */}
          <WeekAgenda
            weekDays={weekDays}
            today={today}
            dayTasks={dayTasks.map((tasksForDay) =>
              tasksForDay.map((task) => ({ task, time: cronTime(task.cron_expr) })),
            )}
            taskThemeMap={taskThemeMap}
            cronToReadable={cronToReadable}
            onTrigger={handleTrigger}
            triggeringSet={triggeringSet}
          />
        </div>
      ) : tab === 'manage' ? (
        // ──── Manage View (任务列表) ────
        <ScheduleList
          tasks={tasks}
          latestRunMap={latestRunMap}
          cronToReadable={cronToReadable}
          cronTime={cronTime}
          onEdit={(task) => setFormMode(task)}
          onDelete={handleDeleteRequest}
          onTrigger={handleTrigger}
          onToggleEnabled={handleToggleEnabled}
          triggeringSet={triggeringSet}
          togglingSet={togglingSet}
        />
      ) : null}

      {/* Task form drawer (新建 / 编辑) */}
      {formMode !== null && (
        <TaskFormDrawer
          task={formMode === 'new' ? null : formMode}
          onClose={() => setFormMode(null)}
          onSaved={handleFormSaved}
        />
      )}

      {deleteTarget && (
        <ConfirmDialog
          eyebrow="删除任务"
          icon={<Trash2 size={18} strokeWidth={2.2} aria-hidden="true" />}
          title="删除这个任务？"
          description={
            <>
              “<span className="font-semibold text-[#37332d]">{deleteTarget.name}</span>”
              将从日程中移除。
            </>
          }
          details={
            <div className="mt-4 overflow-hidden rounded-xl border border-[#e8e3d9] bg-white">
              <div className="flex items-center gap-3 px-3.5 py-3">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-red-50 text-red-600">
                  <CalendarX2 size={16} aria-hidden="true" />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-semibold text-[#2d2923]">后续计划</p>
                  <p className="mt-0.5 text-[12px] text-[#7c756b]">删除后不再自动执行</p>
                </div>
                <span className="rounded-md bg-red-50 px-2 py-1 text-[11px] font-semibold text-red-600">停止</span>
              </div>
              <div className="mx-3.5 h-px bg-[#eee9df]" />
              <div className="flex items-center gap-3 px-3.5 py-3">
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
                  <History size={16} aria-hidden="true" />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[13px] font-semibold text-[#2d2923]">历史执行记录</p>
                  <p className="mt-0.5 text-[12px] text-[#7c756b]">已产生的结果不会删除</p>
                </div>
                <span className="rounded-md bg-emerald-50 px-2 py-1 text-[11px] font-semibold text-emerald-700">保留</span>
              </div>
            </div>
          }
          confirmLabel="删除任务"
          busyLabel="正在删除…"
          busy={deletePending}
          error={deleteError}
          onCancel={handleDeleteCancel}
          onConfirm={() => void handleDeleteConfirm()}
        />
      )}
    </div>
  );
};

export default CronSchedule;
