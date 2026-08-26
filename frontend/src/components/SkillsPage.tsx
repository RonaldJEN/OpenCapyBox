import { Blocks } from 'lucide-react';

import SkillsPanel from './SkillsPanel';

export default function SkillsPage() {
  return (
    <main className="h-full min-w-0 flex-1 overflow-y-auto bg-[#fbfaf7] px-4 pb-24 pt-16 text-[#1c1a16] sm:px-7 md:pb-10 md:pt-9 lg:px-10">
      <div className="mx-auto w-full max-w-[1120px]">
        <header className="mb-6 flex items-start gap-4 border-b border-[#e8e3d9] pb-6">
          <div className="mt-0.5 flex h-12 w-12 shrink-0 items-center justify-center rounded-[15px] border border-[#dde2f5] bg-[#eef1ff] text-[#5968d8] shadow-[0_1px_3px_rgba(30,26,20,0.04)]">
            <Blocks size={22} strokeWidth={1.9} />
          </div>
          <div>
            <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.18em] text-[#7b82ad]">Agent 能力库</div>
            <h1 className="text-[28px] font-bold tracking-[-0.035em] text-[#1c1a16]">Skills</h1>
            <p className="mt-1 max-w-[720px] text-sm leading-6 text-[#6f6960]">复用成熟经验，让 Agent 在合适的任务中调用对应工作流。启停只影响后续运行。</p>
          </div>
        </header>
        <SkillsPanel />
      </div>
    </main>
  );
}
