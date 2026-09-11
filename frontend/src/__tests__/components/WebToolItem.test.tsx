import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '../utils/test-utils';
import { WebToolItem } from '../../components/WebToolItem';
import { getWebToolSources, isWebSearchTool } from '../../utils/webToolSources';
import type { ToolGroupItem } from '../../utils/displayBlocks';

const content = 'Query: Python\n\n[1] 官方教程\nURL: https://docs.python.org/3/tutorial/\nSource: Python\nIcon: https://docs.python.org/icon.png\nContent: 教程\n\n[2] 同站另一页\nURL: https://docs.python.org/3/library/\nSource: Python\nContent: 文档\n\n[3] 示例\nURL: https://example.com/article\nSource: Example\nContent: URL: https://unrelated.invalid\n';
const item: ToolGroupItem = { id: 'search-1', toolName: 'glm_batch_search', description: '搜索', status: 'completed', input: { queries: ['Python'] }, result: { tool_call_id: 'search-1', content, success: true } };

describe('website tool rendering', () => {
  it('counts real unique hosts and reveals linked pills only on request', () => {
    render(<WebToolItem item={item} />);
    const toggle = screen.getByRole('button', { name: '已搜索 2 个网站' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getAllByRole('link')).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'docs.python.org' })).toHaveAttribute('href', 'https://docs.python.org/3/tutorial/');
    const details = screen.getByRole('button', { name: '查看搜索详情' });
    expect(details).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Python')).not.toBeInTheDocument();
    fireEvent.click(details);
    expect(screen.getByText('Python')).toBeVisible();
    expect(screen.getByLabelText('搜索原始输出')).toBeVisible();
  });
  it('does not turn unknown or failed output into successful zero-site search', () => {
    const view = render(<WebToolItem item={{ ...item, status: 'unknown', result: undefined }} />);
    expect(screen.getByRole('button', { name: '网站搜索结果待确认' })).toBeInTheDocument();
    view.rerender(<WebToolItem item={{ ...item, status: 'failed' }} />);
    expect(screen.getByRole('button', { name: '已搜索 2 个网站 部分失败' })).toBeInTheDocument();
  });
  it('rejects unsafe source URLs and does not classify memory or tool search as web', () => {
    expect(getWebToolSources('[1] 坏链接\nURL: javascript:alert(1)\nSource: x\n')).toEqual([]);
    expect(getWebToolSources(content)).toHaveLength(2);
    expect(isWebSearchTool('search_memory')).toBe(false);
    expect(isWebSearchTool('mcp_tool_search')).toBe(false);
  });
});
