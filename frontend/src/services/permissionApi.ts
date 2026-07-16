import { apiService } from './api';

const client = apiService.getAxiosClient();

export type ToolProvider = 'builtin' | 'mcp';
export type PermissionEffect = 'allow' | 'ask' | 'deny';

export interface ToolPermissionRule {
  id: string;
  scope_type: 'platform' | 'user' | 'session';
  scope_id: string | null;
  provider: ToolProvider;
  server_id: string | null;
  tool_name: string;
  tool_ref: string;
  effect: PermissionEffect;
  priority: number;
  managed: boolean;
  conditions: Record<string, unknown> | null;
  description: string | null;
  enabled: boolean;
  expires_at: string | null;
  created_by: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PermissionTool {
  tool_ref: string;
  provider: ToolProvider;
  server_id: string | null;
  server_name: string | null;
  source_type: 'builtin' | 'official' | 'personal';
  tool_name: string;
  title: string;
  description: string;
  effect: PermissionEffect;
  matched_rule_id: string | null;
  schema_hash?: string | null;
}

export interface PermissionRuleInput {
  provider: ToolProvider;
  server_id?: string | null;
  tool_name: string;
  effect: PermissionEffect;
  priority?: number;
  description?: string | null;
}

export interface PermissionSelectionInput {
  provider: ToolProvider;
  server_id?: string | null;
  tool_name: string;
  effect: PermissionEffect;
}

export interface PermissionSelectionItem {
  provider: ToolProvider;
  server_id?: string | null;
  tool_name: string;
}

export interface PermissionSelectionBatchInput {
  effect: PermissionEffect;
  items: PermissionSelectionItem[];
}

export async function getPermissionRules(): Promise<ToolPermissionRule[]> {
  const response = await client.get<{ rules: ToolPermissionRule[] }>('/permissions/rules');
  return response.data.rules;
}

export async function getPermissionTools(): Promise<PermissionTool[]> {
  const response = await client.get<{ tools: PermissionTool[] }>('/permissions/tools');
  return response.data.tools;
}

export async function createPermissionRule(
  payload: PermissionRuleInput,
): Promise<ToolPermissionRule> {
  const response = await client.post<ToolPermissionRule>('/permissions/rules', payload);
  return response.data;
}

export async function setPermissionSelection(
  payload: PermissionSelectionInput,
): Promise<ToolPermissionRule> {
  const response = await client.put<ToolPermissionRule>('/permissions/rules/selection', payload);
  return response.data;
}

export async function setPermissionSelectionBatch(
  payload: PermissionSelectionBatchInput,
): Promise<ToolPermissionRule[]> {
  const response = await client.put<{ rules: ToolPermissionRule[] }>(
    '/permissions/rules/selection/batch',
    payload,
  );
  return response.data.rules;
}

export async function clearPermissionSelection(
  payload: PermissionSelectionItem,
): Promise<number> {
  const response = await client.delete<{ deleted: number }>('/permissions/rules/selection', {
    data: payload,
  });
  return response.data.deleted;
}

export async function updatePermissionRule(
  ruleId: string,
  payload: Partial<Pick<ToolPermissionRule, 'effect' | 'priority' | 'description' | 'enabled'>>,
): Promise<ToolPermissionRule> {
  const response = await client.patch<ToolPermissionRule>(
    `/permissions/rules/${encodeURIComponent(ruleId)}`,
    payload,
  );
  return response.data;
}

export async function deletePermissionRule(ruleId: string): Promise<void> {
  await client.delete(`/permissions/rules/${encodeURIComponent(ruleId)}`);
}

export async function getAdminPermissionRules(): Promise<ToolPermissionRule[]> {
  const response = await client.get<{ rules: ToolPermissionRule[] }>('/admin/tool-permissions');
  return response.data.rules;
}

export async function createAdminPermissionRule(
  payload: PermissionRuleInput,
): Promise<ToolPermissionRule> {
  const response = await client.post<ToolPermissionRule>('/admin/tool-permissions', payload);
  return response.data;
}

export async function updateAdminPermissionRule(
  ruleId: string,
  payload: Partial<Pick<ToolPermissionRule, 'effect' | 'priority' | 'description' | 'enabled'>>,
): Promise<ToolPermissionRule> {
  const response = await client.patch<ToolPermissionRule>(
    `/admin/tool-permissions/${encodeURIComponent(ruleId)}`,
    payload,
  );
  return response.data;
}

export async function deleteAdminPermissionRule(ruleId: string): Promise<void> {
  await client.delete(`/admin/tool-permissions/${encodeURIComponent(ruleId)}`);
}
