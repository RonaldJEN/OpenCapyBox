import { describe, expect, it } from 'vitest';
import { selectLatestActivityKeys, type LatestActivityEntry } from '../../transcript/latestActivity';

const text = (key: string, step: number, interrupted = false): LatestActivityEntry => ({
  key, step, process: true, kind: 'text', node: { interrupted },
});
const tool = (key: string, step: number, status = 'completed'): LatestActivityEntry => ({
  key, step, process: true, kind: 'tool', item: { status },
});

describe('latest activity preview', () => {
  it('replaces earlier progress and tool batches, keeping the whole latest parallel batch', () => {
    const entries = [
      text('initial', 1), tool('old-search', 1), text('progress', 2),
      tool('previous-batch', 2), tool('current-a', 3), tool('current-b', 3, 'running'),
    ];
    const snapshot = structuredClone(entries);
    entries.forEach(Object.freeze);
    Object.freeze(entries);
    expect([...selectLatestActivityKeys(entries)]).toEqual(['progress', 'current-a', 'current-b']);
    expect(entries).toEqual(snapshot);
  });

  it('uses message order to keep completed tools before a new paragraph out of its preview', () => {
    expect([...selectLatestActivityKeys([
      text('first', 1), tool('before-progress', 1), text('latest', 1), tool('after-progress', 1),
    ])]).toEqual(['latest', 'after-progress']);
    expect([...selectLatestActivityKeys([
      text('first', 1), tool('finished', 1), text('latest', 2),
    ])]).toEqual(['latest']);
  });

  it('retains older running tools but lets missing historical results leave the preview', () => {
    expect([...selectLatestActivityKeys([
      tool('still-running', 1, 'running'), tool('old-unknown', 1, 'unknown'),
      text('latest', 2), tool('current-unknown', 2, 'unknown'), tool('current-failed', 2, 'failed'),
    ])]).toEqual(['still-running', 'latest', 'current-unknown', 'current-failed']);
  });

  it('shows the latest tool batch when no progress text has arrived', () => {
    expect([...selectLatestActivityKeys([
      tool('old', 1), tool('a', 2), tool('b', 2, 'running'),
    ])]).toEqual(['a', 'b']);
    expect([...selectLatestActivityKeys([])]).toEqual([]);
  });

  it('does not treat interrupted text, answers or attachments as new progress', () => {
    expect([...selectLatestActivityKeys([
      text('accepted-progress', 1), tool('current', 1), text('failed-attempt', 2, true),
      { ...text('answer', 3), process: false },
      { key: 'file', kind: 'file', step: 4, process: true },
      { ...tool('not-process', 5, 'running'), process: false },
    ])]).toEqual(['accepted-progress', 'current']);
  });
});
