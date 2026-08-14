import { apiService } from './api';

const client = apiService.getAxiosClient();

export interface AdminOverview {
  window_days: number;
  summary: {
    users_total: number;
    admins_total: number;
    sessions_total: number;
    rounds_total: number;
    rounds_24h: number;
    rounds_running: number;
    cron_jobs_total: number;
    cron_jobs_enabled: number;
    cron_failed_24h: number;
    llm_calls_24h: number;
    tokens_24h: number;
    avg_completion_latency_24h: number | null;
  };
  trends: Array<{ date: string; rounds: number; tokens: number }>;
}

export interface AdminRoundStepItem {
  llm_record_id: number;
  step_index: number;
  call_kind?: 'agent_step' | 'compaction';
  checkpoint_id?: string | null;
  request_message_count: number;
  request_messages: string;
  request_tools: string;
  finish_reason: string | null;
  response_error: string | null;
  response_preview: string;
  response_content: string;
  response_thinking: string;
  response_tool_calls: string;
  usage_prompt_tokens: number;
  usage_completion_tokens: number;
  usage_total_tokens: number;
  first_token_latency_s: number | null;
  completion_latency_s: number | null;
  compaction_triggered: boolean;
  compaction_pre_tokens: number;
  compaction_post_tokens: number;
  compaction_tokens_saved: number;
  compaction_microcompact_compacted_messages: number;
  compaction_summary_generated_count: number;
  compaction_summary_reused_count: number;
  compaction_summary_quality_repair_count: number;
  compaction_emergency_truncate_dropped_rounds: number;
  manual_review_status: string;
  created_at: string | null;
}

export interface AdminLLMReviewUpdateResponse {
  llm_record_id: number;
  manual_review_status: string;
}

export interface AdminLLMCallRecordDetail extends AdminRoundStepItem {
  round_id: string;
}

export interface AdminRoundTreeItem {
  round_id: string;
  session_id: string;
  user_id: string | null;
  session_title: string | null;
  run_kind: 'main' | 'subagent';
  parent_run_id: string | null;
  root_run_id: string | null;
  subagent_edge_id: string | null;
  subagent_type: string | null;
  subagent_description: string | null;
  subagent_prompt_preview: string | null;
  subagent_child_count: number;
  status: string;
  step_count: number;
  started_at: string;
  completed_at: string | null;
  duration_s: number;
  user_message_preview: string;
  final_response_preview: string;
  total_tokens: number;
  llm_calls: number;
  error_calls: number;
  compaction_steps: number;
  steps: AdminRoundStepItem[];
}

export interface AdminRoundSessionItem {
  session_id: string;
  user_id: string | null;
  session_title: string | null;
  rounds_count: number;
  last_round_at: string | null;
  sum_step_count: number;
  total_tokens: number;
  llm_calls: number;
  error_calls: number;
  compaction_steps: number;
  total_duration_s: number;
  status: string;
  rounds_loaded?: boolean;
  rounds: AdminRoundTreeItem[];
}

export interface AdminRoundTreeResponse {
  total_sessions: number;
  offset: number;
  limit: number;
  sessions: AdminRoundSessionItem[];
}

export interface AdminSessionRoundsResponse {
  session_id: string;
  rounds: AdminRoundTreeItem[];
}

export type AdminSandboxProfileSource = 'explicit' | 'default' | 'missing' | 'disabled';

export interface AdminUserItem {
  user_id: string;
  username: string;
  auth_type: 'simple' | 'ldap';
  enabled: boolean;
  role: 'admin' | 'user';
  is_admin: boolean;
  status: string;
  sessions_count: number;
  rounds_count: number;
  running_rounds: number;
  total_tokens: number;
  weekly_tokens_used: number;
  monthly_tokens_used: number;
  token_limit_per_week: number | null;
  token_limit_per_month: number | null;
  cron_jobs_total: number;
  cron_jobs_enabled: number;
  cron_failed_24h: number;
  last_active_at: string | null;
  last_login_at: string | null;
  last_login_ip: string | null;
  sandbox_profile_id?: string | null;
  sandbox_profile_name?: string | null;
  sandbox_profile_source?: AdminSandboxProfileSource;
  sandbox_profile_error?: string | null;
  sandbox_id?: string | null;
  sandbox_status?: string;
  sandbox_needs_recreate?: boolean;
  model_permission_group_ids?: string[];
  model_permission_group_names?: string[];
  model_permission_default_group_name?: string;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AdminUsersResponse {
  summary: {
    users_total: number;
    admins_total: number;
    active_total: number;
    running_total: number;
  };
  users: AdminUserItem[];
}

export interface AdminSystemResponse {
  window_hours: number;
  summary: {
    running_rounds: number;
    active_sessions_30m: number;
    round_status_counts: Record<string, number>;
    cron_status_counts: Record<string, number>;
    avg_completion_latency_s: number | null;
    p50_completion_latency_s: number | null;
    p95_completion_latency_s: number | null;
    avg_first_token_latency_s: number | null;
    llm_calls: number;
    compaction_calls: number;
    compaction_tokens_saved: number;
    compaction_quality_repairs: number;
    compaction_emergency_drops: number;
    llm_response_errors: number;
  };
  database?: {
    pool: {
      url_database: string | null;
      pool_class: string;
      status: string;
      size: number | null;
      checked_in: number | null;
      checked_out: number | null;
      overflow: number | null;
      configured: {
        pool_size: number;
        max_overflow: number;
        pool_timeout_seconds: number;
        pool_recycle_seconds: number;
      };
    };
    activity?: Array<{
      state: string | null;
      wait_event_type: string;
      wait_event: string;
      count: number;
    }>;
    blocked_locks?: number;
    long_queries?: Array<{
      pid: number;
      state: string | null;
      wait_event_type: string;
      wait_event: string;
      age_seconds: number;
      query_sample: string;
    }>;
    error?: string;
  };
}

export async function getAdminOverview(days: number = 7): Promise<AdminOverview> {
  const resp = await client.get<AdminOverview>('/admin/overview', { params: { days } });
  return resp.data;
}

export async function getAdminRoundsTree(params?: {
  limit?: number;
  offset?: number;
  status?: string;
  user_id?: string;
  search?: string;
}): Promise<AdminRoundTreeResponse> {
  const resp = await client.get<AdminRoundTreeResponse>('/admin/rounds-tree', { params });
  return resp.data;
}

export async function getAdminSessionRounds(
  sessionId: string,
  params?: {
    status?: string;
    search?: string;
  },
): Promise<AdminSessionRoundsResponse> {
  const resp = await client.get<AdminSessionRoundsResponse>(
    `/admin/sessions/${encodeURIComponent(sessionId)}/rounds`,
    { params },
  );
  return resp.data;
}

export async function updateAdminLLMCallReview(
  llmRecordId: number,
  manualReviewStatus: string,
): Promise<AdminLLMReviewUpdateResponse> {
  const resp = await client.put<AdminLLMReviewUpdateResponse>(
    `/admin/llm-call-records/${llmRecordId}/review`,
    { manual_review_status: manualReviewStatus },
  );
  return resp.data;
}

export async function getAdminLLMCallRecordDetail(
  llmRecordId: number,
): Promise<AdminLLMCallRecordDetail> {
  const resp = await client.get<AdminLLMCallRecordDetail>(`/admin/llm-call-records/${llmRecordId}`);
  return resp.data;
}

export async function getAdminUsers(): Promise<AdminUsersResponse> {
  const resp = await client.get<AdminUsersResponse>('/admin/users');
  return resp.data;
}

export interface AdminUserLoginEventItem {
  id: number;
  user_id: string;
  username: string;
  auth_type: 'simple' | 'ldap';
  ip_address: string | null;
  user_agent: string | null;
  login_at: string | null;
}

export interface AdminUserLoginEventsResponse {
  user_id: string;
  events: AdminUserLoginEventItem[];
}

export async function getAdminUserLoginEvents(
  userId: string,
  limit: number = 50,
): Promise<AdminUserLoginEventsResponse> {
  const resp = await client.get<AdminUserLoginEventsResponse>(
    `/admin/users/${encodeURIComponent(userId)}/login-events`,
    { params: { limit } },
  );
  return resp.data;
}

export interface AdminCreateSimpleUserRequest {
  username: string;
  password: string;
  enabled: boolean;
  is_admin: boolean;
  token_limit_per_week: number | null;
  token_limit_per_month: number | null;
  sandbox_profile_id?: string | null;
}

export interface AdminCreateLdapUserRequest {
  user_id: string;
  username: string | null;
  enabled: boolean;
  is_admin: boolean;
  token_limit_per_week: number | null;
  token_limit_per_month: number | null;
  sandbox_profile_id?: string | null;
}

export interface AdminTokenLimitsUpdateRequest {
  token_limit_per_week: number | null;
  token_limit_per_month: number | null;
}

export interface AdminDeleteUserResponse {
  user_id: string;
  deleted: boolean;
}

export interface AdminAuthUserResponse {
  user_id: string;
  username: string;
  auth_type: 'simple' | 'ldap';
  enabled: boolean;
  role: 'admin' | 'user';
  is_admin: boolean;
  token_limit_per_week: number | null;
  token_limit_per_month: number | null;
  last_login_at: string | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AdminSandboxProfile {
  id: string;
  name: string;
  description: string | null;
  department: string | null;
  domain: string;
  protocol: 'http' | 'https';
  api_key_set: boolean;
  use_server_proxy: boolean;
  is_default: boolean;
  enabled: boolean;
  version: number;
  bound_users: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface AdminSandboxProfilesResponse {
  profiles: AdminSandboxProfile[];
}

export interface AdminSandboxProfilePayload {
  name: string;
  description: string | null;
  department: string | null;
  domain: string;
  protocol: 'http' | 'https';
  api_key?: string;
  use_server_proxy: boolean;
  enabled?: boolean;
}

export interface AdminUserSandboxProfileResponse {
  sandbox_profile_id: string | null;
  sandbox_profile_name: string | null;
  sandbox_profile_source: AdminSandboxProfileSource;
  sandbox_profile_error: string | null;
  sandbox_id: string | null;
  sandbox_status: string;
  sandbox_active_profile_id: string | null;
  sandbox_active_profile_version: number | null;
  sandbox_desired_profile_id: string | null;
  sandbox_desired_profile_version: number | null;
  sandbox_needs_recreate: boolean;
}

export interface AdminModelItem {
  id: string;
  name: string;
  provider: 'openai' | 'anthropic' | string;
  api_base: string;
  model_name: string;
  max_tokens: number;
  context_window: number;
  reasoning_format: string;
  reasoning_split: boolean;
  enable_thinking: boolean;
  thinking_mode: 'provider_default' | 'enabled' | 'disabled';
  thinking_wire_format: 'none' | 'enable_thinking' | 'thinking_object';
  reasoning_effort: string | null;
  default_reasoning_level?: string | null;
  supported_reasoning_efforts: string[];
  supports_thinking: boolean;
  supports_image: boolean;
  max_images: number;
  supports_video: boolean;
  max_videos: number;
  enabled: boolean;
  tags: string[];
  api_key_set: boolean;
  group_names: string[];
  session_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface AdminModelSettings {
  default_model_id: string | null;
  cron_default_model_id: string | null;
  subagent_default_model_id: string | null;
}

export interface AdminModelsResponse {
  models: AdminModelItem[];
  settings: AdminModelSettings;
}

export interface AdminDeleteModelResponse {
  model_id: string;
  deleted: boolean;
  replacement_model_id: string | null;
  sessions_reassigned: number;
  defaults_reassigned: string[];
}

export interface AdminModelPayload {
  model_id?: string;
  display_name?: string;
  provider?: string;
  api_base?: string;
  api_key?: string;
  model_name?: string;
  max_tokens?: number;
  context_window?: number;
  reasoning_format?: string;
  reasoning_split?: boolean;
  enable_thinking?: boolean;
  thinking_mode?: 'provider_default' | 'enabled' | 'disabled';
  thinking_wire_format?: 'none' | 'enable_thinking' | 'thinking_object';
  reasoning_effort?: string | null;
  supported_reasoning_efforts?: string[];
  supports_image?: boolean;
  max_images?: number;
  supports_video?: boolean;
  max_videos?: number;
  enabled?: boolean;
  tags?: string[];
}

export interface AdminModelPermissionGroup {
  id: string;
  name: string;
  description: string | null;
  is_default: boolean;
  model_ids: string[];
  model_count: number;
  bound_users: number;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AdminModelPermissionGroupsResponse {
  groups: AdminModelPermissionGroup[];
}

export interface AdminUserModelGroupsResponse {
  user_id: string;
  default_group: AdminModelPermissionGroup;
  extra_groups: AdminModelPermissionGroup[];
  group_ids: string[];
  group_names: string[];
}

export async function createAdminSimpleUser(payload: AdminCreateSimpleUserRequest): Promise<AdminAuthUserResponse> {
  const resp = await client.post<AdminAuthUserResponse>('/admin/users/simple', payload);
  return resp.data;
}

export async function createAdminLdapUser(payload: AdminCreateLdapUserRequest): Promise<AdminAuthUserResponse> {
  const resp = await client.post<AdminAuthUserResponse>('/admin/users/ldap', payload);
  return resp.data;
}

export async function updateAdminUserEnabled(userId: string, enabled: boolean): Promise<AdminAuthUserResponse> {
  const resp = await client.patch<AdminAuthUserResponse>(`/admin/users/${encodeURIComponent(userId)}/enabled`, { enabled });
  return resp.data;
}

export async function updateAdminUserAdmin(userId: string, isAdmin: boolean): Promise<AdminAuthUserResponse> {
  const resp = await client.patch<AdminAuthUserResponse>(`/admin/users/${encodeURIComponent(userId)}/admin`, { is_admin: isAdmin });
  return resp.data;
}

export async function updateAdminUserTokenLimits(
  userId: string,
  payload: AdminTokenLimitsUpdateRequest,
): Promise<AdminAuthUserResponse> {
  const resp = await client.patch<AdminAuthUserResponse>(`/admin/users/${encodeURIComponent(userId)}/token-limits`, payload);
  return resp.data;
}

export async function resetAdminSimpleUserPassword(userId: string, password: string): Promise<AdminAuthUserResponse> {
  const resp = await client.post<AdminAuthUserResponse>(`/admin/users/${encodeURIComponent(userId)}/reset-password`, { password });
  return resp.data;
}

export async function deleteAdminUser(userId: string): Promise<AdminDeleteUserResponse> {
  const resp = await client.delete<AdminDeleteUserResponse>(`/admin/users/${encodeURIComponent(userId)}`);
  return resp.data;
}

export async function getAdminModels(): Promise<AdminModelsResponse> {
  const resp = await client.get<AdminModelsResponse>('/admin/models');
  return resp.data;
}

export async function createAdminModel(payload: Required<AdminModelPayload>): Promise<AdminModelItem> {
  const resp = await client.post<AdminModelItem>('/admin/models', payload);
  return resp.data;
}

export async function updateAdminModel(modelId: string, payload: AdminModelPayload): Promise<AdminModelItem> {
  const resp = await client.patch<AdminModelItem>(`/admin/models/${encodeURIComponent(modelId)}`, payload);
  return resp.data;
}

export async function deleteAdminModel(
  modelId: string,
  replacementModelId?: string,
): Promise<AdminDeleteModelResponse> {
  const resp = await client.delete<AdminDeleteModelResponse>(
    `/admin/models/${encodeURIComponent(modelId)}`,
    {
      params: replacementModelId ? { replacement_model_id: replacementModelId } : undefined,
    },
  );
  return resp.data;
}

export async function updateAdminModelSettings(payload: {
  default_model_id: string;
  cron_default_model_id?: string | null;
  subagent_default_model_id?: string | null;
}): Promise<AdminModelSettings> {
  const resp = await client.patch<AdminModelSettings>('/admin/models/settings', payload);
  return resp.data;
}

export async function getAdminModelPermissionGroups(): Promise<AdminModelPermissionGroupsResponse> {
  const resp = await client.get<AdminModelPermissionGroupsResponse>('/admin/model-permission-groups');
  return resp.data;
}

export async function createAdminModelPermissionGroup(payload: {
  name: string;
  description?: string | null;
}): Promise<AdminModelPermissionGroup> {
  const resp = await client.post<AdminModelPermissionGroup>('/admin/model-permission-groups', payload);
  return resp.data;
}

export async function updateAdminModelPermissionGroup(
  groupId: string,
  payload: { name?: string; description?: string | null },
): Promise<AdminModelPermissionGroup> {
  const resp = await client.patch<AdminModelPermissionGroup>(
    `/admin/model-permission-groups/${encodeURIComponent(groupId)}`,
    payload,
  );
  return resp.data;
}

export async function updateAdminModelPermissionGroupModels(
  groupId: string,
  modelIds: string[],
): Promise<AdminModelPermissionGroup> {
  const resp = await client.put<AdminModelPermissionGroup>(
    `/admin/model-permission-groups/${encodeURIComponent(groupId)}/models`,
    { model_ids: modelIds },
  );
  return resp.data;
}

export async function updateAdminModelPermissionGroupUsers(
  groupId: string,
  userIds: string[],
): Promise<AdminModelPermissionGroup> {
  const resp = await client.put<AdminModelPermissionGroup>(
    `/admin/model-permission-groups/${encodeURIComponent(groupId)}/users`,
    { user_ids: userIds },
  );
  return resp.data;
}

export async function updateAdminUserModelPermissionGroups(
  userId: string,
  groupIds: string[],
): Promise<AdminUserModelGroupsResponse> {
  const resp = await client.put<AdminUserModelGroupsResponse>(
    `/admin/users/${encodeURIComponent(userId)}/model-permission-groups`,
    { group_ids: groupIds },
  );
  return resp.data;
}

export async function getAdminSandboxProfiles(): Promise<AdminSandboxProfilesResponse> {
  const resp = await client.get<AdminSandboxProfilesResponse>('/admin/sandbox-profiles');
  return resp.data;
}

export async function createAdminSandboxProfile(payload: AdminSandboxProfilePayload): Promise<AdminSandboxProfile> {
  const resp = await client.post<AdminSandboxProfile>('/admin/sandbox-profiles', payload);
  return resp.data;
}

export async function updateAdminSandboxProfile(
  profileId: string,
  payload: Partial<AdminSandboxProfilePayload>,
): Promise<AdminSandboxProfile> {
  const resp = await client.patch<AdminSandboxProfile>(
    `/admin/sandbox-profiles/${encodeURIComponent(profileId)}`,
    payload,
  );
  return resp.data;
}

export async function setAdminSandboxProfileDefault(profileId: string): Promise<AdminSandboxProfile> {
  const resp = await client.patch<AdminSandboxProfile>(
    `/admin/sandbox-profiles/${encodeURIComponent(profileId)}/default`,
    {},
  );
  return resp.data;
}

export async function setAdminSandboxProfileEnabled(profileId: string, enabled: boolean): Promise<AdminSandboxProfile> {
  const resp = await client.patch<AdminSandboxProfile>(
    `/admin/sandbox-profiles/${encodeURIComponent(profileId)}/enabled`,
    { enabled },
  );
  return resp.data;
}

export async function updateAdminUserSandboxProfile(
  userId: string,
  sandboxProfileId: string | null,
  forceRecreate: boolean = false,
): Promise<AdminUserSandboxProfileResponse> {
  const resp = await client.patch<AdminUserSandboxProfileResponse>(
    `/admin/users/${encodeURIComponent(userId)}/sandbox-profile`,
    { sandbox_profile_id: sandboxProfileId, force_recreate: forceRecreate },
  );
  return resp.data;
}

export async function getAdminSystem(hours: number = 24): Promise<AdminSystemResponse> {
  const resp = await client.get<AdminSystemResponse>('/admin/system', { params: { hours } });
  return resp.data;
}

export type AdminOperationLogOutcome = 'started' | 'succeeded' | 'failed';
export type AdminOperationLogRiskLevel = 'high' | 'normal';

export interface AdminOperationLogItem {
  id: number;
  request_id: string;
  actor_user_id: string;
  action: string;
  risk_level: AdminOperationLogRiskLevel;
  target_type: string | null;
  target_id: string | null;
  target_user_id: string | null;
  session_id: string | null;
  step_record_id: number | null;
  outcome: AdminOperationLogOutcome;
  http_method: string;
  route_template: string;
  status_code: number | null;
  ip_address: string | null;
  user_agent: string | null;
  changed_fields: unknown;
  details: unknown;
  started_at: string;
  completed_at: string | null;
}

export interface AdminOperationLogFilters {
  from?: string;
  to?: string;
  action?: string;
  target_user_id?: string;
  session_id?: string;
  outcome?: AdminOperationLogOutcome;
  risk_level?: AdminOperationLogRiskLevel;
  cursor?: string;
  limit?: number;
}

export interface AdminOperationLogsResponse {
  items: AdminOperationLogItem[];
  next_cursor: string | null;
}

export async function getAdminOperationLogs(
  params: AdminOperationLogFilters,
): Promise<AdminOperationLogsResponse> {
  const resp = await client.get<AdminOperationLogsResponse>('/admin/operation-logs', { params });
  return resp.data;
}

export async function exportAdminOperationLogs(
  params: Omit<AdminOperationLogFilters, 'cursor' | 'limit'>,
): Promise<Blob> {
  const resp = await client.get<Blob>('/admin/operation-logs/export', {
    params,
    responseType: 'blob',
  });
  return resp.data;
}

export async function exportAdminUsers(userIds: string[]): Promise<Blob> {
  const resp = await client.post<Blob>(
    '/admin/users/export',
    { user_ids: userIds },
    { responseType: 'blob' },
  );
  return resp.data;
}
