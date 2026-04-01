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
});
