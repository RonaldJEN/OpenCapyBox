import { describe, expect, it } from 'vitest';

import {
  MAX_SELECTED_SKILLS,
  restoreFailedSkillDraft,
} from '../../utils/skillDrafts';

describe('restoreFailedSkillDraft', () => {
  it('restores the exact snapshot when the cleared draft was not edited', () => {
    const restored = restoreFailedSkillDraft(
      { 'session-a': { keys: [], revision: 4 } },
      'session-a',
      { keys: ['pdf'], revision: 3 },
      4,
    );
    expect(restored['session-a']).toEqual({ keys: ['pdf'], revision: 5 });
  });

  it('preserves new edits and appends the failed snapshot without duplicates', () => {
    const restored = restoreFailedSkillDraft(
      { 'session-a': { keys: ['data', 'pdf'], revision: 5 } },
      'session-a',
      { keys: ['pdf', 'docs'], revision: 3 },
      4,
    );
    expect(restored['session-a']).toEqual({
      keys: ['data', 'pdf', 'docs'],
      revision: 6,
    });
  });

  it('keeps current edits first and never restores more than the selection limit', () => {
    const currentKeys = Array.from({ length: 30 }, (_, index) => `current-${index}`);
    const snapshotKeys = Array.from({ length: 30 }, (_, index) => `snapshot-${index}`);
    const restored = restoreFailedSkillDraft(
      { 'session-a': { keys: currentKeys, revision: 5 } },
      'session-a',
      { keys: snapshotKeys, revision: 3 },
      4,
    );

    expect(restored['session-a'].keys).toHaveLength(MAX_SELECTED_SKILLS);
    expect(restored['session-a'].keys.slice(0, currentKeys.length)).toEqual(currentKeys);
    expect(restored['session-a'].keys.slice(currentKeys.length)).toEqual(snapshotKeys.slice(0, 20));
  });

  it('deduplicates and caps an exact snapshot restore defensively', () => {
    const snapshotKeys = [
      'duplicate',
      'duplicate',
      ...Array.from({ length: 60 }, (_, index) => `skill-${index}`),
    ];
    const restored = restoreFailedSkillDraft(
      { 'session-a': { keys: [], revision: 4 } },
      'session-a',
      { keys: snapshotKeys, revision: 3 },
      4,
    );

    expect(restored['session-a'].keys).toHaveLength(MAX_SELECTED_SKILLS);
    expect(restored['session-a'].keys[0]).toBe('duplicate');
    expect(new Set(restored['session-a'].keys).size).toBe(MAX_SELECTED_SKILLS);
  });
});
