import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '../utils/test-utils';
import { ToolApprovalCard } from '../../components/ToolApprovalCard';
import type { ToolApprovalPayload } from '../../types';

const approval: ToolApprovalPayload = {
  kind: 'tool_approval',
  tool_ref: 'mcp:server-1:search',
  provider: 'mcp',
  source_type: 'official',
  server_id: 'server-1',
  server_name: '官方知识库',
  tool_name: 'search',
  tool_title: '知识检索',
  tool_description: '检索企业知识库',
  arguments_display: '{"query":"季度收入"}',
  warning: '该调用会向外部服务发送查询内容',
};

describe('ToolApprovalCard', () => {
  it('展示来源、参数和风险提示，并提交精确审批选项', () => {
    const onSubmit = vi.fn();
    render(<ToolApprovalCard approval={approval} onSubmit={onSubmit} />);

    expect(screen.getByText('官方 MCP · 官方知识库')).toBeInTheDocument();
    expect(screen.getByText('mcp:server-1:search')).toBeInTheDocument();
    expect(screen.getByText('{"query":"季度收入"}')).toBeInTheDocument();
    expect(screen.getByText('该调用会向外部服务发送查询内容')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /本会话允许/ }));
    expect(onSubmit).toHaveBeenCalledWith({ approval: 'allow_session' });
  });

  it('disabled 时不提交审批', () => {
    const onSubmit = vi.fn();
    render(<ToolApprovalCard approval={approval} onSubmit={onSubmit} disabled />);
    fireEvent.click(screen.getByRole('button', { name: /允许本次/ }));
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
