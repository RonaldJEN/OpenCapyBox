import { Database } from 'lucide-react';

import McpConnectionsPanel from './McpConnectionsPanel';

interface ConnectionsPageProps {
  active: boolean;
  onDirtyChange: (dirty: boolean) => void;
  onPermissionsInvalidated?: () => void;
}

export default function ConnectionsPage({
  active,
  onDirtyChange,
  onPermissionsInvalidated,
}: ConnectionsPageProps) {
  return (
    <main className="h-full min-w-0 flex-1 overflow-y-auto bg-[#fbfaf7] px-4 pb-24 pt-16 text-[#1c1a16] sm:px-7 md:pb-10 md:pt-9 lg:px-10">
      <div className="mx-auto w-full max-w-[1120px]">
        <header className="mb-6 flex items-start gap-4 border-b border-[#e8e3d9] pb-6">
          <div className="mt-0.5 flex h-12 w-12 shrink-0 items-center justify-center rounded-[15px] border border-[#dceade] bg-[#eef7f0] text-[#4d795d] shadow-[0_1px_3px_rgba(30,26,20,0.04)]">
            <Database size={22} strokeWidth={1.9} />
          </div>
          <div>
            <div className="mb-1 text-[11px] font-bold uppercase tracking-[0.18em] text-[#6f927a]">MCP 数据源</div>
            <h1 className="text-[28px] font-bold tracking-[-0.035em] text-[#1c1a16]">数据</h1>
            <p className="mt-1 max-w-[760px] text-sm leading-6 text-[#6f6960]">连接内外部数据，为 Agent 补充实时信息与专业工具。底层通过 MCP 安全接入，连接配置与执行权限分别管理。</p>
          </div>
        </header>
        <McpConnectionsPanel
          active={active}
          onDirtyChange={onDirtyChange}
          onPermissionsInvalidated={onPermissionsInvalidated}
        />
      </div>
    </main>
  );
}
