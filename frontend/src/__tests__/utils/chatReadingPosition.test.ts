import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, renderHook } from '@testing-library/react';
import { captureChatReadingPosition, chatReadingScrollTop } from '../../utils/chatReadingPosition';
import { useChatReadingPosition } from '../../components/useChatReadingPosition';

afterEach(() => { vi.restoreAllMocks(); document.body.replaceChildren(); });

function paragraphFixture(nested = false) {
  const content = '0123456789'.repeat(5);
  document.body.innerHTML = `<div id="viewport"><div data-round-id="round"><p data-reading-block="paragraph" data-top="50">${content}</p></div></div>`;
  const container = document.getElementById('viewport')!;
  const block = container.querySelector<HTMLElement>('p')!;
  if (nested) {
    const parent = document.createElement('li');
    parent.dataset.readingBlock = 'parent';
    parent.dataset.top = '0';
    parent.append('prefix');
    block.replaceWith(parent);
    parent.append(block);
  }
  let columns = 5;
  Object.defineProperties(container, {
    scrollHeight: { value: 500 }, clientHeight: { value: 80 },
    scrollTop: { writable: true, value: 100 },
  });
  vi.spyOn(container, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 0, 300, 80));
  for (const element of container.querySelectorAll<HTMLElement>('[data-reading-block], [data-round-id]')) {
    vi.spyOn(element, 'getBoundingClientRect').mockImplementation(() => new DOMRect(
      0, Number(element.dataset.top ?? 0) - container.scrollTop, 300,
      element === block ? Math.ceil(content.length / columns) * 20 : 500,
    ));
  }
  // Model real line boxes, including DOM replacement/splitting by inline Markdown.
  Object.defineProperty(Range.prototype, 'getClientRects', { configurable: true, writable: true, value: vi.fn(function (this: Range) {
    const node = this.startContainer as Text;
    const owner = node.parentElement!.closest<HTMLElement>('[data-reading-block]')!;
    const walker = document.createTreeWalker(owner, NodeFilter.SHOW_TEXT);
    let base = 0;
    let current: Node | null;
    while ((current = walker.nextNode()) && current !== node) base += current.textContent?.length ?? 0;
    const start = base + this.startOffset;
    const end = base + this.endOffset;
    const rects: DOMRect[] = [];
    for (let line = Math.floor(start / columns); line <= Math.floor((end - 1) / columns); line++) {
      rects.push(new DOMRect(0, Number(owner.dataset.top) - container.scrollTop + line * 20, 200, 20));
    }
    return rects;
  }) });
  return { container, block, content, widen: () => { columns = 25; } };
}

describe('chat reading identity', () => {
  it('retains the visible character when a paragraph shrinks and inline nodes remount', () => {
    const { container, block, content, widen } = paragraphFixture();
    const position = captureChatReadingPosition(container);
    expect(position).toMatchObject({ mode: 'reading', anchor: { blockId: 'paragraph', character: 10, offset: -10 } });
    widen();
    block.innerHTML = `<strong>${content.slice(0, 8)}</strong>${content.slice(8)}`;
    container.scrollTop = chatReadingScrollTop(container, position);
    expect(container.scrollTop).toBe(60);
    expect(block.getBoundingClientRect().bottom).toBeGreaterThan(0);
    // Repeated layout notifications preserve the SAME character, without drift.
    expect(chatReadingScrollTop(container, position)).toBe(60);
  });

  it('anchors the visible child text instead of the outer list item spanning it', () => {
    const { container, widen } = paragraphFixture(true);
    const position = captureChatReadingPosition(container);
    expect(position).toMatchObject({ mode: 'reading', anchor: { blockId: 'paragraph', character: 10 } });
    widen();
    expect(chatReadingScrollTop(container, position)).toBe(60);
  });

  it('preserves the bookmark between gestures when CSS is zero width but React still says split', () => {
    const { container, block, widen } = paragraphFixture();
    let width = 300;
    Object.defineProperty(container, 'clientWidth', { get: () => width });
    const { result } = renderHook(() => useChatReadingPosition({
      sessionId: 'test', containerRef: { current: container as HTMLDivElement },
      contentRef: { current: block as HTMLDivElement }, hidden: false, loading: false,
      layoutKey: 'split', hasContent: true, contentVersion: null,
    }));
    act(() => {
      container.scrollTop = 100;
      fireEvent.wheel(container);
      fireEvent.scroll(container);
    });
    act(() => {
      result.current.beginResize();
      width = 0;
      container.scrollTop = 32000; // zero-width wrapping has no readable geometry
      result.current.endResize();
      // A second gesture starts before layout has ever become readable again.
      result.current.beginResize();
      width = 300;
      widen();
      result.current.endResize();
    });
    expect(container.scrollTop).toBe(60);
    expect(result.current.showScrollButton).toBe(true);
  });
});
