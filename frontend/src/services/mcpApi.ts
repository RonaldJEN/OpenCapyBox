import { apiService } from './api';

const client = apiService.getAxiosClient();

export type McpServerSource = 'official' | 'personal';
export type McpServerStatus = 'draft' | 'published' | 'disabled';
export type McpAuthMode = 'none' | 'bearer' | 'headers';

export interface McpServer {
  id: string;
  name: string;
  description: string;
  url: string;
  source: McpServerSource;
  status: McpServerStatus;
  enabled: boolean;
  required: boolean;
  auth_type: McpAuthMode;
  credential_set: boolean;
  header_names: string[];
  allow_private_network: boolean;
  allow_insecure_http: boolean;
  installation_id: string | null;
  tools_count: number | null;
  enabled_tools_count: number;
  enabled_tools: string[] | null;
  disabled_tools: string[];
  last_tested_at: string | null;
  last_error: string | null;
  created_at: string | null;
  updated_at: string | null;
  version: number;
}

export interface McpTool {
  name: string;
  title: string | null;
  description: string | null;
  schema_hash: string;
  enabled: boolean;
  discovered_at: string | null;
}

export interface McpToolList {
  server_id: string;
  installation_id: string | null;
  visibility_revision: number;
  tools_count: number;
  enabled_tools_count: number;
  enabled_tools: string[] | null;
  disabled_tools: string[];
  tools: McpTool[];
}

export interface McpServerPayload {
  name: string;
  description?: string | null;
  url: string;
  auth_type: McpAuthMode;
  bearer_token?: string;
  headers?: Record<string, string>;
  clear_credential?: boolean;
  enabled?: boolean;
}

export interface AdminMcpServerPayload extends Omit<McpServerPayload, 'enabled'> {
  status: McpServerStatus;
  allow_private_network: boolean;
  allow_insecure_http: boolean;
  required: boolean;
}

export interface McpConnectionPayload {
  enabled: boolean;
  auth_type?: McpAuthMode;
  bearer_token?: string;
  headers?: Record<string, string>;
  clear_credential?: boolean;
}

export type McpActivationPayload = Omit<McpConnectionPayload, 'enabled'>;

export interface McpTestResult {
  ok: boolean;
  tools_count: number;
  latency_ms: number | null;
  error: string | null;
}

export interface McpImportResult {
  imported: number;
  errors: Array<{ name: string; error: string }>;
  servers: McpServer[];
}

export interface PersonalMcpNetworkPolicy {
  domain_suffixes: string[];
  cidrs: string[];
  version: number;
  updated_at: string | null;
  disabled_installations: number;
}

type UnknownRecord = Record<string, unknown>;

function isRecord(value: unknown): value is UnknownRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function normalizeSource(value: unknown, raw: UnknownRecord): McpServerSource {
  if (value === 'personal' || value === 'user') return 'personal';
  if (value === 'official') return 'official';
  return raw.owner_user_id ? 'personal' : 'official';
}

function normalizeStatus(value: unknown, source: McpServerSource): McpServerStatus {
  if (value === 'draft' || value === 'published' || value === 'disabled') return value;
  return source === 'official' ? 'published' : 'published';
}

function normalizeAuthMode(value: unknown): McpAuthMode {
  if (value === 'bearer' || value === 'headers') return value;
  if (value === 'custom_headers') return 'headers';
  return 'none';
}

function normalizeServer(value: unknown): McpServer {
  const raw = isRecord(value) ? value : {};
  const source = normalizeSource(raw.source ?? raw.source_type, raw);
  const headerNames = Array.isArray(raw.header_names)
    ? raw.header_names.filter((item): item is string => typeof item === 'string')
    : [];
  const toolsCount = raw.tools_count;
  const enabledToolsCount = raw.enabled_tools_count;
  const disabledTools = Array.isArray(raw.disabled_tools)
    ? raw.disabled_tools.filter((item): item is string => typeof item === 'string')
    : [];
  const enabledTools = raw.enabled_tools === null || raw.enabled_tools === undefined
    ? null
    : Array.isArray(raw.enabled_tools)
      ? raw.enabled_tools.filter((item): item is string => typeof item === 'string')
      : null;
  return {
    id: stringValue(raw.id ?? raw.server_id),
    name: stringValue(raw.name),
    description: stringValue(raw.description),
    url: stringValue(raw.url ?? raw.streamable_http_url),
    source,
    status: normalizeStatus(raw.status, source),
    enabled: raw.enabled !== false && raw.status !== 'disabled',
    required: raw.required === true,
    auth_type: normalizeAuthMode(raw.auth_type ?? raw.auth_mode),
    credential_set: raw.credential_set === true || raw.secret_set === true || raw.api_key_set === true,
    header_names: headerNames,
    allow_private_network: raw.allow_private_network === true,
    allow_insecure_http: raw.allow_insecure_http === true,
    installation_id: nullableString(raw.installation_id),
    tools_count: typeof toolsCount === 'number' ? toolsCount : null,
    enabled_tools_count: typeof enabledToolsCount === 'number'
      ? enabledToolsCount
      : Math.max(0, (typeof toolsCount === 'number' ? toolsCount : 0) - disabledTools.length),
    enabled_tools: enabledTools,
    disabled_tools: disabledTools,
    last_tested_at: nullableString(raw.last_tested_at),
    last_error: nullableString(raw.last_error),
    created_at: nullableString(raw.created_at),
    updated_at: nullableString(raw.updated_at),
    version: typeof raw.version === 'number' ? raw.version : 1,
  };
}

function normalizeToolList(value: unknown): McpToolList {
  const raw = isRecord(value) ? value : {};
  const tools = Array.isArray(raw.tools)
    ? raw.tools.map((value): McpTool => {
      const tool = isRecord(value) ? value : {};
      return {
        name: stringValue(tool.name),
        title: nullableString(tool.title),
        description: nullableString(tool.description),
        schema_hash: stringValue(tool.schema_hash),
        enabled: tool.enabled !== false,
        discovered_at: nullableString(tool.discovered_at),
      };
    })
    : [];
  const disabledTools = Array.isArray(raw.disabled_tools)
    ? raw.disabled_tools.filter((item): item is string => typeof item === 'string')
    : tools.filter((tool) => !tool.enabled).map((tool) => tool.name);
  const enabledTools = raw.enabled_tools === null || raw.enabled_tools === undefined
    ? null
    : Array.isArray(raw.enabled_tools)
      ? raw.enabled_tools.filter((item): item is string => typeof item === 'string')
      : null;
  return {
    server_id: stringValue(raw.server_id),
    installation_id: nullableString(raw.installation_id),
    visibility_revision: typeof raw.visibility_revision === 'number'
      ? raw.visibility_revision
      : 0,
    tools_count: typeof raw.tools_count === 'number' ? raw.tools_count : tools.length,
    enabled_tools_count: typeof raw.enabled_tools_count === 'number'
      ? raw.enabled_tools_count
      : tools.filter((tool) => tool.enabled).length,
    enabled_tools: enabledTools,
    disabled_tools: disabledTools,
    tools,
  };
}

function normalizeServerList(value: unknown): McpServer[] {
  if (Array.isArray(value)) return value.map(normalizeServer);
  if (!isRecord(value)) return [];
  const servers = value.servers ?? value.items;
  return Array.isArray(servers) ? servers.map(normalizeServer) : [];
}

function normalizeTestResult(value: unknown): McpTestResult {
  const raw = isRecord(value) ? value : {};
  const toolsCount = raw.tools_count ?? raw.tool_count;
  return {
    ok: raw.ok === true || raw.success === true,
    tools_count: typeof toolsCount === 'number' ? toolsCount : 0,
    latency_ms: typeof raw.latency_ms === 'number' ? raw.latency_ms : null,
    error: nullableString(raw.error ?? raw.detail),
  };
}

function normalizeImportResult(value: unknown): McpImportResult {
  const raw = isRecord(value) ? value : {};
  const errors = Array.isArray(raw.errors)
    ? raw.errors.map((value) => {
      const error = isRecord(value) ? value : {};
      return {
        name: stringValue(error.name),
        error: stringValue(error.error, '导入失败'),
      };
    })
    : [];
  return {
    imported: typeof raw.imported === 'number' ? raw.imported : 0,
    errors,
    servers: Array.isArray(raw.servers) ? raw.servers.map(normalizeServer) : [],
  };
}

export async function getMcpServers(): Promise<McpServer[]> {
  const response = await client.get('/mcp/servers');
  return normalizeServerList(response.data);
}

export async function createMcpServer(payload: McpServerPayload): Promise<McpServer> {
  const response = await client.post('/mcp/servers', payload);
  return normalizeServer(response.data);
}

export async function updateMcpServer(
  serverId: string,
  payload: Partial<McpServerPayload>,
): Promise<McpServer> {
  const response = await client.patch(`/mcp/servers/${encodeURIComponent(serverId)}`, payload);
  return normalizeServer(response.data);
}

export async function deleteMcpServer(serverId: string): Promise<void> {
  await client.delete(`/mcp/servers/${encodeURIComponent(serverId)}`);
}

export async function updateMcpConnection(
  serverId: string,
  payload: McpConnectionPayload,
): Promise<McpServer> {
  const response = await client.put(
    `/mcp/servers/${encodeURIComponent(serverId)}/connection`,
    payload,
  );
  return normalizeServer(response.data);
}

export async function activateMcpServer(
  serverId: string,
  payload?: McpActivationPayload,
): Promise<McpServer> {
  const path = `/mcp/servers/${encodeURIComponent(serverId)}/activate`;
  const response = payload === undefined
    ? await client.post(path)
    : await client.post(path, payload);
  return normalizeServer(response.data);
}

export async function testMcpServer(serverId: string): Promise<McpTestResult> {
  const response = await client.post(`/mcp/servers/${encodeURIComponent(serverId)}/test`);
  return normalizeTestResult(response.data);
}

export async function getMcpServerTools(serverId: string): Promise<McpToolList> {
  const response = await client.get(`/mcp/servers/${encodeURIComponent(serverId)}/tools`);
  return normalizeToolList(response.data);
}

export async function updateMcpToolVisibility(
  serverId: string,
  visibility: {
    expected_revision: number;
    enabled_tools: string[] | null;
    disabled_tools: string[];
  },
): Promise<McpToolList> {
  const response = await client.put(
    `/mcp/servers/${encodeURIComponent(serverId)}/tools/visibility`,
    visibility,
  );
  return normalizeToolList(response.data);
}

export async function importMcpConfig(config: unknown): Promise<McpImportResult> {
  const response = await client.post('/mcp/import', config);
  return normalizeImportResult(response.data);
}

export async function exportMcpConfig(): Promise<unknown> {
  const response = await client.get('/mcp/export');
  return response.data;
}

export async function getAdminMcpServers(): Promise<McpServer[]> {
  const response = await client.get('/admin/mcp/servers');
  return normalizeServerList(response.data).map((server) => ({ ...server, source: 'official' }));
}

export async function createAdminMcpServer(payload: AdminMcpServerPayload): Promise<McpServer> {
  const response = await client.post('/admin/mcp/servers', payload);
  return { ...normalizeServer(response.data), source: 'official' };
}

export async function updateAdminMcpServer(
  serverId: string,
  payload: Partial<AdminMcpServerPayload>,
): Promise<McpServer> {
  const response = await client.patch(`/admin/mcp/servers/${encodeURIComponent(serverId)}`, payload);
  return { ...normalizeServer(response.data), source: 'official' };
}

export async function deleteAdminMcpServer(serverId: string): Promise<void> {
  await client.delete(`/admin/mcp/servers/${encodeURIComponent(serverId)}`);
}

export async function testAdminMcpServer(serverId: string): Promise<McpTestResult> {
  const response = await client.post(`/admin/mcp/servers/${encodeURIComponent(serverId)}/test`);
  return normalizeTestResult(response.data);
}

export async function getPersonalMcpNetworkPolicy(): Promise<PersonalMcpNetworkPolicy> {
  const response = await client.get('/admin/mcp/personal-network-policy');
  const raw = isRecord(response.data) ? response.data : {};
  return {
    domain_suffixes: Array.isArray(raw.domain_suffixes)
      ? raw.domain_suffixes.filter((item): item is string => typeof item === 'string')
      : [],
    cidrs: Array.isArray(raw.cidrs)
      ? raw.cidrs.filter((item): item is string => typeof item === 'string')
      : [],
    version: typeof raw.version === 'number' ? raw.version : 0,
    updated_at: nullableString(raw.updated_at),
    disabled_installations: typeof raw.disabled_installations === 'number'
      ? raw.disabled_installations
      : 0,
  };
}

export async function updatePersonalMcpNetworkPolicy(payload: {
  domain_suffixes: string[];
  cidrs: string[];
}): Promise<PersonalMcpNetworkPolicy> {
  const response = await client.put('/admin/mcp/personal-network-policy', payload);
  const raw = isRecord(response.data) ? response.data : {};
  return {
    domain_suffixes: Array.isArray(raw.domain_suffixes)
      ? raw.domain_suffixes.filter((item): item is string => typeof item === 'string')
      : [],
    cidrs: Array.isArray(raw.cidrs)
      ? raw.cidrs.filter((item): item is string => typeof item === 'string')
      : [],
    version: typeof raw.version === 'number' ? raw.version : 0,
    updated_at: nullableString(raw.updated_at),
    disabled_installations: typeof raw.disabled_installations === 'number'
      ? raw.disabled_installations
      : 0,
  };
}
