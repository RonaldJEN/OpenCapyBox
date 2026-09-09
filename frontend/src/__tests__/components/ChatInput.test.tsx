import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '../utils/test-utils';
import { ChatInput } from '../../components/ChatInput';

describe('ChatInput drag/drop behavior', () => {
  it.each([1000, 1001])('单次粘贴 %i 个 Unicode 字符时，仅超过 1000 才转附件', (length) => {
    const onPasteText = vi.fn();
    const onChange = vi.fn();
    render(<ChatInput value="保留的输入" onChange={onChange} onSend={() => {}} onPasteText={onPasteText} />);

    const text = `${'😀'.repeat(length - 1)}a`;
    const result = fireEvent.paste(screen.getByPlaceholderText('输入消息...'), {
      clipboardData: { items: [], getData: () => text },
    });

    expect(result).toBe(length <= 1000);
    if (length > 1000) expect(onPasteText).toHaveBeenCalledWith(text);
    else expect(onPasteText).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it('IME 组合输入期间按 Enter 不发送', () => {
    const onSend = vi.fn();
    render(<ChatInput value="正在输入" onChange={() => {}} onSend={onSend} />);

    fireEvent.keyDown(screen.getByPlaceholderText('输入消息...'), {
      key: 'Enter',
      isComposing: true,
    });

    expect(onSend).not.toHaveBeenCalled();
  });

  it('显示真实上传进度，并允许失败附件重试和恢复文本', () => {
    const onRetryAttachment = vi.fn();
    const onRestorePastedText = vi.fn();
    const { rerender } = render(
      <ChatInput
        value=""
        onChange={() => {}}
        onSend={() => {}}
        attachedFiles={[{
          clientId: 'uploading-file', name: 'demo.md', path: 'demo.md', size: 12, modified: '', type: 'text/markdown', uploadStatus: 'uploading', uploadProgress: 42,
        }]}
      />,
    );
    expect(screen.getByRole('progressbar', { name: 'demo.md 上传进度' })).toHaveAttribute('aria-valuenow', '42');

    rerender(
      <ChatInput
        value=""
        onChange={() => {}}
        onSend={() => {}}
        attachedFiles={[{
          clientId: 'failed-file', name: 'paste.txt', path: 'paste.txt', size: 12, modified: '', type: 'text/plain', uploadStatus: 'error', pastedText: '原文',
        }]}
        onRetryAttachment={onRetryAttachment}
        onRestorePastedText={onRestorePastedText}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    fireEvent.click(screen.getByRole('button', { name: '恢复文本' }));
    expect(onRetryAttachment).toHaveBeenCalledWith(0);
    expect(onRestorePastedText).toHaveBeenCalledWith(0);
    rerender(<ChatInput value="" onChange={() => {}} onSend={() => {}}
      attachedFiles={[{ clientId: 'ready-file', name: 'paste.txt', path: '', size: 12,
        modified: '', type: 'text/plain', uploadStatus: 'ready', pastedText: '原文' }]}
      onRestorePastedText={onRestorePastedText} />);
    expect(screen.getByRole('button', { name: '恢复文本' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument();
  });

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
