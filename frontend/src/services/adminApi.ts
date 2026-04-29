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
  role: 'admin' | 'user';
  is_admin: boolean;
  status: string;
  sessions_count: number;
  rounds_count: number;
  running_rounds: number;
  total_tokens: number;
  cron_jobs_total: number;
  cron_jobs_enabled: number;
  cron_failed_24h: number;
  last_active_at: string | null;
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

export async function getAdminSystem(hours: number = 24): Promise<AdminSystemResponse> {
  const resp = await client.get<AdminSystemResponse>('/admin/system', { params: { hours } });
  return resp.data;
}
