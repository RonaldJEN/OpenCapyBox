import { describe, expect, it } from 'vitest';
import { projectRoundTranscript } from '../../transcript/projectRoundTranscript';
import type { RoundData } from '../../types';

const makeRound = (texts: string[], extra: Partial<RoundData> = {}): RoundData => ({
  round_id: 'r1', status: 'completed', user_message: 'question', created_at: '',
  final_response: '', step_count: texts.length,
  steps: texts.map((text, i) => ({ step_number: i + 1, assistant_content: text, tool_calls: [], tool_results: [], status: 'completed' })),
  ...extra,
});

describe('legacy transcript projection', () => {
  it('preserves exact bytes and equal text in separate steps; only aliases the final step', () => {
    const text = '  相同文字\n\n表格：ＡＢＣ\n';
    const result = projectRoundTranscript(makeRound([text, text], { final_response: text }));
    expect(result.nodes.map((n) => n.text)).toEqual([text, text]);
    expect(result.copyText).toBe(text);
    expect(result.nodes[0].id).not.toBe(result.nodes[1].id);
  });
  it('does not remove partially overlapping finals or re-key an existing step', () => {
    const round = makeRound(['前半段'], { status: 'running', idempotency_key: 'stable' });
    const before = projectRoundTranscript(round);
    const after = projectRoundTranscript({ ...round, round_id: 'server-r1', status: 'completed', final_response: '前半段加后半段' });
    expect(after.nodes[0].id).toBe(before.nodes[0].id);
    expect(after.nodes.map((n) => n.text)).toEqual(['前半段', '前半段加后半段']);
    expect(before.copyText).toBe('');
    expect(after.copyText).toBe('前半段加后半段');
  });
  it('separates proven errors and unknown legacy endings from reply copy', () => {
    const round = makeRound(['有效正文'], { status: 'failed', final_response: 'provider unavailable' });
    const unknown = projectRoundTranscript(round);
    expect(unknown.nodes.map(node => node.text)).toEqual(['有效正文']);
    expect(unknown.copyText).toBe('');
    expect(unknown.notice?.label).toBe('运行结束说明（旧版记录）');
    const known = projectRoundTranscript({ ...round, terminal_presentation: { final_response_origin: 'run_error', error: { source: 'durable_run_error', message: 'provider unavailable' } } });
    expect(known.nodes.map(node => node.text)).toEqual(['有效正文']);
    expect(known.copyText).toBe('');
    expect(known.notice).toBeUndefined();
    expect(known.error).toBe('provider unavailable');
  });
  it('never deletes text-event content equal to a system error or cancellation sentinel', () => {
    const round = makeRound(['Failed: unavailable', 'Cancelled'], { status: 'cancelled', final_response: 'Cancelled', terminal_presentation: { final_response_origin: 'system_notice' } });
    round.steps.forEach((step) => { step.assistant_content_source = 'text_message'; });
    const result = projectRoundTranscript(round);
    expect(result.nodes.map(node => node.text)).toEqual(['Failed: unavailable', 'Cancelled']);
    expect(result.copyText).toBe('');
  });
  it('keeps a proven assistant final even when a later error exists', () => {
    const round = makeRound([], { status: 'failed', final_response: '真实回答', terminal_presentation: {
      final_response_origin: 'assistant', error: { source: 'durable_run_error', message: '真实回答' },
    } });
    expect(projectRoundTranscript(round).copyText).toBe('真实回答');
  });
});

describe('answer copy projection', () => {
  const message = (message_id: string, content: string, extra: Partial<NonNullable<RoundData['assistant_messages']>[number]> = {}) => ({
    message_id, content, step_number: 1, state: 'complete' as const, content_committed: true, ...extra,
  });

  it.each([false, true])('preserves distinct equal final messages without copying commentary (native phases: %s)', (native) => {
    const text = '  正式答复\n';
    const result = projectRoundTranscript(makeRound([], {
      final_response: `${text}\n\n${text}`, final_message_ids: ['a', 'b'],
      assistant_messages: [
        message('progress', '我先核对资料。', { phase: 'commentary' }),
        message('a', text, { phase: native ? 'final_answer' : undefined }),
        message('b', text, { phase: native ? 'final_answer' : undefined }),
      ],
    }));
    expect(result.nodes).toHaveLength(3);
    expect(result.answerNodes.map(node => node.messageId)).toEqual(['a', 'b']);
    expect(result.copyText).toBe(`${text}\n\n${text}`);
  });

  it('copies a native answer while leaving the live commentary tail out', () => {
    const result = projectRoundTranscript(makeRound([], { status: 'running', assistant_messages: [
      message('progress', '我先核对资料。', { phase: 'commentary' }),
      message('a', '正式答复已输出的部分。', { phase: 'final_answer', state: 'streaming' }),
    ] }), true);
    expect(result.nodes).toHaveLength(2);
    expect(result.copyText).toBe('正式答复已输出的部分。');
  });

  it('includes the confirmed delivery body and final supplement, never a commentary in the same step', () => {
    const round = makeRound([], {
      final_message_id: 'supplement',
      steps: [{ step_number: 1, status: 'completed', assistant_content: '',
        tool_calls: [{ id: 'present', name: 'present_files', input: {} }], tool_results: [] }],
      assistant_messages: [
        message('progress', '我先生成文件。', { phase: 'commentary' }),
        message('body', '交付正文。'),
        message('supplement', '补充说明。', { step_number: 2 }),
      ],
      assistant_file_references: [{ ref_id: 'file', source: 'session', session_id: 's1', name: 'report.md',
        path: 'report.md', size: 1, type: 'md', modified: '', revision: '1', operation: 'PRESENTED', tool_call_id: 'present' }],
    });
    expect(projectRoundTranscript(round).copyText).toBe('交付正文。\n\n补充说明。');
    expect(projectRoundTranscript({ ...round, assistant_file_references: round.assistant_file_references!.map(file => ({ ...file, operation: 'UPDATED' })) }).copyText).toBe('补充说明。');
  });

  it('uses the full final fallback once when only some final aliases have been restored', () => {
    const result = projectRoundTranscript(makeRound([], {
      final_response: '主体。\n\n补充。', final_message_ids: ['a', 'b'],
      assistant_messages: [message('a', '主体。', { phase: 'final_answer' })],
    }));
    expect(result.nodes.map(node => node.text)).toContain('主体。');
    expect(result.copyText).toBe('主体。\n\n补充。');
  });

  it('excludes interrupted attempts even when an alias identifies one as final', () => {
    const result = projectRoundTranscript(makeRound([], {
      final_response: '成功答复。', final_message_ids: ['old', 'answer'],
      assistant_messages: [
        message('old', '旧的失败尝试。', { phase: 'final_answer', state: 'interrupted' }),
        message('answer', '成功答复。'),
      ],
    }));
    expect(result.nodes.map(node => node.text)).toEqual(['旧的失败尝试。', '成功答复。']);
    expect(result.copyText).toBe('成功答复。');
  });
});
