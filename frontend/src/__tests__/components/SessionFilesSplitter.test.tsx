import { useRef, useState } from 'react';
import { fireEvent, render, screen } from '../utils/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SessionFilesSplitter } from '../../components/session-files/SessionFilesSplitter';

function SplitterHarness({
  initialRatio = 48,
  onStartEdgeCollapse,
  onRatioCommit,
}: {
  initialRatio?: number;
  onStartEdgeCollapse?: () => void;
  onRatioCommit?: (ratio: number) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [ratio, setRatio] = useState(initialRatio);
  return (
    <div ref={containerRef} data-testid="splitter-container">
      <output data-testid="splitter-ratio">{ratio}</output>
      <SessionFilesSplitter
        containerRef={containerRef}
        chatRatio={ratio}
        onRatioChange={(nextRatio) => {
          onRatioCommit?.(nextRatio);
          setRatio(nextRatio);
        }}
        onStartEdgeCollapse={onStartEdgeCollapse}
      />
    </div>
  );
}

describe('SessionFilesSplitter', () => {
  let nextAnimationFrameId = 1;
  let animationFrames = new Map<number, FrameRequestCallback>();

  const flushAnimationFrames = () => {
    const pending = [...animationFrames.values()];
    animationFrames.clear();
    pending.forEach((callback) => callback(16));
  };

  beforeEach(() => {
    nextAnimationFrameId = 1;
    animationFrames = new Map();
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      const id = nextAnimationFrameId++;
      animationFrames.set(id, callback);
      return id;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      animationFrames.delete(id);
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('拖动按帧预览最新比例，并只在松手时提交中间比例', () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    const onRatioCommit = vi.fn();
    render(<SplitterHarness onRatioCommit={onRatioCommit} />);
    const container = screen.getByTestId('splitter-container');
    const readBounds = vi.spyOn(container, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      right: 1100,
      top: 0,
      bottom: 600,
      width: 1000,
      height: 600,
      toJSON: () => ({}),
    });
    const splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });

    fireEvent.pointerDown(splitter, { button: 0, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 200, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 300, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 350, pointerId: 1 });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('48');
    expect(onRatioCommit).not.toHaveBeenCalled();
    expect(readBounds).toHaveBeenCalledTimes(1);

    flushAnimationFrames();
    expect(container.style.getPropertyValue('--session-files-chat-ratio')).toBe('25%');
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('48');
    expect(onRatioCommit).not.toHaveBeenCalled();

    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('25');
    expect(onRatioCommit).toHaveBeenCalledTimes(1);
    expect(onRatioCommit).toHaveBeenLastCalledWith(25);
  });

  it('松手前仍会冲刷最后一个尚未绘制的比例', () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    const onRatioCommit = vi.fn();
    render(<SplitterHarness onRatioCommit={onRatioCommit} />);
    const container = screen.getByTestId('splitter-container');
    vi.spyOn(container, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      right: 1100,
      top: 0,
      bottom: 600,
      width: 1000,
      height: 600,
      toJSON: () => ({}),
    });
    const splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });

    fireEvent.pointerDown(splitter, { button: 0, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 2000, pointerId: 1 });
    fireEvent.pointerUp(window, { pointerId: 1 });

    expect(container.style.getPropertyValue('--session-files-chat-ratio')).toBe('100%');
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('100');
    expect(onRatioCommit).toHaveBeenCalledTimes(1);
    expect(onRatioCommit).toHaveBeenLastCalledWith(100);
  });

  it('键盘 Home/End 和方向键覆盖完整 0–100 范围', () => {
    render(<SplitterHarness />);
    const splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });
    expect(splitter).toHaveAttribute('aria-valuemin', '0');
    expect(splitter).toHaveAttribute('aria-valuemax', '100');

    fireEvent.keyDown(splitter, { key: 'Home' });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('0');
    fireEvent.keyDown(splitter, { key: 'ArrowLeft' });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('0');

    fireEvent.keyDown(splitter, { key: 'End' });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('100');
    fireEvent.keyDown(splitter, { key: 'ArrowRight' });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('100');
  });

  it('端点标记分隔条所在边界', () => {
    const { rerender } = render(<SplitterHarness initialRatio={0} />);
    expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' }))
      .toHaveAttribute('data-edge', 'start');

    rerender(<SplitterHarness key="end" initialRatio={100} />);
    expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' }))
      .toHaveAttribute('data-edge', 'end');
  });

  it('左端点以第一次拖动方向锁定操作：向左收导航，向右拉会话', () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    const collapseSidebar = vi.fn();
    render(<SplitterHarness initialRatio={0} onStartEdgeCollapse={collapseSidebar} />);
    const container = screen.getByTestId('splitter-container');
    vi.spyOn(container, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      right: 1100,
      top: 0,
      bottom: 600,
      width: 1000,
      height: 600,
      toJSON: () => ({}),
    });
    const splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });

    fireEvent.pointerDown(splitter, { button: 0, pointerId: 1, clientX: 100 });
    fireEvent.pointerMove(window, { clientX: 90, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 400, pointerId: 1 });
    expect(collapseSidebar).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('0');
    fireEvent.pointerUp(window, { pointerId: 1 });

    fireEvent.pointerDown(splitter, { button: 0, pointerId: 2, clientX: 100 });
    fireEvent.pointerMove(window, { clientX: 120, pointerId: 2 });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('2');
    fireEvent.pointerUp(window, { pointerId: 2 });
  });

  it('左端点按 ArrowLeft 收起导航，ArrowRight 仍拉出会话', () => {
    const collapseSidebar = vi.fn();
    render(<SplitterHarness initialRatio={0} onStartEdgeCollapse={collapseSidebar} />);
    const splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });

    fireEvent.keyDown(splitter, { key: 'ArrowLeft' });
    expect(collapseSidebar).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('0');
    fireEvent.keyDown(splitter, { key: 'ArrowRight' });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('2');
  });
});
