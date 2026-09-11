import type { LucideIcon } from 'lucide-react';

export function ActivityIcon({ icon: Icon, spinning = false, className = '' }: {
  icon: LucideIcon; spinning?: boolean; className?: string;
}) {
  return <Icon size={16} strokeWidth={1.75} aria-hidden="true"
    className={`shrink-0 ${spinning ? 'animate-spin motion-reduce:animate-none' : ''} ${className}`} />;
}
