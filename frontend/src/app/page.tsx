'use client';

import { useState, useCallback, useRef, useEffect } from 'react';
import ChatPanel from '@/components/chat/ChatPanel';
import Dashboard from '@/components/dashboard/Dashboard';

const CHAT_MIN = 320;
const CHAT_MAX = 1100;
const CHAT_DEFAULT = 560;

export default function Home() {
  const [chatWidth, setChatWidth] = useState(CHAT_DEFAULT);

  // droppedPath: from Tauri's native drag-drop (gives OS file paths)
  const [droppedPath, setDroppedPath] = useState<string | null>(null);
  // droppedFile: from HTML5 drag-drop (gives File objects, works in some contexts)
  const [droppedFile, setDroppedFile] = useState<File | null>(null);

  const [isDraggingOver, setIsDraggingOver] = useState(false);
  const dragDepth = useRef(0); // HTML5 depth counter

  const dragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(CHAT_DEFAULT);

  // ── Pane resize ──────────────────────────────────────────────────────────────

  const onDividerMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    startX.current = e.clientX;
    startWidth.current = chatWidth;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, [chatWidth]);

  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragging.current) return;
      const delta = e.clientX - startX.current;
      const next = Math.min(CHAT_MAX, Math.max(CHAT_MIN, startWidth.current + delta));
      setChatWidth(next);
    };
    const onMouseUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    return () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
    };
  }, []);

  // ── Tauri native drag-drop ───────────────────────────────────────────────────
  // Fires for OS-level file drops (Explorer, downloads bar, etc.)
  // Gives real file-system paths.

  useEffect(() => {
    let unlisten: (() => void) | undefined;

    import('@tauri-apps/api/webview')
      .then(({ getCurrentWebview }) =>
        getCurrentWebview().onDragDropEvent((event) => {
          const p = event.payload;
          console.log('[tauri drag-drop]', p.type, p);

          if (p.type === 'enter') {
            setIsDraggingOver(true);
          } else if (p.type === 'leave') {
            setIsDraggingOver(false);
          } else if (p.type === 'drop') {
            setIsDraggingOver(false);
            const paths = (p as { type: 'drop'; paths: string[] }).paths ?? [];
            console.log('[tauri drag-drop] paths:', paths);

            if (paths.length > 0) {
              // Accept the first dropped path; let the backend validate the type
              setDroppedPath(paths[0]);
            } else {
              // Tauri got the drop event but no path (Chrome virtual-file format).
              // Signal ChatPanel to show a helpful error.
              setDroppedPath('__NO_PATH__');
            }
          }
        }),
      )
      .then((fn) => { unlisten = fn; })
      .catch(() => {
        // Not running inside Tauri: ignore
      });

    return () => { unlisten?.(); };
  }, []);

  // ── HTML5 drag-drop (fallback) ───────────────────────────────────────────────
  // Fires when the drag originates from within a web context and Tauri's native
  // handler does NOT intercept it (e.g. dragging from a browser's PDF viewer pane).

  const onDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current += 1;
    setIsDraggingOver(true);
  }, []);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = Math.max(0, dragDepth.current - 1);
    if (dragDepth.current === 0) setIsDraggingOver(false);
  }, []);

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  }, []);

  const onDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    dragDepth.current = 0;
    setIsDraggingOver(false);

    const file = e.dataTransfer.files[0];
    if (file) {
      console.log('[html5 drop] file:', file.name, file.type);
      setDroppedFile(file);
    }
  }, []);

  return (
    <main
      className="flex h-screen w-screen overflow-hidden bg-zinc-950 text-zinc-100"
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      {/* PDF drop overlay */}
      {isDraggingOver && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/90">
          <div className="flex flex-col items-center gap-4 rounded-sm border-2 border-dashed border-cyan-500/60 px-16 py-12">
            <svg className="h-10 w-10 text-cyan-500/80" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m6.75 12-3-3m0 0-3 3m3-3v6m-1.5-15H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
            </svg>
            <div className="text-center">
              <p className="font-mono text-sm tracking-widest text-cyan-400">DROP LAB RESULTS</p>
              <p className="mt-1 font-mono text-[0.6875rem] tracking-widest text-zinc-500">PDF or CSV · values extracted and committed</p>
            </div>
          </div>
        </div>
      )}

      {/* Left: chat */}
      <section
        className="flex flex-none flex-col bg-zinc-900"
        style={{ width: chatWidth }}
      >
        <ChatPanel
          onIngestSuccess={() => {}}
          droppedPath={droppedPath}
          onPathConsumed={() => setDroppedPath(null)}
          droppedFile={droppedFile}
          onFileConsumed={() => setDroppedFile(null)}
        />
      </section>

      {/* Draggable divider */}
      <div
        onMouseDown={onDividerMouseDown}
        className="w-2 flex-none cursor-col-resize bg-zinc-800/60 hover:bg-zinc-700 transition-colors"
        title="Drag to resize"
      />

      {/* Right: dashboard */}
      <section className="min-w-0 flex-1 overflow-y-auto bg-zinc-950">
        <Dashboard />
      </section>
    </main>
  );
}
