import axios from 'axios';

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
    config.headers = {
      ...config.headers,
      Authorization: `Bearer ${accessToken}`,
    };
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

export interface AgentFileInfo {
  name: string;
  file_type: string;
  filename: string;
  has_content: boolean;
  version: number;
  updated_at: string | null;
}

export interface AgentFileDetail {
  name: string;
  file_type: string;
  content: string;
  version: number;
}

export async function listAgentFiles(): Promise<AgentFileInfo[]> {
  const resp = await client.get<{ files: AgentFileInfo[] }>('/config/agent-files');
  return resp.data.files;
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
  enabled: boolean;
}

export async function getSkills(): Promise<SkillInfo[]> {
  const resp = await client.get<{ skills: SkillInfo[] }>('/config/skills');
  return resp.data.skills;
}

export async function toggleSkill(
  skillName: string,
  enabled: boolean,
): Promise<void> {
  await client.put(`/config/skills/${skillName}`, { enabled });
}

// ========== Cron 任务 API ==========

export interface CronTask {
  name: string;
  cron_expr: string;
  description: string;
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
}

export async function getHeartbeat(): Promise<{
  content: string;
  tasks: CronTask[];
}> {
  const resp = await client.get<{ content: string; tasks: CronTask[] }>(
    '/cron/heartbeat',
  );
  return resp.data;
}

export async function getCronJobs(): Promise<CronTask[]> {
  const resp = await client.get<{ jobs: CronTask[] }>('/cron/jobs');
  return resp.data.jobs;
}

export async function getCronRuns(
  jobName?: string,
  limit: number = 20,
): Promise<CronJobRun[]> {
  const params: Record<string, string | number> = { limit };
  if (jobName) params.job_name = jobName;
  const resp = await client.get<{ runs: CronJobRun[] }>('/cron/runs', { params });
  return resp.data.runs;
}

export async function triggerCronJob(
  jobName: string,
): Promise<{ status: string; output: string | null }> {
  const resp = await client.post<{ status: string; output: string | null }>(
    `/cron/jobs/${jobName}/run`,
  );
  return resp.data;
}
