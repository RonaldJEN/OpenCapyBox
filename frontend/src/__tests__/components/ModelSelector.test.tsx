import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ModelSelector } from '../../components/ModelSelector';
import type { ModelInfo } from '../../types';

const models: ModelInfo[] = [
  {
    id: 'model-a',
    name: 'Model A',
    provider: 'openai',
    supports_thinking: false,
    supports_image: false,
    max_images: 0,
    supports_video: false,
    max_videos: 0,
    max_tokens: 8192,
    enabled: true,
    tags: [],
  },
  {
    id: 'model-b',
    name: 'Model B',
    provider: 'openai',
    supports_thinking: true,
    supports_image: false,
    max_images: 0,
    supports_video: false,
    max_videos: 0,
    max_tokens: 8192,
    enabled: true,
    tags: [],
  },
  {
    id: 'model-c',
    name: 'Model C',
    provider: 'openai',
    supports_thinking: false,
    supports_image: false,
    max_images: 0,
    supports_video: false,
    max_videos: 0,
    max_tokens: 8192,
    enabled: true,
    tags: [],
  },
];

function renderSelector(onModelChange = vi.fn()) {
  render(
    <>
      <ModelSelector
        selectedModelId="model-b"
        onModelChange={onModelChange}
        availableModels={models}
      />
      <button type="button">下一个控件</button>
    </>,
  );
  return { onModelChange };
}

describe('ModelSelector', () => {
  it('使用稳定 listbox 语义并在打开时聚焦当前模型', async () => {
    const user = userEvent.setup();
    renderSelector();
    const trigger = screen.getByRole('button', { name: '选择模型，当前为 Model B' });

    expect(trigger).toHaveAttribute('aria-haspopup', 'listbox');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    const listboxId = trigger.getAttribute('aria-controls');
    expect(listboxId).toBeTruthy();

    await act(async () => { await user.click(trigger); });

    const listbox = screen.getByRole('listbox', { name: '可用模型' });
    expect(listbox).toHaveAttribute('id', listboxId);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    const options = screen.getAllByRole('option');
    expect(options).toHaveLength(3);
    expect(options[1]).toHaveAttribute('aria-selected', 'true');
    await waitFor(() => expect(options[1]).toHaveFocus());
  });

  it('支持方向键、Home/End 和 Enter 选择，并在选择后恢复触发按钮焦点', async () => {
    const user = userEvent.setup();
    const onModelChange = vi.fn();
    renderSelector(onModelChange);
    const trigger = screen.getByRole('button', { name: '选择模型，当前为 Model B' });

    await act(async () => { await user.click(trigger); });
    const options = screen.getAllByRole('option');
    await waitFor(() => expect(options[1]).toHaveFocus());

    await act(async () => { await user.keyboard('{ArrowDown}'); });
    expect(options[2]).toHaveFocus();
    await act(async () => { await user.keyboard('{Home}'); });
    expect(options[0]).toHaveFocus();
    await act(async () => { await user.keyboard('{End}'); });
    expect(options[2]).toHaveFocus();
    await act(async () => { await user.keyboard('{ArrowUp}'); });
    expect(options[1]).toHaveFocus();
    await act(async () => { await user.keyboard('{Enter}'); });

    expect(onModelChange).toHaveBeenCalledWith('model-b');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('Escape 关闭并恢复焦点，Tab 关闭后保持正常焦点移动', async () => {
    const user = userEvent.setup();
    renderSelector();
    const trigger = screen.getByRole('button', { name: '选择模型，当前为 Model B' });

    await act(async () => { await user.click(trigger); });
    await waitFor(() => expect(screen.getAllByRole('option')[1]).toHaveFocus());
    await act(async () => { await user.keyboard('{Escape}'); });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await act(async () => { await user.keyboard('{ArrowDown}'); });
    await waitFor(() => expect(screen.getAllByRole('option')[1]).toHaveFocus());
    await act(async () => { await user.tab(); });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下一个控件' })).toHaveFocus();
  });

  it('点击组件外部关闭列表', async () => {
    const user = userEvent.setup();
    renderSelector();
    const trigger = screen.getByRole('button', { name: '选择模型，当前为 Model B' });

    await act(async () => { await user.click(trigger); });
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('模型列表为空时仍可用 Escape 或 Tab 关闭', async () => {
    const user = userEvent.setup();
    render(
      <>
        <ModelSelector
          selectedModelId=""
          onModelChange={vi.fn()}
          availableModels={[]}
        />
        <button type="button">下一个控件</button>
      </>,
    );
    const trigger = screen.getByRole('button', { name: '选择模型' });

    await act(async () => { await user.click(trigger); });
    expect(screen.getByRole('listbox', { name: '可用模型' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('加载中...');
    await act(async () => { await user.keyboard('{Escape}'); });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await act(async () => { await user.click(trigger); });
    await act(async () => { await user.tab(); });
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下一个控件' })).toHaveFocus();
  });
});
