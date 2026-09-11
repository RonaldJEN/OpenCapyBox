import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '../utils/test-utils';
import { QuestionCard } from '../../components/QuestionCard';
import type { AskUserQuestion } from '../../types';

const mockQuestions: AskUserQuestion[] = [
  {
    header: 'DB',
    question: '选哪个数据库？',
    options: [
      { label: 'PostgreSQL', description: '功能强、生态完整' },
      { label: 'MySQL', description: '易用且部署广泛' },
    ],
  },
];

describe('QuestionCard 组件', () => {
  it('第二题缺 options 时保留题文与自由输入，来回翻页不丢答案', () => {
    const onSubmit = vi.fn();
    const questions = [mockQuestions[0], { header: '防覆盖', question: '是否保留补丁？', label: '另存补丁并记忆', description: '错误地铺在题目顶层' }];
    render(<QuestionCard questions={questions} onSubmit={onSubmit} />);
    fireEvent.click(screen.getByRole('button', { name: /PostgreSQL/ }));
    fireEvent.click(screen.getByRole('button', { name: /下一题/ }));
    expect(screen.getByText('是否保留补丁？')).toBeVisible();
    expect(screen.getByText('本题未提供有效选项，请直接输入回答。')).toBeVisible();
    expect(screen.queryByRole('button', { name: '另存补丁并记忆' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: '是否保留补丁？' }), { target: { value: '只保存补丁' } });
    fireEvent.click(screen.getByRole('button', { name: /上一题/ }));
    expect(screen.getByRole('button', { name: /PostgreSQL/ })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: /下一题/ }));
    expect(screen.getByRole('textbox', { name: '是否保留补丁？' })).toHaveValue('只保存补丁');
    fireEvent.click(screen.getByRole('button', { name: '提交' }));
    expect(onSubmit).toHaveBeenCalledWith({ '选哪个数据库？': 'PostgreSQL', '是否保留补丁？': '只保存补丁' });
  });

  it('旧的多选题 options 为 null 时自由输入与跳过都可用', () => {
    const onSubmit = vi.fn();
    render(<QuestionCard questions={[{ header: '旧题', question: '输入要求', options: null, multiSelect: true }]} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '保留格式' } });
    fireEvent.click(screen.getByRole('button', { name: '提交' }));
    expect(onSubmit).toHaveBeenLastCalledWith({ '输入要求': '保留格式' });
    fireEvent.click(screen.getByRole('button', { name: '跳过并提交' }));
    expect(onSubmit).toHaveBeenLastCalledWith({ '输入要求': '[No preference]' });
  });

  it('题目正文缺失时局部提示错误，不提交无法识别的问题', () => {
    render(<QuestionCard questions={[{ options: mockQuestions[0].options }]} onSubmit={vi.fn()} />);
    expect(screen.getByRole('alert')).toHaveTextContent('问题内容不完整');
    expect(screen.queryByRole('button', { name: '提交' })).not.toBeInTheDocument();
  });

});
