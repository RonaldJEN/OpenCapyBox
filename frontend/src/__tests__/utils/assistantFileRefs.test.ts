import { describe, expect, it } from 'vitest';
import { createAssistantFileInfoFromHref } from '../../utils/assistantFileRefs';

describe('assistantFileRefs', () => {
  it('解码当前 Session 的 Markdown 文件链接', () => {
    expect(
      createAssistantFileInfoFromHref(
        'reports/%E6%8A%A5%E5%91%8A%20%E7%BB%88%E7%89%88.pdf',
        'session-1',
      ),
    ).toMatchObject({
      name: '报告 终版.pdf',
      path: 'reports/报告 终版.pdf',
      session_id: 'session-1',
      type: 'pdf',
    });
  });

  it('拒绝越界路径和编码分隔符', () => {
    expect(createAssistantFileInfoFromHref('%2e%2e/secret.pdf', 'session-1')).toBeNull();
    expect(createAssistantFileInfoFromHref('reports%2Fsecret.pdf', 'session-1')).toBeNull();
    expect(createAssistantFileInfoFromHref('reports/%00secret.pdf', 'session-1')).toBeNull();
  });
});
