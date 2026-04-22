/**
 * ScheduleList — Cron 列表视图
 *
 * 对应 plan Phase 3：
 * - 顶部支持筛选与搜索；展示启用/暂停/失败计数。
 * - 任务数 < 10：卡片态，每行展示 名称 + 合并 meta + 主次操作。
 * - 任务数 ≥ 10：表格密排态。
 * - 行点击打开详情，按钮区只做操作。
 *
 * 不做：uptime 条、批量启停（在 plan Phase 7 范围）。
 */
import React, { useEffect, useMemo, useState } from 'react';
import {
  CalendarDays,
  Loader2,
  MoreHorizontal,
  PenSquare,
  Trash2,
  Zap,
} from 'lucide-react';
import { type CronJobRun, type CronTask } from '../../services/configApi';

const TABLE_THRESHOLD = 10;
const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'enabled', label: '启用中' },
  { key: 'paused', label: '已暂停' },
  { key: 'failed', label: '最近失败' },
] as const;

type FilterKey = typeof FILTERS[number]['key'];

function statusGlyph(status: string | undefined): { ch: string; cls: string; label: string } | null {
  if (!status) return null;
  if (status === 'success') return { ch: '✓', cls: 'text-green-500', label: '成功' };
  if (status === 'failed') return { ch: '✕', cls: 'text-red-500', label: '失败' };
  if (status === 'running') return { ch: '⟳', cls: 'text-yellow-500', label: '运行中' };
  return { ch: '○', cls: 'text-claude-muted', label: status };
}

interface Props {
  tasks: CronTask[];
  latestRunMap: Map<string, CronJobRun>;
  cronToReadable: (expr: string) => string;
  /** 从 cron 表达式提取固定时间（HH:MM），供排序使用。 */
  cronTime: (expr: string) => string | null;
  onEdit: (task: CronTask) => void;
  onDelete: (name: string) => void;
  onTrigger: (name: string) => void;
  onToggleEnabled: (task: CronTask) => void;
  triggeringSet: Set<string>;
  togglingSet: Set<string>;
}

interface ToggleTaskSwitchProps {
  task: CronTask;
  toggling: boolean;
  onToggle: (task: CronTask) => void;
}

const ToggleTaskSwitch: React.FC<ToggleTaskSwitchProps> = ({
  task,
  toggling,
  onToggle,
}) => (
  <button
    type="button"
    role="switch"
    aria-checked={task.enabled}
    aria-label={task.enabled ? '暂停任务' : '启用任务'}
    title={task.enabled ? '暂停任务' : '启用任务'}
    onClick={(e) => {
      e.stopPropagation();
      onToggle(task);
    }}
    disabled={toggling}
    className={`relative inline-flex h-7 w-11 shrink-0 items-center rounded-full border transition-colors disabled:opacity-50 ${
      task.enabled
        ? 'bg-emerald-500 border-emerald-500'
        : 'bg-claude-surface border-claude-border'
    }`}
  >
    {toggling && (
      <Loader2
        size={11}
        className="absolute left-1.5 top-1/2 -translate-y-1/2 text-white animate-spin"
        aria-hidden="true"
      />
    )}
    <span
      className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform ${
        task.enabled ? 'translate-x-5' : 'translate-x-1'
      }`}
    />
  </button>
);

function compareTaskByTime(
  a: CronTask,
  b: CronTask,
  cronTime: (expr: string) => string | null,
): number {
  const ta = cronTime(a.cron_expr);
  const tb = cronTime(b.cron_expr);
  if (ta && tb) {
    const byTime = ta.localeCompare(tb);
    if (byTime !== 0) return byTime;
    return a.name.localeCompare(b.name);
  }
  if (ta && !tb) return -1;
  if (!ta && tb) return 1;
  return a.name.localeCompare(b.name);
}

const ScheduleList: React.FC<Props> = ({
  tasks,
  latestRunMap,
  cronToReadable,
  cronTime,
  onEdit,
  onDelete,
  onTrigger,
  onToggleEnabled,
  triggeringSet,
  togglingSet,
}) => {
  const [filter, setFilter] = useState<FilterKey>('all');
  const [search, setSearch] = useState('');
  const [openMenuTask, setOpenMenuTask] = useState<string | null>(null);

  useEffect(() => {
    if (!openMenuTask) return;
    const handleMouseDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      const insideCurrentMenuRoot = target.closest(
        `[data-schedule-menu-root="${openMenuTask}"]`,
      );
      if (insideCurrentMenuRoot) return;
      setOpenMenuTask(null);
    };
    document.addEventListener('mousedown', handleMouseDown);
    return () => {
      document.removeEventListener('mousedown', handleMouseDown);
    };
  }, [openMenuTask]);

  const stats = useMemo(() => {
    const enabled = tasks.filter((t) => t.enabled).length;
    const paused = tasks.length - enabled;
    const failed = tasks.filter((t) => latestRunMap.get(t.name)?.status === 'failed').length;
    return { total: tasks.length, enabled, paused, failed };
  }, [tasks, latestRunMap]);

  const filteredTasks = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    return tasks.filter((task) => {
      const latest = latestRunMap.get(task.name);
      if (filter === 'enabled' && !task.enabled) return false;
      if (filter === 'paused' && task.enabled) return false;
      if (filter === 'failed' && latest?.status !== 'failed') return false;
      if (!keyword) return true;
      const hay = `${task.name} ${task.description} ${task.cron_expr}`.toLowerCase();
      return hay.includes(keyword);
    });
  }, [tasks, latestRunMap, filter, search]);

  const sortedTasks = useMemo(
    () => [...filteredTasks].sort((a, b) => compareTaskByTime(a, b, cronTime)),
    [filteredTasks, cronTime],
  );
  const dense = sortedTasks.length >= TABLE_THRESHOLD;
  const hasTasks = tasks.length > 0;
  const hasMatches = sortedTasks.length > 0;

  if (!hasTasks) {
    return (
      <div className="flex-1 overflow-y-auto p-4">
        <div className="text-center text-claude-muted py-8">
          <p className="mb-2">暂无日程</p>
          <p className="text-xs">点击右上角「+ 新建任务」创建</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-xl border border-claude-border bg-claude-surface p-0.5">
          {FILTERS.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setFilter(item.key)}
              className={`px-3 py-1 text-xs rounded-lg transition-colors ${
                filter === item.key
                  ? 'bg-claude-bg text-claude-text font-medium border border-claude-border'
                  : 'text-claude-secondary hover:text-claude-text'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>

        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索任务名 / cron 表达式"
          className="h-9 min-w-[240px] flex-1 rounded-xl border border-claude-border bg-claude-bg px-3 text-sm text-claude-text placeholder:text-claude-muted"
        />

        <div className="text-sm text-claude-secondary font-medium">
          {stats.total} 个任务 · {stats.enabled} 启用 / {stats.paused} 暂停 · {stats.failed} 失败
        </div>

        {dense && <span className="text-xs text-claude-muted">密排模式</span>}
      </div>

      {hasMatches ? (dense ? (
        <div className="rounded-lg border border-claude-border overflow-hidden" data-testid="schedule-list-table">
          <table className="w-full text-xs">
            <thead className="bg-claude-surface text-claude-secondary">
              <tr>
                <th className="text-left px-2 py-1.5 font-medium">名称</th>
                <th className="text-left px-2 py-1.5 font-medium">频率</th>
                <th className="text-left px-2 py-1.5 font-medium">状态</th>
                <th className="text-right px-2 py-1.5 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {sortedTasks.map((task) => {
                const latest = latestRunMap.get(task.name);
                const glyph = statusGlyph(latest?.status);
                return (
                  <tr
                    key={task.name}
                    data-task-name={task.name}
                    className={`border-t border-claude-border ${
                      task.enabled ? '' : 'bg-claude-surface/35'
                    }`}
                  >
                    <td className="px-2 py-1.5 truncate max-w-[140px]">
                      <span className={`text-claude-text ${task.enabled ? '' : 'text-claude-muted'}`}>
                        {task.description || task.name}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-claude-secondary truncate max-w-[120px]">
                      {cronToReadable(task.cron_expr)}
                    </td>
                    <td className="px-2 py-1.5">
                      <span className={`inline-flex items-center gap-1 ${glyph?.cls ?? 'text-claude-muted'}`}>
                        <span>{glyph?.ch ?? '○'}</span>
                        <span>{glyph?.label ?? '未执行'}</span>
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-right">
                      <TaskActions
                        task={task}
                        triggering={triggeringSet.has(task.name)}
                        toggling={togglingSet.has(task.name)}
                        openMenu={openMenuTask === task.name}
                        onMenuOpenChange={(open) => setOpenMenuTask(open ? task.name : null)}
                        onToggleEnabled={onToggleEnabled}
                        onEdit={onEdit}
                        onDelete={onDelete}
                        onTrigger={onTrigger}
                        compact
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="space-y-2" data-testid="schedule-list-cards">
          {sortedTasks.map((task) => {
            const latest = latestRunMap.get(task.name);
            const glyph = statusGlyph(latest?.status);
            return (
              <div
                key={task.name}
                data-task-name={task.name}
                className={`p-3 rounded-lg border border-claude-border ${
                  task.enabled ? 'hover:bg-claude-hover/30' : 'bg-claude-surface/35'
                }`}
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2 text-left flex-1 min-w-0">
                    <span className={`font-medium text-sm truncate ${task.enabled ? 'text-claude-text' : 'text-claude-muted'}`}>
                      {task.description || task.name}
                    </span>
                  </div>
                  <TaskActions
                    task={task}
                    triggering={triggeringSet.has(task.name)}
                    toggling={togglingSet.has(task.name)}
                    openMenu={openMenuTask === task.name}
                    onMenuOpenChange={(open) => setOpenMenuTask(open ? task.name : null)}
                    onToggleEnabled={onToggleEnabled}
                    onEdit={onEdit}
                    onDelete={onDelete}
                    onTrigger={onTrigger}
                  />
                </div>

                <div className="text-xs text-claude-secondary mt-1.5 flex items-center gap-3 flex-wrap min-w-0">
                  <span className="inline-flex items-center gap-1">
                    <CalendarDays size={12} aria-hidden="true" className="text-claude-muted" />
                    {cronToReadable(task.cron_expr)}
                  </span>
                  {glyph && (
                    <span className={`inline-flex items-center gap-1 ${glyph.cls}`}>
                      <span>{glyph.ch}</span>
                      <span>{glyph.label}</span>
                    </span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )) : (
        <div className="rounded-lg border border-dashed border-claude-border bg-claude-surface/40 p-8 text-center text-claude-secondary">
          <p className="text-sm font-medium text-claude-text mb-1">未找到匹配任务</p>
          <p className="text-xs mb-3">请调整筛选条件或搜索关键词</p>
          <button
            type="button"
            onClick={() => {
              setFilter('all');
              setSearch('');
            }}
            className="px-3 py-1.5 text-xs rounded border border-claude-border bg-claude-bg text-claude-text hover:bg-claude-hover"
          >
            清空筛选与搜索
          </button>
        </div>
      )}
    </div>
  );
};

interface TaskActionsProps {
  task: CronTask;
  triggering: boolean;
  toggling: boolean;
  openMenu: boolean;
  onMenuOpenChange: (open: boolean) => void;
  onToggleEnabled: (task: CronTask) => void;
  onEdit: (task: CronTask) => void;
  onDelete: (name: string) => void;
  onTrigger: (name: string) => void;
  compact?: boolean;
}

interface ActionButtonProps {
  label: string;
  icon: React.ReactNode;
  compact?: boolean;
  tone?: 'accent' | 'neutral' | 'danger';
  disabled?: boolean;
  onClick: () => void;
}

const ActionButton: React.FC<ActionButtonProps> = ({
  label,
  icon,
  compact = false,
  tone = 'neutral',
  disabled = false,
  onClick,
}) => {
  const toneCls =
    tone === 'accent'
      ? 'text-amber-700 hover:bg-amber-50'
      : tone === 'danger'
        ? 'text-red-600 hover:bg-red-50'
        : 'text-claude-text hover:bg-claude-hover';

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={`inline-flex w-full justify-start items-center rounded-lg font-medium transition-colors disabled:opacity-50 ${toneCls} ${
        compact ? 'gap-1.5 px-2 py-1.5 text-xs' : 'gap-1 px-2 py-1 text-xs'
      }`}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
};

const TaskActions: React.FC<TaskActionsProps> = ({
  task,
  triggering,
  toggling,
  openMenu,
  onMenuOpenChange,
  onToggleEnabled,
  onEdit,
  onDelete,
  onTrigger,
  compact = false,
}) => (
  <div
    data-schedule-menu-root={task.name}
    className="relative inline-flex items-center gap-1.5 shrink-0"
    onClick={(e) => e.stopPropagation()}
  >
    <button
      type="button"
      onClick={() => onTrigger(task.name)}
      disabled={triggering}
      aria-label={triggering ? '执行中…' : '执行'}
      className={`inline-flex items-center rounded-lg border border-claude-border bg-claude-bg font-semibold transition-colors hover:bg-claude-hover disabled:opacity-50 ${
        compact ? 'gap-1 px-2 py-1 text-[11px]' : 'gap-1.5 px-3 py-1.5 text-xs'
      }`}
    >
      {triggering ? (
        <Loader2 size={compact ? 11 : 12} className="animate-spin" aria-hidden="true" />
      ) : (
        <Zap size={compact ? 11 : 12} strokeWidth={2.3} aria-hidden="true" className="text-amber-600" />
      )}
      <span className="text-claude-text">{triggering ? '执行中…' : '执行'}</span>
    </button>

    <ToggleTaskSwitch task={task} toggling={toggling} onToggle={onToggleEnabled} />

    <button
      type="button"
      aria-label="更多操作"
      title="更多操作"
      onClick={() => onMenuOpenChange(!openMenu)}
      className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-claude-border bg-claude-bg text-claude-secondary hover:bg-claude-hover"
    >
      <MoreHorizontal size={14} strokeWidth={2.2} aria-hidden="true" />
    </button>

    {openMenu && (
      <div className="absolute right-0 top-10 z-50 min-w-[110px] rounded-xl border border-claude-border bg-claude-bg shadow-[0_10px_24px_rgba(0,0,0,0.14)] p-1.5">
        <ActionButton
          label="编辑"
          icon={<PenSquare size={12} strokeWidth={2.2} aria-hidden="true" />}
          compact
          onClick={() => {
            onMenuOpenChange(false);
            onEdit(task);
          }}
        />
        <ActionButton
          label="删除"
          icon={<Trash2 size={12} strokeWidth={2.2} aria-hidden="true" />}
          compact
          tone="danger"
          onClick={() => {
            onMenuOpenChange(false);
            onDelete(task.name);
          }}
        />
      </div>
    )}
  </div>
);

export default ScheduleList;
