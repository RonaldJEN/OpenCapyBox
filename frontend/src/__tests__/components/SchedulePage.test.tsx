import { render, screen } from '../utils/test-utils';
import { describe, expect, it, vi } from 'vitest';

import SchedulePage from '../../components/SchedulePage';

vi.mock('../../components/CronSchedule', () => ({
  default: ({ variant, unreadCount }: { variant?: string; unreadCount?: number }) => (
    <div data-testid="cron-schedule-mock" data-variant={variant} data-unread-count={unreadCount} />
  ),
}));

describe('SchedulePage', () => {
  it('与 Skills 和数据共用一级页面标题结构，并以内嵌形态承载日程', () => {
    render(<SchedulePage unreadCount={9} onUnreadChange={vi.fn()} />);

    expect(screen.getByRole('heading', { level: 1, name: '日程管理' })).toBeInTheDocument();
    expect(screen.getByText('Agent 自动化')).toBeInTheDocument();
    expect(screen.getByText(/安排重复任务/)).toBeInTheDocument();
    expect(screen.getByTestId('cron-schedule-mock')).toHaveAttribute('data-variant', 'page');
    expect(screen.getByTestId('cron-schedule-mock')).toHaveAttribute('data-unread-count', '9');
  });
});
