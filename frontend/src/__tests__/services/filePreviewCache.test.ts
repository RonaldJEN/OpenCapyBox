import { beforeEach, describe, expect, it } from 'vitest';

import {
  invalidateWorkspacePreviewCache,
  readFilePreviewCache,
  resetFilePreviewCacheForTests,
  writeFilePreviewCache,
} from '../../services/filePreviewCache';

describe('filePreviewCache Workspace tombstone', () => {
  beforeEach(() => resetFilePreviewCacheForTests());

  it('只淘汰命中 entry_id 的预览正文', () => {
    writeFilePreviewCache('entry-a-version-1', 'a', 'entry-a');
    writeFilePreviewCache('entry-b-version-1', 'b', 'entry-b');

    invalidateWorkspacePreviewCache(['entry-a']);

    expect(readFilePreviewCache('entry-a-version-1')).toBeNull();
    expect(readFilePreviewCache('entry-b-version-1')).toBe('b');
  });
});
