/**
 * WeekAgenda — Cron 周视图（方案 B · 时间轴日历）
 *
 * - 左侧 00:00–23:00 时间轴，每小时 40px。
 * - 7 列，每列内任务 absolute 定位到对应小时（top = ((hh-HOUR_START) + mm/60) * HOUR_H）。
 * - 任务统一柔和米橘色块；disabled = 灰；不再用 6 色区分。
 * - 选中（点击）或 hover 时浮出操作工具条（仅 ▶ 运行，视觉弱化）。
 * - 点击其他区域自动收起选中。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Loader2, Play } from 'lucide-react';
import type { CronTask } from '../../services/configApi';

const DAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

const HOUR_START = 0;
const HOUR_END = 23;
const HOUR_H = 40; // px per hour
const EVENT_H = HOUR_H - 4; // 事件块高度 ≈ 一个小时槽，留 4px gutter
const AXIS_W = 56; // px left axis width
const HEADER_H = 50; // px day header height

function isSameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

interface Props {
  weekDays: Date[];
  today: Date;
  /** 每一天对应的 (task, time) 列表，由父组件按 cron 表达式预过滤。 */
  dayTasks: Array<Array<{ task: CronTask; time: string | null }>>;
  /** 兼容旧签名，方案 B 不再使用颜色主题。 */
  taskThemeMap: Map<string, number>;
  cronToReadable: (expr: string) => string;
  onTrigger: (name: string) => void;
  triggeringSet: Set<string>;
}

const WeekAgenda: React.FC<Props> = ({
  weekDays,
  today,
  dayTasks,
  cronToReadable,
  onTrigger,
  triggeringSet,
}) => {
  /** 选中事件键：`${dayIdx}:${taskName}`，全周仅一个选中。 */
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [hoverKey, setHoverKey] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedKey) return;
    const onMouseDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest('[data-week-expand-toggle="true"]')) return;
      if (target.closest('[data-week-expand-panel="true"]')) return;
      setSelectedKey(null);
    };
    document.addEventListener('mousedown', onMouseDown);
    return () => document.removeEventListener('mousedown', onMouseDown);
  }, [selectedKey]);

  const hours = useMemo(() => {
    const arr: number[] = [];
    for (let h = HOUR_START; h <= HOUR_END; h++) arr.push(h);
    return arr;
  }, []);

  const bodyHeight = (HOUR_END - HOUR_START + 1) * HOUR_H;

  return (
    <div className="flex-1 overflow-auto">
      <div
        className="grid"
        style={{ gridTemplateColumns: `${AXIS_W}px repeat(7, minmax(0, 1fr))` }}
      >
        {/* 左轴 header 占位 */}
        <div
          className="border-r border-b border-claude-border bg-claude-bg sticky left-0 z-10"
          style={{ height: HEADER_H }}
        />
        {/* 7 列 header */}
        {weekDays.map((day, idx) => {
          const isToday = isSameDay(day, today);
          return (
            <div
              key={`h-${idx}`}
              className={`relative border-r border-b border-claude-border last:border-r-0 px-3 flex items-baseline gap-2 ${
                isToday ? 'bg-claude-accent/5' : ''
              }`}
              style={{ height: HEADER_H }}
            >
              <span
                className={`text-base font-semibold leading-none tracking-tight ${
                  isToday ? 'text-claude-accent' : 'text-claude-text'
                }`}
              >
                {day.getDate()}
              </span>
              <span className="text-[10px] text-claude-muted">{DAY_LABELS[idx]}</span>
            </div>
          );
        })}

        {/* 左轴：小时刻度 */}
        <div
          className="border-r border-claude-border bg-claude-bg sticky left-0 z-10"
          style={{ height: bodyHeight, position: 'relative' }}
        >
          {hours.map((h, i) => (
            <div
              key={`hr-${h}`}
              className="text-[10px] text-claude-muted text-right pr-2"
              style={{
                position: 'absolute',
                top: i * HOUR_H - 6,
                right: 0,
                left: 0,
              }}
            >
              {String(h).padStart(2, '0')}:00
            </div>
          ))}
        </div>

        {/* 7 列 body */}
        {weekDays.map((day, idx) => {
          const isToday = isSameDay(day, today);
          const items = dayTasks[idx] ?? [];
          return (
            <div
              key={`b-${idx}`}
              className={`border-r border-claude-border last:border-r-0 ${
                isToday ? 'bg-claude-accent/5' : ''
              }`}
              style={{ height: bodyHeight, position: 'relative' }}
            >
              {/* 小时网格线 */}
              {hours.map((_, i) => (
                <div
                  key={`g-${idx}-${i}`}
                  className="bg-claude-border"
                  style={{ position: 'absolute', left: 0, right: 0, top: i * HOUR_H, height: 1 }}
                />
              ))}

              {/* 事件块 */}
              {items.map(({ task, time }) => {
                if (!time) return null;
                const [hh, mm] = time.split(':').map(Number);
                if (Number.isNaN(hh) || Number.isNaN(mm)) return null;
                if (hh < HOUR_START || hh > HOUR_END) return null;
                const top = ((hh - HOUR_START) + mm / 60) * HOUR_H;
                const key = `${idx}:${task.name}`;
                const selected = selectedKey === key;
                const dimmed = !task.enabled;
                const isTriggering = triggeringSet.has(task.name);

                // 执行中时工具条常驻，避免鼠标移开后动画消失。
                const showTools = isTriggering || selected || hoverKey === key;
                const eventTone = dimmed
                  ? { backgroundColor: '#F3F1EB', borderLeftColor: '#AEA89B' }
                  : isTriggering
                    ? { backgroundColor: '#D4A574', borderLeftColor: '#B58658' }
                  : showTools
                    ? { backgroundColor: '#D9B688', borderLeftColor: '#D4A574' }
                    : { backgroundColor: '#E6CEAF', borderLeftColor: '#D4A574' };
                return (
                  <div
                    key={task.name}
                    className="absolute"
                    style={{ top, left: 4, right: 4, height: EVENT_H, zIndex: selected || showTools ? 20 : 1 }}
                    onMouseEnter={() => setHoverKey(key)}
                    onMouseLeave={() => setHoverKey((prev) => (prev === key ? null : prev))}
                  >
                    <button
                      type="button"
                      data-week-expand-toggle="true"
                      onClick={() => setSelectedKey(selected ? null : key)}
                      title={`${time} · ${task.description || task.name}\n${cronToReadable(task.cron_expr)}`}
                      className={`relative w-full h-full px-2 py-1 rounded text-left flex flex-col justify-start gap-0.5 border-l-[3px] text-claude-text transition-colors ${
                        dimmed
                          ? 'text-claude-muted'
                          : ''
                      } ${selected ? 'shadow-sm ring-1 ring-claude-accent/70' : ''} ${isTriggering ? 'ring-1 ring-claude-accent/80' : ''}`}
                      style={eventTone}
                    >
                      <div className="flex items-center gap-1.5 leading-none">
                        <span className="text-[10px] tabular-nums text-claude-secondary shrink-0">
                          {time}
                        </span>
                      </div>
                      <span className="min-w-0 text-[11px] truncate leading-tight">
                        {task.description || task.name}
                      </span>
                    </button>

                    {/* 浮出工具条：选中或 hover 显示 */}
                    {showTools && (
                    <div
                      data-week-expand-panel="true"
                      className="absolute top-1 right-1 flex items-center gap-0.5 rounded-md border border-claude-border bg-claude-bg/92 shadow-sm p-0.5"
                    >
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onTrigger(task.name);
                        }}
                        disabled={isTriggering}
                        aria-busy={isTriggering}
                        aria-label="运行任务"
                        title={isTriggering ? '执行中…' : '运行任务'}
                        className={`w-6 h-6 inline-flex items-center justify-center rounded transition-colors ${
                          isTriggering
                            ? 'bg-claude-bg text-claude-secondary ring-1 ring-claude-accent/60 shadow-sm'
                            : 'bg-claude-hover/70 text-claude-secondary'
                        } disabled:cursor-default`}
                      >
                        {isTriggering ? (
                          <Loader2 size={12} strokeWidth={2.4} className="animate-spin" aria-hidden="true" />
                        ) : (
                          <Play size={11} strokeWidth={2.1} aria-hidden="true" />
                        )}
                      </button>
                    </div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default WeekAgenda;
