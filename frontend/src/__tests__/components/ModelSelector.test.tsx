import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ModelSelector } from '../../components/ModelSelector';
import type { ModelInfo, TurnReasoningSelection } from '../../types';

const models: ModelInfo[] = [
  {
    id: 'model-a', name: 'Model A', provider: 'openai', supports_thinking: false,
    supports_reasoning_control: false,
    thinking_mode: 'provider_default', reasoning_effort: null,
    supported_reasoning_efforts: [], supports_image: false, max_images: 0,
    supports_video: false, max_videos: 0, max_tokens: 8192, enabled: true, tags: [],
  },
  {
    id: 'model-b', name: 'Model B', provider: 'openai', supports_thinking: true,
    thinking_mode: 'enabled', reasoning_effort: 'high',
    default_reasoning_level: 'high',
    supported_reasoning_efforts: ['off', 'high', 'max'], supports_image: false, max_images: 0,
    supports_video: false, max_videos: 0, max_tokens: 8192, enabled: true, tags: [],
  },
];

function renderSelector(options: {
  onModelChange?: (modelId: string) => void;
  onReasoningChange?: (selection: TurnReasoningSelection) => void;
  readOnly?: boolean;
} = {}) {
  const onModelChange = options.onModelChange || vi.fn();
  const onReasoningChange = options.onReasoningChange || vi.fn();
  render(
    <ModelSelector
      selectedModelId="model-b"
      onModelChange={onModelChange}
      availableModels={models}
      reasoningSelection={{ mode: 'enabled', effort: 'high' }}
      onReasoningChange={onReasoningChange}
      readOnly={options.readOnly}
    />,
  );
  return { onModelChange, onReasoningChange };
}

describe('ModelSelector', () => {
  it('以模型和本轮推理等级组成统一触发器', async () => {
    const user = userEvent.setup();
    renderSelector();
    const trigger = screen.getByRole('button', {
      name: '选择模型，当前 Model B，推理等级 High',
    });
    expect(trigger).toHaveTextContent('Model B');
    expect(trigger).toHaveTextContent('High');

    await act(async () => { await user.click(trigger); });
    expect(screen.getByRole('menu', { name: '模型与推理等级' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /模型 Model B/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /推理等级 High/ })).toBeInTheDocument();
  });

  it('可进入模型子菜单并选择模型', async () => {
    const user = userEvent.setup();
    const onModelChange = vi.fn();
    renderSelector({ onModelChange });
    await act(async () => { await user.click(screen.getByRole('button', { name: /选择模型/ })); });
    await act(async () => { await user.click(screen.getByRole('menuitem', { name: /模型 Model B/ })); });

    expect(screen.getByRole('listbox', { name: '可用模型' })).toBeInTheDocument();
    await act(async () => { await user.click(screen.getByRole('option', { name: 'Model A' })); });
    expect(onModelChange).toHaveBeenCalledWith('model-a');
  });

  it('模型子菜单保留能力说明，触发器保持简洁', async () => {
    const user = userEvent.setup();
    const onModelChange = vi.fn();
    render(
      <ModelSelector
        selectedModelId="model-b"
        onModelChange={onModelChange}
        availableModels={[
          ...models,
          {
            ...models[0],
            id: 'model-c',
            name: 'Model C',
            supports_reasoning_control: true,
            supported_reasoning_efforts: ['off', 'on'],
            supports_image: true,
            max_images: 5,
          },
        ]}
        reasoningSelection={{ mode: 'enabled', effort: 'high' }}
        onReasoningChange={vi.fn()}
      />,
    );

    const trigger = screen.getByRole('button', { name: /选择模型/ });
    expect(trigger).not.toHaveTextContent('支持深度思考');

    await act(async () => { await user.click(trigger); });
    await act(async () => { await user.click(screen.getByRole('menuitem', { name: /模型 Model B/ })); });

    expect(screen.getByRole('option', { name: /Model B 支持深度思考/ })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Model C 支持深度思考 支持图片（最多 5 张）/ })).toBeInTheDocument();
  });

  it('推理等级只提供模型目录按顺序声明的等级', async () => {
    const user = userEvent.setup();
    const onReasoningChange = vi.fn();
    renderSelector({ onReasoningChange });
    await act(async () => { await user.click(screen.getByRole('button', { name: /选择模型/ })); });
    await act(async () => { await user.click(screen.getByRole('menuitem', { name: /推理等级 High/ })); });

    expect(screen.getAllByRole('menuitemradio')).toHaveLength(3);
    expect(screen.getByRole('menuitemradio', { name: 'High' })).toHaveAttribute('aria-checked', 'true');
    await act(async () => { await user.click(screen.getByRole('menuitemradio', { name: 'Max' })); });
    expect(onReasoningChange).toHaveBeenCalledWith({ mode: 'enabled', effort: 'max' });
  });

  it('区分目录 Default 二元组与同名的显式具体等级', async () => {
    const user = userEvent.setup();
    const onReasoningChange = vi.fn();
    const providerDefaultModel: ModelInfo = {
      ...models[1],
      id: 'provider-default-high',
      thinking_mode: 'provider_default',
      reasoning_effort: 'high',
      default_reasoning_level: 'high',
      supported_reasoning_efforts: ['high', 'max'],
    };
    render(
      <ModelSelector
        selectedModelId={providerDefaultModel.id}
        onModelChange={vi.fn()}
        availableModels={[providerDefaultModel]}
        reasoningSelection={{ mode: 'provider_default', effort: 'high' }}
        onReasoningChange={onReasoningChange}
        readOnly
      />,
    );

    const trigger = screen.getByRole('button', { name: /选择模型/ });
    expect(trigger).toHaveTextContent('Default (High)');

    await act(async () => { await user.click(trigger); });
    const catalogDefault = screen.getByRole('menuitemradio', { name: 'Default (High)' });
    expect(catalogDefault).toHaveAttribute('aria-checked', 'true');
    expect(screen.getByRole('menuitemradio', { name: 'High' })).toHaveAttribute('aria-checked', 'false');
    await act(async () => { await user.click(catalogDefault); });

    expect(onReasoningChange).toHaveBeenCalledWith({
      mode: 'provider_default',
      effort: 'high',
    });

    await act(async () => { await user.click(trigger); });
    await act(async () => { await user.click(screen.getByRole('menuitemradio', { name: 'High' })); });
    expect(onReasoningChange).toHaveBeenLastCalledWith({
      mode: 'enabled',
      effort: 'high',
    });
  });

  it('会话内锁定模型但仍允许切换下一轮推理等级', async () => {
    const user = userEvent.setup();
    const onReasoningChange = vi.fn();
    renderSelector({ readOnly: true, onReasoningChange });
    await act(async () => { await user.click(screen.getByRole('button', { name: /选择模型/ })); });

    expect(screen.queryByRole('menuitem', { name: /模型 Model B/ })).not.toBeInTheDocument();
    await act(async () => { await user.click(screen.getByRole('menuitemradio', { name: 'Off' })); });
    expect(onReasoningChange).toHaveBeenCalledWith({ mode: 'disabled', effort: null });
  });

  it('Escape 或点击外部关闭菜单', async () => {
    const user = userEvent.setup();
    renderSelector();
    const trigger = screen.getByRole('button', { name: /选择模型/ });
    await act(async () => { await user.click(trigger); });
    await act(async () => { await user.keyboard('{Escape}'); });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await act(async () => { await user.click(trigger); });
    act(() => { fireEvent.mouseDown(document.body); });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('打开菜单后聚焦当前项，并支持方向键、Home、End 和 Enter', async () => {
    const user = userEvent.setup();
    const onModelChange = vi.fn();
    renderSelector({ onModelChange });
    const trigger = screen.getByRole('button', { name: /选择模型/ });

    trigger.focus();
    await act(async () => { await user.keyboard('{ArrowDown}'); });
    const modelEntry = await screen.findByRole('menuitem', { name: /模型 Model B/ });
    await waitFor(() => expect(modelEntry).toHaveFocus());

    await act(async () => { await user.keyboard('{Enter}'); });
    const selected = await screen.findByRole('option', { name: /^Model B/ });
    await waitFor(() => expect(selected).toHaveFocus());

    await act(async () => { await user.keyboard('{Home}'); });
    expect(screen.getByRole('option', { name: 'Model A' })).toHaveFocus();
    await act(async () => { await user.keyboard('{End}'); });
    expect(selected).toHaveFocus();
    await act(async () => { await user.keyboard('{ArrowDown}'); });
    expect(screen.getByRole('option', { name: 'Model A' })).toHaveFocus();
    await act(async () => { await user.keyboard('{Enter}'); });

    expect(onModelChange).toHaveBeenCalledWith('model-a');
    expect(trigger).toHaveFocus();
  });

  it('模型 listbox 只包含 option，返回按钮和加载状态位于其外部', async () => {
    const user = userEvent.setup();
    renderSelector();
    await act(async () => { await user.click(screen.getByRole('button', { name: /选择模型/ })); });
    await act(async () => { await user.click(screen.getByRole('menuitem', { name: /模型 Model B/ })); });

    const listbox = screen.getByRole('listbox', { name: '可用模型' });
    expect(Array.from(listbox.children).every((child) => child.getAttribute('role') === 'option')).toBe(true);
    expect(screen.getByRole('button', { name: '模型' })).not.toBe(listbox.firstElementChild);
  });
});
