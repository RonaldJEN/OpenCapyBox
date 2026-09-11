import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
import { Round } from '../../components/Round';

vi.mock('../../components/AssistantMarkdown', () => ({
  AssistantMarkdown: ({ content }: { content: string }) => {
    if (content === '<broken-render>') throw new Error('fixture render failure');
    return <p>{content}</p>;
  },
}));

describe('message error isolation', () => {
  it('falls back to escaped raw text for one message while keeping other text, files and copy', async () => {
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    try {
      render(<Round round={{ round_id: 'r1', user_message: 'question', status: 'completed', created_at: '', step_count: 2,
        final_response: '<broken-render>\n\n正常正文', final_message_ids: ['a', 'b'],
        assistant_messages: ['<broken-render>', '正常正文'].map((content, i) => ({
          message_id: i === 0 ? 'a' : 'b', content, phase: 'final_answer', state: 'complete', step_number: i + 1, content_committed: true,
        })), steps: ['<broken-render>', '正常正文'].map((text, i) => ({
          step_number: i + 1, assistant_content: text, status: 'completed', tool_calls: [], tool_results: [],
        })), assistant_file_references: [{ source: 'session', session_id: 's1', ref_id: 'file1', path: 'manual.md', name: 'manual.md', size: 1, revision: '1', modified: '', type: 'md' }],
      }} />);
      expect(screen.getByText('<broken-render>')).toBeInTheDocument();
      expect(document.querySelector('broken-render')).toBeNull();
      expect(screen.getByText('正常正文')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '打开 manual.md' })).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: '复制回复' }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith('<broken-render>\n\n正常正文'));
    } finally { errorLog.mockRestore(); }
  });
});
