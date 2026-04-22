import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '../utils/test-utils';
import { ChatInput } from '../../components/ChatInput';

describe('ChatInput drag/drop behavior', () => {
  it('drop on input should not bubble to parent and should invoke upload + handled callback once', () => {
    const onFileUpload = vi.fn();
    const onParentDrop = vi.fn();
    const onInputDropHandled = vi.fn();

    render(
      <div onDrop={onParentDrop}>
        <ChatInput
          value=""
          onChange={() => {}}
          onSend={() => {}}
          onFileUpload={onFileUpload}
          onInputDropHandled={onInputDropHandled}
        />
      </div>
    );

    const textbox = screen.getByPlaceholderText('输入消息...');
    const dropTarget = textbox.parentElement as HTMLElement;
    const file = new File(['demo'], 'demo.txt', { type: 'text/plain' });

    fireEvent.drop(dropTarget, {
      dataTransfer: {
        files: [file],
        types: ['Files'],
      },
    });

    expect(onFileUpload).toHaveBeenCalledTimes(1);
    expect(onInputDropHandled).toHaveBeenCalledTimes(1);
    expect(onParentDrop).not.toHaveBeenCalled();
  });

  it('clearing long text should hide textarea scrollbar and reset scrollTop', () => {
    const { rerender } = render(
      <ChatInput
        value=""
        onChange={() => {}}
        onSend={() => {}}
      />
    );

    const textarea = screen.getByPlaceholderText('输入消息...') as HTMLTextAreaElement;
    let mockScrollHeight = 360;

    Object.defineProperty(textarea, 'scrollHeight', {
      configurable: true,
      get: () => mockScrollHeight,
    });

    rerender(
      <ChatInput
        value={'x'.repeat(600)}
        onChange={() => {}}
        onSend={() => {}}
      />
    );

    expect(textarea.style.height).toBe('200px');
    expect(textarea.style.overflowY).toBe('auto');

    textarea.scrollTop = 140;
    mockScrollHeight = 42;

    rerender(
      <ChatInput
        value=""
        onChange={() => {}}
        onSend={() => {}}
      />
    );

    expect(textarea.style.height).toBe('42px');
    expect(textarea.style.overflowY).toBe('hidden');
    expect(textarea.scrollTop).toBe(0);
  });
});
