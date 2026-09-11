import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '../utils/test-utils';
import { ToolItemView } from '../../components/ReasoningPanel';
import { projectToolItems } from '../../utils/displayBlocks';
import type { ToolCall } from '../../types';

function toolItem(call: ToolCall) {
  return projectToolItems([{ step_number: 1, thinking: '', assistant_content: '', status: 'completed',
    tool_calls: [call], tool_results: [{ tool_call_id: call.id, content: '查询结果', success: true }],
  }])[0];
}

describe('工具身份的渐进披露', () => {
  it('折叠展示服务和标题，展开同时保留真实标识、参数和输出', () => {
    render(<ToolItemView disableMotion item={toolItem({ id: 'mcp-1', name: 'mcp__a__research_query',
      tool_display: { provider: 'mcp', server_name: 'AlphaPai', tool_title: '投研知识检索', tool_name: 'research_query' },
      input: { query: '财报' },
    })} />);
    const toggle = screen.getByRole('button', { name: 'AlphaPai · 投研知识检索' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('mcp__a__research_query')).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.getByText('工具标识')).toBeVisible();
    expect(screen.getByText('mcp__a__research_query')).toBeVisible();
    expect(screen.getByText('原工具名')).toBeVisible();
    expect(screen.getByText('research_query')).toBeVisible();
    expect(screen.getByText('查询结果')).toBeVisible();
  });

  it('旧未知工具保留真实名称；展开不重复相同的原工具名', () => {
    render(<ToolItemView disableMotion item={toolItem({ id: 'legacy', name: 'mcp__legacy__search_reports', input: {} })} />);
    const toggle = screen.getByRole('button', { name: 'mcp__legacy__search_reports' });
    fireEvent.click(toggle);
    expect(screen.getByText('工具标识')).toBeVisible();
    expect(screen.queryByText('原工具名')).not.toBeInTheDocument();
    expect(screen.queryByText('调用工具')).not.toBeInTheDocument();
  });
});
