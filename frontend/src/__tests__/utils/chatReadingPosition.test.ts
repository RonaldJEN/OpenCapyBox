import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, renderHook } from '@testing-library/react';
import { captureChatReadingPosition, chatReadingScrollTop } from '../../utils/chatReadingPosition';
import { useChatReadingPosition } from '../../components/useChatReadingPosition';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.replaceChildren(); });

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
    scrollHeight: { value: 500, configurable: true }, clientHeight: { value: 80 },
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
  it('finishes pending search after reveal through the resize observer, then preserves user scrolling', () => {
    const { container, block } = paragraphFixture();
    block.dataset.messageId = 'message';
    block.hidden = true;
    Object.defineProperty(container, 'clientWidth', { value: 300 });
    vi.spyOn(block, 'getClientRects').mockImplementation(() => (
      block.hidden ? [] : [block.getBoundingClientRect()]
    ) as unknown as DOMRectList);
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: true } as MediaQueryList);
    let notifyResize!: () => void;
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: ResizeObserverCallback) { notifyResize = () => callback([], this as unknown as ResizeObserver); }
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    const options = {
      sessionId: 'test', containerRef: { current: container as HTMLDivElement },
      contentRef: { current: container.firstElementChild as HTMLDivElement },
      hidden: false, loading: false, layoutKey: '', contentVersion: null, hasContent: true,
      scrollTarget: { sessionId: 'test', roundId: 'round', messageId: 'message', nonce: 1 },
    };
    renderHook(() => useChatReadingPosition(options));
    act(() => notifyResize());
    expect(container.scrollTop).toBe(100); // No bottom restore while the search target is hidden.
    act(() => { block.hidden = false; notifyResize(); });
    expect(container.scrollTop).toBe(26);
    act(() => { container.scrollTop = 80; fireEvent.wheel(container); fireEvent.scroll(container); notifyResize(); });
    expect(container.scrollTop).toBe(80); // The consumed search must not pull the reader back.
  });

  it('lets a newer search replace an unfinished smooth navigation', () => {
    const { container, block } = paragraphFixture();
    block.dataset.messageId = 'first';
    const second = document.createElement('p');
    second.dataset.messageId = 'second';
    block.parentElement!.append(second);
    Object.defineProperty(container, 'clientWidth', { value: 300 });
    vi.spyOn(block, 'getClientRects').mockReturnValue([block.getBoundingClientRect()] as unknown as DOMRectList);
    vi.spyOn(second, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 300 - container.scrollTop, 300, 20));
    vi.spyOn(second, 'getClientRects').mockReturnValue([second.getBoundingClientRect()] as unknown as DOMRectList);
    vi.spyOn(window, 'matchMedia').mockReturnValue({ matches: false } as MediaQueryList);
    const scrollTo = vi.fn(); // Leave the first animation in progress.
    Object.defineProperty(container, 'scrollTo', { configurable: true, value: scrollTo });
    const options = {
      sessionId: 'test', containerRef: { current: container as HTMLDivElement },
      contentRef: { current: container.firstElementChild as HTMLDivElement },
      hidden: false, loading: false, layoutKey: '', contentVersion: null, hasContent: true,
    };
    const view = renderHook(({ messageId, nonce }) => useChatReadingPosition({ ...options,
      scrollTarget: { sessionId: 'test', roundId: 'round', messageId, nonce },
    }), { initialProps: { messageId: 'first', nonce: 1 } });
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 26, behavior: 'smooth' });
    view.rerender({ messageId: 'second', nonce: 2 });
    expect(scrollTo).toHaveBeenLastCalledWith({ top: 276, behavior: 'smooth' });
  });

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

  it('maps a removed process paragraph to its own round summary after collapse', () => {
    const { container, block } = paragraphFixture();
    block.dataset.readingBlock = 'answer:round:message:m1:p:0';
    const row = document.createElement('div');
    row.dataset.processRow = 'true';
    row.dataset.transcriptNode = 'round:message:m1';
    const process = document.createElement('div');
    process.dataset.processContainer = '';
    const summary = document.createElement('button');
    summary.dataset.processSummary = '';
    summary.dataset.readingBlock = 'process:round';
    block.replaceWith(process);
    process.append(summary, row);
    row.append(block);
    const position = captureChatReadingPosition(container);
    expect(position).toMatchObject({ mode: 'reading', anchor: { blockId: block.dataset.readingBlock } });

    // A different round also has a summary; its position must never win.
    const otherRound = document.createElement('div');
    otherRound.dataset.roundId = 'other';
    otherRound.innerHTML = '<button data-process-summary></button>';
    container.prepend(otherRound);
    row.hidden = true;
    row.replaceChildren();
    vi.spyOn(summary, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 30 - container.scrollTop, 200, 24));
    expect(chatReadingScrollTop(container, position)).toBe(30);
    container.scrollTop = 30;
    expect(chatReadingScrollTop(container, position)).toBe(30);
    expect(chatReadingScrollTop(container, { mode: 'bottom' })).toBe(420);
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

  it('opening history from a compact bottom view preserves reading when content grows', () => {
    document.body.innerHTML = '<div id="viewport"><div data-round-id="round"><button data-reading-block="process:round">运行中</button></div></div>';
    const container = document.getElementById('viewport')! as HTMLDivElement;
    const content = container.firstElementChild! as HTMLDivElement;
    const summary = content.firstElementChild! as HTMLButtonElement;
    let height = 400;
    Object.defineProperties(container, { clientWidth: { value: 300 }, clientHeight: { value: 400 },
      scrollHeight: { get: () => height }, scrollTop: { value: 0, writable: true } });
    vi.spyOn(container, 'getBoundingClientRect').mockReturnValue(new DOMRect(0, 0, 300, 400));
    vi.spyOn(content, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, -container.scrollTop, 300, height));
    vi.spyOn(summary, 'getBoundingClientRect').mockImplementation(() => new DOMRect(0, 50 - container.scrollTop, 180, 28));
    const { result } = renderHook(() => useChatReadingPosition({ sessionId: 'test', containerRef: { current: container },
      contentRef: { current: content }, hidden: false, loading: false, layoutKey: '', contentVersion: null, hasContent: true }));
    act(() => result.current.beginReading());
    act(() => { height = 1000; result.current.restore(); });
    expect(container.scrollTop).toBe(0);
    expect(result.current.showScrollButton).toBe(true);
    act(() => { height = 1200; result.current.restore(); });
    expect(container.scrollTop).toBe(0);
  });

  it('hides the bottom button after content shrinks without losing the reading bookmark', () => {
    const { container, block } = paragraphFixture();
    let scrollHeight = 500;
    Object.defineProperties(container, {
      clientWidth: { value: 300 },
      scrollHeight: { get: () => scrollHeight },
    });
    const { result } = renderHook(() => useChatReadingPosition({
      sessionId: 'test', containerRef: { current: container as HTMLDivElement },
      contentRef: { current: block as HTMLDivElement }, hidden: false, loading: false,
      layoutKey: 'chat', hasContent: true, contentVersion: null,
    }));
    act(() => {
      container.scrollTop = 100;
      fireEvent.wheel(container);
      fireEvent.scroll(container);
    });
    expect(result.current.showScrollButton).toBe(true);

    act(() => { scrollHeight = 80; result.current.restore(); });
    expect(container.scrollTop).toBe(0);
    expect(result.current.showScrollButton).toBe(false);

    // Reopening a tool restores the reader's paragraph, not the new bottom.
    act(() => { scrollHeight = 500; result.current.restore(); });
    expect(container.scrollTop).toBe(100);
    expect(result.current.showScrollButton).toBe(true);
    act(() => { container.scrollTop = 420; fireEvent.scroll(container); });
    expect(result.current.showScrollButton).toBe(false);
  });
});
