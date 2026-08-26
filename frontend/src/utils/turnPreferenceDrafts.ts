import type { PreferredMcpConnectionSnapshot } from '../types';

export interface TurnPreferenceDraft {
  skillKeys: string[];
  mcpConnections: PreferredMcpConnectionSnapshot[];
  revision: number;
}

export const MAX_SELECTED_SKILLS = 50;
export const MAX_SELECTED_MCP_SERVERS = 20;

function mergeValues(primary: string[], fallback: string[], limit: number): string[] {
  const merged: string[] = [];
  const seen = new Set<string>();
  for (const value of [...primary, ...fallback]) {
    if (seen.has(value)) continue;
    seen.add(value);
    merged.push(value);
    if (merged.length >= limit) break;
  }
  return merged;
}

function mergeMcpConnections(
  primary: PreferredMcpConnectionSnapshot[],
  fallback: PreferredMcpConnectionSnapshot[],
): PreferredMcpConnectionSnapshot[] {
  const merged: PreferredMcpConnectionSnapshot[] = [];
  const seen = new Set<string>();
  for (const connection of [...primary, ...fallback]) {
    if (seen.has(connection.server_id)) continue;
    seen.add(connection.server_id);
    merged.push({ ...connection });
    if (merged.length >= MAX_SELECTED_MCP_SERVERS) break;
  }
  return merged;
}

export function emptyTurnPreferenceDraft(): TurnPreferenceDraft {
  return { skillKeys: [], mcpConnections: [], revision: 0 };
}

export function restoreFailedTurnPreferenceDraft(
  previous: Record<string, TurnPreferenceDraft>,
  originSessionKey: string,
  snapshot: TurnPreferenceDraft,
  clearedRevision: number,
): Record<string, TurnPreferenceDraft> {
  const current = previous[originSessionKey] || emptyTurnPreferenceDraft();
  const exactRestore = current.revision === clearedRevision;
  return {
    ...previous,
    [originSessionKey]: {
      skillKeys: mergeValues(
        exactRestore ? snapshot.skillKeys : current.skillKeys,
        exactRestore ? [] : snapshot.skillKeys,
        MAX_SELECTED_SKILLS,
      ),
      mcpConnections: mergeMcpConnections(
        exactRestore ? snapshot.mcpConnections : current.mcpConnections,
        exactRestore ? [] : snapshot.mcpConnections,
      ),
      revision: current.revision + 1,
    },
  };
}
