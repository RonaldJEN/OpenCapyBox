import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Edit3,
  Package,
  Plus,
  Save,
  Search,
  Settings2,
  Trash2,
  X,
} from 'lucide-react';

import {
  createAdminModel,
  createAdminModelPermissionGroup,
  deleteAdminModel,
  getAdminModelPermissionGroups,
  getAdminModels,
  getAdminUsers,
  updateAdminModel,
  updateAdminModelPermissionGroupModels,
  updateAdminModelPermissionGroupUsers,
  updateAdminModelSettings,
  type AdminModelItem,
  type AdminModelPermissionGroup,
  type AdminModelsResponse,
  type AdminUsersResponse,
} from '../services/adminApi';
import FeedbackMessage from './FeedbackMessage';

interface AdminModelAccessPanelProps {
  apiErrorDetail: (err: unknown) => string;
  refreshToken?: number;
}

type AccessTab = 'catalog' | 'permission';
type PackageSubTab = 'scope' | 'users';

type ModelForm = {
  model_id: string;
  display_name: string;
  provider: string;
  api_base: string;
  api_key: string;
  model_name: string;
  max_tokens: string;
  context_window: string;
  reasoning_format: string;
  reasoning_split: boolean;
  thinking_wire_format: 'none' | 'enable_thinking' | 'thinking_object';
  default_reasoning_level: string;
  initial_default_reasoning_level: string;
  initial_thinking_mode: 'provider_default' | 'enabled' | 'disabled';
  initial_reasoning_effort: string | null;
  supported_reasoning_efforts: string;
  supports_image: boolean;
  max_images: string;
  supports_video: boolean;
  max_videos: string;
  enabled: boolean;
  tags: string;
};

const emptyModelForm: ModelForm = {
  model_id: '',
  display_name: '',
  provider: 'openai',
  api_base: '',
  api_key: '',
  model_name: '',
  max_tokens: '16384',
  context_window: '128000',
  reasoning_format: 'none',
  reasoning_split: false,
  thinking_wire_format: 'enable_thinking',
  default_reasoning_level: 'on',
  initial_default_reasoning_level: 'on',
  initial_thinking_mode: 'provider_default',
  initial_reasoning_effort: null,
  supported_reasoning_efforts: 'off, on',
  supports_image: false,
  max_images: '0',
  supports_video: false,
  max_videos: '0',
  enabled: true,
  tags: '',
};

function modelToForm(model: AdminModelItem): ModelForm {
  const defaultReasoningLevel = model.default_reasoning_level
    || model.reasoning_effort
    || (model.thinking_mode === 'disabled' ? 'off' : model.thinking_mode === 'enabled' ? 'on' : '');
  return {
    model_id: model.id,
    display_name: model.name,
    provider: model.provider,
    api_base: model.api_base,
    api_key: '',
    model_name: model.model_name,
    max_tokens: String(model.max_tokens),
    context_window: String(model.context_window),
    reasoning_format: model.reasoning_format,
    reasoning_split: model.reasoning_split,
    thinking_wire_format: model.provider === 'openai'
      ? (model.thinking_wire_format || 'enable_thinking')
      : 'none',
    default_reasoning_level: defaultReasoningLevel,
    initial_default_reasoning_level: defaultReasoningLevel,
    initial_thinking_mode: model.thinking_mode,
    initial_reasoning_effort: model.reasoning_effort,
    supported_reasoning_efforts: model.supported_reasoning_efforts.join(', '),
    supports_image: model.supports_image,
    max_images: String(model.max_images),
    supports_video: model.supports_video,
    max_videos: String(model.max_videos),
    enabled: model.enabled,
    tags: model.tags.join(', '),
  };
}

function numberOrDefault(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function tagsFromInput(value: string): string[] {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function groupLabel(group?: AdminModelPermissionGroup | null): string {
  if (!group) return '-';
  return group.is_default ? `${group.name}（全体用户）` : group.name;
}

function modelSearchText(model: AdminModelItem): string {
  return `${model.id} ${model.name} ${model.provider} ${model.model_name}`.toLowerCase();
}

function modelFeatureText(model: AdminModelItem): string {
  const features = [
    model.supports_thinking ? 'thinking' : '',
    model.supports_image ? 'image' : '',
    model.supports_video ? 'video' : '',
    ...model.tags,
  ].filter(Boolean);
  return features.length ? features.join(' / ') : 'base';
}

function modelDefaultUsages(
  model: AdminModelItem,
  settings?: AdminModelsResponse['settings'] | null,
): string[] {
  if (!settings) return [];
  return [
    settings.default_model_id === model.id ? '普通对话默认模型' : '',
    settings.cron_default_model_id === model.id ? 'Cron 默认模型' : '',
    settings.subagent_default_model_id === model.id ? 'Subagent 默认模型' : '',
  ].filter(Boolean);
}

function userPermissionText(user: AdminUsersResponse['users'][number]): string {
  if (user.is_admin) return '管理员全部可见';
  return [user.model_permission_default_group_name || '默认', ...(user.model_permission_group_names || [])].join(' + ');
}

export default function AdminModelAccessPanel({ apiErrorDetail, refreshToken = 0 }: AdminModelAccessPanelProps) {
  const [modelsData, setModelsData] = useState<AdminModelsResponse | null>(null);
  const [groups, setGroups] = useState<AdminModelPermissionGroup[]>([]);
  const [usersData, setUsersData] = useState<AdminUsersResponse | null>(null);
  const [activeTab, setActiveTab] = useState<AccessTab>('catalog');
  const [packageSubTab, setPackageSubTab] = useState<PackageSubTab>('scope');
  const [selectedGroupId, setSelectedGroupId] = useState('');
  const [catalogSearch, setCatalogSearch] = useState('');
  const [modelSearch, setModelSearch] = useState('');
  const [userSearch, setUserSearch] = useState('');
  const [modelDraftIds, setModelDraftIds] = useState<Set<string>>(() => new Set());
  const [userDraftIds, setUserDraftIds] = useState<Set<string>>(() => new Set());
  const [showNewGroupForm, setShowNewGroupForm] = useState(false);
  const [newGroupName, setNewGroupName] = useState('');
  const [newGroupDescription, setNewGroupDescription] = useState('');
  const [editingModelId, setEditingModelId] = useState<string | null>(null);
  const [modelDrawerOpen, setModelDrawerOpen] = useState(false);
  const [modelForm, setModelForm] = useState<ModelForm>(emptyModelForm);
  const [deleteTargetModel, setDeleteTargetModel] = useState<AdminModelItem | null>(null);
  const [replacementModelId, setReplacementModelId] = useState('');
  const [loading, setLoading] = useState(true);
  const [savingKey, setSavingKey] = useState('');
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [message, setMessage] = useState('');

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [nextModels, nextGroups, nextUsers] = await Promise.all([
        getAdminModels(),
        getAdminModelPermissionGroups(),
        getAdminUsers(),
      ]);
      setModelsData(nextModels);
      setGroups(nextGroups.groups);
      setUsersData(nextUsers);
      setSelectedGroupId((prev) => (
        prev && nextGroups.groups.some((group) => group.id === prev)
          ? prev
          : nextGroups.groups[0]?.id || ''
      ));
    } catch (err) {
      console.error('Failed to load model access data:', err);
      setError(apiErrorDetail(err) || '模型权限数据加载失败');
    } finally {
      setLoading(false);
    }
  }, [apiErrorDetail]);

  useEffect(() => {
    void loadAll();
  }, [loadAll, refreshToken]);

  const models = useMemo(() => modelsData?.models || [], [modelsData]);
  const users = useMemo(() => usersData?.users || [], [usersData]);
  const enabledModels = useMemo(() => models.filter((model) => model.enabled), [models]);
  const enabledModelIds = useMemo(() => new Set(enabledModels.map((model) => model.id)), [enabledModels]);
  const defaultGroup = groups.find((group) => group.is_default);
  const businessGroups = groups.filter((group) => !group.is_default);
  const selectedGroup = useMemo(
    () => groups.find((group) => group.id === selectedGroupId) || null,
    [groups, selectedGroupId],
  );
  const deleteTargetDefaultUsages = useMemo(
    () => deleteTargetModel ? modelDefaultUsages(deleteTargetModel, modelsData?.settings) : [],
    [deleteTargetModel, modelsData?.settings],
  );
  const deleteReplacementCandidates = useMemo(
    () => enabledModels.filter((model) => model.id !== deleteTargetModel?.id),
    [deleteTargetModel, enabledModels],
  );
  const deleteTargetNeedsReplacement = Boolean(
    deleteTargetModel
    && ((deleteTargetModel.session_count || 0) > 0 || deleteTargetDefaultUsages.length > 0),
  );

  useEffect(() => {
    const selectedEnabledIds = (selectedGroup?.model_ids || []).filter((id) => enabledModelIds.has(id));
    setModelDraftIds(new Set(selectedEnabledIds));
    if (!selectedGroup || selectedGroup.is_default) {
      setUserDraftIds(new Set());
      return;
    }
    const bound = users
      .filter((user) => (user.model_permission_group_ids || []).includes(selectedGroup.id))
      .map((user) => user.user_id);
    setUserDraftIds(new Set(bound));
  }, [enabledModelIds, selectedGroup, users]);

  const filteredCatalogModels = useMemo(() => {
    const query = catalogSearch.trim().toLowerCase();
    if (!query) return models;
    return models.filter((model) => modelSearchText(model).includes(query));
  }, [catalogSearch, models]);

  const filteredScopeModels = useMemo(() => {
    const query = modelSearch.trim().toLowerCase();
    if (!query) return enabledModels;
    return enabledModels.filter((model) => modelSearchText(model).includes(query));
  }, [enabledModels, modelSearch]);

  const filteredUsers = useMemo(() => {
    const query = userSearch.trim().toLowerCase();
    return users.filter((user) => (
      !query
      || user.user_id.toLowerCase().includes(query)
      || user.username.toLowerCase().includes(query)
    ));
  }, [userSearch, users]);

  const selectedInactiveModelNames = useMemo(() => (
    (selectedGroup?.model_ids || [])
      .filter((id) => !enabledModelIds.has(id))
      .map((id) => models.find((model) => model.id === id)?.name || id)
  ), [enabledModelIds, models, selectedGroup]);

  const defaultEnabledCount = defaultGroup
    ? defaultGroup.model_ids.filter((id) => enabledModelIds.has(id)).length
    : 0;
  const businessBindingCount = businessGroups.reduce((sum, group) => sum + group.bound_users, 0);

  const runSave = async (key: string, action: () => Promise<void>, success: string) => {
    setSavingKey(key);
    setError('');
    setActionError('');
    setMessage('');
    try {
      await action();
      await loadAll();
      setMessage(success);
    } catch (err) {
      console.error('Model access action failed:', err);
      setActionError(apiErrorDetail(err) || '模型权限操作失败');
    } finally {
      setSavingKey('');
    }
  };

  const handleCreateGroup = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = newGroupName.trim();
    if (!name) return;
    let createdGroupId = '';
    await runSave('create-group', async () => {
      const created = await createAdminModelPermissionGroup({
        name,
        description: newGroupDescription.trim() || null,
      });
      createdGroupId = created.id;
      setNewGroupName('');
      setNewGroupDescription('');
      setShowNewGroupForm(false);
    }, '权限包已创建');
    if (createdGroupId) {
      setSelectedGroupId(createdGroupId);
      setPackageSubTab('scope');
    }
  };

  const handleSaveGroupModels = async () => {
    if (!selectedGroup) return;
    await runSave(`models-${selectedGroup.id}`, async () => {
      const enabledDraftIds = Array.from(modelDraftIds).filter((id) => enabledModelIds.has(id));
      await updateAdminModelPermissionGroupModels(selectedGroup.id, enabledDraftIds);
    }, '权限包模型范围已保存');
  };

  const handleSaveGroupUsers = async () => {
    if (!selectedGroup || selectedGroup.is_default) return;
    await runSave(`users-${selectedGroup.id}`, async () => {
      await updateAdminModelPermissionGroupUsers(selectedGroup.id, Array.from(userDraftIds));
    }, '权限包绑定用户已保存');
  };

  const startCreateModel = () => {
    setEditingModelId(null);
    setModelForm(emptyModelForm);
    setModelDrawerOpen(true);
  };

  const startEditModel = (model: AdminModelItem) => {
    setEditingModelId(model.id);
    setModelForm(modelToForm(model));
    setModelDrawerOpen(true);
  };

  const closeModelDrawer = () => {
    setModelDrawerOpen(false);
    setEditingModelId(null);
    setModelForm(emptyModelForm);
  };

  const openDeleteModel = (model: AdminModelItem) => {
    const firstReplacement = enabledModels.find((candidate) => candidate.id !== model.id);
    setDeleteTargetModel(model);
    setReplacementModelId(firstReplacement?.id || '');
  };

  const closeDeleteModel = () => {
    setDeleteTargetModel(null);
    setReplacementModelId('');
  };

  const handleDeleteModel = async () => {
    if (!deleteTargetModel) return;
    if (deleteTargetNeedsReplacement && !replacementModelId) {
      setActionError('该模型有关联默认配置或历史会话，需要先选择替换模型。');
      return;
    }

    const modelId = deleteTargetModel.id;
    await runSave(`delete-model-${modelId}`, async () => {
      await deleteAdminModel(modelId, deleteTargetNeedsReplacement ? replacementModelId : undefined);
      if (editingModelId === modelId) {
        closeModelDrawer();
      }
      closeDeleteModel();
    }, '模型已删除');
  };

  const handleSaveModel = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const defaultReasoningLevel = modelForm.default_reasoning_level.trim();
    const supportedReasoningLevels = tagsFromInput(modelForm.supported_reasoning_efforts);
    // Legacy catalogs carry a default level with no whitelist; only an explicit
    // whitelist makes the default level a membership constraint.
    if (
      defaultReasoningLevel
      && supportedReasoningLevels.length > 0
      && !supportedReasoningLevels.includes(defaultReasoningLevel)
    ) {
      setError('默认推理等级必须包含在支持的推理等级中');
      return;
    }
    const preserveStoredDefault = editingModelId !== null
      && defaultReasoningLevel === modelForm.initial_default_reasoning_level;
    const thinkingMode: 'provider_default' | 'enabled' | 'disabled' = preserveStoredDefault
      ? modelForm.initial_thinking_mode
      : defaultReasoningLevel === 'off'
        ? 'disabled'
        : defaultReasoningLevel
          ? 'enabled'
          : 'provider_default';
    const reasoningEffort = preserveStoredDefault
      ? modelForm.initial_reasoning_effort
      : !defaultReasoningLevel || defaultReasoningLevel === 'off' || defaultReasoningLevel === 'on'
        ? null
        : defaultReasoningLevel;
    const payload = {
      model_id: modelForm.model_id.trim(),
      display_name: modelForm.display_name.trim(),
      provider: modelForm.provider,
      api_base: modelForm.api_base.trim(),
      api_key: modelForm.api_key.trim(),
      model_name: modelForm.model_name.trim(),
      max_tokens: numberOrDefault(modelForm.max_tokens, 16384),
      context_window: numberOrDefault(modelForm.context_window, 128000),
      reasoning_format: modelForm.reasoning_format,
      reasoning_split: modelForm.reasoning_split,
      thinking_wire_format: modelForm.thinking_wire_format,
      enable_thinking: thinkingMode === 'enabled',
      thinking_mode: thinkingMode,
      reasoning_effort: reasoningEffort,
      supported_reasoning_efforts: supportedReasoningLevels,
      supports_image: modelForm.supports_image,
      max_images: numberOrDefault(modelForm.max_images, 0),
      supports_video: modelForm.supports_video,
      max_videos: numberOrDefault(modelForm.max_videos, 0),
      enabled: modelForm.enabled,
      tags: tagsFromInput(modelForm.tags),
    };
    await runSave('model-form', async () => {
      if (editingModelId) {
        const updatePayload: Partial<typeof payload> = { ...payload };
        delete updatePayload.model_id;
        if (!updatePayload.api_key) {
          delete updatePayload.api_key;
        }
        await updateAdminModel(editingModelId, updatePayload);
      } else {
        await createAdminModel(payload);
      }
      closeModelDrawer();
    }, editingModelId ? '模型已更新' : '模型已创建');
  };

  const handleSaveSettings = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = event.currentTarget;
    const formData = new FormData(form);
    const defaultModel = String(formData.get('default_model_id') || '');
    if (!defaultModel) return;
    await runSave('model-settings', async () => {
      await updateAdminModelSettings({
        default_model_id: defaultModel,
        cron_default_model_id: String(formData.get('cron_default_model_id') || '') || null,
        subagent_default_model_id: String(formData.get('subagent_default_model_id') || '') || null,
      });
    }, '默认模型设置已保存');
  };

  if (loading) {
    return <div className="admin-card admin-empty-card">正在加载模型权限...</div>;
  }

  return (
    <div className="admin-model-access">
      {error ? (
        <FeedbackMessage
          className="admin-error admin-inline-message"
          tone="error"
          icon={<AlertTriangle size={14} />}
          onDismiss={() => setError('')}
        >
          {error}
        </FeedbackMessage>
      ) : null}
      {message ? (
        <FeedbackMessage
          className="admin-toast"
          tone="success"
          autoDismissMs={4000}
          icon={<CheckCircle2 size={14} />}
          onDismiss={() => setMessage('')}
        >
          {message}
        </FeedbackMessage>
      ) : null}

      <div className="admin-model-topline">
        <div className="admin-model-tabs" role="tablist" aria-label="模型权限配置">
          <button
            type="button"
            className={`admin-model-tab ${activeTab === 'catalog' ? 'active' : ''}`}
            onClick={() => setActiveTab('catalog')}
          >
            模型目录
          </button>
          <button
            type="button"
            className={`admin-model-tab ${activeTab === 'permission' ? 'active' : ''}`}
            onClick={() => setActiveTab('permission')}
          >
            权限包
          </button>
        </div>
      </div>

      {activeTab === 'catalog' ? (
        <div className="admin-model-tab-panel">
          <section className="admin-model-card admin-model-default-card">
            <div className="admin-model-card-head">
              <div>
                <div className="admin-model-card-title">
                  <Settings2 size={16} />
                  默认模型
                </div>
                <div className="admin-model-card-desc">按场景选择默认调用的启用模型</div>
              </div>
            </div>
            <form
              className="admin-model-default-form"
              onSubmit={handleSaveSettings}
              key={[
                modelsData?.settings.default_model_id,
                modelsData?.settings.cron_default_model_id,
                modelsData?.settings.subagent_default_model_id,
                enabledModels.length,
              ].join(':')}
            >
              <label className="admin-model-field">
                普通对话
                <select name="default_model_id" defaultValue={modelsData?.settings.default_model_id || ''}>
                  <option value="">选择启用模型</option>
                  {enabledModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
                </select>
              </label>
              <label className="admin-model-field">
                Cron
                <select name="cron_default_model_id" defaultValue={modelsData?.settings.cron_default_model_id || ''}>
                  <option value="">继承普通默认</option>
                  {enabledModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
                </select>
              </label>
              <label className="admin-model-field">
                Subagent
                <select name="subagent_default_model_id" defaultValue={modelsData?.settings.subagent_default_model_id || ''}>
                  <option value="">继承普通默认</option>
                  {enabledModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
                </select>
              </label>
              <button
                className="admin-button admin-primary-button admin-model-save-button"
                type="submit"
                disabled={savingKey === 'model-settings' || !enabledModels.length}
              >
                <Save size={14} />
                保存默认模型
              </button>
            </form>
          </section>

          <section className="admin-model-card">
            <div className="admin-model-card-head">
              <div>
                <div className="admin-model-card-title">
                  <Database size={16} />
                  模型目录
                </div>
                <div className="admin-model-card-desc">共 {models.length} 个模型，{enabledModels.length} 个启用</div>
              </div>
              <button className="admin-button admin-primary-button" type="button" onClick={startCreateModel}>
                <Plus size={14} />
                新增模型
              </button>
            </div>
            <label className="admin-model-search">
              <Search size={15} />
              <input value={catalogSearch} onChange={(event) => setCatalogSearch(event.target.value)} placeholder="搜索模型" />
            </label>
            <div className="admin-model-table-wrap">
              <table className="admin-model-table">
                <thead>
                  <tr>
                    <th>模型</th>
                    <th>状态</th>
                    <th>API Key</th>
                    <th>使用范围</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredCatalogModels.map((model) => {
                    return (
                      <tr key={model.id}>
                        <td>
                          <strong>{model.name}</strong>
                          <span><b>ID</b>{model.id}<i />{model.model_name}</span>
                          {(model.session_count || 0) > 0 ? <span><b>会话</b>{model.session_count}</span> : null}
                        </td>
                        <td>
                          <span className={`admin-model-pill ${model.enabled ? 'on' : 'off'}`}>
                            {model.enabled ? '启用' : '停用'}
                          </span>
                        </td>
                        <td>{model.api_key_set ? '已设置' : '未设置'}</td>
                        <td className={model.group_names.length ? 'admin-model-scope-text active' : 'admin-model-scope-text'}>
                          {model.group_names.join(' / ') || '未加入权限包'}
                        </td>
                        <td>
                          <div className="admin-model-actions">
                            <button className="admin-button admin-icon-button" type="button" onClick={() => startEditModel(model)}>
                              <Edit3 size={13} />
                              编辑
                            </button>
                            <button
                              className="admin-button admin-icon-button admin-danger-button"
                              type="button"
                              onClick={() => openDeleteModel(model)}
                              disabled={savingKey === `delete-model-${model.id}`}
                              title="删除模型"
                            >
                              <Trash2 size={13} />
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {!filteredCatalogModels.length ? <div className="admin-model-empty">没有匹配的模型</div> : null}
            </div>
          </section>
        </div>
      ) : (
        <div className="admin-model-tab-panel">
          <div className="admin-model-stat-row">
            <div className="admin-model-stat-cell">
              <span>模型目录</span>
              <strong>{models.length}</strong>
              <small>{enabledModels.length} 个启用</small>
            </div>
            <div className="admin-model-stat-cell">
              <span>权限包</span>
              <strong>{groups.length}</strong>
              <small>默认 + {businessGroups.length} 个业务包</small>
            </div>
            <div className="admin-model-stat-cell active">
              <span>默认范围</span>
              <strong>{defaultEnabledCount}</strong>
              <small>全体用户自动可见</small>
            </div>
            <div className="admin-model-stat-cell">
              <span>业务授权</span>
              <strong>{businessBindingCount}</strong>
              <small>用户绑定记录</small>
            </div>
          </div>

          <div className="admin-model-permission-layout">
            <aside className="admin-model-package-rail">
              <div className="admin-model-rail-head">
                <div>
                  <h3>权限包</h3>
                  <p>默认包自动应用给所有用户</p>
                </div>
                <button
                  className="admin-button admin-icon-only-button"
                  type="button"
                  onClick={() => setShowNewGroupForm((prev) => !prev)}
                  aria-label="新建权限包"
                >
                  <Plus size={15} />
                </button>
              </div>
              <div className="admin-model-package-list">
                {groups.map((group) => {
                  const enabledCount = group.model_ids.filter((id) => enabledModelIds.has(id)).length;
                  return (
                    <button
                      type="button"
                      key={group.id}
                      className={`admin-model-package-item ${selectedGroupId === group.id ? 'selected' : ''}`}
                      onClick={() => setSelectedGroupId(group.id)}
                    >
                      <span>{groupLabel(group)}</span>
                      <small>{enabledCount} 模型 · {group.is_default ? '全体用户' : `${group.bound_users} 用户绑定`}</small>
                      {group.description ? <em>{group.description}</em> : null}
                    </button>
                  );
                })}
              </div>
              {showNewGroupForm ? (
                <form className="admin-model-new-package-form" onSubmit={handleCreateGroup}>
                  <input
                    value={newGroupName}
                    onChange={(event) => setNewGroupName(event.target.value)}
                    placeholder="新权限包名称"
                  />
                  <input
                    value={newGroupDescription}
                    onChange={(event) => setNewGroupDescription(event.target.value)}
                    placeholder="说明，可选"
                  />
                  <div>
                    <button className="admin-button admin-primary-button" type="submit" disabled={savingKey === 'create-group'}>
                      <Plus size={14} />
                      创建
                    </button>
                    <button className="admin-button admin-icon-button" type="button" onClick={() => setShowNewGroupForm(false)}>
                      取消
                    </button>
                  </div>
                </form>
              ) : null}
            </aside>

            <section className="admin-model-card admin-model-package-detail">
              <div className="admin-model-card-head">
                <div>
                  <div className="admin-model-card-title">
                    <Package size={16} />
                    {groupLabel(selectedGroup)}
                  </div>
                  <div className="admin-model-card-desc">
                    {selectedGroup?.is_default ? '默认范围覆盖所有普通用户' : `${selectedGroup?.bound_users || 0} 个用户已绑定`}
                  </div>
                </div>
                <div className="admin-model-subtabs">
                  <button
                    type="button"
                    className={packageSubTab === 'scope' ? 'active' : ''}
                    onClick={() => setPackageSubTab('scope')}
                  >
                    模型范围
                  </button>
                  <button
                    type="button"
                    className={packageSubTab === 'users' ? 'active' : ''}
                    onClick={() => setPackageSubTab('users')}
                  >
                    绑定用户
                  </button>
                </div>
              </div>

              {packageSubTab === 'scope' ? (
                <div className="admin-model-detail-panel">
                  <div className="admin-model-panel-toolbar">
                    <label className="admin-model-search">
                      <Search size={15} />
                      <input value={modelSearch} onChange={(event) => setModelSearch(event.target.value)} placeholder="搜索启用模型" />
                    </label>
                    <button
                      className="admin-button admin-primary-button"
                      type="button"
                      disabled={!selectedGroup || savingKey === `models-${selectedGroup.id}`}
                      onClick={() => { void handleSaveGroupModels(); }}
                    >
                      <Save size={14} />
                      保存模型范围
                    </button>
                  </div>
                  {selectedInactiveModelNames.length ? (
                    <div className="admin-model-warning">
                      历史配置中包含停用或不可用模型：{selectedInactiveModelNames.join('、')}。保存后会从该权限包移除。
                    </div>
                  ) : null}
                  <div className="admin-model-check-list">
                    {filteredScopeModels.map((model) => (
                      <label key={model.id} className="admin-model-check-row">
                        <input
                          type="checkbox"
                          checked={modelDraftIds.has(model.id)}
                          onChange={(event) => {
                            setModelDraftIds((prev) => {
                              const next = new Set(prev);
                              if (event.target.checked) next.add(model.id);
                              else next.delete(model.id);
                              return next;
                            });
                          }}
                        />
                        <span>
                          <strong>{model.name}</strong>
                          <small><b>ID</b>{model.id}<i />{model.model_name}</small>
                        </span>
                        <em>{modelFeatureText(model)}</em>
                      </label>
                    ))}
                  </div>
                  {!filteredScopeModels.length ? <div className="admin-model-empty">没有可加入权限包的启用模型</div> : null}
                </div>
              ) : (
                <div className="admin-model-detail-panel">
                  <div className="admin-model-panel-toolbar">
                    <label className="admin-model-search">
                      <Search size={15} />
                      <input value={userSearch} onChange={(event) => setUserSearch(event.target.value)} placeholder="搜索用户" />
                    </label>
                    <button
                      className="admin-button admin-primary-button"
                      type="button"
                      disabled={!selectedGroup || selectedGroup.is_default || savingKey === `users-${selectedGroup.id}`}
                      onClick={() => { void handleSaveGroupUsers(); }}
                    >
                      <Save size={14} />
                      保存绑定
                    </button>
                  </div>
                  <div className="admin-model-user-list">
                    {filteredUsers.map((user) => (
                      <label key={user.user_id} className="admin-model-user-row">
                        <input
                          type="checkbox"
                          disabled={!selectedGroup || selectedGroup.is_default || user.is_admin}
                          checked={selectedGroup?.is_default ? true : userDraftIds.has(user.user_id)}
                          onChange={(event) => {
                            setUserDraftIds((prev) => {
                              const next = new Set(prev);
                              if (event.target.checked) next.add(user.user_id);
                              else next.delete(user.user_id);
                              return next;
                            });
                          }}
                        />
                        <span>
                          <strong>{user.username}</strong>
                          <small>@{user.user_id} · {userPermissionText(user)}</small>
                        </span>
                        {user.is_admin ? <em>管理员</em> : null}
                      </label>
                    ))}
                  </div>
                  {!filteredUsers.length ? <div className="admin-model-empty">没有匹配的用户</div> : null}
                </div>
              )}
            </section>
          </div>
        </div>
      )}

      {modelDrawerOpen ? (
        <>
          <div className="admin-model-drawer-overlay" onClick={closeModelDrawer} />
          <form className="admin-model-drawer" onSubmit={handleSaveModel}>
            <div className="admin-model-drawer-head">
              <div>
                <h3>{editingModelId ? '编辑模型' : '新增模型'}</h3>
                <p>{editingModelId || '注册外部模型'}</p>
              </div>
              <button className="admin-button admin-icon-only-button" type="button" onClick={closeModelDrawer} aria-label="关闭">
                <X size={15} />
              </button>
            </div>
            <label className="admin-model-field">Model ID<input value={modelForm.model_id} disabled={!!editingModelId} onChange={(event) => setModelForm((prev) => ({ ...prev, model_id: event.target.value }))} /></label>
            <label className="admin-model-field">显示名称<input value={modelForm.display_name} onChange={(event) => setModelForm((prev) => ({ ...prev, display_name: event.target.value }))} /></label>
            <label className="admin-model-field">Provider<select value={modelForm.provider} onChange={(event) => setModelForm((prev) => ({ ...prev, provider: event.target.value, thinking_wire_format: event.target.value === 'openai' ? (prev.thinking_wire_format === 'none' ? 'enable_thinking' : prev.thinking_wire_format) : 'none', default_reasoning_level: event.target.value === 'openai' ? (prev.default_reasoning_level || 'on') : '', supported_reasoning_efforts: event.target.value === 'openai' ? (prev.supported_reasoning_efforts || 'off, on') : '' }))}><option value="openai">openai</option><option value="anthropic">anthropic</option></select></label>
            <label className="admin-model-field">API Base<input value={modelForm.api_base} onChange={(event) => setModelForm((prev) => ({ ...prev, api_base: event.target.value }))} /></label>
            <label className="admin-model-field">API Key<input value={modelForm.api_key} placeholder={editingModelId ? '留空则保持不变' : ''} onChange={(event) => setModelForm((prev) => ({ ...prev, api_key: event.target.value }))} /></label>
            <label className="admin-model-field">Model Name<input value={modelForm.model_name} onChange={(event) => setModelForm((prev) => ({ ...prev, model_name: event.target.value }))} /></label>
            <label className="admin-model-field">Reasoning<select value={modelForm.reasoning_format} onChange={(event) => setModelForm((prev) => ({ ...prev, reasoning_format: event.target.value }))}><option value="none">none</option><option value="reasoning_content">reasoning_content</option><option value="reasoning_details">reasoning_details</option><option value="anthropic_thinking">anthropic_thinking</option></select></label>
            <label className="admin-model-field">思考请求协议<select value={modelForm.thinking_wire_format} disabled={modelForm.provider !== 'openai'} onChange={(event) => setModelForm((prev) => ({ ...prev, thinking_wire_format: event.target.value as ModelForm['thinking_wire_format'] }))}><option value="none">不发送思考开关</option><option value="enable_thinking">enable_thinking 布尔值</option><option value="thinking_object">thinking.type 对象</option></select></label>
            <label className="admin-model-field">默认推理等级<input value={modelForm.default_reasoning_level} disabled={modelForm.provider !== 'openai'} placeholder="off / on / high / max" onChange={(event) => setModelForm((prev) => ({ ...prev, default_reasoning_level: event.target.value }))} /></label>
            <label className="admin-model-field">支持的推理等级（按显示顺序）<input value={modelForm.supported_reasoning_efforts} disabled={modelForm.provider !== 'openai'} placeholder="off, high, max" onChange={(event) => setModelForm((prev) => ({ ...prev, supported_reasoning_efforts: event.target.value }))} /></label>
            <div className="admin-model-form-pair">
              <label className="admin-model-field">Max tokens<input value={modelForm.max_tokens} onChange={(event) => setModelForm((prev) => ({ ...prev, max_tokens: event.target.value }))} /></label>
              <label className="admin-model-field">Context<input value={modelForm.context_window} onChange={(event) => setModelForm((prev) => ({ ...prev, context_window: event.target.value }))} /></label>
            </div>
            <div className="admin-model-form-pair">
              <label className="admin-model-field">Max images<input value={modelForm.max_images} onChange={(event) => setModelForm((prev) => ({ ...prev, max_images: event.target.value }))} /></label>
              <label className="admin-model-field">Max videos<input value={modelForm.max_videos} onChange={(event) => setModelForm((prev) => ({ ...prev, max_videos: event.target.value }))} /></label>
            </div>
            <label className="admin-model-field">Tags<input value={modelForm.tags} placeholder="thinking, coding" onChange={(event) => setModelForm((prev) => ({ ...prev, tags: event.target.value }))} /></label>
            <div className="admin-model-flags">
              <label><input type="checkbox" checked={modelForm.enabled} onChange={(event) => setModelForm((prev) => ({ ...prev, enabled: event.target.checked }))} /> <CheckCircle2 size={13} />启用</label>
              <label><input type="checkbox" checked={modelForm.reasoning_split} onChange={(event) => setModelForm((prev) => ({ ...prev, reasoning_split: event.target.checked }))} /> reasoning_split</label>
              <label><input type="checkbox" checked={modelForm.supports_image} onChange={(event) => setModelForm((prev) => ({ ...prev, supports_image: event.target.checked }))} /> 图片</label>
              <label><input type="checkbox" checked={modelForm.supports_video} onChange={(event) => setModelForm((prev) => ({ ...prev, supports_video: event.target.checked }))} /> 视频</label>
            </div>
            <button className="admin-button admin-primary-button admin-model-drawer-submit" type="submit" disabled={savingKey === 'model-form'}>
              <Save size={14} />
              {editingModelId ? '保存修改' : '创建模型'}
            </button>
          </form>
        </>
      ) : null}

      {deleteTargetModel ? (
        <div className="admin-model-error-backdrop" role="presentation" onClick={closeDeleteModel}>
          <section
            className="admin-model-error-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-model-delete-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="admin-model-error-head">
              <div className="admin-model-error-icon">
                <Trash2 size={18} />
              </div>
              <div>
                <h3 id="admin-model-delete-title">删除模型</h3>
                <p>{deleteTargetModel.name}</p>
              </div>
              <button className="admin-button admin-icon-only-button" type="button" onClick={closeDeleteModel} aria-label="关闭删除弹窗">
                <X size={15} />
              </button>
            </div>
            <div className="admin-model-error-body">
              {(deleteTargetModel.session_count || 0) > 0 ? (
                <p>将 {deleteTargetModel.session_count} 个历史会话迁移到替换模型。</p>
              ) : (
                <p>该模型没有历史会话引用。</p>
              )}
              {deleteTargetDefaultUsages.length ? <p>同步替换：{deleteTargetDefaultUsages.join('、')}。</p> : null}
              {deleteTargetNeedsReplacement ? (
                <label className="admin-model-field">
                  替换模型
                  <select value={replacementModelId} onChange={(event) => setReplacementModelId(event.target.value)}>
                    <option value="">选择启用模型</option>
                    {deleteReplacementCandidates.map((model) => (
                      <option key={model.id} value={model.id}>{model.name}</option>
                    ))}
                  </select>
                </label>
              ) : null}
            </div>
            <div className="admin-model-error-actions">
              <button className="admin-button" type="button" onClick={closeDeleteModel}>
                取消
              </button>
              <button
                className="admin-button admin-danger-button"
                type="button"
                onClick={() => void handleDeleteModel()}
                disabled={savingKey === `delete-model-${deleteTargetModel.id}` || (deleteTargetNeedsReplacement && !replacementModelId)}
              >
                <Trash2 size={14} />
                确认删除
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {actionError ? (
        <div className="admin-model-error-backdrop" role="presentation" onClick={() => setActionError('')}>
          <section
            className="admin-model-error-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="admin-model-error-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="admin-model-error-head">
              <div className="admin-model-error-icon">
                <AlertTriangle size={18} />
              </div>
              <div>
                <h3 id="admin-model-error-title">操作失败</h3>
                <p>请检查提示后再重试。</p>
              </div>
              <button className="admin-button admin-icon-only-button" type="button" onClick={() => setActionError('')} aria-label="关闭错误弹窗">
                <X size={15} />
              </button>
            </div>
            <div className="admin-model-error-body">{actionError}</div>
            <div className="admin-model-error-actions">
              <button className="admin-button admin-primary-button" type="button" onClick={() => setActionError('')}>
                知道了
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </div>
  );
}
