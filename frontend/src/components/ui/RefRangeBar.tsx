interface RefRangeBarProps {
  value: number;
  low: number | null;
  high: number | null;
}

export default function RefRangeBar({ value, low, high }: RefRangeBarProps) {
  if (low === null && high === null) {
    return <div className="h-1 w-full bg-white/5" title="No reference range" />;
  }

  // For one-sided ranges, synthesize a bound so the bar still renders sensibly.
  // High-only (< X): left edge is 0 or half the threshold, green band fills left→high.
  // Low-only (> X): right edge is 2× the threshold or 1.2× value, green band fills low→right.
  const effectiveLow  = low  ?? Math.min(0, value * 0.5);
  const effectiveHigh = high ?? Math.max((low ?? 0) * 2, value * 1.2, (low ?? 0) + 1);

  const range   = effectiveHigh - effectiveLow;
  const padding = range * 0.2 || 1;
  const axisMin = effectiveLow  - padding;
  const axisMax = effectiveHigh + padding;
  const axisRange = axisMax - axisMin;

  const toPercent = (v: number) =>
    Math.min(100, Math.max(0, ((v - axisMin) / axisRange) * 100));

  const refLeft  = toPercent(effectiveLow);
  const refWidth = toPercent(effectiveHigh) - refLeft;
  const markerLeft = toPercent(value);
  const isOutOfRange =
    (high !== null && value > high) ||
    (low  !== null && value < low);

  const label = low !== null && high !== null
    ? `Value ${value}, range ${low}–${high}`
    : low !== null
      ? `Value ${value}, minimum ${low}`
      : `Value ${value}, maximum ${high}`;

  return (
    <div className="relative h-3 w-full" aria-label={label}>
      {/* Track */}
      <div className="absolute inset-y-1/2 w-full -translate-y-1/2 h-px bg-white/10" />

      {/* Reference band */}
      <div
        className="absolute inset-y-1/2 -translate-y-1/2 h-1.5 bg-emerald-400/20"
        style={{ left: `${refLeft}%`, width: `${refWidth}%` }}
      />

      {/* Value marker: vertical tick */}
      <div
        className={`absolute inset-y-1/2 -translate-y-1/2 -translate-x-1/2 h-3 w-0.5 ${
          isOutOfRange ? 'bg-rose-500' : 'bg-emerald-400'
        }`}
        style={{ left: `${markerLeft}%` }}
      />
    </div>
  );
}
