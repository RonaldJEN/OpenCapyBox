import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '../utils/test-utils';
import { Round } from '../../components/Round';
import { RoundData } from '../../types';

// Mock ReasoningPanel 组件
vi.mock('../../components/ReasoningPanel', () => ({
  ReasoningPanel: ({ steps, isStreaming, isCompleted }: any) => (
    <div data-testid="reasoning-panel">
      <span>Steps: {steps.length}</span>
      <span>Streaming: {String(isStreaming)}</span>
      <span>Completed: {String(isCompleted)}</span>
    </div>
  ),
}));

// Mock FileAttachment 组件
vi.mock('../../components/FileAttachment', () => ({
  FileAttachment: ({ filename, size }: any) => (
    <div data-testid="file-attachment">
      {filename} - {size}
    </div>
  ),
}));

describe('Round 组件', () => {
  const createMockRound = (overrides?: Partial<RoundData>): RoundData => ({
    round_id: 'round-1',
    user_message: '请帮我分析这个问题',
    final_response: '这是我的分析结果...',
    steps: [
      {
        step_number: 1,
        thinking: '思考中...',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
      },
    ],
    step_count: 1,
    status: 'completed',
    created_at: new Date().toISOString(),
    completed_at: new Date().toISOString(),
    ...overrides,
  });

  it('应该渲染用户消息', () => {
    const round = createMockRound();

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('请帮我分析这个问题')).toBeInTheDocument();
  });

  it('应该渲染助手最终响应', () => {
    const round = createMockRound();

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('这是我的分析结果...')).toBeInTheDocument();
  });

  it('应该渲染 ReasoningPanel 组件', () => {
    const round = createMockRound();

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByTestId('reasoning-panel')).toBeInTheDocument();
    expect(screen.getByText('Steps: 1')).toBeInTheDocument();
  });

  it('流式传输时应该传递正确的 props 给 ReasoningPanel', () => {
    const round = createMockRound({ status: 'running' });

    render(<Round round={round} isStreaming={true} />);

    expect(screen.getByText('Streaming: true')).toBeInTheDocument();
    expect(screen.getByText('Completed: false')).toBeInTheDocument();
  });

  it('完成时应该传递 isCompleted=true', () => {
    const round = createMockRound({ status: 'completed' });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('Completed: true')).toBeInTheDocument();
  });

  it('没有步骤时不应该渲染 ReasoningPanel', () => {
    const round = createMockRound({ steps: [] });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.queryByTestId('reasoning-panel')).not.toBeInTheDocument();
  });

  it('失败状态应该显示错误提示', () => {
    const round = createMockRound({ status: 'failed' });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('执行失败')).toBeInTheDocument();
  });

  it('达到最大步数应该显示警告', () => {
    const round = createMockRound({ status: 'max_steps_reached' });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('达到最大步数限制')).toBeInTheDocument();
  });

  it('没有最终响应时不应该渲染响应区域', () => {
    const round = createMockRound({ final_response: '' });

    render(<Round round={round} isStreaming={false} />);

    // 用户消息应该存在
    expect(screen.getByText('请帮我分析这个问题')).toBeInTheDocument();
    // 但最终响应不存在（因为是空的）
    expect(screen.queryByText('这是我的分析结果...')).not.toBeInTheDocument();
  });

  it('用户消息包含附件标记时应该显示附件', () => {
    const round = createMockRound({
      user_message: '请分析这个文件 [ATTACHMENT:report.pdf|1 KB]',
    });

    render(<Round round={round} isStreaming={false} />);

    // 应该渲染 FileAttachment 组件
    expect(screen.getByTestId('file-attachment')).toBeInTheDocument();
  });

  it('应该正确应用 Apple 风格的消息气泡样式', () => {
    const round = createMockRound();

    render(<Round round={round} isStreaming={false} />);

    const userText = screen.getByText('请帮我分析这个问题');
    expect(userText).toBeInTheDocument();
    expect(userText.className).toContain('text-claude-text');
    expect(userText.className).toContain('leading-relaxed');
  });

  it('用户头像应该使用正确的样式', () => {
    const round = createMockRound();

    const { container } = render(<Round round={round} isStreaming={false} />);

    const userAvatar = container.querySelector('.w-7.h-7.rounded-full.bg-claude-surface');
    expect(userAvatar).toBeInTheDocument();
  });

  it('助手头像应该使用黑色背景', () => {
    const round = createMockRound();

    const { container } = render(<Round round={round} isStreaming={false} />);

    const assistantLabel = screen.getByText('助手');
    const botAvatar = assistantLabel.parentElement?.previousElementSibling as HTMLElement | null;
    expect(botAvatar).toBeInTheDocument();
    expect(botAvatar?.className).toContain('bg-claude-accent/20');
  });

  it('默认应该有淡入动画', () => {
    const round = createMockRound();

    const { container } = render(<Round round={round} isStreaming={false} />);

    // 检查是否有淡入动画类
    const animatedElement = container.querySelector('.animate-fade-in');
    expect(animatedElement).toBeInTheDocument();
  });

  it('disableMotion=true 时不应有淡入动画', () => {
    const round = createMockRound();

    const { container } = render(<Round round={round} isStreaming={false} disableMotion />);

    const animatedElement = container.querySelector('.animate-fade-in');
    expect(animatedElement).not.toBeInTheDocument();
  });
});
