import { useState, useEffect, useRef } from 'react';

export interface TelemetryData {
  cpu_pct: number;
  mem_mb: number;
  db_mb: number;
  uptime_s: number;
  records_today: number;
  api_calls_today: number;
}

const BASE = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8787';

export function useTelemetry(intervalMs = 5000) {
  const [data, setData] = useState<TelemetryData | null>(null);
  const [online, setOnline] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = async () => {
    try {
      const res = await fetch(`${BASE}/api/v1/telemetry`);
      if (!res.ok) throw new Error('non-ok');
      setData(await res.json());
      setOnline(true);
    } catch {
      setOnline(false);
    }
  };

  useEffect(() => {
    poll();
    timer.current = setInterval(poll, intervalMs);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);

  return { data, online };
}
