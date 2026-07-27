/**
 * TaskFormDrawer — Cron 任务新建/编辑表单（次级抽屉）
 *
 * 关键约束（来自 plan §0.3）：
 * - 用户不输入 cron 表达式，只用 SchedulePicker 选时间。
 * - 编辑回显仅依赖 schedule（不反解析 cron_expr）；老数据 schedule=null 时
 *   降级显示原始 cron_expr 只读。
 */
import React, { useCallback, useEffect, useMemo, useState } from 'react';

import {
  createCronJob,
  previewSchedule,
  updateCronJob,
  type CronTask,
  type Schedule,
  type SchedulePreviewResult,
} from '../../services/configApi';
import SchedulePicker, { defaultScheduleForKind } from './SchedulePicker';

interface Props {
  /** 编辑模式：传入要编辑的任务；新建模式：null。 */
  task: CronTask | null;
  onClose: () => void;
  onSaved: (saved: CronTask) => void;
}

const NAME_RE = /^[A-Za-z0-9_-]{1,100}$/;
const DESCRIPTION_MAX_LEN = 500;

function resolveInitialContent(task: CronTask | null): string {
  if (!task) return '';
  if (task.content && task.content.trim()) return task.content;
  return task.description ?? '';
}

function translateFieldLoc(loc: string): string {
  if (loc === 'body.content') return '任务内容';
  if (loc === 'body.description') return '任务描述';
  if (loc === 'body.name') return '任务名';
  if (loc === 'body.schedule') return '执行时间';
  return loc;
}

function translateValidationMsg(msg: string, fieldLabel?: string): string {
  const maxMatch = msg.match(/^String should have at most (\d+) characters$/);
  if (maxMatch) {
    return fieldLabel ? `${fieldLabel}最多 ${maxMatch[1]} 个字符` : `最多 ${maxMatch[1]} 个字符`;
  }

  const minMatch = msg.match(/^String should have at least (\d+) characters$/);
  if (minMatch) {
    return fieldLabel ? `${fieldLabel}至少 ${minMatch[1]} 个字符` : `至少 ${minMatch[1]} 个字符`;
  }

  if (msg === 'Field required') {
    return fieldLabel ? `${fieldLabel}不能为空` : '字段不能为空';
  }

  return fieldLabel ? `${fieldLabel}: ${msg}` : msg;
}

function normalizeSaveError(e: unknown): string {
  const anyErr = e as {
    response?: { data?: { detail?: unknown } };
    message?: string;
  };

  const detail = anyErr.response?.data?.detail;
  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    const msgs = detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const rec = item as { loc?: unknown; msg?: unknown };
          const rawLoc = Array.isArray(rec.loc)
            ? rec.loc.map((p) => String(p)).join('.')
            : '';
          const loc = rawLoc ? translateFieldLoc(rawLoc) : '';
          const msg = typeof rec.msg === 'string' ? rec.msg : '';
          if (msg) return translateValidationMsg(msg, loc || undefined);
          return JSON.stringify(item);
        }
        return String(item);
      })
      .filter((s) => s && s.trim());

    if (msgs.length > 0) return msgs.join('；');
  }

  return anyErr.message ?? '保存失败';
}

const TaskFormDrawer: React.FC<Props> = ({ task, onClose, onSaved }) => {
  const isEdit = task !== null;

  const [name, setName] = useState(task?.name ?? '');
  const [content, setContent] = useState(resolveInitialContent(task));
  const [enabled, setEnabled] = useState(task?.enabled ?? true);
  // 老数据 schedule=null 时，进入编辑保留 schedule=null 模式（cron_expr 只读不可改时间），
  // 用户可点"重新选择时间"按钮切到 SchedulePicker。
  const [schedule, setSchedule] = useState<Schedule | null>(
    task?.schedule ?? (isEdit ? null : defaultScheduleForKind('daily')),
  );

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<SchedulePreviewResult | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(() => {
    setError(null);
  }, [name, content, schedule, enabled]);

  useEffect(() => {
    const payload = schedule !== null
      ? { schedule, n: 5 }
      : task?.cron_expr
        ? { cron_expr: task.cron_expr, n: 5 }
        : null;
    if (!payload) {
      setPreview(null);
      return undefined;
    }
    let active = true;
    const timer = window.setTimeout(() => {
      previewSchedule(payload)
        .then((result) => {
          if (!active) return;
          setPreview(result);
          setPreviewError(null);
        })
        .catch((e: unknown) => {
          if (!active) return;
          setPreview(null);
          setPreviewError(normalizeSaveError(e));
        });
    }, 250);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [schedule, task?.cron_expr]);

  const canSubmit = useMemo(() => {
    if (!isEdit && !NAME_RE.test(name)) return false;
    if (!content.trim()) return false;
    if (schedule === null && !task?.cron_expr) return false;
    return !submitting;
  }, [isEdit, name, content, schedule, task?.cron_expr, submitting]);

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const normalizedContent = content.trim();
      const normalizedDescription = normalizedContent.slice(0, DESCRIPTION_MAX_LEN);
      let saved: CronTask;
      if (isEdit && task) {
        saved = await updateCronJob(task.name, {
          description: normalizedDescription,
          content: normalizedContent,
          enabled,
          // schedule=null 表示用户没改时间 → 不传 schedule/cron_expr，后端保留原值
          ...(schedule !== null ? { schedule } : {}),
        });
      } else {
        saved = await createCronJob({
          name,
          description: normalizedDescription,
          content: normalizedContent,
          enabled,
          schedule,
        });
      }
      onSaved(saved);
    } catch (e: unknown) {
      const message = normalizeSaveError(e);
      setError(message);
      window.alert(message);
    } finally {
      setSubmitting(false);
    }
  }, [isEdit, task, name, content, enabled, schedule, onSaved]);

  return (
    <div className="fixed inset-0 z-[60] flex" onClick={onClose}>
      <div className="absolute inset-0 bg-black/30" />
      <div
        className="relative ml-auto h-full w-[460px] bg-claude-bg shadow-2xl border-l border-claude-border flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
          <h3 className="text-base font-semibold text-claude-text">
            {isEdit ? '编辑任务' : '新建任务'}
          </h3>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded hover:bg-claude-hover text-claude-muted"
            aria-label="关闭任务表单"
            title="关闭任务表单"
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4 text-sm">
          {/* Name (新建可编辑；编辑只读) */}
          <div>
            <label className="block text-xs text-claude-secondary mb-1">任务名（不可重复，仅字母数字 _ -）</label>
            <input
              type="text"
              value={name}
              disabled={isEdit}
              aria-label="任务名"
              onChange={(e) => setName(e.target.value)}
              placeholder="daily-report"
              className="w-full px-2.5 py-1.5 border border-claude-border rounded bg-claude-bg text-claude-text disabled:opacity-60"
            />
            {!isEdit && name && !NAME_RE.test(name) && (
              <div className="mt-1 text-xs text-red-500">仅允许字母/数字/下划线/连字符，长度 1-100</div>
            )}
          </div>

          {/* Content */}
          <div>
            <label className="block text-xs text-claude-secondary mb-1">任务内容</label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              aria-label="任务内容"
              rows={5}
              placeholder="如：检索今天的科技新闻，输出 Markdown 摘要到工作目录"
              className="w-full px-2.5 py-1.5 border border-claude-border rounded bg-claude-bg text-claude-text resize-none"
            />
          </div>

          {/* Schedule */}
          <div>
            <label className="block text-xs text-claude-secondary mb-1">执行时间</label>
            {schedule === null ? (
              <div className="flex items-center justify-between p-2 border border-claude-border rounded bg-claude-surface">
                <code className="text-xs text-claude-text">{task?.cron_expr ?? ''}</code>
                <button
                  type="button"
                  onClick={() => setSchedule(defaultScheduleForKind('daily'))}
                  className="px-2 py-0.5 text-xs rounded bg-claude-bg border border-claude-border hover:bg-claude-hover"
                >
                  重新选择
                </button>
              </div>
            ) : (
              <SchedulePicker value={schedule} onChange={setSchedule} />
            )}
          </div>

          {(preview || previewError) && (
            <div className="rounded border border-claude-border bg-claude-surface p-3 space-y-2">
              <div className="text-xs font-medium text-claude-text">保存前确认</div>
              {preview ? (
                <>
                  <div className="text-xs text-claude-secondary">
                    执行计划：<span className="text-claude-text">{preview.schedule_text}</span>
                  </div>
                  <div className="text-xs text-claude-secondary">
                    Cron：<code className="text-claude-text">{preview.cron_expr}</code>
                  </div>
                  <div className="text-xs text-claude-secondary">未来五次执行：</div>
                  <ol className="pl-5 text-xs text-claude-text list-decimal space-y-0.5">
                    {preview.next_fires.map((fire) => (
                      <li key={fire}>{new Date(fire).toLocaleString()}</li>
                    ))}
                  </ol>
                </>
              ) : (
                <div className="text-xs text-red-600">{previewError}</div>
              )}
            </div>
          )}

          {/* Enabled */}
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <span className="text-claude-text">启用</span>
          </label>

          {error && (
            <div className="p-2 text-xs rounded bg-red-50 border border-red-200 text-red-700">
              {error}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="border-t border-claude-border px-4 py-3 bg-claude-surface/40">
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="px-3 py-1.5 text-sm rounded border border-claude-border bg-claude-bg text-claude-text hover:bg-claude-hover"
            >
              取消
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              disabled={!canSubmit}
              className="px-3 py-1.5 text-sm rounded bg-claude-accent text-white hover:opacity-90 disabled:opacity-50"
            >
              {submitting ? '保存中…' : '保存'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default TaskFormDrawer;
