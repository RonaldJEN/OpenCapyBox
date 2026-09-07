import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import {
  captureChatReadingPosition, chatReadingScrollTop, CHAT_BOTTOM_TOLERANCE,
  type ChatReadingPosition,
} from '../utils/chatReadingPosition';

interface Options {
  sessionId: string;
  containerRef: RefObject<HTMLDivElement>;
  contentRef: RefObject<HTMLDivElement>;
  hidden: boolean;
  loading: boolean;
  layoutKey: string;
  contentVersion: unknown;
  hasContent: boolean;
  scrollTarget?: { sessionId: string; roundId: string; nonce: number } | null;
}

const sizeOf = (element: HTMLElement) => `${element.clientWidth}:${element.clientHeight}:${element.scrollHeight}`;

/** The only writer of the chat viewport: user reading, explicit navigation, or following. */
export function useChatReadingPosition(options: Options) {
  const { sessionId, containerRef, contentRef, hidden, loading, layoutKey, contentVersion, hasContent, scrollTarget } = options;
  const current = useRef(options);
  const positions = useRef(new Map<string, ChatReadingPosition>());
  const owner = useRef(sessionId);
  const writtenTop = useRef<number | null>(null);
  const navigatingTo = useRef<number | null>(null);
  const resizing = useRef(false);
  const measuredSize = useRef('');
  const handledTarget = useRef('');
  const [followingBottom, setFollowingBottom] = useState(true);

  const remember = useCallback(() => {
    const ctx = current.current;
    const element = ctx.containerRef.current;
    if (!element || ctx.hidden || ctx.loading || resizing.current
      || element.clientWidth === 0 || element.clientHeight === 0) return;
    const position = captureChatReadingPosition(element);
    positions.current.set(ctx.sessionId, position);
    setFollowingBottom(position.mode === 'bottom');
    measuredSize.current = sizeOf(element);
  }, []);

  const write = useCallback((top: number, smooth = false) => {
    const element = current.current.containerRef.current;
    if (!element) return;
    const target = Math.max(0, Math.min(top, Math.max(0, element.scrollHeight - element.clientHeight)));
    measuredSize.current = sizeOf(element);
    if (smooth && Math.abs(element.scrollTop - target) > CHAT_BOTTOM_TOLERANCE
      && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      navigatingTo.current = target;
      element.scrollTo({ top: target, behavior: 'smooth' });
    } else {
      navigatingTo.current = null;
      element.scrollTop = target;
      writtenTop.current = element.scrollTop;
    }
  }, []);

  const restore = useCallback(() => {
    const ctx = current.current;
    const element = ctx.containerRef.current;
    if (!element || ctx.hidden || ctx.loading || navigatingTo.current !== null) return;
    // CSS can already have zero width while React still says split, including
    // between two pointer gestures. Such geometry never owns the bookmark.
    if (element.clientWidth === 0 || element.clientHeight === 0) return;
    const position = positions.current.get(ctx.sessionId) ?? { mode: 'bottom' };
    write(chatReadingScrollTop(element, position));
    setFollowingBottom(position.mode === 'bottom');
  }, [write]);

  const beforeLayoutChange = useCallback(() => {
    navigatingTo.current = null;
    const element = current.current.containerRef.current;
    // Layout clamping to the bottom is not the user's choice to follow it.
    if (element && writtenTop.current !== null
      && Math.abs(element.scrollTop - writtenTop.current) <= CHAT_BOTTOM_TOLERANCE) return;
    remember();
  }, [remember]);
  const beginResize = useCallback(() => {
    beforeLayoutChange();
    resizing.current = true;
  }, [beforeLayoutChange]);
  const endResize = useCallback(() => {
    restore();
    resizing.current = false;
  }, [restore]);
  const scrollToBottom = useCallback(() => {
    positions.current.set(current.current.sessionId, { mode: 'bottom' });
    setFollowingBottom(true);
    const element = current.current.containerRef.current;
    if (element) write(element.scrollHeight - element.clientHeight, true);
  }, [write]);

  useLayoutEffect(() => {
    current.current = options;
    if (owner.current !== sessionId) {
      owner.current = sessionId;
      writtenTop.current = null;
      navigatingTo.current = null;
      resizing.current = false;
      // Ordinary navigation still opens the latest messages. A full file view keeps
      // its hidden reading bookmark until the user makes the chat visible again.
      if (!hidden) positions.current.set(sessionId, { mode: 'bottom' });
    }
  });

  useLayoutEffect(() => {
    if (loading || hidden) return;
    const element = containerRef.current;
    const targetKey = scrollTarget ? `${scrollTarget.sessionId}:${scrollTarget.nonce}` : '';
    if (element && scrollTarget?.sessionId === sessionId && handledTarget.current !== targetKey) {
      const target = Array.from(element.querySelectorAll<HTMLElement>('[data-round-id]'))
        .find((round) => round.dataset.roundId === scrollTarget.roundId);
      if (!target) return; // History has not projected the requested round yet.
      handledTarget.current = targetKey;
      setFollowingBottom(false);
      const top = element.scrollTop + target.getBoundingClientRect().top - element.getBoundingClientRect().top
        - (element.clientHeight - target.getBoundingClientRect().height) / 2;
      positions.current.set(sessionId, { mode: 'reading', scrollTop: top, anchor: null });
      write(top, true);
      if (navigatingTo.current === null) remember();
      return;
    }
    restore();
  }, [sessionId, hidden, loading, layoutKey, contentVersion, scrollTarget, containerRef, restore, write, remember]);

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const onUserInput = () => {
      measuredSize.current = sizeOf(element);
      if (navigatingTo.current !== null) {
        navigatingTo.current = null;
        writtenTop.current = null;
        remember();
      }
    };
    const onScroll = () => {
      if (current.current.hidden || current.current.loading || resizing.current) return;
      if (navigatingTo.current !== null) {
        if (Math.abs(element.scrollTop - navigatingTo.current) <= CHAT_BOTTOM_TOLERANCE) {
          navigatingTo.current = null;
          // The bottom can move while smooth navigation is running (streaming).
          if (positions.current.get(current.current.sessionId)?.mode === 'bottom') restore();
          else remember();
        }
        return;
      }
      if (writtenTop.current !== null && Math.abs(element.scrollTop - writtenTop.current) <= CHAT_BOTTOM_TOLERANCE) {
        return;
      }
      writtenTop.current = null;
      if (measuredSize.current && measuredSize.current !== sizeOf(element)) restore();
      else remember();
    };
    element.addEventListener('scroll', onScroll);
    element.addEventListener('wheel', onUserInput, { passive: true });
    element.addEventListener('pointerdown', onUserInput);
    element.addEventListener('keydown', onUserInput);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => restore());
    observer?.observe(element);
    if (contentRef.current) observer?.observe(contentRef.current);
    return () => {
      observer?.disconnect();
      element.removeEventListener('scroll', onScroll);
      element.removeEventListener('wheel', onUserInput);
      element.removeEventListener('pointerdown', onUserInput);
      element.removeEventListener('keydown', onUserInput);
    };
  }, [sessionId, hasContent, containerRef, contentRef, remember, restore]);

  return { showScrollButton: !followingBottom, scrollToBottom, beforeLayoutChange, beginResize, restore, endResize };
}
