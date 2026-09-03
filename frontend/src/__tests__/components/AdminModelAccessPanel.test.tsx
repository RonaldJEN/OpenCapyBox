import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import AdminModelAccessPanel from '../../components/AdminModelAccessPanel';
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
} from '../../services/adminApi';

vi.mock('../../services/adminApi', () => ({
  createAdminModel: vi.fn(),
  createAdminModelPermissionGroup: vi.fn(),
  deleteAdminModel: vi.fn(),
  getAdminModelPermissionGroups: vi.fn(),
  getAdminModels: vi.fn(),
  getAdminUsers: vi.fn(),
  updateAdminModel: vi.fn(),
  updateAdminModelPermissionGroupModels: vi.fn(),
  updateAdminModelPermissionGroupUsers: vi.fn(),
  updateAdminModelSettings: vi.fn(),
}));

const modelsResponse = {
  models: [
    {
      id: 'delete-model',
      name: '可删除模型',
      provider: 'openai',
      openai_protocol: 'chat_completions' as const,
      api_base: 'https://api.example.com/v1',
      model_name: 'delete-model',
      max_tokens: 1024,
      context_window: 4096,
      reasoning_format: 'none',
      reasoning_split: false,
      enable_thinking: false,
      thinking_mode: 'provider_default' as const,
      thinking_wire_format: 'enable_thinking' as const,
      reasoning_effort: null,
      supported_reasoning_efforts: [],
      supports_thinking: false,
      supports_image: false,
      max_images: 0,
      supports_video: false,
      max_videos: 0,
      enabled: true,
      tags: [],
      api_key_set: true,
      group_names: ['默认权限包'],
      session_count: 0,
      created_at: null,
      updated_at: null,
    },
  ],
  settings: {
    default_model_id: null,
    cron_default_model_id: null,
    subagent_default_model_id: null,
  },
};

const groupsResponse = {
  groups: [
    {
      id: 'default-group',
      name: '默认权限包',
      description: null,
      is_default: true,
      model_ids: ['delete-model'],
      model_count: 1,
      bound_users: 0,
      created_by: null,
      created_at: null,
      updated_at: null,
    },
  ],
};

const usersResponse = {
  summary: {
    users_total: 0,
    admins_total: 0,
    active_total: 0,
    running_total: 0,
  },
  users: [],
};

describe('AdminModelAccessPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getAdminModels).mockResolvedValue(modelsResponse);
    vi.mocked(getAdminModelPermissionGroups).mockResolvedValue(groupsResponse);
    vi.mocked(getAdminUsers).mockResolvedValue(usersResponse);
    vi.mocked(createAdminModel).mockResolvedValue(modelsResponse.models[0]);
    vi.mocked(createAdminModelPermissionGroup).mockResolvedValue(groupsResponse.groups[0]);
    vi.mocked(updateAdminModel).mockResolvedValue(modelsResponse.models[0]);
    vi.mocked(updateAdminModelSettings).mockResolvedValue(modelsResponse.settings);
    vi.mocked(updateAdminModelPermissionGroupModels).mockResolvedValue(groupsResponse.groups[0]);
    vi.mocked(updateAdminModelPermissionGroupUsers).mockResolvedValue(groupsResponse.groups[0]);
    vi.mocked(deleteAdminModel).mockResolvedValue({
      model_id: 'delete-model',
      deleted: true,
      replacement_model_id: null,
      sessions_reassigned: 0,
      defaults_reassigned: [],
    });
  });

  it('deletes a catalog model from the model directory', async () => {
    render(<AdminModelAccessPanel apiErrorDetail={() => '请求失败'} />);

    const deleteButton = await screen.findByRole('button', { name: /删除/ });
    fireEvent.click(deleteButton);
    fireEvent.click(await screen.findByRole('button', { name: /确认删除/ }));

    await waitFor(() => {
      expect(deleteAdminModel).toHaveBeenCalledWith('delete-model', undefined);
    });
  });

  it('存量空白名单模型可以只改无关字段并保存', async () => {
    vi.mocked(getAdminModels).mockResolvedValue({
      ...modelsResponse,
      models: [{
        ...modelsResponse.models[0],
        id: 'legacy-model',
        name: '旧思考模型',
        enable_thinking: true,
        thinking_mode: 'enabled' as const,
        default_reasoning_level: 'on',
        supported_reasoning_efforts: [],
        supports_thinking: true,
      }],
    });

    render(<AdminModelAccessPanel apiErrorDetail={() => '请求失败'} />);

    fireEvent.click(await screen.findByRole('button', { name: '编辑' }));
    fireEvent.change(await screen.findByLabelText('显示名称'), {
      target: { value: '旧思考模型 v2' },
    });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));

    await waitFor(() => {
      expect(updateAdminModel).toHaveBeenCalled();
    });
    expect(screen.queryByText('默认推理等级必须包含在支持的推理等级中')).toBeNull();
    expect(vi.mocked(updateAdminModel).mock.calls[0][1]).toMatchObject({
      display_name: '旧思考模型 v2',
      thinking_mode: 'enabled',
      reasoning_effort: null,
      supported_reasoning_efforts: [],
    });
  });

  it('未修改默认等级时保留目录的 provider_default 与具体强度二元组', async () => {
    vi.mocked(getAdminModels).mockResolvedValue({
      ...modelsResponse,
      models: [{
        ...modelsResponse.models[0],
        id: 'provider-default-high',
        name: '供应商默认 High',
        thinking_mode: 'provider_default' as const,
        reasoning_effort: 'high',
        default_reasoning_level: 'high',
        supported_reasoning_efforts: ['high', 'max'],
        supports_thinking: true,
      }],
    });

    render(<AdminModelAccessPanel apiErrorDetail={() => '请求失败'} />);

    fireEvent.click(await screen.findByRole('button', { name: '编辑' }));
    fireEvent.change(await screen.findByLabelText('显示名称'), {
      target: { value: '供应商默认 High v2' },
    });
    fireEvent.click(screen.getByRole('button', { name: /保存修改/ }));

    await waitFor(() => expect(updateAdminModel).toHaveBeenCalled());
    expect(vi.mocked(updateAdminModel).mock.calls[0][1]).toMatchObject({
      display_name: '供应商默认 High v2',
      thinking_mode: 'provider_default',
      reasoning_effort: 'high',
      enable_thinking: false,
    });
  });

  it('默认推理等级映射为管理端 thinking_mode / reasoning_effort payload', async () => {
    const cases = [
      { level: 'off', expected: { thinking_mode: 'disabled', reasoning_effort: null, enable_thinking: false } },
      { level: 'on', expected: { thinking_mode: 'enabled', reasoning_effort: null, enable_thinking: true } },
      { level: 'high', expected: { thinking_mode: 'enabled', reasoning_effort: 'high', enable_thinking: true } },
      { level: '', expected: { thinking_mode: 'provider_default', reasoning_effort: null, enable_thinking: false } },
    ];

    for (const { level, expected } of cases) {
      vi.mocked(createAdminModel).mockClear();
      const { unmount } = render(<AdminModelAccessPanel apiErrorDetail={() => '请求失败'} />);

      fireEvent.click(await screen.findByRole('button', { name: '新增模型' }));
      fireEvent.change(await screen.findByLabelText('Model ID'), { target: { value: 'new-model' } });
      fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: 'New Model' } });
      fireEvent.change(screen.getByLabelText('API Base'), { target: { value: 'https://api.example.com/v1' } });
      fireEvent.change(screen.getByLabelText('API Key'), { target: { value: 'k' } });
      fireEvent.change(screen.getByLabelText('Model Name'), { target: { value: 'new-model' } });
      fireEvent.change(screen.getByLabelText('支持的推理等级（按显示顺序）'), {
        target: { value: 'off, on, high' },
      });
      fireEvent.change(screen.getByLabelText('默认推理等级'), { target: { value: level } });
      fireEvent.click(screen.getByRole('button', { name: /创建模型/ }));

      await waitFor(() => {
        expect(createAdminModel).toHaveBeenCalled();
      });
      expect(vi.mocked(createAdminModel).mock.calls[0][0]).toMatchObject(expected);
      unmount();
    }
  });
});
