import { describe, expect, it } from 'vitest';

import {
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
