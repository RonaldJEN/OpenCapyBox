import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '../utils/test-utils';
import FeedbackMessage from '../../components/FeedbackMessage';

describe('FeedbackMessage', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('成功提示默认 3 秒自动关闭，并在悬停期间暂停倒计时', () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(
      <FeedbackMessage tone="success" onDismiss={onDismiss}>
        保存成功
      </FeedbackMessage>,
    );

    const message = screen.getByRole('status');
    act(() => vi.advanceTimersByTime(1500));
    fireEvent.mouseEnter(message);
    act(() => vi.advanceTimersByTime(5000));
    expect(onDismiss).not.toHaveBeenCalled();

    fireEvent.mouseLeave(message);
    act(() => vi.advanceTimersByTime(1499));
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('警告默认不自动消失，但可以手动关闭', () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(
      <FeedbackMessage tone="warning" onDismiss={onDismiss}>
        配置已保存但未启用
      </FeedbackMessage>,
    );

    act(() => vi.advanceTimersByTime(60_000));
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('错误使用 alert 语义', () => {
    render(
      <FeedbackMessage tone="error" onDismiss={() => {}}>
        操作失败
      </FeedbackMessage>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('操作失败');
  });

  it('错误 3 秒后隐藏，富文本重渲染不续期，新一次相同错误重新展示', () => {
    vi.useFakeTimers();
    const view = render(<FeedbackMessage tone="error" messageKey={1}><span>操作失败</span></FeedbackMessage>);
    act(() => vi.advanceTimersByTime(1500));
    view.rerender(<FeedbackMessage tone="error" messageKey={1}><span>操作失败</span></FeedbackMessage>);
    act(() => vi.advanceTimersByTime(1499));
    expect(screen.getByRole('alert')).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    view.rerender(<FeedbackMessage tone="error" messageKey={2}><span>操作失败</span></FeedbackMessage>);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    act(() => vi.advanceTimersByTime(3000));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    view.rerender(<FeedbackMessage tone="error" messageKey={3}>另一个错误</FeedbackMessage>);
    view.rerender(<FeedbackMessage tone="error" messageKey={2}>操作失败</FeedbackMessage>);
    expect(screen.getByRole('alert')).toHaveTextContent('操作失败');
  });
});
