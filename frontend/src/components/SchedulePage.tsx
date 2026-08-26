import { CalendarClock } from 'lucide-react';

import CronSchedule from './CronSchedule';

interface SchedulePageProps {
  unreadCount: number;
  onUnreadChange: (count: number) => void;
}

export default function SchedulePage({
  unreadCount,
  onUnreadChange,
}: SchedulePageProps) {
  return (
    <main className="h-full min-w-0 flex-1 overflow-hidden bg-[#fbfaf7] px-4 pb-4 pt-16 text-[#1c1a16] sm:px-7 md:pb-6 md:pt-9 lg:px-10">
      <div className="mx-auto flex h-full w-full max-w-[1120px] flex-col">
        <header className="mb-5 flex shrink-0 items-start gap-4 border-b border-[#e8e3d9] pb-6">
          <div className="mt-0.5 flex h-12 w-12 shrink-0 items-center justify-center rounded-[15px] border border-[#eadfcf] bg-[#fbf2e6] text-[#9a6a36] shadow-[0_1px_3px_rgba(30,26,20,0.04)]">
            <CalendarClock size={22} strokeWidth={1.9} aria-hidden="true" />
          </div>
          <div>
            <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.18em] text-[#a27b52]">Agent 自动化</div>
            <h1 className="text-[28px] font-bold tracking-[-0.035em] text-[#1c1a16]">日程管理</h1>
            <p className="mt-1 max-w-[760px] text-sm leading-6 text-[#6f6960]">安排重复任务，查看执行状态与历史结果，让 Agent 在约定时间持续工作。</p>
          </div>
        </header>

        <div className="min-h-0 flex-1">
          <CronSchedule
            variant="page"
            unreadCount={unreadCount}
            onUnreadChange={onUnreadChange}
          />
        </div>
      </div>
    </main>
  );
}
