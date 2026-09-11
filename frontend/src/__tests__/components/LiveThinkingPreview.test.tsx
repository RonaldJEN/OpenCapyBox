import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '../utils/test-utils';
import { LiveThinkingPreview } from '../../components/LiveThinkingPreview';
import { RunStatusBar } from '../../components/RunStatusBar';
import { emptyBuffers, type ChatRunRuntimeState } from '../../runtime/chatRuntimeTypes';
import type { RoundData } from '../../types';

afterEach(() => { vi.useRealTimers(); });

describe('live thinking status preview', () => {
  it('samples continuous tokens without debounce starvation and bounds the displayed tail', () => {
    vi.useFakeTimers();
    const view = render(<LiveThinkingPreview text="开头" />);
    for (let n = 1; n <= 5; n++) {
      view.rerender(<LiveThinkingPreview text={`新内容${n}`} />);
      act(() => vi.advanceTimersByTime(20));
    }
    expect(screen.getByText('新内容5')).toBeInTheDocument();
    view.rerender(<LiveThinkingPreview text={'旧'.repeat(500) + '\n最新的中文片段'} />);
    act(() => vi.advanceTimersByTime(100));
    expect(view.container.textContent?.length).toBeLessThanOrEqual(180);
    expect(view.container.textContent).toContain('最新的中文片段');
    view.unmount();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps the summary accessible and removes old text immediately when its live segment ends', () => {
    vi.useFakeTimers();
    const round: RoundData = { round_id: 'r', status: 'running', user_message: '', created_at: '', final_response: '', steps: [], step_count: 0 };
    const run: ChatRunRuntimeState = { clientRunKey: 'r', ownerSessionId: 's', tempRoundId: 'r', source: 'direct', status: 'streaming',
      lastSequence: 1, createdAt: 0, updatedAt: 0, buffers: { ...emptyBuffers(), currentThinkingMessageId: 't1',
        thinkingByMessageId: { t1: '真实思考片段' }, thinkingSegmentStateByMessageId: { t1: { open: true, dirty: true } } } };
    const view = render(<RunStatusBar round={round} run={run} expanded={false} onToggle={() => undefined} />);
    expect(screen.getByRole('button', { name: '思考中' })).toBeInTheDocument();
    expect(screen.getByText('真实思考片段').closest('[aria-hidden="true"]')).not.toBeNull();
    view.rerender(<RunStatusBar round={round} run={{ ...run, buffers: { ...run.buffers, currentThinkingMessageId: null } }} expanded={false} onToggle={() => undefined} />);
    expect(screen.queryByText('真实思考片段')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行中' })).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });
});
