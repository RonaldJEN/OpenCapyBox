import { beforeEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '../utils/test-utils';
import { InlineRoundTranscript } from '../../components/InlineRoundTranscript';
import type { RoundData } from '../../types';

const round: RoundData = { round_id: 'inline', user_message: '查文档', final_response: '', created_at: '', status: 'running', step_count: 1,
  started_at_ts: 1000, steps: [{ step_number: 1, status: 'completed', thinking: 'DO NOT DISPLAY REASONING', assistant_content: '',
    tool_calls: [{ id: 't1', name: 'bash', input: { command: 'git status' }, sequence: 3 }],
    tool_results: [{ tool_call_id: 't1', content: 'clean', success: true }] }],
  assistant_messages: [
    { message_id: 'm1', content: '我先核对官方文档，然后查看当前分支。', state: 'complete', step_number: 1, first_sequence: 1, content_committed: true },
    { message_id: 'm2', content: '文档已核对，接下来整理结论。', state: 'complete', step_number: 1, first_sequence: 6, content_committed: true },
    { message_id: 'm3', content: '最终结论。', state: 'streaming', step_number: 1, first_sequence: 9, content_committed: true },
  ] };
const props = { renderFile: () => null };
const done = { ...round, status: 'completed', finished_at_ts: 36000, final_message_id: 'm3' };
beforeEach(() => sessionStorage.clear());

describe('inline process', () => {
  it('keeps explicitly presented delivery text visible alongside a later final supplement', () => {
    const delivered: RoundData = { ...done,
      steps: [round.steps[0], { ...round.steps[0], step_number: 2, tool_calls: [{ id: 'present', name: 'present_files', input: { paths: ['report.md'] }, sequence: 7 }] }],
      assistant_messages: done.assistant_messages!.map((message, i) => ({ ...message, step_number: i + 1 })),
      assistant_file_references: [{ ref_id: 'report', source: 'session', session_id: 's1', name: 'report.md', path: 'report.md', size: 1, type: 'md', modified: '', revision: '1', operation: 'PRESENTED', tool_call_id: 'present' }],
    };
    const view = render(<InlineRoundTranscript round={delivered} streaming={false} {...props} />);
    expect(screen.getByRole('button', { name: '处理了 35 秒' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('文档已核对，接下来整理结论。')).toBeVisible();
    expect(screen.getByText('最终结论。')).toBeVisible();
    expect(screen.queryByText('我先核对官方文档，然后查看当前分支。')).not.toBeInTheDocument();
    view.rerender(<InlineRoundTranscript round={{ ...delivered, assistant_file_references: delivered.assistant_file_references!.map(file => ({ ...file, operation: 'UPDATED' })) }} streaming={false} {...props} />);
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
  });

  it('collapses on native final-answer START, preserves all answer messages, and does not end the run', () => {
    const working: RoundData = { ...round, assistant_messages: round.assistant_messages!.slice(0, 2).map(message => ({ ...message, phase: 'commentary' as const })) };
    const view = render(<InlineRoundTranscript round={working} streaming {...props} />);
    const answering: RoundData = { ...working, assistant_messages: [...working.assistant_messages!, { ...round.assistant_messages![2], phase: 'final_answer', content: '' }] };
    view.rerender(<InlineRoundTranscript round={answering} streaming {...props} />);
    expect(screen.getByRole('button', { name: '正在回答' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('我先核对官方文档，然后查看当前分支。')).not.toBeInTheDocument();
    const answerMessages = [...answering.assistant_messages!.slice(0, 2),
      { ...answering.assistant_messages![2], content: '正式答复主体。', state: 'complete' as const },
      { ...answering.assistant_messages![2], message_id: 'm4', content: '正式答复补充。', first_sequence: 12 }];
    view.rerender(<InlineRoundTranscript round={{ ...answering, assistant_messages: answerMessages }} streaming {...props} />);
    const main = screen.getByText('正式答复主体。');
    expect(screen.getByText('正式答复补充。')).toBeVisible();
    view.rerender(<InlineRoundTranscript round={{ ...answering, status: 'completed', final_response: '正式答复主体。\n\n正式答复补充。', final_message_ids: ['m3', 'm4'], finished_at_ts: 36000, assistant_messages: answerMessages }} streaming={false} {...props} />);
    expect(screen.getByText('正式答复主体。')).toBe(main);
    expect(screen.getByText('正式答复补充。')).toBeVisible();
  });

  it('keeps an interrupted native answer in the process when a valid answer replaces the attempt', () => {
    render(<InlineRoundTranscript round={{ ...done, assistant_messages: [
      { ...round.assistant_messages![0], phase: 'final_answer', state: 'interrupted', content: '未完成的失败尝试。' },
      { ...round.assistant_messages![2], phase: 'final_answer', state: 'complete' },
    ] }} streaming={false} {...props} />);
    expect(screen.queryByText('未完成的失败尝试。')).not.toBeInTheDocument();
    expect(screen.getByText('最终结论。')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '处理了 35 秒' }));
    expect(screen.getByText('未完成的失败尝试。')).toBeVisible();
    expect(screen.getByText('本段输出已中断')).toBeVisible();
  });

  it('defaults to the latest progress and keeps manually opened history available as new progress arrives', () => {
    const onInspectProcess = vi.fn(() => {
      // Inform the scroll owner before mounting older content changes the layout.
      expect(screen.queryByText('我先核对官方文档，然后查看当前分支。')).not.toBeInTheDocument();
    });
    const view = render(<InlineRoundTranscript round={round} streaming onInspectProcess={onInspectProcess} {...props} />);
    const { container } = view;
    const rows = container.querySelector('.chat-transcript-rows')!;
    expect(screen.getByText('最终结论。')).toBeVisible();
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '已执行 git status' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '运行中' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: /查看此前过程|收起此前过程/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '运行中' }));
    expect(onInspectProcess).toHaveBeenCalledTimes(1);
    expect([...rows.children].map((row) => row.textContent)).toEqual([
      '我先核对官方文档，然后查看当前分支。', '已执行git status', '文档已核对，接下来整理结论。', '最终结论。',
    ]);
    expect(screen.getByText('我先核对官方文档，然后查看当前分支。').closest('button')).toBeNull();
    expect(screen.queryByText('DO NOT DISPLAY REASONING')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '已执行 git status' })).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(screen.getByRole('button', { name: '已执行 git status' }));
    expect(screen.getByText('Shell')).toBeVisible();
    expect(screen.getByText('clean')).toBeVisible();

    view.rerender(<InlineRoundTranscript round={{ ...round, assistant_messages: [...round.assistant_messages!, {
      ...round.assistant_messages![2], message_id: 'm4', content: '新到达的进展。', first_sequence: 12,
    }] }} streaming onInspectProcess={onInspectProcess} {...props} />);
    expect(screen.getByRole('button', { name: '运行中' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('我先核对官方文档，然后查看当前分支。')).toBeVisible();
    expect(screen.getByText('clean')).toBeVisible();
    expect(screen.getByText('新到达的进展。')).toBeVisible();
    expect(onInspectProcess).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '运行中' }));
    expect(screen.queryByText('最终结论。')).not.toBeInTheDocument();
    expect(screen.getByText('新到达的进展。')).toBeVisible();
  });
  it('automatically collapses settled progress and preserves the final DOM node', () => {
    const view = render(<InlineRoundTranscript round={round} streaming {...props} />);
    const final = screen.getByText('最终结论。');
    view.rerender(<InlineRoundTranscript round={{ ...round, assistant_file_references: [] }} streaming onOpenFile={() => undefined} {...props} />);
    expect(screen.getByText('最终结论。')).toBe(final);
    view.rerender(<InlineRoundTranscript round={done} streaming={false} {...props} />);
    expect(screen.getByText('最终结论。')).toBe(final);
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '处理了 35 秒' }));
    expect(screen.getByText('文档已核对，接下来整理结论。')).toBeVisible();
    expect(screen.queryByText('DO NOT DISPLAY REASONING')).not.toBeInTheDocument();
  });
  it('collapses on completion after manual expansion and a focused tool, then permits reopening', () => {
    const reveal = { messageId: 'm2', nonce: 1 };
    const view = render(<InlineRoundTranscript round={round} streaming reveal={reveal} {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '运行中' }));
    fireEvent.click(screen.getByRole('button', { name: '运行中' }));
    const tool = screen.getByRole('button', { name: '已执行 git status' });
    fireEvent.click(tool);
    tool.focus();
    expect(tool).toHaveFocus();

    view.rerender(<InlineRoundTranscript round={done} streaming={false} reveal={reveal} {...props} />);
    const summary = screen.getByRole('button', { name: '处理了 35 秒' });
    expect(summary).toHaveAttribute('aria-expanded', 'false');
    expect(summary).toHaveFocus();
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
    expect(screen.getByText('最终结论。')).toBeVisible();
    fireEvent.click(summary);
    expect(screen.getByText('文档已核对，接下来整理结论。')).toBeVisible();
    expect(screen.getByText('clean')).toBeVisible();
  });
  it('keeps the latest progress with its current tool batch without accumulating finished batches', () => {
    const view = render(<InlineRoundTranscript round={round} streaming {...props} />);
    const tail = screen.getByText('最终结论。');
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
    expect(tail).toBeVisible();
    expect(round.final_message_id).toBeUndefined();

    const nextTool: RoundData = { ...round, steps: [...round.steps, {
      step_number: 2, status: 'streaming', thinking: '', assistant_content: '',
      tool_calls: [{ id: 't2', name: 'bash', input: { command: 'git diff' }, sequence: 12 }], tool_results: [],
    }] };
    view.rerender(<InlineRoundTranscript round={nextTool} streaming {...props} />);
    expect(screen.getByText('最终结论。')).toBe(tail);
    expect(screen.getByRole('button', { name: '正在执行 git diff' })).toBeVisible();
    expect(screen.queryByRole('button', { name: '已执行 git status' })).not.toBeInTheDocument();

    const latestBatch: RoundData = { ...nextTool, steps: [nextTool.steps[0], {
      ...nextTool.steps[1], status: 'completed', tool_results: [{ tool_call_id: 't2', content: 'diff', success: true }],
    }, {
      step_number: 3, status: 'streaming', thinking: '', assistant_content: '',
      tool_calls: [{ id: 't3', name: 'bash', input: { command: 'git log' }, sequence: 15 },
        { id: 't4', name: 'bash', input: { command: 'git branch' }, sequence: 16 }], tool_results: [],
    }] };
    view.rerender(<InlineRoundTranscript round={latestBatch} streaming {...props} />);
    expect(screen.getByText('最终结论。')).toBe(tail);
    expect(screen.queryByRole('button', { name: '已执行 git diff' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '正在执行 git log' })).toBeVisible();
    expect(screen.getByRole('button', { name: '正在执行 git branch' })).toBeVisible();
  });
  it('does not carry a main process disclosure choice across remounts', () => {
    const view = render(<InlineRoundTranscript round={done} streaming={false} {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '处理了 35 秒' }));
    view.unmount();
    const restored = render(<InlineRoundTranscript round={done} streaming={false} {...props} />);
    expect(screen.getByRole('button', { name: '处理了 35 秒' })).toHaveAttribute('aria-expanded', 'false');
    restored.unmount();
    const running = render(<InlineRoundTranscript round={round} streaming {...props} />);
    fireEvent.click(screen.getByRole('button', { name: '运行中' }));
    running.unmount();
    render(<InlineRoundTranscript round={round} streaming {...props} />);
    expect(screen.getByRole('button', { name: '运行中' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
  });
  it('does not replay a running-time search when its visible tail is archived by a terminal snapshot', () => {
    const reveal = { messageId: 'm2', nonce: 1 };
    const earlier = { ...round, assistant_messages: round.assistant_messages!.slice(0, 2) };
    const view = render(<InlineRoundTranscript round={earlier} streaming reveal={reveal} {...props} />);
    expect(screen.getByText('文档已核对，接下来整理结论。')).toBeVisible();
    view.rerender(<InlineRoundTranscript round={done} streaming={false} reveal={reveal} {...props} />);
    expect(screen.getByRole('button', { name: '处理了 35 秒' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByText('最终结论。')).toBeVisible();
  });
  it('reveals a searched progress message and allows subsequent manual collapse', () => {
    const view = render(<InlineRoundTranscript round={done} streaming={false} {...props} />);
    view.rerender(<InlineRoundTranscript round={done} streaming={false} reveal={{ messageId: 'm2', nonce: 1 }} {...props} />);
    expect(screen.getByText('文档已核对，接下来整理结论。')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '处理了 35 秒' }));
    expect(screen.queryByText('文档已核对，接下来整理结论。')).not.toBeInTheDocument();
    expect(screen.getByText('最终结论。')).toBeVisible();
    view.rerender(<InlineRoundTranscript round={done} streaming={false} reveal={{ messageId: 'm2', nonce: 1 }} {...props} />);
    expect(screen.getByRole('button', { name: '处理了 35 秒' })).toHaveAttribute('aria-expanded', 'false');
    view.rerender(<InlineRoundTranscript round={done} streaming={false} reveal={{ messageId: 'm2', nonce: 2 }} {...props} />);
    expect(screen.getByText('文档已核对，接下来整理结论。')).toBeVisible();
  });
});
