import { describe, expect, it } from 'vitest';

import {
  MAX_SELECTED_MCP_SERVERS,
  MAX_SELECTED_SKILLS,
  restoreFailedTurnPreferenceDraft,
} from '../../utils/turnPreferenceDrafts';

describe('restoreFailedTurnPreferenceDraft', () => {
  const connection = (serverId: string) => ({
    server_id: serverId,
    display_name: `连接 ${serverId}`,
  });

  it('restores the exact Skill and MCP snapshot when the cleared draft was not edited', () => {
    const restored = restoreFailedTurnPreferenceDraft(
      { 'session-a': { skillKeys: [], mcpConnections: [], revision: 4 } },
      'session-a',
      { skillKeys: ['pdf'], mcpConnections: [connection('server-a')], revision: 3 },
      4,
    );
    expect(restored['session-a']).toEqual({
      skillKeys: ['pdf'],
      mcpConnections: [connection('server-a')],
      revision: 5,
    });
  });

  it('preserves new edits and appends failed snapshots without duplicates', () => {
    const restored = restoreFailedTurnPreferenceDraft(
      {
        'session-a': {
          skillKeys: ['data', 'pdf'],
          mcpConnections: [connection('server-b'), connection('server-a')],
          revision: 5,
        },
      },
      'session-a',
      {
        skillKeys: ['pdf', 'docs'],
        mcpConnections: [connection('server-a'), connection('server-c')],
        revision: 3,
      },
      4,
    );
    expect(restored['session-a']).toEqual({
      skillKeys: ['data', 'pdf', 'docs'],
      mcpConnections: [
        connection('server-b'),
        connection('server-a'),
        connection('server-c'),
      ],
      revision: 6,
    });
  });

  it('caps the two preference categories independently', () => {
    const restored = restoreFailedTurnPreferenceDraft(
      {
        'session-a': {
          skillKeys: Array.from({ length: 30 }, (_, index) => `current-${index}`),
          mcpConnections: Array.from(
            { length: 12 },
            (_, index) => connection(`current-server-${index}`),
          ),
          revision: 5,
        },
      },
      'session-a',
      {
        skillKeys: Array.from({ length: 30 }, (_, index) => `snapshot-${index}`),
        mcpConnections: Array.from(
          { length: 12 },
          (_, index) => connection(`snapshot-server-${index}`),
        ),
        revision: 3,
      },
      4,
    );

    expect(restored['session-a'].skillKeys).toHaveLength(MAX_SELECTED_SKILLS);
    expect(restored['session-a'].mcpConnections).toHaveLength(MAX_SELECTED_MCP_SERVERS);
  });
});
