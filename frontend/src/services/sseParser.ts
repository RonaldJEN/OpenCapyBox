export function eventSequence(event: any): number | undefined {
  const value = typeof event?.sequence === 'number'
    ? event.sequence
    : (typeof event?._sequence === 'number' ? event._sequence : undefined);
  return Number.isFinite(value) ? value : undefined;
}

export function eventMessageId(event: any): string | undefined {
  return typeof event?.messageId === 'string' ? event.messageId : undefined;
}

export function eventToolCallId(event: any): string | undefined {
  return typeof event?.toolCallId === 'string' ? event.toolCallId : undefined;
}

export function parseSSELines(buffer: string, chunk: string): { events: any[]; buffer: string } {
  const nextBuffer = buffer + chunk;
  const lines = nextBuffer.split('\n');
  const tail = lines.pop() || '';
  const events: any[] = [];

  for (const line of lines) {
    if (!line.startsWith('data: ')) {
      continue;
    }
    const data = line.slice(6);
    try {
      events.push(JSON.parse(data));
    } catch (error) {
      console.error('Failed to parse SSE data:', error, 'Line:', data);
    }
  }

  return { events, buffer: tail };
}

export function flushSSEBuffer(buffer: string): any[] {
  const trimmed = buffer.trim();
  if (!trimmed) {
    return [];
  }
  return parseSSELines('', `${buffer}\n`).events;
}
