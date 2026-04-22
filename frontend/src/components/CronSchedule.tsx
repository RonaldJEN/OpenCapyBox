import React, { useState, useEffect, useMemo, useCallback, useRef } from 'react';
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
    // M H * * 0-4 → 工作日（与 APScheduler: 0=周一..6=周日 对齐）
    if (!hasDom && !hasMon && dow === '0-4') {
      return `工作日 ${timeStr}`;
    }
    // M H * * 5,6 or 6,5 → 周末（周六/周日）
    if (!hasDom && !hasMon && (dow === '5,6' || dow === '6,5')) {
      return `周末 ${timeStr}`;
    }
    // M H * * N,N,... → 每周多天
    if (!hasDom && !hasMon && hasDow) {
      const dayNames = ['一', '二', '三', '四', '五', '六', '日'];
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
  if (dowSet) {
    // 与 APScheduler 对齐：0=周一..6=周日
    const dowMonFirst = (date.getDay() + 6) % 7;
    if (!dowSet.has(dowMonFirst)) return false;
  }
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
}

const CronSchedule: React.FC<Props> = ({ onClose, unreadCount = 0, onUnreadChange }) => {
  const [tasks, setTasks] = useState<CronTask[]>([]);
  const [allRuns, setAllRuns] = useState<CronJobRun[]>([]);
  const [weekOffset, setWeekOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [triggeringSet, setTriggeringSet] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<'calendar' | 'manage'>('calendar');
  const [showMessages, setShowMessages] = useState(false);
  const [notice, setNotice] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
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

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 2500);
    return () => clearTimeout(timer);
  }, [notice]);

  const handleTrigger = useCallback(async (name: string) => {
    setTriggeringSet((prev) => new Set(prev).add(name));
    const clearTriggering = () => {
      if (mountedRef.current) {
        setTriggeringSet((prev) => { const next = new Set(prev); next.delete(name); return next; });
      }
    };
    try {
      const result = await triggerCronJob(name);

      // 轮询执行状态，直到退出 running。
      const runId = result.run_id;
      const poll = async () => {
        while (true) {
          if (!mountedRef.current) return;
          await new Promise((r) => setTimeout(r, 2000));
          if (!mountedRef.current) return;
          try {
            const run = await getCronRunStatus(runId);
            if (run.status !== 'running') {
              if (!mountedRef.current) return;
              // 执行完成后：成功静默，失败才提示。
              if (run.status !== 'success') {
                setNotice({ type: 'error', text: `任务 ${name} 执行失败` });
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

  const handleDelete = useCallback(async (name: string) => {
    if (!window.confirm(`确认删除任务「${name}」？历史执行记录会保留。`)) return;
    try {
      await deleteCronJob(name);
      setNotice({ type: 'success', text: `已删除任务 ${name}` });
      await loadData();
    } catch (e) {
      setNotice({ type: 'error', text: e instanceof Error ? e.message : '删除失败' });
    }
  }, [loadData]);

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
    <div className="relative flex flex-col h-full bg-claude-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-semibold text-claude-text">日程</h2>
          {/* View switcher（去 messages tab，messages 改为顶栏按钮覆盖式弹出） */}
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
              列表
            </button>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setFormMode('new')}
            className="px-2.5 py-1 text-xs rounded bg-claude-accent text-white hover:opacity-90"
          >
            + 新建任务
          </button>
          <button
            onClick={() => setShowMessages(true)}
            className="relative px-2.5 py-1 text-xs rounded border border-claude-border bg-claude-surface text-claude-text hover:bg-claude-hover"
          >
            执行记录
            {unreadCount > 0 && (
              <span className="absolute -top-1.5 -right-1.5 min-w-[18px] h-[18px] flex items-center justify-center px-1 text-[10px] font-bold text-white bg-red-500 rounded-full">
                {unreadCount > 99 ? '99+' : unreadCount}
              </span>
            )}
          </button>
          {onClose && (
            <button onClick={onClose} className="p-1 rounded hover:bg-claude-hover text-claude-muted">✕</button>
          )}
        </div>
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
          onDelete={handleDelete}
          onTrigger={handleTrigger}
          onToggleEnabled={handleToggleEnabled}
          triggeringSet={triggeringSet}
          togglingSet={togglingSet}
        />
      ) : null}

      {/* 执行记录覆盖式抽屉（顶栏按钮触发） */}
      {showMessages && (
        <div className="absolute inset-0 z-40 flex flex-col bg-claude-bg">
          <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
            <h3 className="text-base font-semibold text-claude-text">执行记录</h3>
            <button
              onClick={() => setShowMessages(false)}
              className="px-2 py-1 text-xs rounded border border-claude-border bg-claude-surface text-claude-text hover:bg-claude-hover"
            >
              ← 返回日程
            </button>
          </div>
          <CronMessageCenter onUnreadChange={onUnreadChange} />
        </div>
      )}

      {/* Task form drawer (新建 / 编辑) */}
      {formMode !== null && (
        <TaskFormDrawer
          task={formMode === 'new' ? null : formMode}
          onClose={() => setFormMode(null)}
          onSaved={handleFormSaved}
        />
      )}
    </div>
  );
};

export default CronSchedule;
