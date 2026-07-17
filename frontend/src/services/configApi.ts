import axios, { AxiosHeaders } from 'axios';

// 复用主 API 的 axios 实例配置
const client = axios.create({
  baseURL: '/api',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// 自动附加 Bearer Token
client.interceptors.request.use((config) => {
  const accessToken = localStorage.getItem('accessToken');
  if (accessToken) {
    const headers = config.headers instanceof AxiosHeaders
      ? config.headers
      : new AxiosHeaders(config.headers);
    headers.set('Authorization', `Bearer ${accessToken}`);
    config.headers = headers;
  }
  return config;
});

// 401 响应拦截：token 过期后清除凭据并跳转登录
client.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem('accessToken');
      localStorage.removeItem('userId');
      window.location.href = '/login';
    }
    return Promise.reject(error);
  },
);

// ========== Agent 配置文件 API ==========

export interface AgentFileDetail {
  name: string;
  file_type: string;
  content: string;
  version: number;
}

export async function getAgentFile(name: string): Promise<AgentFileDetail> {
  const resp = await client.get<AgentFileDetail>(`/config/agent-files/${name}`);
  return resp.data;
}

export async function updateAgentFile(
  name: string,
  content: string,
): Promise<{ version: number }> {
  const resp = await client.put<{ version: number; message: string }>(
    `/config/agent-files/${name}`,
    { content },
  );
  return resp.data;
}

// ========== Skill 管理 API ==========

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  source: 'official' | 'user';
  enabled: boolean;
}

export type SkillSandboxStatus = 'available' | 'unavailable' | 'not_created';

export interface SkillsResponse {
  skills: SkillInfo[];
  sandbox_status: SkillSandboxStatus;
}

export async function getSkills(): Promise<SkillsResponse> {
  const resp = await client.get<SkillsResponse>('/config/skills', {
    timeout: 240_000,
  });
  return resp.data;
}

export async function toggleSkill(
  skillName: string,
  enabled: boolean,
): Promise<void> {
  await client.put(`/config/skills/${encodeURIComponent(skillName)}`, { enabled });
}

// ========== Cron 任务 API ==========

/** 结构化时间配置 — 由 SchedulePicker 产出，后端转 cron_expr。 */
export type Schedule =
  | { kind: 'daily'; time: string }                                    // HH:MM
  | { kind: 'weekdays'; time: string }                                  // 周一-五
  | { kind: 'weekly'; time: string; days: number[] }                   // 0=Mon..6=Sun
  | { kind: 'monthly'; time: string; dayOfMonth: number }              // 1-31
  | { kind: 'interval'; everyMinutes?: number; everyHours?: number };

export interface CronTask {
  id?: number | null;
  name: string;
  cron_expr: string;
  schedule: Schedule | null;
  description: string;
  content: string;
  enabled: boolean;
}

export interface CronJobRun {
  id: string;
  job_name: string;
  cron_expr: string;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  output: string | null;
  is_read: boolean;
  artifacts: ArtifactFile[] | null;
  run_workspace: string | null;
}

export interface ArtifactFile {
  name: string;
  path: string;
  size: number;
  type: string;
}

export async function getCronJobs(): Promise<CronTask[]> {
  const resp = await client.get<{ jobs: CronTask[] }>('/cron/jobs');
  return resp.data.jobs;
}

export interface CronJobInput {
  name: string;
  description?: string;
  content?: string;
  schedule?: Schedule | null;
  cron_expr?: string | null;
  enabled?: boolean;
}

export async function createCronJob(payload: CronJobInput): Promise<CronTask> {
  const resp = await client.post<{ job: CronTask }>('/cron/jobs', payload);
  return resp.data.job;
}

export interface CronJobUpdateInput {
  description?: string;
  content?: string;
  schedule?: Schedule | null;
  cron_expr?: string | null;
  enabled?: boolean;
}

export async function updateCronJob(name: string, payload: CronJobUpdateInput): Promise<CronTask> {
  const resp = await client.put<{ job: CronTask }>(`/cron/jobs/${encodeURIComponent(name)}`, payload);
  return resp.data.job;
}

export async function deleteCronJob(name: string): Promise<void> {
  await client.delete(`/cron/jobs/${encodeURIComponent(name)}`);
}

export interface SchedulePreviewResult {
  cron_expr: string;
  next_fires: string[];  // ISO datetime strings (本地 naive)
}

export async function previewSchedule(
  payload: { schedule?: Schedule | null; cron_expr?: string | null; n?: number },
): Promise<SchedulePreviewResult> {
  const resp = await client.post<SchedulePreviewResult>('/cron/jobs/preview', payload);
  return resp.data;
}

export async function getCronRuns(
  jobName?: string,
  limit: number = 20,
  offset: number = 0,
): Promise<{ runs: CronJobRun[]; total: number; offset: number; limit: number }> {
  const params: Record<string, string | number> = { limit, offset };
  if (jobName) params.job_name = jobName;
  const resp = await client.get<{ runs: CronJobRun[]; total: number; offset: number; limit: number }>(
    '/cron/runs',
    { params },
  );
  return resp.data;
}

export async function triggerCronJob(
  jobName: string,
): Promise<{ job_name: string; run_id: string; status: string; message?: string }> {
  const resp = await client.post<{ job_name: string; run_id: string; status: string; message?: string }>(
    `/cron/jobs/${jobName}/run`,
  );
  return resp.data;
}

export async function getCronRunStatus(runId: string): Promise<CronJobRun> {
  const resp = await client.get<CronJobRun>(`/cron/runs/${runId}`);
  return resp.data;
}

export async function getUnreadCount(): Promise<{ count: number }> {
  const resp = await client.get<{ count: number }>('/cron/runs/unread-count');
  return resp.data;
}

export async function markCronRunsRead(runId?: string): Promise<{ marked: number }> {
  const resp = await client.post<{ marked: number }>(
    '/cron/runs/mark-read',
    undefined,
    runId ? { params: { run_id: runId } } : undefined,
  );
  return resp.data;
}

export async function getCronRunFiles(runId: string): Promise<{ files: ArtifactFile[] }> {
  const resp = await client.get<{ files: ArtifactFile[] }>(`/cron/runs/${runId}/files`);
  return resp.data;
}

export async function downloadCronRunFile(
  runId: string,
  filePath: string,
  fileName?: string,
): Promise<void> {
  const encodedPath = filePath
    .split('/')
    .map((seg) => encodeURIComponent(seg))
    .join('/');

  const resp = await client.get<Blob>(`/cron/runs/${runId}/files/${encodedPath}`, {
    responseType: 'blob',
  });

  const blob = resp.data;
  const objectUrl = window.URL.createObjectURL(blob);
  try {
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = fileName || filePath.split('/').pop() || 'download';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  } finally {
    window.URL.revokeObjectURL(objectUrl);
  }
}
