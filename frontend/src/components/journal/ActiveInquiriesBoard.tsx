'use client';

import { useEffect, useState } from 'react';
import { fetchDailyInquiries } from '@/lib/api';
import { bus } from '@/lib/events';

export function ActiveInquiriesBoard() {
  const [questions, setQuestions] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    fetchDailyInquiries()
      .then((data) => {
        setQuestions(Array.isArray(data.questions) ? data.questions : []);
        setLoaded(true);
      })
      .catch(() => setLoaded(true));
  }, []);

  // Don't flash an empty state while the request is in flight
  if (!loaded) return null;

  // ── Empty state ──────────────────────────────────────────────────────────────
  if (questions.length === 0) {
    return (
      <div className="flex items-center gap-3 border border-dashed border-zinc-800 px-4 py-3">
        <span className="h-1.5 w-1.5 flex-none rounded-full bg-zinc-700" />
        <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-600 uppercase">
          No active inquiries for today · Telemetry complete
        </span>
      </div>
    );
  }

  // ── Active inquiries ─────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-3">
      <span className="font-mono text-[0.6875rem] tracking-widest text-zinc-400 uppercase">
        Today's Objectives
      </span>

      <div className="flex flex-col gap-3 border border-zinc-800 bg-zinc-900 p-4">
        {/* Board header */}
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 flex-none rounded-full bg-cyan-400 shadow-[0_0_6px_rgba(34,211,238,0.55)]" />
          <span className="font-mono text-[0.6875rem] tracking-widest text-cyan-500/70 uppercase">
            Active Clinical Inquiries
          </span>
          <span className="ml-auto font-mono text-[0.6875rem] tabular-nums text-zinc-600">
            {questions.length} {questions.length === 1 ? 'question' : 'questions'}
          </span>
        </div>

        {/* Question list: each question is now a clickable reply trigger */}
        <div className="flex flex-col gap-2">
          {questions.map((q, i) => (
            <button
              key={i}
              type="button"
              onClick={() => bus.emit('INITIATE_JOURNAL_REPLY', { question: q })}
              className="group w-full cursor-pointer border-l-2 border-cyan-500/40 bg-cyan-500/[0.03] py-2 pl-3 pr-3 text-left ring-1 ring-inset ring-cyan-500/[0.06] transition-all hover:border-cyan-400/70 hover:bg-cyan-500/[0.09] hover:ring-cyan-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/50"
            >
              <div className="flex items-start gap-2.5">
                <span className="mt-px flex-none font-mono text-[0.6875rem] font-semibold tabular-nums text-cyan-600/60">
                  {String(i + 1).padStart(2, '0')}
                </span>
                <p className="flex-1 text-sm leading-relaxed text-zinc-200">{q}</p>
                {/* Reply indicator: invisible until hover */}
                <span className="mt-0.5 shrink-0 font-mono text-[0.625rem] tracking-widest text-cyan-600/0 transition-colors group-hover:text-cyan-500/70 whitespace-nowrap">
                  Reply ↵
                </span>
              </div>
            </button>
          ))}
        </div>

        {/* UX sub-note */}
        <p className="border-t border-zinc-800 pt-2.5 font-mono text-[0.625rem] tracking-widest text-zinc-600">
          CLICK TO REPLY IN CHAT · AI AUTOMATICALLY LINKS RESPONSES TO THESE QUESTIONS
        </p>
      </div>
    </div>
  );
}
