import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import { Round } from '../../components/Round';
import type { RoundData } from '../../types';
beforeEach(() => sessionStorage.clear());


const roundWith = (texts: string[], overrides: Partial<RoundData> = {}): RoundData => ({
  round_id: 'transcript-round', user_message: '看看体检通知', status: 'completed',
  created_at: '2026-09-10T08:00:00Z', step_count: texts.length,
  final_response: texts[texts.length - 1] || '',
  steps: texts.map((text, i) => ({
    step_number: i + 1, assistant_content: text, status: 'completed',
    tool_calls: i === 0 ? [{ id: 'present-1', name: 'present_files', input: {} }] : [],
    tool_results: [],
  })),
  ...overrides,
});
const files: RoundData['assistant_file_references'] = [{
  ref_id: 'manual', source: 'session', session_id: 's1', name: '手册.pdf',
  path: '手册.pdf', size: 100, modified: '', type: 'application/pdf', revision: '1',
}];

describe('Round transcript', () => {
  it('keeps progress available but copies only the final answer regardless of process disclosure', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const texts = ['体检通知关键事项……', '以上就是全部要点。'];
    const round = roundWith(texts, { status: 'running', final_response: '', assistant_file_references: files });
    const view = render(<Round round={round} isStreaming />);
    expect(screen.queryByText(texts[0])).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '运行中' }));
    expect(screen.getByText(texts[0])).toBeVisible();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
    const final = screen.getByText(texts[1]);
    view.rerender(<Round round={{ ...round, status: 'completed', final_response: texts[1] }} />);
    expect(screen.queryByText(texts[0])).not.toBeInTheDocument();
    expect(screen.getByText(texts[1])).toBe(final);
    fireEvent.click(screen.getByRole('button', { name: '复制回复' }));
    await waitFor(() => expect(writeText).toHaveBeenNthCalledWith(1, texts[1]));
    fireEvent.click(screen.getByRole('button', { name: '处理过程' }));
    expect(screen.getByText(texts[0])).toBeVisible();
    expect(screen.getAllByText(texts[1])).toHaveLength(1);
    expect(screen.getByRole('button', { name: '打开 手册.pdf' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '复制回复' }));
    await waitFor(() => expect(writeText).toHaveBeenNthCalledWith(2, texts[1]));
    fireEvent.click(screen.getByRole('button', { name: '处理过程' }));
    expect(screen.queryByText(texts[0])).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '复制回复' }));
    await waitFor(() => expect(writeText).toHaveBeenNthCalledWith(3, texts[1]));
  });

  it('shows an artifact-only delivery independently of text', () => {
    render(<Round round={roundWith([], { assistant_file_references: files })} />);
    expect(screen.getByRole('button', { name: '打开 手册.pdf' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
  });

  it.each(['running', 'completed', 'failed', 'cancelled', 'max_steps_reached'])('keeps unclassified partial text without exposing reply copy (%s)', (status) => {
    render(<Round round={roundWith(['有效正文'], { status, final_response: null })} />);
    expect(screen.getByText('有效正文')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
  });

  it('gives independent Markdown messages unique reading anchors', () => {
    const { container } = render(<Round round={roundWith(['第一条正文', '第二条正文'])} />);
    fireEvent.click(screen.getByRole('button', { name: '处理过程' }));
    const first = screen.getByText('第一条正文').getAttribute('data-reading-block');
    const second = screen.getByText('第二条正文').getAttribute('data-reading-block');
    expect(first).toBeTruthy();
    expect(second).not.toBe(first);
    const ids = [...container.querySelectorAll('[data-reading-block]')].map((el) => el.getAttribute('data-reading-block'));
    expect(new Set(ids).size).toBe(ids.length);
  });
});
