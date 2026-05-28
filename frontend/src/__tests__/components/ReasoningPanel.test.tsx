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

  it('应该渲染完成态 ThinkingBlock 胶囊并显示思考时长', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.getByText(/已完成思考\s*3s/)).toBeInTheDocument();
  });

  it('点击完成态胶囊应打开活动抽屉并显示 thinking 内容', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    const thinkingButton = screen.getByText(/已完成思考\s*3s/);
    fireEvent.click(thinkingButton);

    expect(screen.getByText('活动')).toBeInTheDocument();
    expect(screen.getAllByText('分析用户问题...').length).toBeGreaterThanOrEqual(1);
  });

  it('主聊天区不直接渲染工具组，活动抽屉中显示工具摘要', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.queryByText(/Read src\/app\.py/)).toBeNull();

    fireEvent.click(screen.getByText(/已完成思考\s*3s/));

    const buttons = screen.getAllByRole('button');
    const summaryButton = buttons.find(btn =>
      btn.textContent?.includes('Edited') || btn.textContent?.includes('Read')
    );
    expect(summaryButton).toBeDefined();
  });

  it('活动抽屉中的 ToolGroupBlock 展开后应显示工具项', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    fireEvent.click(screen.getByText(/已完成思考\s*3s/));

    expect(screen.getAllByText(/Read src\/app\.py/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Update src\/app\.py/).length).toBeGreaterThanOrEqual(1);
  });

  it('活动抽屉中应显示 Done 标记', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.queryByText('Done')).toBeNull();
    fireEvent.click(screen.getByText(/已完成思考\s*3s/));
    expect(screen.getByText('Done')).toBeInTheDocument();
  });
  it('只有工具活动且没有 thinking 时完成态应显示活动入口', () => {
    const toolOnlySteps: StepData[] = [
      {
        step_number: 1,
        thinking: '',
        assistant_content: '',
        tool_calls: [{ name: 'read_file', input: { path: 'package.json' } }],
        tool_results: [{ success: true, content: 'ok' }],
        status: 'completed',
      },
    ];

    render(
      <ReasoningPanel steps={toolOnlySteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.getByText('已完成活动')).toBeInTheDocument();
    expect(screen.queryByText(/已完成思考/)).toBeNull();

    fireEvent.click(screen.getByText('已完成活动'));
    expect(screen.getByText('活动')).toBeInTheDocument();
    expect(screen.getAllByText(/Read package\.json/).length).toBeGreaterThanOrEqual(1);
  });
  it('工具描述应使用粗体样式', () => {
    const { container } = render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    fireEvent.click(screen.getByText(/已完成思考\s*3s/));

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

  it('流式传输中的 thinking 应显示进行中卡片', () => {
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

    expect(screen.getByText('正在思考')).toBeInTheDocument();
    expect(screen.getByText('查看活动')).toBeInTheDocument();
    expect(screen.getByText('正在思考中')).toBeInTheDocument();
  });

  it('流式 thinking 外部预览不应省略截断', () => {
    const longThinking = '用户想了解美国伊朗最新情况。我已经搜索过了，但可以再搜一下更详细的信息，或者整理一下已有的内容。我应该提供更全面的分析，包括各方立场、关键分歧、最新动态等。';
    const streamingSteps: StepData[] = [
      {
        step_number: 1,
        thinking: longThinking,
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'streaming',
        thinking_start_ts: Date.now(),
      },
    ];

    const { container } = render(
      <ReasoningPanel steps={streamingSteps} isStreaming={true} isCompleted={false} />
    );

    expect(screen.getByText(longThinking)).toBeInTheDocument();
    expect(screen.getByText('_')).toBeInTheDocument();
    expect(container.querySelector('.line-clamp-3')).toBeNull();
  });

  it('流式 thinking 分组点击查看活动后应显示历史思考内容', () => {
    const streamingGroupSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '前一次思考',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
      {
        step_number: 2,
        thinking: '当前思考',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'streaming',
        thinking_start_ts: Date.now(),
      },
    ];

    render(
      <ReasoningPanel steps={streamingGroupSteps} isStreaming={true} isCompleted={false} />
    );

    expect(screen.queryByText('前一次思考')).toBeNull();
    fireEvent.click(screen.getByText('查看活动'));
    expect(screen.getByText('活动')).toBeInTheDocument();
    expect(screen.getAllByText('前一次思考').length).toBeGreaterThanOrEqual(1);
  });

  it('工具调用尚未返回时外层应显示调用中动画，而不是完成态胶囊', () => {
    const runningToolSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '先判断需要搜索什么。',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
      {
        step_number: 2,
        thinking: '',
        assistant_content: '',
        tool_calls: [{ name: 'search_web', input: { query: '黄金价格 美伊冲突 地缘政治 2026' }, started_at_ts: Date.now() }],
        tool_results: [],
        status: 'streaming',
      },
    ];

    const { container } = render(
      <ReasoningPanel steps={runningToolSteps} isStreaming={true} isCompleted={false} />
    );

    expect(screen.getByText('正在调用工具')).toBeInTheDocument();
    expect(screen.getByText('Searched')).toBeInTheDocument();
    expect(screen.queryByText(/已完成思考/)).toBeNull();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();

    fireEvent.click(screen.getByText('查看活动'));
    expect(screen.getByText('活动')).toBeInTheDocument();
    expect(screen.getByText(/Search "黄金价格 美伊冲突 地缘政治 2026"/)).toBeInTheDocument();
  });

  it('停止后终态工具缺少结果时不应显示调用中动画', () => {
    const stoppedToolSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '',
        assistant_content: '',
        tool_calls: [{ name: 'search_web', input: { query: '黄金价格' } }],
        tool_results: [],
        status: 'completed',
      },
    ];

    const { container } = render(
      <ReasoningPanel steps={stoppedToolSteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.queryByText('正在调用工具')).toBeNull();
    expect(container.querySelector('.animate-spin')).toBeNull();
    expect(screen.getByText('已完成活动')).toBeInTheDocument();
  });

  it('工具已返回但整轮仍在等待下一段 thinking 时应显示处理中状态', () => {
    const waitingNextThinkingSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '先判断需要搜索什么。',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
      {
        step_number: 2,
        thinking: '',
        assistant_content: '',
        tool_calls: [{ name: 'search_web', input: { query: '黄金价格 美伊冲突 地缘政治 2026' } }],
        tool_results: [{ success: false, content: '', error: 'Search failed:', received_at_ts: Date.now() }],
        status: 'failed',
      },
    ];

    const { container } = render(
      <ReasoningPanel steps={waitingNextThinkingSteps} isStreaming={true} isCompleted={false} />
    );

    expect(screen.getByText('正在处理工具结果')).toBeInTheDocument();
    expect(screen.getByText('Searched')).toBeInTheDocument();
    expect(screen.queryByText(/已完成思考/)).toBeNull();
    expect(container.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('流式正文到达后不应继续显示工具结果处理中状态', () => {
    const streamingAnswerAfterToolSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '先判断需要搜索什么。',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
      {
        step_number: 2,
        thinking: '',
        assistant_content: '',
        tool_calls: [{ name: 'search_web', input: { query: '黄金价格 美伊冲突 地缘政治 2026' } }],
        tool_results: [{ success: true, content: 'result', received_at_ts: Date.now() }],
        status: 'completed',
      },
      {
        step_number: 3,
        thinking: '',
        assistant_content: '正文已经开始流式输出。',
        tool_calls: [],
        tool_results: [],
        status: 'streaming',
      },
    ];

    render(
      <ReasoningPanel steps={streamingAnswerAfterToolSteps} isStreaming={true} isCompleted={false} />
    );

    expect(screen.queryByText('正在处理工具结果')).toBeNull();
    expect(screen.getByText(/已完成思考\s*1s/)).toBeInTheDocument();
  });

  it('同一 step 中工具后流式正文到达后不应继续显示工具结果处理中状态', () => {
    const streamingAnswerInToolStep: StepData[] = [
      {
        step_number: 1,
        thinking: '先判断需要搜索什么。',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
      {
        step_number: 2,
        thinking: '',
        assistant_content: '正文已经开始流式输出。',
        tool_calls: [{ name: 'search_web', input: { query: '黄金价格 美伊冲突 地缘政治 2026' } }],
        tool_results: [{ success: true, content: 'result', received_at_ts: Date.now() }],
        status: 'streaming',
      },
    ];

    render(
      <ReasoningPanel steps={streamingAnswerInToolStep} isStreaming={true} isCompleted={false} />
    );

    expect(screen.queryByText('正在处理工具结果')).toBeNull();
    expect(screen.queryByText('正在调用工具')).toBeNull();
    expect(screen.getByText(/已完成思考\s*1s/)).toBeInTheDocument();
  });

  it('活动抽屉中的 thinking 标题应按首个标点截断', () => {
    const punctuatedSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '用户说“继续”，但之前只是打了个招呼，没有明确上下文。正文仍应完整显示。',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
    ];

    render(
      <ReasoningPanel steps={punctuatedSteps} isStreaming={false} isCompleted={true} />
    );

    fireEvent.click(screen.getByText(/已完成思考\s*1s/));

    expect(screen.getByText('用户说“继续”')).toBeInTheDocument();
    expect(screen.getByText('用户说“继续”，但之前只是打了个招呼，没有明确上下文。正文仍应完整显示。')).toBeInTheDocument();
  });

  it('活动抽屉中点击工具项可展开详细输入输出', () => {
    render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    fireEvent.click(screen.getByText(/已完成思考\s*3s/));

    const matches = screen.getAllByText(/Read src\/app\.py/);
    const toolItem = matches.find(el => el.classList.contains('font-semibold')) || matches[matches.length - 1];
    fireEvent.click(toolItem);

    expect(screen.getByText('file content...')).toBeInTheDocument();
  });

  it('活动抽屉中编辑工具应显示 diff 统计（独立行）', () => {
    const { container } = render(
      <ReasoningPanel steps={mockSteps} isStreaming={false} isCompleted={true} />
    );

    fireEvent.click(screen.getByText(/已完成思考\s*3s/));

    expect(screen.getByText('+5')).toBeInTheDocument();
    expect(screen.getByText('-2')).toBeInTheDocument();
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

  it('被工具分隔的 thinking 整轮也只应显示一个完成胶囊', () => {
    const separatedThinkingSteps: StepData[] = [
      {
        step_number: 1,
        thinking: '工具前思考',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 1000,
        thinking_end_ts: 2000,
      },
      {
        step_number: 2,
        thinking: '',
        assistant_content: '',
        tool_calls: [{ name: 'read_file', input: { path: 'HEARTBEAT.md' } }],
        tool_results: [{ success: true, content: 'ok' }],
        status: 'completed',
      },
      {
        step_number: 3,
        thinking: '工具后思考',
        assistant_content: '',
        tool_calls: [],
        tool_results: [],
        status: 'completed',
        thinking_start_ts: 3000,
        thinking_end_ts: 5000,
      },
    ];

    render(
      <ReasoningPanel steps={separatedThinkingSteps} isStreaming={false} isCompleted={true} />
    );

    expect(screen.getAllByText(/已完成思考/)).toHaveLength(1);
    fireEvent.click(screen.getByText(/已完成思考\s*3s/));
    expect(screen.getAllByText('工具前思考').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('工具后思考').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/Read HEARTBEAT\.md/).length).toBeGreaterThanOrEqual(1);
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

    expect(screen.getByText(/已完成思考\s*5s/)).toBeInTheDocument();

    expect(screen.queryByText('第一次思考内容')).toBeNull();

    const groupButton = screen.getByText(/已完成思考\s*5s/);
    fireEvent.click(groupButton);

    expect(screen.getAllByText('第一次思考内容').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('第二次思考内容').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('第三次思考内容').length).toBeGreaterThanOrEqual(1);
  });
});
