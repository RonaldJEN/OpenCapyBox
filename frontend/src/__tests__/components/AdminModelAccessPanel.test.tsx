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
      api_base: 'https://api.example.com/v1',
      model_name: 'delete-model',
      max_tokens: 1024,
      context_window: 4096,
      reasoning_format: 'none',
      reasoning_split: false,
      enable_thinking: false,
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
});
