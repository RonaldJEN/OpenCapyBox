import { describe, expect, it } from 'vitest';

import {
  extractBlobAwareErrorMessage,
  formatHttpErrorMessage,
  formatSendError,
} from '../../utils/errorMessages';

const preferredSkillKeyTooLong = [{
  type: 'string_too_long',
  loc: ['body', 'preferred_skill_keys', 0],
  msg: 'Request validation failed',
  ctx: { max_length: 128 },
}];

describe('send validation error formatting', () => {
  it('attributes an overlong preferred Skill key to the Skill field', () => {
    const message = formatHttpErrorMessage(422, JSON.stringify({
      detail: preferredSkillKeyTooLong,
    }));

    expect(message).toBe('优先 Skill：每项最多 128 字符');
    expect(message).not.toContain('消息太长');
  });

  it('does not turn a preferred Skill list limit into a message-length error', () => {
    const message = formatSendError({
      status: 422,
      response: {
        data: {
          detail: [{
            type: 'too_long',
            loc: ['body', 'preferred_skill_keys'],
            msg: 'Request validation failed',
            ctx: { max_length: 50 },
          }],
        },
      },
    });

    expect(message).toBe('优先 Skill：最多 50 项');
    expect(message).not.toContain('消息太长');
  });

  it('attributes MCP connection item and list limits to the data connection field', () => {
    const itemMessage = formatHttpErrorMessage(422, JSON.stringify({
      detail: [{
        type: 'string_too_long',
        loc: ['body', 'preferred_mcp_server_ids', 0],
        msg: 'Request validation failed',
        ctx: { max_length: 36 },
      }],
    }));
    const listMessage = formatHttpErrorMessage(422, JSON.stringify({
      detail: [{
        type: 'too_long',
        loc: ['body', 'preferred_mcp_server_ids'],
        msg: 'Request validation failed',
        ctx: { max_length: 20 },
      }],
    }));

    expect(itemMessage).toBe('优先数据连接：每项最多 36 字符');
    expect(listMessage).toBe('优先数据连接：最多 20 项');
  });

  it('keeps the dedicated wording for an overlong message text block', () => {
    const message = formatHttpErrorMessage(422, JSON.stringify({
      detail: [{
        type: 'string_too_long',
        loc: ['body', 'content', 0, 'text'],
        msg: 'Request validation failed',
        ctx: { max_length: 10000 },
      }],
    }));

    expect(message).toContain('消息太长');
    expect(message).toContain('10000');
  });
});

describe('blob response error formatting', () => {
  it('extracts FastAPI detail from a blob error response', async () => {
    const message = await extractBlobAwareErrorMessage({
      response: {
        status: 400,
        data: new Blob([
          JSON.stringify({ detail: '导出结果过多，请缩小筛选范围' }),
        ], { type: 'application/json' }),
      },
    });

    expect(message).toBe('导出结果过多，请缩小筛选范围');
  });

  it('falls back to the plain blob body', async () => {
    const message = await extractBlobAwareErrorMessage({
      response: {
        status: 503,
        data: new Blob(['审计服务不可用'], { type: 'text/plain' }),
      },
    });

    expect(message).toBe('审计服务不可用');
  });
});
