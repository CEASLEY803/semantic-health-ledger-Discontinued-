'use client';

import { useRef, useState } from 'react';
import {
  useTranscriptIngest,
  type TranscriptStage,
} from '@/hooks/useTranscriptIngest';

const STAGE_LABEL: Record<TranscriptStage, string> = {
  chunking: 'CHUNKING',
  embedding: 'EMBEDDING',
  extracting_facts: 'EXTRACTING FACTS',
  complete: 'COMPLETE',
  failed: 'FAILED',
};

const STATUS_COLOR: Record<string, string> = {
  pending: 'text-zinc-400',
  processing: 'text-amber-400',
  complete: 'text-emerald-400',
  failed: 'text-rose-400',
};

const ACCEPT = '.pdf,.txt,.md';

export default function TranscriptImport() {
  const { documents, stages, uploadFile, deleteDocument, isUploading, error } =
    useTranscriptIngest();
  const [open, setOpen] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    for (const file of Array.from(files)) {
      try {
        await uploadFile(file);
      } catch {
        /* error surfaced via hook state */
      }
    }
  };

  const inProgress = documents.filter(
    (d) => d.status === 'pending' || d.status === 'processing',
  ).length;

  return (
    <div className="absolute right-4 top-4 z-20 w-80 font-mono text-[0.75rem]">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center justify-between rounded-sm border border-zinc-700 bg-zinc-900/90 px-3 py-2 tracking-widest text-zinc-300 backdrop-blur transition-colors hover:border-cyan-500/60 hover:text-cyan-300"
      >
        <span>TRANSCRIPT MEMORY</span>
        <span className="flex items-center gap-2 text-zinc-500">
          {inProgress > 0 && (
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
          )}
          {documents.length}
          <span>{open ? '▾' : '▸'}</span>
        </span>
      </button>

      {open && (
        <div className="mt-2 rounded-sm border border-zinc-800 bg-zinc-950/95 p-3 backdrop-blur">
          {/* Dropzone */}
          <div
            onClick={() => inputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              void handleFiles(e.dataTransfer.files);
            }}
            className={`cursor-pointer rounded-sm border border-dashed px-3 py-4 text-center transition-colors ${
              dragOver
                ? 'border-cyan-400 bg-cyan-950/30 text-cyan-300'
                : 'border-zinc-700 text-zinc-500 hover:border-zinc-500 hover:text-zinc-300'
            }`}
          >
            {isUploading ? 'UPLOADING…' : 'DROP PDF / TXT  ·  OR CLICK'}
          </div>
          <input
            ref={inputRef}
            type="file"
            accept={ACCEPT}
            multiple
            className="hidden"
            onChange={(e) => {
              void handleFiles(e.target.files);
              e.target.value = '';
            }}
          />

          <p className="mt-2 leading-relaxed text-zinc-600">
            Transcripts are chunked &amp; embedded for semantic recall; the full
            text never loads into the chat context.
          </p>

          {error && <p className="mt-2 text-rose-400">ERR // {error}</p>}

          {/* Document list */}
          {documents.length > 0 && (
            <ul className="mt-3 max-h-64 space-y-1 overflow-y-auto">
              {documents.map((doc) => {
                const liveStage = stages[doc.id];
                const label =
                  doc.status === 'processing' && liveStage
                    ? STAGE_LABEL[liveStage] ?? doc.status.toUpperCase()
                    : doc.status.toUpperCase();
                return (
                  <li
                    key={doc.id}
                    className="group flex items-center justify-between gap-2 rounded-sm border border-zinc-900 bg-zinc-900/40 px-2 py-1.5"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-zinc-300" title={doc.filename}>
                        {doc.filename}
                      </div>
                      <div
                        className={`text-[0.65rem] tracking-wider ${
                          STATUS_COLOR[doc.status] ?? 'text-zinc-500'
                        }`}
                      >
                        {label}
                        {doc.status === 'complete' && doc.total_chunks > 0 && (
                          <span className="text-zinc-600">
                            {' '}
                            · {doc.total_chunks} chunks
                          </span>
                        )}
                        {doc.status === 'failed' && doc.error && (
                          <span className="text-zinc-600"> · {doc.error}</span>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={() => void deleteDocument(doc.id)}
                      className="shrink-0 text-zinc-600 opacity-0 transition-opacity hover:text-rose-400 group-hover:opacity-100"
                      title="Delete transcript"
                    >
                      ×
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
