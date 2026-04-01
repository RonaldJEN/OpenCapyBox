import React, { useState, useEffect } from 'react';
import {
  getHeartbeat,
  getCronRuns,
  triggerCronJob,
  type CronTask,
  type CronJobRun,
} from '../services/configApi';

interface Props {
  onClose?: () => void;
}

const STATUS_COLORS: Record<string, string> = {
  success: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  running: 'bg-yellow-100 text-yellow-800',
};

const CronHistory: React.FC<Props> = ({ onClose }) => {
  const [tasks, setTasks] = useState<CronTask[]>([]);
  const [, setHeartbeatContent] = useState('');
  const [runs, setRuns] = useState<CronJobRun[]>([]);
  const [selectedJob, setSelectedJob] = useState<string | undefined>();
  const [loading, setLoading] = useState(false);
  const [triggering, setTriggering] = useState<string | null>(null);
  const [tab, setTab] = useState<'tasks' | 'history'>('tasks');

  // 加载数据
  useEffect(() => {
    setLoading(true);
    Promise.all([getHeartbeat(), getCronRuns()])
      .then(([hb, cronRuns]) => {
        setTasks(hb.tasks);
        setHeartbeatContent(hb.content);
        setRuns(cronRuns);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  // 按任务筛选历史
  useEffect(() => {
    getCronRuns(selectedJob).then(setRuns).catch(console.error);
  }, [selectedJob]);

  const handleTrigger = async (name: string) => {
    setTriggering(name);
    try {
      await triggerCronJob(name);
      // 通知 ChatV2 立即刷新
      window.dispatchEvent(new CustomEvent('cron-job-done'));
      // 刷新历史
      const newRuns = await getCronRuns(selectedJob);
      setRuns(newRuns);
    } catch (err) {
      console.error('触发失败:', err);
    } finally {
      setTriggering(null);
    }
  };

  return (
    <div className="flex flex-col h-full bg-claude-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
        <h2 className="text-lg font-semibold text-claude-text">
          定时任务
        </h2>
        {onClose && (
          <button
            onClick={onClose}
            className="p-1 rounded hover:bg-claude-hover text-claude-muted"
          >
            ✕
          </button>
        )}
      </div>

      {/* Tab bar */}
      <div className="flex border-b border-claude-border">
        <button
          onClick={() => setTab('tasks')}
          className={`px-4 py-2 text-sm font-medium border-b-2 ${
            tab === 'tasks'
              ? 'border-claude-accent text-claude-text'
              : 'border-transparent text-claude-secondary hover:text-claude-text'
          }`}
        >
          任务列表
        </button>
        <button
          onClick={() => setTab('history')}
          className={`px-4 py-2 text-sm font-medium border-b-2 ${
            tab === 'history'
              ? 'border-claude-accent text-claude-text'
              : 'border-transparent text-claude-secondary hover:text-claude-text'
          }`}
        >
          执行历史
        </button>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center h-32 text-claude-muted">
            加载中...
          </div>
        ) : tab === 'tasks' ? (
          <div className="p-4 space-y-3">
            {tasks.length === 0 ? (
              <div className="text-center text-claude-muted py-8">
                <p className="mb-2">暂无定时任务</p>
                <p className="text-xs">
                  让 Agent 使用 manage_cron 工具创建定时任务
                </p>
              </div>
            ) : (
              tasks.map((task) => (
                <div
                  key={task.name}
                  className="flex items-center justify-between p-3 rounded-lg border border-claude-border"
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium text-sm text-claude-text">
                        {task.name}
                      </span>
                      <span
                        className={`px-1.5 py-0.5 text-xs rounded ${
                          task.enabled
                            ? 'bg-green-100 text-green-700'
                            : 'bg-claude-surface text-claude-muted'
                        }`}
                      >
                        {task.enabled ? '启用' : '暂停'}
                      </span>
                    </div>
                    <div className="text-xs text-claude-secondary mt-1">
                      <code className="bg-claude-surface px-1 rounded">
                        {task.cron_expr}
                      </code>
                      {task.description && (
                        <span className="ml-2">{task.description}</span>
                      )}
                    </div>
                  </div>
                  <button
                    onClick={() => handleTrigger(task.name)}
                    disabled={triggering === task.name}
                    className="ml-3 px-3 py-1 text-xs font-medium rounded bg-claude-surface text-claude-accent hover:bg-claude-hover disabled:opacity-50"
                  >
                    {triggering === task.name ? '执行中...' : '手动执行'}
                  </button>
                </div>
              ))
            )}
          </div>
        ) : (
          <div className="p-4 space-y-2">
            {/* Job filter */}
            <div className="mb-3">
              <select
                value={selectedJob || ''}
                onChange={(e) =>
                  setSelectedJob(e.target.value || undefined)
                }
                className="text-sm border border-claude-border rounded px-2 py-1 bg-claude-bg text-claude-text"
              >
                <option value="">全部任务</option>
                {tasks.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name}
                  </option>
                ))}
              </select>
            </div>

            {runs.length === 0 ? (
              <div className="text-center text-claude-muted py-8">
                暂无执行记录
              </div>
            ) : (
              runs.map((run) => (
                <div
                  key={run.id}
                  className="p-3 rounded-lg border border-claude-border"
                >
                  <div className="flex items-center justify-between mb-1">
                    <span className="font-medium text-sm text-claude-text">
                      {run.job_name}
                    </span>
                    <span
                      className={`px-1.5 py-0.5 text-xs rounded ${
                        STATUS_COLORS[run.status] || 'bg-claude-surface text-claude-muted'
                      }`}
                    >
                      {run.status}
                    </span>
                  </div>
                  <div className="text-xs text-claude-secondary">
                    {run.started_at && (
                      <span>
                        开始: {new Date(run.started_at).toLocaleString()}
                      </span>
                    )}
                    {run.completed_at && (
                      <span className="ml-3">
                        结束: {new Date(run.completed_at).toLocaleString()}
                      </span>
                    )}
                  </div>
                  {run.output && (
                    <pre className="mt-2 text-xs text-claude-secondary bg-claude-surface p-2 rounded overflow-x-auto max-h-32">
                      {run.output}
                    </pre>
                  )}
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default CronHistory;
