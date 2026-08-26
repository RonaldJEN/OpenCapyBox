import { useRef, useState } from 'react';
import { fireEvent, render, screen } from '../utils/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SessionFilesSplitter } from '../../components/session-files/SessionFilesSplitter';

function SplitterHarness({
  initialRatio = 48,
  onStartEdgeCollapse,
}: {
  initialRatio?: number;
  onStartEdgeCollapse?: () => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [ratio, setRatio] = useState(initialRatio);
  return (
    <div ref={containerRef} data-testid="splitter-container">
      <output data-testid="splitter-ratio">{ratio}</output>
      <SessionFilesSplitter
        containerRef={containerRef}
        chatRatio={ratio}
        onRatioChange={setRatio}
        onStartEdgeCollapse={onStartEdgeCollapse}
      />
    </div>
  );
}

describe('SessionFilesSplitter', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('指针可以按容器几何拖到 0%、任意中间值和 100%', () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    render(<SplitterHarness />);
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
    fireEvent.pointerMove(window, { clientX: -500, pointerId: 1 });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('0');

    fireEvent.pointerMove(window, { clientX: 350, pointerId: 1 });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('25');

    fireEvent.pointerMove(window, { clientX: 2000, pointerId: 1 });
    expect(screen.getByTestId('splitter-ratio')).toHaveTextContent('100');
    fireEvent.pointerUp(window, { pointerId: 1 });
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
