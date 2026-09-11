/** Reading identity survives wrapping and Markdown DOM replacement. Pixels do not. */
export const CHAT_BOTTOM_TOLERANCE = 2;
const BLOCK_SELECTOR = '[data-reading-block]';

export type ChatReadingPosition = { mode: 'bottom' } | {
  mode: 'reading';
  scrollTop: number;
  anchor: {
    roundId: string;
    blockId: string | null;
    character: number | null;
    offset: number;
  } | null;
};

function ownedTextNodes(block: HTMLElement): Text[] {
  const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  let node: Node | null;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (parent?.closest(BLOCK_SELECTOR) === block
      && !parent.closest('button, [aria-hidden="true"], [data-reading-ignore]')) {
      nodes.push(node as Text);
    }
  }
  return nodes;
}

function textRects(node: Text, start: number, end: number): DOMRect[] {
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, end);
  return Array.from(range.getClientRects());
}

export function captureChatReadingPosition(container: HTMLElement, preferReading = false): ChatReadingPosition {
  const { scrollTop, scrollHeight, clientHeight } = container;
  if (!preferReading && clientHeight > 0 && scrollHeight - scrollTop - clientHeight <= CHAT_BOTTOM_TOLERANCE) {
    return { mode: 'bottom' };
  }
  const viewport = container.getBoundingClientRect();
  const visible = (rect: DOMRect) => rect.height > 0 && rect.bottom > viewport.top
    && rect.top < viewport.bottom && rect.right > viewport.left && rect.left < viewport.right;
  const rounds = Array.from(container.querySelectorAll<HTMLElement>('[data-round-id]'));
  const round = rounds.find((element) => visible(element.getBoundingClientRect()));
  if (!round) return { mode: 'reading', scrollTop, anchor: null };

  const blocks = Array.from(round.querySelectorAll<HTMLElement>(BLOCK_SELECTOR));
  let elementFallback: HTMLElement | undefined;
  for (const block of blocks) {
    if (!visible(block.getBoundingClientRect())) continue;
    if (block.matches('img, button')) {
      return {
        mode: 'reading', scrollTop,
        anchor: {
          roundId: round.dataset.roundId!, blockId: block.dataset.readingBlock!,
          character: null, offset: block.getBoundingClientRect().top - viewport.top,
        },
      };
    }
    elementFallback ??= block;
    let character = 0;
    for (const node of ownedTextNodes(block)) {
      const length = node.length;
      if (!node.textContent?.trim() || !textRects(node, 0, length).some(visible)) {
        character += length;
        continue;
      }
      // Prefix rectangles are monotonic by line, including whitespace and inline spans.
      let low = 0;
      let high = length - 1;
      while (low < high) {
        const middle = Math.floor((low + high) / 2);
        const prefix = textRects(node, 0, middle + 1);
        const last = prefix[prefix.length - 1];
        if (last && last.bottom > viewport.top) high = middle;
        else low = middle + 1;
      }
      const rect = textRects(node, low, low + 1).find(visible);
      if (rect) {
        return {
          mode: 'reading', scrollTop,
          anchor: {
            roundId: round.dataset.roundId!, blockId: block.dataset.readingBlock!,
            character: character + low, offset: rect.top - viewport.top,
          },
        };
      }
      character += length;
    }
  }
  const element = elementFallback ?? round;
  return {
    mode: 'reading', scrollTop,
    anchor: {
      roundId: round.dataset.roundId!, blockId: element.dataset.readingBlock ?? null,
      character: null, offset: element.getBoundingClientRect().top - viewport.top,
    },
  };
}

export function chatReadingScrollTop(container: HTMLElement, position: ChatReadingPosition): number {
  if (position.mode === 'bottom') return Math.max(0, container.scrollHeight - container.clientHeight);
  const anchor = position.anchor;
  if (!anchor) return position.scrollTop;
  const round = Array.from(container.querySelectorAll<HTMLElement>('[data-round-id]'))
    .find((element) => element.dataset.roundId === anchor.roundId);
  const block = anchor.blockId === null ? round : Array.from(round?.querySelectorAll<HTMLElement>(BLOCK_SELECTOR) ?? [])
    .find((element) => element.dataset.readingBlock === anchor.blockId);
  if (!block || block.closest('[data-process-row][hidden]')) {
    // The process has collapsed. Its message row keeps identity even when the
    // Markdown is unmounted; map that anchor to this round's surviving summary.
    const collapsedRow = Array.from(round?.querySelectorAll<HTMLElement>('[data-process-row="true"][hidden][data-transcript-node]') ?? [])
      .find((row) => anchor.blockId?.startsWith(`answer:${row.dataset.transcriptNode}:`));
    const summary = collapsedRow?.closest('[data-process-container]')?.querySelector<HTMLElement>('[data-process-summary]');
    return summary ? container.scrollTop + summary.getBoundingClientRect().top - container.getBoundingClientRect().top : position.scrollTop;
  }
  let rect = block.getBoundingClientRect();
  let offset = anchor.offset;
  if (anchor.character !== null) {
    let remaining = anchor.character;
    let characterRect: DOMRect | undefined;
    for (const node of ownedTextNodes(block)) {
      if (remaining < node.length) {
        characterRect = textRects(node, remaining, remaining + 1)[0];
        break;
      }
      remaining -= node.length;
    }
    if (characterRect?.height) rect = characterRect;
    else offset = 0; // The anchored text was removed; keep its surviving block visible.
  } else {
    // An image/card can shrink too. Never restore a point beyond its new extent.
    offset = Math.max(offset, 1 - rect.height);
  }
  return container.scrollTop + rect.top - container.getBoundingClientRect().top - offset;
}
