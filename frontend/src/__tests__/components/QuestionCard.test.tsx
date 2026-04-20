import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '../utils/test-utils';
import { QuestionCard } from '../../components/QuestionCard';
import type { AskUserQuestion } from '../../types';

const mockQuestions: AskUserQuestion[] = [
  {
    header: 'DB',
    question: '选哪个数据库？',
    options: [
      { label: 'PostgreSQL' },
      { label: 'MySQL' },
    ],
  },
];

describe('QuestionCard 组件', () => {
  it('未传 onDismiss 时不渲染关闭按钮', () => {
    render(
      <QuestionCard questions={mockQuestions} onSubmit={vi.fn()} />
    );

    expect(screen.queryByLabelText('关闭问题')).not.toBeInTheDocument();
  });

  it('传 onDismiss 时渲染关闭按钮，点击触发回调且不提交答案', () => {
    const onDismiss = vi.fn();
    const onSubmit = vi.fn();

    render(
      <QuestionCard
        questions={mockQuestions}
        onSubmit={onSubmit}
        onDismiss={onDismiss}
      />
    );

    const closeBtn = screen.getByLabelText('关闭问题');
    expect(closeBtn).toBeInTheDocument();

    fireEvent.click(closeBtn);

    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('disabled 时关闭按钮不可用，点击不触发 onDismiss', () => {
    const onDismiss = vi.fn();

    render(
      <QuestionCard
        questions={mockQuestions}
        onSubmit={vi.fn()}
        onDismiss={onDismiss}
        disabled
      />
    );

    const closeBtn = screen.getByLabelText('关闭问题') as HTMLButtonElement;
    expect(closeBtn).toBeDisabled();

    fireEvent.click(closeBtn);
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it('Skip 与 Dismiss 是不同路径：Skip 提交 [No preference]，Dismiss 不提交', () => {
    const onSubmit = vi.fn();
    const onDismiss = vi.fn();

    render(
      <QuestionCard
        questions={mockQuestions}
        onSubmit={onSubmit}
        onDismiss={onDismiss}
      />
    );

    // 点击 Skip：单题场景下直接提交 [No preference]
    fireEvent.click(screen.getByText('Skip'));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith({ '选哪个数据库？': '[No preference]' });
    expect(onDismiss).not.toHaveBeenCalled();

    // 点击 Dismiss：不提交，仅触发 onDismiss
    onSubmit.mockClear();
    fireEvent.click(screen.getByLabelText('关闭问题'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
