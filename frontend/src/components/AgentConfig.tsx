import React, { useState, useEffect, useCallback } from 'react';
import {
  listAgentFiles,
  getAgentFile,
  updateAgentFile,
  type AgentFileInfo,
} from '../services/configApi';

const FILE_LABELS: Record<string, { label: string; icon: string; desc: string }> = {
  user: { label: 'User', icon: '👤', desc: '用户画像/偏好 (USER.md)' },
  soul: { label: 'Soul', icon: '🧠', desc: 'Agent 人格/风格 (SOUL.md)' },
  agents: { label: 'Agents', icon: '📋', desc: '行为规则/任务指南 (AGENTS.md)' },
  memory: { label: 'Memory', icon: '💾', desc: '长期记忆/共识 (MEMORY.md)' },
  heartbeat: { label: 'Heartbeat', icon: '⏰', desc: '定时任务定义 (HEARTBEAT.md)' },
};

const TABS = ['user', 'soul', 'agents', 'memory', 'heartbeat'] as const;

interface Props {
  onClose?: () => void;
}

const AgentConfig: React.FC<Props> = ({ onClose }) => {
  const [_files, setFiles] = useState<AgentFileInfo[]>([]);
  const [activeTab, setActiveTab] = useState<string>('user');
  const [content, setContent] = useState('');
  const [originalContent, setOriginalContent] = useState('');
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');

  // 加载文件列表
  useEffect(() => {
    listAgentFiles().then(setFiles).catch(console.error);
  }, []);

  // 加载选中文件内容
  const loadFile = useCallback(async (name: string) => {
    setLoading(true);
    setMessage('');
    try {
      const detail = await getAgentFile(name);
      setContent(detail.content);
      setOriginalContent(detail.content);
    } catch (err) {
      setContent('');
      setOriginalContent('');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadFile(activeTab);
  }, [activeTab, loadFile]);

  const handleSave = async () => {
    setSaving(true);
    setMessage('');
    try {
      const result = await updateAgentFile(activeTab, content);
      setOriginalContent(content);
      setMessage(`保存成功 (v${result.version})`);
      // 刷新文件列表
      listAgentFiles().then(setFiles).catch(console.error);
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err.message;
      setMessage(`保存失败: ${detail}`);
    } finally {
      setSaving(false);
    }
  };

  const isDirty = content !== originalContent;

  return (
    <div className="flex flex-col h-full bg-claude-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
        <h2 className="text-lg font-semibold text-claude-text">
          Agent 配置
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

      {/* Tabs */}
      <div className="flex border-b border-claude-border overflow-x-auto scrollbar-hide">
        {TABS.map((name) => {
          const meta = FILE_LABELS[name];
          const isActive = activeTab === name;
          return (
            <button
              key={name}
              onClick={() => setActiveTab(name)}
              className={`flex-1 min-w-0 px-1.5 py-2 text-xs font-medium truncate border-b-2 transition-colors ${
                isActive
                  ? 'border-claude-accent text-claude-text'
                  : 'border-transparent text-claude-secondary hover:text-claude-text'
              }`}
            >
              {meta?.label}
            </button>
          );
        })}
      </div>

      {/* Description */}
      <div className="px-4 py-2 text-xs text-claude-secondary bg-claude-surface">
        {FILE_LABELS[activeTab]?.desc}
      </div>

      {/* Editor */}
      <div className="flex-1 overflow-hidden relative">
        {loading ? (
          <div className="flex items-center justify-center h-full text-claude-muted">
            加载中...
          </div>
        ) : (
          <textarea
            value={content}
            onChange={(e) => setContent(e.target.value)}
            className="w-full h-full resize-none p-4 font-mono text-sm
              bg-claude-bg text-claude-text
              focus:outline-none"
            placeholder={`在此编辑 ${FILE_LABELS[activeTab]?.label}...`}
            spellCheck={false}
          />
        )}
      </div>

      {/* Footer */}
      <div className="flex items-center justify-between px-4 py-3 border-t border-claude-border bg-claude-surface">
        <span className="text-xs text-claude-secondary">
          {message || (isDirty ? '有未保存的更改' : '')}
        </span>
        <button
          onClick={handleSave}
          disabled={saving || !isDirty}
          className={`px-4 py-1.5 text-sm font-medium rounded-md transition-colors ${
            isDirty
              ? 'bg-claude-accent text-white hover:opacity-90'
              : 'bg-claude-surface text-claude-muted border border-claude-border cursor-not-allowed'
          }`}
        >
          {saving ? '保存中...' : '保存'}
        </button>
      </div>
    </div>
  );
};

export default AgentConfig;
