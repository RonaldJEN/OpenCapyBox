import type { ToolResult } from '../types';

type ToolResultWire = ToolResult & Record<string, unknown>;

function asObject(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function parseObject(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'string') return asObject(value);
  try {
    return asObject(JSON.parse(value));
  } catch {
    return null;
  }
}

function booleanField(value: Record<string, unknown> | null, field: 'success' | 'isError'): boolean | undefined {
  return value && typeof value[field] === 'boolean' ? value[field] : undefined;
}

function nonEmptyError(value: Record<string, unknown> | null): string | undefined {
  return value && typeof value.error === 'string' && value.error.trim()
    ? value.error
    : undefined;
}

/**
 * Preserve the source result text while deriving its three-state outcome once.
 * Explicit booleans win. Error text remains display context only; absence of a
 * boolean outcome is deliberately unknown rather than optimistic.
 */
export function projectToolResult(input: ToolResultWire): ToolResult {
  const contentObject = parseObject(input.content);
  const explicitSuccess = booleanField(input, 'success');
  const explicitIsError = booleanField(input, 'isError');
  const nestedSuccess = booleanField(contentObject, 'success');
  const nestedIsError = booleanField(contentObject, 'isError');
  const structuredError = nonEmptyError(input) || nonEmptyError(contentObject);

  const success = explicitSuccess !== undefined
    ? explicitSuccess
    : explicitIsError !== undefined
      ? !explicitIsError
      : nestedSuccess !== undefined
        ? nestedSuccess
        : nestedIsError !== undefined
          ? !nestedIsError : null;

  return {
    ...input,
    success,
    content: input.content,
    error: structuredError || (success === false ? input.content : undefined),
  };
}

/** Live cards retain their established concise output display; history stays raw. */
export function displayToolResultContent(content: string): string {
  const parsed = parseObject(content);
  return typeof parsed?.output === 'string' ? parsed.output : content;
}

export function projectRoundToolResults(round: import('../types').RoundData): import('../types').RoundData {
  return {
    ...round,
    steps: round.steps.map((step) => ({
      ...step,
      tool_results: step.tool_results.map((result) => projectToolResult(result as ToolResultWire)),
    })),
  };
}
