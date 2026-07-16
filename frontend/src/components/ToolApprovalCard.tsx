import { CheckCircle2, Clock3, ShieldAlert, ShieldCheck, X, XCircle } from 'lucide-react';

import type { ToolApprovalPayload } from '../types';

interface ToolApprovalCardProps {
  approval: ToolApprovalPayload;
  onSubmit: (answers: Record<string, string>) => void;
  onDismiss?: () => void;
  disabled?: boolean;
}

const choices = [
  { resolution: 'allow_once', label: '允许本次', description: '仅执行当前这一次调用', icon: CheckCircle2, className: 'border-[#b9d8c0] text-[#327043] hover:bg-[#edf7ef]' },
  { resolution: 'allow_session', label: '本会话允许', description: '当前对话后续不再询问', icon: Clock3, className: 'border-[#cfc8ea] text-[#6759a6] hover:bg-[#f1eefb]' },
  { resolution: 'allow_always', label: '永久允许', description: '创建一条用户级 ALLOW 规则', icon: ShieldCheck, className: 'border-[#c4d5e7] text-[#426b91] hover:bg-[#edf4fa]' },
  { resolution: 'deny', label: '拒绝', description: '不执行这次工具调用', icon: XCircle, className: 'border-[#e7b7b2] text-[#a33b31] hover:bg-[#fff0ee]' },
] as const;

export function ToolApprovalCard({ approval, onSubmit, onDismiss, disabled = false }: ToolApprovalCardProps) {
  const submit = (resolution: typeof choices[number]['resolution']) => {
    if (!disabled) onSubmit({ approval: resolution });
  };

  return (
    <div className="overflow-hidden rounded-2xl border border-[#e3c894] bg-white shadow-xl">
      <div className="flex items-start justify-between gap-3 border-b border-[#eee3cf] bg-[#fffaf0] px-4 py-3">
        <div className="flex min-w-0 items-start gap-2.5">
          <ShieldAlert size={18} className="mt-0.5 shrink-0 text-[#a66a19]" />
          <div>
            <p className="text-sm font-semibold text-[#352f27]">工具执行需要确认</p>
            <p className="mt-0.5 text-xs leading-5 text-[#796f60]">
              {approval.source_type === 'official' ? '官方 MCP' : approval.source_type === 'personal' ? '个人 MCP' : '内置工具'}
              {approval.server_name ? ` · ${approval.server_name}` : ''}
            </p>
          </div>
        </div>
        {onDismiss && (
          <button type="button" onClick={onDismiss} disabled={disabled} aria-label="暂时隐藏审批" className="rounded-md p-1 text-[#968b7b] hover:bg-[#f4ead7] hover:text-[#5f5549] disabled:opacity-50">
            <X size={14} />
          </button>
        )}
      </div>

      <div className="px-4 py-4">
        <code className="block break-all rounded-lg bg-[#f5f2ec] px-3 py-2 text-[12px] text-[#302d28]">{approval.tool_ref}</code>
        {approval.tool_description && <p className="mt-3 text-xs leading-5 text-[#746d62]">{approval.tool_description}</p>}
        <div className="mt-3 rounded-xl border border-[#e8e3d9] bg-[#fcfbf8] px-3 py-3">
          <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-[#8c8478]">调用参数</p>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-[11px] leading-5 text-[#4d4840]">{approval.arguments_display || '{}'}</pre>
        </div>
        {approval.warning && <p className="mt-3 rounded-lg bg-[#fff4e1] px-3 py-2 text-xs leading-5 text-[#93621b]">{approval.warning}</p>}

        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {choices.map((choice) => {
            const Icon = choice.icon;
            return (
              <button type="button" key={choice.resolution} onClick={() => submit(choice.resolution)} disabled={disabled} className={`flex items-start gap-2.5 rounded-xl border px-3 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-50 ${choice.className}`}>
                <Icon size={16} className="mt-0.5 shrink-0" />
                <span>
                  <span className="block text-xs font-semibold">{choice.label}</span>
                  <span className="mt-0.5 block text-[11px] leading-4 opacity-75">{choice.description}</span>
                </span>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
