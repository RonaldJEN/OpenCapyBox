import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../utils/test-utils';
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

// Mock AuthenticatedImage 组件
vi.mock('../../components/AuthenticatedImage', () => ({
  AuthenticatedImage: ({ src, alt, fallback, ...rest }: any) => (
    <img data-testid="auth-image" src={src} alt={alt} {...rest} />
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

  it('在原始用户消息下渲染本轮优先 Skill 快照', () => {
    const round = createMockRound({
      preferred_skills: [
        { key: 'pdf', display_name: 'PDF 处理' },
        { key: 'data_analysis', display_name: '数据分析' },
      ],
    });

    render(<Round round={round} isStreaming={false} />);

    const preferredSkills = screen.getByLabelText('本轮优先 Skill');
    expect(preferredSkills).toHaveTextContent('PDF 处理');
    expect(preferredSkills).toHaveTextContent('数据分析');
    expect(screen.getByTitle('pdf')).toHaveTextContent('PDF 处理');
  });

  it('工具审批控制标记不渲染为用户消息气泡', () => {
    const round = createMockRound({
      user_message: 'Tool approval: allow_once',
      control_kind: 'tool_approval',
      preferred_skills: [{ key: 'pdf', display_name: 'PDF 处理' }],
    });

    render(<Round round={round} isStreaming={false} />);

    // 审批解决结果是控制决策而非用户输入，不应出现在对话气泡里。
    expect(screen.queryByText('Tool approval: allow_once')).not.toBeInTheDocument();
    expect(screen.queryByText('你')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('本轮优先 Skill')).not.toBeInTheDocument();
    // 助手响应仍正常渲染。
    expect(screen.getByText('这是我的分析结果...')).toBeInTheDocument();
  });

  it('普通用户发送审批格式文本时仍渲染用户消息气泡', () => {
    const round = createMockRound({ user_message: 'Tool approval: allow_once' });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('Tool approval: allow_once')).toBeInTheDocument();
    expect(screen.getByText('你')).toBeInTheDocument();
  });

  it('应该渲染助手最终响应', () => {
    const round = createMockRound();

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('这是我的分析结果...')).toBeInTheDocument();
  });

  it('点击复制回复应复制助手回复内容', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    });
    const round = createMockRound({
      final_response: '这是要复制的回复\n\n```ts\nconst ok = true;\n```',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    fireEvent.click(screen.getByRole('button', { name: '复制回复' }));

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(round.final_response);
      expect(screen.getByRole('button', { name: '复制回复' })).toHaveAttribute('title', '已复制');
    });
  });

  it('助手回复操作区包含复制图标', () => {
    const round = createMockRound({ steps: [] });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByRole('button', { name: '复制回复' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '复制回复' }).parentElement).not.toHaveClass('opacity-0');
  });

  it('中断态即使有响应内容也不应显示复制按钮', () => {
    const round = createMockRound({
      status: 'interrupted',
      final_response: '这是一段未完成的响应',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('这是一段未完成的响应')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
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

  it('终态 round 即使父级仍传入 isStreaming 也不应继续按流式渲染', () => {
    const round = createMockRound({
      status: 'completed',
      final_response: '最终响应',
      steps: [
        {
          step_number: 1,
          thinking: '',
          assistant_content: '临时正文',
          tool_calls: [],
          tool_results: [],
          status: 'completed',
        },
      ],
    });

    render(<Round round={round} isStreaming={true} />);

    expect(screen.getByText('Streaming: false')).toBeInTheDocument();
    expect(screen.getByText('Completed: true')).toBeInTheDocument();
    expect(screen.getByText('最终响应')).toBeInTheDocument();
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

  it('取消状态应该显示中性提示而不是错误', () => {
    const round = createMockRound({
      status: 'cancelled',
      final_response: '',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('已取消')).toBeInTheDocument();
    expect(screen.queryByText('执行失败')).not.toBeInTheDocument();
  });

  it('取消发生在响应生成前时应该兼容 null 响应', () => {
    const round = createMockRound({
      status: 'cancelled',
      final_response: null,
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('已取消')).toBeInTheDocument();
    expect(screen.queryByText('执行失败')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
  });

  it('取消状态应该隐藏完整响应中的 Cancelled 占位内容', () => {
    const round = createMockRound({
      status: 'cancelled',
      final_response: '  Cancelled\n',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.queryByText('Cancelled')).not.toBeInTheDocument();
    expect(screen.getByText('已取消')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
  });

  it('仅当取消响应完整等于 sentinel 时才隐藏正文', () => {
    const round = createMockRound({
      status: 'cancelled',
      final_response: 'Cancelled after completing the cleanup.',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('Cancelled after completing the cleanup.')).toBeInTheDocument();
    expect(screen.getByText('已取消')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
  });

  it.each(['', 'Cancelled'])(
    '取消响应为 %j 时应该保留已流式生成的有效正文',
    (finalResponse) => {
      const round = createMockRound({
        status: 'cancelled',
        final_response: finalResponse,
        steps: [
          {
            step_number: 1,
            thinking: '',
            assistant_content: '取消前已经生成的有效正文。',
            tool_calls: [],
            tool_results: [],
            status: 'completed',
          },
        ],
      });

      render(<Round round={round} isStreaming={false} />);

      expect(screen.getByText('取消前已经生成的有效正文。')).toBeInTheDocument();
      expect(screen.queryByText('Cancelled')).not.toBeInTheDocument();
      expect(screen.getByText('已取消')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
    },
  );

  it('最后一步是取消占位符时应该继续显示更早的有效正文', () => {
    const round = createMockRound({
      status: 'cancelled',
      final_response: '',
      steps: [
        {
          step_number: 1,
          thinking: '',
          assistant_content: '取消前已经生成的有效正文。',
          tool_calls: [],
          tool_results: [],
          status: 'completed',
        },
        {
          step_number: 2,
          thinking: '',
          assistant_content: 'Cancelled',
          tool_calls: [],
          tool_results: [],
          status: 'completed',
        },
      ],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('取消前已经生成的有效正文。')).toBeInTheDocument();
    expect(screen.queryByText('Cancelled')).not.toBeInTheDocument();
    expect(screen.getByText('已取消')).toBeInTheDocument();
  });

  it('非取消状态应该保留内容恰为 Cancelled 的完整响应', () => {
    const round = createMockRound({
      status: 'completed',
      final_response: 'Cancelled',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} />);

    expect(screen.getByText('Cancelled')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '复制回复' })).toBeInTheDocument();
  });

  it('没有最终响应时不应该渲染响应区域', () => {
    const round = createMockRound({ final_response: '' });

    render(<Round round={round} isStreaming={false} />);

    // 用户消息应该存在
    expect(screen.getByText('请帮我分析这个问题')).toBeInTheDocument();
    // 但最终响应不存在（因为是空的）
    expect(screen.queryByText('这是我的分析结果...')).not.toBeInTheDocument();
  });

  it('流式正文和工具同 step 时仍应渲染正文', () => {
    const round = createMockRound({
      final_response: '',
      status: 'running',
      steps: [
        {
          step_number: 1,
          thinking: '',
          assistant_content: '正文已经开始流式输出。',
          tool_calls: [{ name: 'search_web', input: { query: '黄金价格' } }],
          tool_results: [{ success: true, content: 'result' }],
          status: 'streaming',
        },
      ],
      step_count: 1,
    });

    render(<Round round={round} isStreaming={true} />);

    expect(screen.getByText('正文已经开始流式输出。')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '复制回复' })).not.toBeInTheDocument();
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

  it('助手头像应该使用品牌图片', () => {
    const round = createMockRound();

    render(<Round round={round} isStreaming={false} />);

    const avatar = screen.getByAltText('AI');
    expect(avatar).toBeInTheDocument();
    expect(avatar).toHaveAttribute('src', '/logo.jpg');
    expect(avatar.className).toContain('object-cover');
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

  it('图片附件使用 AuthenticatedImage 而非直接 <img src="/api/...">', () => {
    const round = createMockRound();
    const attachments = [
      {
        name: '每日复盘模板.png',
        path: 'uploads/每日复盘模板.png',
        size: 12345,
        type: 'png',
        session_id: 's1',
      },
    ];

    const { container } = render(
      <Round
        round={round}
        isStreaming={false}
        userAttachments={attachments as any}
        sessionId="s1"
      />,
    );

    // AuthenticatedImage mock 渲染为 data-testid="auth-image"
    const authImg = screen.getByTestId('auth-image');
    expect(authImg).toBeInTheDocument();
    expect(authImg.getAttribute('src')).toContain('/api/sessions/s1/files/');
    expect(authImg.getAttribute('src')).toContain(encodeURIComponent('每日复盘模板.png'));

    // 不应存在未经认证的直接 <img src="/api/...">
    const allImgs = container.querySelectorAll('img:not([data-testid="auth-image"])');
    const directApiImgs = Array.from(allImgs).filter(
      (img) => img.getAttribute('src')?.startsWith('/api/'),
    );
    expect(directApiImgs).toHaveLength(0);
  });

  it('有 data_url 的图片附件直接用 data_url', () => {
    const round = createMockRound();
    const attachments = [
      {
        name: 'screenshot.png',
        path: 'uploads/screenshot.png',
        size: 100,
        type: 'png',
        data_url: 'data:image/png;base64,iVBOR',
        session_id: 's1',
      },
    ];

    render(
      <Round
        round={round}
        isStreaming={false}
        userAttachments={attachments as any}
        sessionId="s1"
      />,
    );

    const authImg = screen.getByTestId('auth-image');
    expect(authImg.getAttribute('src')).toBe('data:image/png;base64,iVBOR');
  });

  it('助手回复中的文件位置应该渲染为可点击文件卡片', () => {
    const round = createMockRound({
      final_response: [
        '写好了，领导！',
        '',
        '**文件位置：** `/home/user/sessions/s1/quick_sort.py`',
        '',
        '包含两个版本：',
      ].join('\n'),
    });
    const onOpenFileInPanel = vi.fn();

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        onOpenFileInPanel={onOpenFileInPanel}
      />,
    );

    expect(screen.getByText('quick_sort.py')).toBeInTheDocument();
    expect(screen.getByText('包含两个版本：')).toBeInTheDocument();
    expect(screen.getByText('/home/user/sessions/s1/quick_sort.py')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '查看 quick_sort.py' }));

    expect(onOpenFileInPanel).toHaveBeenCalledWith(expect.objectContaining({
      name: 'quick_sort.py',
      path: 'quick_sort.py',
      session_id: 's1',
      type: 'py',
    }));
  });

  it('传入 session 文件校验结果时只渲染匹配文件卡片', () => {
    const round = createMockRound({
      final_response: [
        '生成结果如下：',
        '',
        '文件位置： `scripts/batch_find_similar.py`',
        '文件位置： `references/db_schema.md`',
      ].join('\n'),
      steps: [],
    });
    const onOpenFileInPanel = vi.fn();

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        assistantFileMatches={{
          'scripts/batch_find_similar.py': null,
          'references/db_schema.md': {
            name: 'db_schema.md',
            path: 'references/db_schema.md',
            size: 42,
            modified: '2026-06-12T10:00:00Z',
            type: 'md',
            is_directory: false,
            session_id: 's1',
          },
        }}
        onOpenFileInPanel={onOpenFileInPanel}
      />,
    );

    expect(screen.queryByRole('button', { name: '查看 batch_find_similar.py' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看 db_schema.md' }));

    expect(onOpenFileInPanel).toHaveBeenCalledWith(expect.objectContaining({
      name: 'db_schema.md',
      path: 'references/db_schema.md',
      size: 42,
      session_id: 's1',
    }));
  });

  it('传入空校验结果时不渲染未确认的文件卡片', () => {
    const round = createMockRound({
      final_response: '文件位置： `scripts/pending.py`',
      steps: [],
    });
    const onOpenFileInPanel = vi.fn();

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        assistantFileMatches={{}}
        onOpenFileInPanel={onOpenFileInPanel}
      />,
    );

    expect(screen.queryByRole('button', { name: '查看 pending.py' })).not.toBeInTheDocument();
    expect(onOpenFileInPanel).not.toHaveBeenCalled();
  });

  it('助手回复句子里的 docx 文件名也应该渲染为文件卡片', () => {
    const round = createMockRound({
      final_response: '搞定！ `DeepSeek_V4_解读.docx` 已生成，22KB。',
      steps: [],
    });
    const onOpenFileInPanel = vi.fn();

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        onOpenFileInPanel={onOpenFileInPanel}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '查看 DeepSeek_V4_解读.docx' }));

    expect(onOpenFileInPanel).toHaveBeenCalledWith(expect.objectContaining({
      name: 'DeepSeek_V4_解读.docx',
      path: 'DeepSeek_V4_解读.docx',
      session_id: 's1',
      type: 'docx',
    }));
  });

  it('助手回复代码块中的文件提示不应该渲染为文件卡片', () => {
    const round = createMockRound({
      final_response: ['```text', '文件位置： quick_sort.py', '```'].join('\n'),
      steps: [],
    });

    render(<Round round={round} isStreaming={false} sessionId="s1" />);

    expect(screen.queryByRole('button', { name: '查看 quick_sort.py' })).not.toBeInTheDocument();
    expect(screen.getByText(/文件位置： quick_sort.py/)).toBeInTheDocument();
  });

  it('助手 Markdown 中的沙箱图片路径不会渲染成破图', () => {
    const round = createMockRound({
      final_response: '![chart](/home/user/sessions/s1/reports/chart.png)',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} sessionId="s1" />);

    expect(screen.queryByTestId('auth-image')).not.toBeInTheDocument();
    expect(screen.queryByAltText('chart')).not.toBeInTheDocument();
  });

  it('助手 Markdown 中相对图片路径不会渲染成破图', () => {
    const round = createMockRound({
      final_response: '![风景插画](docx_images/image1.png)',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} sessionId="s1" />);

    expect(screen.queryByTestId('auth-image')).not.toBeInTheDocument();
    expect(screen.queryByAltText('风景插画')).not.toBeInTheDocument();
  });

  it('助手图片文件卡片应显示预览图', () => {
    const round = createMockRound({
      final_response: '图片文件在 `docx_images/image1.png`。',
      steps: [],
    });

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        assistantFileMatches={{
          'docx_images/image1.png': {
            name: 'image1.png',
            path: 'docx_images/image1.png',
            session_id: 's1',
            size: 22323,
            modified: '2026-06-25T10:00:00+00:00',
            type: 'png',
            is_directory: false,
          },
        }}
      />,
    );

    const authImg = screen.getByTestId('auth-image');
    expect(authImg).toHaveAttribute(
      'src',
      '/api/sessions/s1/files/docx_images/image1.png?preview=true',
    );
    expect(authImg).toHaveAttribute('alt', 'image1.png');
  });

  it('助手 Markdown 中跨 session 的沙箱图片路径不会转换', () => {
    const round = createMockRound({
      final_response: '![chart](/home/user/sessions/other/reports/chart.png)',
      steps: [],
    });

    render(<Round round={round} isStreaming={false} sessionId="s1" />);

    expect(screen.queryByTestId('auth-image')).not.toBeInTheDocument();
    expect(screen.queryByAltText('chart')).not.toBeInTheDocument();
  });

  it('助手 Markdown 本地文件链接在当前 Files 面板打开', () => {
    const round = createMockRound({
      final_response: '[查看压缩包](exports/bundle.zip)',
      steps: [],
    });
    const onOpenFileInPanel = vi.fn();

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        onOpenFileInPanel={onOpenFileInPanel}
      />,
    );

    const link = screen.getByRole('link', { name: '查看压缩包' });
    expect(link).not.toHaveAttribute('target', '_blank');
    fireEvent.click(link);
    expect(onOpenFileInPanel).toHaveBeenCalledWith(expect.objectContaining({
      name: 'bundle.zip',
      path: 'exports/bundle.zip',
      session_id: 's1',
      type: 'zip',
    }));
  });

  it('助手 Markdown 中文及空格文件链接使用解码后的沙箱路径', () => {
    const round = createMockRound({
      final_response: '[查看报告](<exports/报告 终版.pdf>)',
      steps: [],
    });
    const onOpenFileInPanel = vi.fn();

    render(
      <Round
        round={round}
        isStreaming={false}
        sessionId="s1"
        onOpenFileInPanel={onOpenFileInPanel}
      />,
    );

    fireEvent.click(screen.getByRole('link', { name: '查看报告' }));
    expect(onOpenFileInPanel).toHaveBeenCalledWith(expect.objectContaining({
      name: '报告 终版.pdf',
      path: 'exports/报告 终版.pdf',
      session_id: 's1',
      type: 'pdf',
    }));
  });
});
