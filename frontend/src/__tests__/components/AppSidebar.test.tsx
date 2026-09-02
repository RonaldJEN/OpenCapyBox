import { useState } from 'react';
import { fireEvent, render, screen } from '../utils/test-utils';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  APP_SIDEBAR_RAIL_WIDTH,
  AppSidebar,
  DEFAULT_APP_SIDEBAR_WIDTH,
} from '../../components/AppSidebar';

function Harness({ boundaryClaimed = false }: { boundaryClaimed?: boolean }) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <AppSidebar
      collapsed={collapsed}
      boundaryClaimed={boundaryClaimed}
      userId="test"
      onCollapsedChange={setCollapsed}
    >
      <div>侧栏内容</div>
    </AppSidebar>
  );
}

describe('AppSidebar', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('正常状态不展示按钮，rail 状态才展示恢复按钮', () => {
    render(<Harness />);
    const shell = screen.getByTestId('app-sidebar-shell');
    expect(shell).toHaveStyle({ width: `${DEFAULT_APP_SIDEBAR_WIDTH}px` });
    expect(shell).not.toHaveClass('transition-[width]');
    expect(screen.queryByRole('button', { name: '展开左侧栏', hidden: true })).not.toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole('separator', { name: '调整左侧栏宽度', hidden: true }), { key: 'Home' });
    expect(shell).toHaveAttribute('data-collapsed', 'true');
    expect(shell).toHaveStyle({ width: `${APP_SIDEBAR_RAIL_WIDTH}px` });
    expect(screen.getByAltText('OpenCapyBox')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '从用户 test 展开左侧栏', hidden: true })).toHaveTextContent('T');

    fireEvent.click(screen.getByRole('button', { name: '展开左侧栏', hidden: true }));
    expect(shell).toHaveAttribute('data-collapsed', 'false');
    expect(shell).toHaveStyle({ width: `${DEFAULT_APP_SIDEBAR_WIDTH}px` });
    expect(shell).not.toHaveClass('transition-[width]');
    expect(screen.queryByRole('button', { name: '展开左侧栏', hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '从用户 test 展开左侧栏', hidden: true })).not.toBeInTheDocument();
  });

  it('向左拖动吸附为 rail，向右恢复时宽度仍固定为 220px', () => {
    vi.stubGlobal('PointerEvent', MouseEvent);
    render(<Harness />);
    const shell = screen.getByTestId('app-sidebar-shell');
    vi.spyOn(shell, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      right: 220,
      top: 0,
      bottom: 600,
      width: 220,
      height: 600,
      toJSON: () => ({}),
    });
    const separator = screen.getByRole('separator', { name: '调整左侧栏宽度', hidden: true });
    expect(separator).toHaveClass('right-0');
    expect(separator).not.toHaveClass('-right-1');

    fireEvent.pointerDown(separator, { button: 0, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 100, pointerId: 1 });
    expect(shell).toHaveAttribute('data-collapsed', 'true');
    fireEvent.pointerMove(window, { clientX: 300, pointerId: 1 });
    expect(shell).toHaveAttribute('data-collapsed', 'false');
    expect(shell).toHaveStyle({ width: '220px' });
    fireEvent.pointerUp(window, { pointerId: 1 });

    fireEvent.keyDown(separator, { key: 'Home' });
    expect(shell).toHaveAttribute('data-collapsed', 'true');
    fireEvent.pointerDown(separator, { button: 0, pointerId: 2 });
    fireEvent.pointerMove(window, { clientX: 80, pointerId: 2 });
    expect(shell).toHaveAttribute('data-collapsed', 'true');
    fireEvent.pointerMove(window, { clientX: 90, pointerId: 2 });
    expect(shell).toHaveAttribute('data-collapsed', 'false');
    expect(shell).toHaveStyle({ width: '220px' });
    fireEvent.pointerUp(window, { pointerId: 2 });
  });

  it('键盘和按钮均可替代拖动', () => {
    render(<Harness />);
    const shell = screen.getByTestId('app-sidebar-shell');
    const separator = screen.getByRole('separator', { name: '调整左侧栏宽度', hidden: true });

    fireEvent.keyDown(separator, { key: 'Home' });
    expect(shell).toHaveAttribute('data-collapsed', 'true');
    expect(separator).toHaveAttribute('aria-valuenow', '0');
    fireEvent.keyDown(separator, { key: 'ArrowRight' });
    expect(shell).toHaveAttribute('data-collapsed', 'false');
    fireEvent.keyDown(separator, { key: 'End' });
    expect(shell).toHaveStyle({ width: '220px' });
    expect(separator).toHaveAttribute('aria-valuemax', '220');
  });

  it('会话全屏占用边界时只保留统一的会话分隔条', () => {
    render(<Harness boundaryClaimed />);
    expect(screen.queryByRole('separator', { name: '调整左侧栏宽度', hidden: true }))
      .not.toBeInTheDocument();
  });

  it('移动全屏投影声明 modal，并把 Tab 焦点限制在同一个侧栏 owner 内', () => {
    render(
      <AppSidebar collapsed={false} mobileOpen userId="test" onCollapsedChange={vi.fn()}>
        <button type="button">第一个动作</button>
        <button type="button">最后一个动作</button>
      </AppSidebar>,
    );
    const dialog = screen.getByRole('dialog', { name: '会话与工作区' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const first = screen.getByRole('button', { name: '第一个动作' });
    const last = screen.getByRole('button', { name: '最后一个动作' });
    last.focus();
    fireEvent.keyDown(last, { key: 'Tab' });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true });
    expect(last).toHaveFocus();
  });
});
