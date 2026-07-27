/**
 * SchedulePicker — 结构化时间选择器
 *
 * 不暴露 cron 表达式，用户只选择频率类型 + 时间。
 * 父组件通过 onChange(schedule) 拿到后端可识别的结构。
 */
import React from 'react';
import type { Schedule } from '../../services/configApi';

interface Props {
  value: Schedule;
  onChange: (next: Schedule) => void;
}

const KINDS: { value: Schedule['kind']; label: string }[] = [
  { value: 'daily', label: '每天' },
  { value: 'weekdays', label: '工作日' },
  { value: 'weekly', label: '每周' },
  { value: 'monthly', label: '每月' },
  { value: 'interval', label: '间隔' },
];

const WEEKDAYS = [
  { label: '一', value: 1 },
  { label: '二', value: 2 },
  { label: '三', value: 3 },
  { label: '四', value: 4 },
  { label: '五', value: 5 },
  { label: '六', value: 6 },
  { label: '日', value: 0 },
] as const;
const WEEKDAY_ORDER: Map<number, number> = new Map(
  WEEKDAYS.map((day, index) => [day.value, index]),
);

/** 返回选择对应 kind 时的默认值 */
export function defaultScheduleForKind(kind: Schedule['kind']): Schedule {
  switch (kind) {
    case 'daily':
      return { kind: 'daily', time: '09:00' };
    case 'weekdays':
      return { kind: 'weekdays', time: '09:00' };
    case 'weekly':
      return { kind: 'weekly', time: '09:00', days: [1] };
    case 'monthly':
      return { kind: 'monthly', time: '09:00', dayOfMonth: 1 };
    case 'interval':
      return { kind: 'interval', everyHours: 1 };
  }
}

const TimeInput: React.FC<{ value: string; onChange: (v: string) => void }> = ({ value, onChange }) => (
  <input
    type="time"
    value={value}
    onChange={(e) => onChange(e.target.value)}
    className="px-2 py-1 border border-claude-border rounded text-sm bg-claude-bg text-claude-text"
  />
);

const SchedulePicker: React.FC<Props> = ({ value, onChange }) => {
  return (
    <div className="space-y-3">
      {/* Kind switcher */}
      <div className="flex flex-wrap gap-1">
        {KINDS.map((k) => (
          <button
            key={k.value}
            type="button"
            onClick={() => onChange(defaultScheduleForKind(k.value))}
            className={`px-2.5 py-1 text-xs rounded border ${
              value.kind === k.value
                ? 'bg-claude-accent text-white border-claude-accent'
                : 'bg-claude-surface text-claude-text border-claude-border hover:bg-claude-hover'
            }`}
          >
            {k.label}
          </button>
        ))}
      </div>

      {/* Per-kind controls */}
      {value.kind === 'daily' && (
        <div className="flex items-center gap-2">
          <span className="text-sm text-claude-secondary">每天</span>
          <TimeInput value={value.time} onChange={(t) => onChange({ kind: 'daily', time: t })} />
        </div>
      )}

      {value.kind === 'weekdays' && (
        <div className="flex items-center gap-2">
          <span className="text-sm text-claude-secondary">周一至周五</span>
          <TimeInput value={value.time} onChange={(t) => onChange({ kind: 'weekdays', time: t })} />
        </div>
      )}

      {value.kind === 'weekly' && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-sm text-claude-secondary">每周</span>
            <TimeInput value={value.time} onChange={(t) => onChange({ ...value, time: t })} />
          </div>
          <div className="flex flex-wrap gap-1">
            {WEEKDAYS.map(({ label, value: dayValue }) => {
              const selected = value.days.includes(dayValue);
              return (
                <button
                  key={dayValue}
                  type="button"
                  onClick={() => {
                    const next = selected
                      ? value.days.filter((d) => d !== dayValue)
                      : [...value.days, dayValue].sort(
                          (a, b) => (WEEKDAY_ORDER.get(a) ?? 0) - (WEEKDAY_ORDER.get(b) ?? 0),
                        );
                    onChange({ ...value, days: next });
                  }}
                  className={`w-8 h-8 text-xs rounded-full border ${
                    selected
                      ? 'bg-claude-accent text-white border-claude-accent'
                      : 'bg-claude-surface text-claude-text border-claude-border hover:bg-claude-hover'
                  }`}
                >
                  {label}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {value.kind === 'monthly' && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm text-claude-secondary">每月</span>
          <input
            type="number"
            min={1}
            max={31}
            value={value.dayOfMonth}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (n >= 1 && n <= 31) onChange({ ...value, dayOfMonth: n });
            }}
            className="w-16 px-2 py-1 border border-claude-border rounded text-sm bg-claude-bg text-claude-text"
          />
          <span className="text-sm text-claude-secondary">日</span>
          <TimeInput value={value.time} onChange={(t) => onChange({ ...value, time: t })} />
        </div>
      )}

      {value.kind === 'interval' && (
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-sm text-claude-secondary">每</span>
            <input
              type="number"
              min={1}
              max={value.everyHours !== undefined ? 23 : 59}
              value={value.everyHours ?? value.everyMinutes ?? 1}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (n < 1) return;
                if (value.everyHours !== undefined) {
                  if (n <= 23) onChange({ kind: 'interval', everyHours: n });
                } else {
                  if (n <= 59) onChange({ kind: 'interval', everyMinutes: n });
                }
              }}
              className="w-16 px-2 py-1 border border-claude-border rounded text-sm bg-claude-bg text-claude-text"
            />
            <select
              value={value.everyHours !== undefined ? 'h' : 'm'}
              onChange={(e) => {
                if (e.target.value === 'h') onChange({ kind: 'interval', everyHours: 1 });
                else onChange({ kind: 'interval', everyMinutes: 30 });
              }}
              className="px-2 py-1 border border-claude-border rounded text-sm bg-claude-bg text-claude-text"
            >
              <option value="m">分钟</option>
              <option value="h">小时</option>
            </select>
          </div>
        </div>
      )}
    </div>
  );
};

export default SchedulePicker;
