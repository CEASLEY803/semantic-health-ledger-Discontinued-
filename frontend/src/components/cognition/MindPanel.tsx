'use client';

import { useCognitionLog } from '@/hooks/useCognitionLog';
import type { CognitionLogEntry, CognitionThread } from '@/hooks/useCognitionLog';

// ── Timestamp helper (SQLite UTC "YYYY-MM-DD HH:MM:SS" → local short time) ──────
function fmtTime(iso: string): string {
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(iso) ? iso.replace(' ', 'T') + 'Z' : iso;
  const d = new Date(normalized);
  if (Number.isNaN(d.getTime())) return '';
  const today = d.toDateString() === new Date().toDateString();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return today ? time : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

const KIND_STYLE: Record<CognitionLogEntry['kind'], { dot: string; label: string; text: string }> = {
  idle:        { dot: 'bg-zinc-600',    label: 'text-zinc-600',    text: 'text-zinc-500' },
  observation: { dot: 'bg-amber-400',   label: 'text-amber-400/70', text: 'text-zinc-300' },
  spoke:       { dot: 'bg-violet-400',  label: 'text-violet-300/80', text: 'text-zinc-100' },
};

function ThreadCard({ thread }: { thread: CognitionThread }) {
  const pct = Math.round(Math.min(1, Math.max(0, thread.salience)) * 100);
  return (
    <div className="border-l-2 border-violet-500/40 bg-zinc-900/60 px-3 py-2 ring-1 ring-zinc-800/60">
      <div className="flex items-start justify-between gap-3">
        <p className="font-sans text-sm leading-snug text-zinc-200">{thread.title}</p>
        <span className="shrink-0 font-mono text-[0.625rem] tracking-widest text-violet-300/70">{pct}%</span>
      </div>
      {thread.detail && (
        <p className="mt-1 font-mono text-[0.6875rem] leading-relaxed text-zinc-500">{thread.detail}</p>
      )}
      <p className="mt-1.5 font-mono text-[0.5625rem] tracking-widest text-zinc-600 uppercase">
        watching since {fmtTime(thread.opened_at)}
        {thread.last_surfaced_at ? ` · raised ${fmtTime(thread.last_surfaced_at)}` : ''}
      </p>
    </div>
  );
}

function LogRow({ entry }: { entry: CognitionLogEntry }) {
  const s = KIND_STYLE[entry.kind] ?? KIND_STYLE.idle;
  return (
    <li className="flex gap-3 px-1 py-1.5">
      <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${s.dot}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-3">
          <span className={`font-mono text-[0.5625rem] tracking-widest uppercase ${s.label}`}>{entry.kind}</span>
          <span className="shrink-0 font-mono text-[0.5625rem] tracking-widest text-zinc-700">{fmtTime(entry.created_at)}</span>
        </div>
        <p className={`font-sans text-[0.8125rem] leading-relaxed ${s.text}`}>{entry.summary}</p>
        {entry.kind === 'spoke' && entry.detail && (
          <p className="mt-1 border-l border-violet-500/30 pl-2 font-sans text-xs italic leading-relaxed text-violet-200/70">
            “{entry.detail}”
          </p>
        )}
      </div>
    </li>
  );
}

export default function MindPanel() {
  const { log, threads, isLoading } = useCognitionLog();

  return (
    <div className="flex h-full flex-col gap-4 overflow-y-auto p-4">
      {/* Open threads: what LEDGER is currently preoccupied with */}
      <section>
        <div className="mb-2 flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-violet-400 shadow-[0_0_5px_rgba(167,139,250,0.7)]" />
          <h2 className="font-mono text-[0.6875rem] font-semibold tracking-widest text-violet-300/80">
            OPEN THREADS
          </h2>
          <span className="font-mono text-[0.625rem] tracking-widest text-zinc-600">{threads.length}</span>
        </div>
        {threads.length === 0 ? (
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
            NOTHING ON ITS MIND RIGHT NOW
          </p>
        ) : (
          <div className="flex flex-col gap-2">
            {threads.map((t) => <ThreadCard key={t.id} thread={t} />)}
          </div>
        )}
      </section>

      {/* Stream of consciousness: the running inner monologue */}
      <section className="min-h-0 flex-1">
        <div className="mb-1.5 flex items-center gap-2">
          <h2 className="font-mono text-[0.6875rem] font-semibold tracking-widest text-zinc-400">
            STREAM OF CONSCIOUSNESS
          </h2>
        </div>
        {isLoading && log.length === 0 ? (
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">LISTENING…</p>
        ) : log.length === 0 ? (
          <p className="font-mono text-[0.6875rem] tracking-widest text-zinc-600">
            NO THOUGHTS RECORDED YET: THE HEARTBEAT WILL BEGIN SHORTLY
          </p>
        ) : (
          <ul className="divide-y divide-zinc-800/50">
            {log.map((e) => <LogRow key={e.id} entry={e} />)}
          </ul>
        )}
      </section>
    </div>
  );
}
