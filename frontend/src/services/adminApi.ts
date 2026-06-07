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
  rounds: AdminRoundTreeItem[];
}

export interface AdminRoundTreeResponse {
  total_sessions: number;
  offset: number;
  limit: number;
  sessions: AdminRoundSessionItem[];
}

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
}

export interface AdminCreateLdapUserRequest {
  user_id: string;
  username: string | null;
  enabled: boolean;
  is_admin: boolean;
  token_limit_per_week: number | null;
  token_limit_per_month: number | null;
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

export async function getAdminSystem(hours: number = 24): Promise<AdminSystemResponse> {
  const resp = await client.get<AdminSystemResponse>('/admin/system', { params: { hours } });
  return resp.data;
}
