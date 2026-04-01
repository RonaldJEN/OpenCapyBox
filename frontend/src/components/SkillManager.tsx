import React, { useState, useEffect } from 'react';
import { getSkills, toggleSkill, type SkillInfo } from '../services/configApi';

interface Props {
  onClose?: () => void;
}

const CATEGORY_COLORS: Record<string, string> = {
  document: 'bg-blue-100 text-blue-700',
  financial: 'bg-green-100 text-green-700',
  general: 'bg-claude-surface text-claude-secondary',
};

const SkillManager: React.FC<Props> = ({ onClose }) => {
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<string>('all');
  const [toggling, setToggling] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    getSkills()
      .then(setSkills)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const categories = [
    'all',
    ...new Set(skills.map((s) => s.category).filter(Boolean)),
  ];

  const filteredSkills =
    filter === 'all'
      ? skills
      : skills.filter((s) => s.category === filter);

  const handleToggle = async (skillName: string, currentEnabled: boolean) => {
    setToggling(skillName);
    try {
      await toggleSkill(skillName, !currentEnabled);
      setSkills((prev) =>
        prev.map((s) =>
          s.name === skillName ? { ...s, enabled: !currentEnabled } : s,
        ),
      );
    } catch (err) {
      console.error('切换失败:', err);
    } finally {
      setToggling(null);
    }
  };

  return (
    <div className="flex flex-col h-full bg-claude-bg">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-claude-border">
        <h2 className="text-lg font-semibold text-claude-text">
          Skills 管理
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

      {/* Category filter */}
      <div className="flex gap-2 px-4 py-2 border-b border-claude-border overflow-x-auto">
        {categories.map((cat) => (
          <button
            key={cat}
            onClick={() => setFilter(cat)}
            className={`px-3 py-1 text-xs font-medium rounded-full whitespace-nowrap transition-colors ${
              filter === cat
                ? 'bg-claude-accent text-white'
                : 'bg-claude-surface text-claude-secondary hover:bg-claude-hover'
            }`}
          >
            {cat === 'all' ? '全部' : cat}
          </button>
        ))}
      </div>

      {/* Skill grid */}
      <div className="flex-1 overflow-y-auto p-4">
        {loading ? (
          <div className="flex items-center justify-center h-32 text-claude-muted">
            加载中...
          </div>
        ) : filteredSkills.length === 0 ? (
          <div className="text-center text-claude-muted py-8">
            没有可用的 Skills
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3">
            {filteredSkills.map((skill) => (
              <div
                key={skill.name}
                className={`relative p-4 rounded-lg border transition-colors ${
                  skill.enabled
                    ? 'border-claude-border'
                    : 'border-claude-border opacity-60'
                }`}
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="font-medium text-sm text-claude-text truncate">
                        {skill.name}
                      </span>
                      {skill.category && (
                        <span
                          className={`px-1.5 py-0.5 text-xs rounded ${
                            CATEGORY_COLORS[skill.category] ||
                            CATEGORY_COLORS.general
                          }`}
                        >
                          {skill.category}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-claude-secondary line-clamp-2">
                      {skill.description || '无描述'}
                    </p>
                  </div>

                  {/* Toggle switch */}
                  <button
                    onClick={() => handleToggle(skill.name, skill.enabled)}
                    disabled={toggling === skill.name}
                    className={`ml-3 relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out ${
                      skill.enabled
                        ? 'bg-claude-accent'
                        : 'bg-claude-border'
                    } ${toggling === skill.name ? 'opacity-50' : ''}`}
                  >
                    <span
                      className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                        skill.enabled ? 'translate-x-4' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-2 border-t border-claude-border bg-claude-surface">
        <span className="text-xs text-claude-secondary">
          {skills.filter((s) => s.enabled).length}/{skills.length} 个 Skill 已启用
        </span>
      </div>
    </div>
  );
};

export default SkillManager;
