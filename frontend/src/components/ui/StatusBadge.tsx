export interface StatusBadgeProps {
  status: 'flagged' | 'high' | 'low' | 'normal' | 'no-range';
}

const CONFIG = {
  flagged:    { label: 'FLAGGED', classes: 'bg-rose-500/10 text-rose-400 ring-rose-500/30' },
  high:       { label: 'HI',      classes: 'bg-amber-400/10 text-amber-400 ring-amber-400/30' },
  low:        { label: 'LO',      classes: 'bg-yellow-400/10 text-yellow-400 ring-yellow-400/30' },
  normal:     { label: 'NL',      classes: 'bg-emerald-400/10 text-emerald-400 ring-emerald-400/30' },
  'no-range': { label: 'N/R',     classes: 'bg-zinc-800/60 text-zinc-600 ring-zinc-700/50' },
} satisfies Record<StatusBadgeProps['status'], { label: string; classes: string }>;

export default function StatusBadge({ status }: StatusBadgeProps) {
  const { label, classes } = CONFIG[status];
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 font-mono text-[0.6875rem] font-bold tracking-widest ring-1 ring-inset ${classes}`}
    >
      {label}
    </span>
  );
}
