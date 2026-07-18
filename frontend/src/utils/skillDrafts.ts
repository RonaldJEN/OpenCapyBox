export interface SkillDraft {
  keys: string[];
  revision: number;
}

export const MAX_SELECTED_SKILLS = 50;

function mergeSkillKeys(primary: string[], fallback: string[]): string[] {
  const merged: string[] = [];
  const seen = new Set<string>();
  for (const key of [...primary, ...fallback]) {
    if (seen.has(key)) continue;
    seen.add(key);
    merged.push(key);
    if (merged.length >= MAX_SELECTED_SKILLS) break;
  }
  return merged;
}

export function restoreFailedSkillDraft(
  previous: Record<string, SkillDraft>,
  originSessionKey: string,
  snapshot: SkillDraft,
  clearedRevision: number,
): Record<string, SkillDraft> {
  const current = previous[originSessionKey] || { keys: [], revision: 0 };
  if (current.revision === clearedRevision) {
    return {
      ...previous,
      [originSessionKey]: {
        keys: mergeSkillKeys(snapshot.keys, []),
        revision: current.revision + 1,
      },
    };
  }

  return {
    ...previous,
    [originSessionKey]: {
      keys: mergeSkillKeys(current.keys, snapshot.keys),
      revision: current.revision + 1,
    },
  };
}
