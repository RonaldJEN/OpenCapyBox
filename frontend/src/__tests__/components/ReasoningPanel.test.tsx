import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '../utils/test-utils';
import { ReasoningPanel } from '../../components/ReasoningPanel';
import { StepData } from '../../types';

describe('ReasoningPanel 组件', () => {
  const mockSteps: StepData[] = [
    {
      step_number: 1,
      thinking: '分析用户问题...',
      assistant_content: '',
      tool_calls: [],
      tool_results: [],
      status: 'completed',
      thinking_start_ts: 1000,
      thinking_end_ts: 4000,
    },
    {
      step_number: 2,
      thinking: '',
      assistant_content: '',
      tool_calls: [
        { name: 'read_file', input: { path: 'src/app.py' } },
      ],
      tool_results: [
        { success: true, content: 'file content...' },
      ],
      status: 'completed',
    },
    {
      step_number: 3,
      thinking: '',
      assistant_content: '',
      tool_calls: [
        { name: 'edit_file', input: { path: 'src/app.py' } },
      ],
      tool_results: [
        { success: true, content: 'File edited successfully +5 -2' },
      ],
      status: 'completed',
    },
  ];

  it('应该渲染 ThinkingBlock 并显示思考时长', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // 应该看到 "思考 3s" 格式的文本
    expect(screen.getByText(/思考\s*3s/)).toBeInTheDocument();
  });

  it('点击 ThinkingBlock 应展开显示 thinking 内容', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // 点击 thinking header 展开
    const thinkingButton = screen.getByText(/思考\s*3s/);
    fireEvent.click(thinkingButton);

    // 应看到 thinking 内容
    expect(screen.getByText('分析用户问题...')).toBeInTheDocument();
  });

  it('应该渲染 ToolGroupBlock 合并摘要', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // 工具组应合并 read + edit 为摘要（如 "Edited src/app.py, Read a file"）
    const buttons = screen.getAllByRole('button');
    const summaryButton = buttons.find(btn =>
      btn.textContent?.includes('Edited') || btn.textContent?.includes('Read')
    );
    expect(summaryButton).toBeDefined();
  });

  it('ToolGroupBlock 展开后应显示工具项', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // 工具组默认展开，应包含工具项描述
    // "Read src/app.py" 同时出现在摘要和工具项中，故用 getAllByText
    expect(screen.getAllByText(/Read src\/app\.py/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Update src\/app\.py/).length).toBeGreaterThanOrEqual(1);
  });

  it('应显示 Done 标记', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.getByText('Done')).toBeInTheDocument();
  });
  it('工具描述应使用粗体样式', () => {
    const { container } = render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // 工具描述应含 font-semibold 类名
    const boldDescriptions = container.querySelectorAll('.font-semibold');
    expect(boldDescriptions.length).toBeGreaterThanOrEqual(2);
  });
  it('空步骤时不应该渲染面板', () => {
    const { container } = render(
      <ReasoningPanel steps={[]} isStreaming={false} isCompleted={true} />
    );

    expect(container.firstChild).toBeNull();
  });

  it('流式传输时无步骤应显示加载状态', () => {
    render(
      <ReasoningPanel steps={[]} isStreaming={true} isCompleted={false} />
    );

    expect(screen.getByText('正在分析请求...')).toBeInTheDocument();
  });

  it('流式传输中的 thinking 应显示 "思考中..."', () => {
    const streamingSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '正在思考中',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'streaming',
        thinking_start_ts: Date.now(),
      },
    ];

    render(
      <ReasoningPanel steps={streamingSteps} isStreaming={true} isCompleted={false} />
    );

    expect(screen.getByText(/思考中/)).toBeInTheDocument();
  });

  it('点击工具项可展开详细输入输出', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // "Read src/app.py" 同时出现在摘要和工具项中，取 font-semibold 的工具项
    const matches = screen.getAllByText(/Read src\/app\.py/);
    const toolItem = matches.find(el => el.classList.contains('font-semibold')) || matches[matches.length - 1];
    fireEvent.click(toolItem);

    // 展开后应能看到输出内容
    expect(screen.getByText('file content...')).toBeInTheDocument();
  });

  it('编辑工具应显示 diff 统计（独立行）', () => {
    const { container } = render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    // diff 统计应在独立行展示（包含 font-mono 类名的 div）
    expect(screen.getByText('+5')).toBeInTheDocument();
    expect(screen.getByText('-2')).toBeInTheDocument();
    // diff 统计行应含文件路径
    const diffLine = container.querySelector('.font-mono');
    expect(diffLine).toBeTruthy();
  });

  it('disableMotion 模式不应添加动画类', () => {
    const { container } = render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} disableMotion={true} />
    );

    const animatedElements = container.querySelectorAll('.animate-fade-in');
    expect(animatedElements.length).toBe(0);
  });

  it('多个连续 thinking 应合并为可折叠分组', () => {
    const multiThinkingSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '第一次思考内容',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 3000,
      },
      {
        step_number: 2,
        thinking: '第二次思考内容',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 3500,
        thinking_end_ts: 5000,
      },
      {
        step_number: 3,
        thinking: '第三次思考内容',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 5500,
        thinking_end_ts: 7000,
      },
    ];

    render(
      <ReasoningPanel steps={multiThinkingSteps} isStreaming={false} isCompleted={true} />
    );

    // 应合并显示为 "思考 3次" 的分组标题
    expect(screen.getByText(/思考 3次/)).toBeInTheDocument();

    // 默认收起时不应显示单条思考内容
    expect(screen.queryByText('第一次思考内容')).toBeNull();

    // 点击展开后应显示各条思考
    const groupButton = screen.getByText(/思考 3次/);
    fireEvent.click(groupButton);

    // 展开后应能看到子条目的按钮（3条各有不同时长）
    const innerItems = screen.getAllByText(/思考\s*\d+s/);
    expect(innerItems.length).toBe(3);
  });
});
