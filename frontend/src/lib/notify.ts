// ── Native OS notifications (Tauri) ───────────────────────────────────────────
// Thin wrapper around @tauri-apps/plugin-notification. Used to surface LEDGER's
// unsolicited thoughts as a native toast when the window is hidden in the tray or
// not focused, so it can reach the user even when they aren't looking at the app.
//
// Dynamically imported and fully guarded: in a plain browser (non-Tauri) context
// every call is a silent no-op rather than a thrown error.

let _permission: 'granted' | 'denied' | 'default' | 'unknown' = 'unknown';

/** Fire a native notification if the platform supports it and permission allows. */
export async function notify(title: string, body: string): Promise<void> {
  try {
    const mod = await import('@tauri-apps/plugin-notification');

    if (_permission !== 'granted') {
      const granted = await mod.isPermissionGranted();
      if (granted) {
        _permission = 'granted';
      } else {
        const result = await mod.requestPermission();
        _permission = result;
        if (result !== 'granted') return;
      }
    }

    mod.sendNotification({ title, body });
  } catch {
    // Not running under Tauri (or plugin unavailable); silently skip.
  }
}

/** True when the user almost certainly isn't looking at the app right now. */
export function userIsAway(): boolean {
  if (typeof document === 'undefined') return false;
  return document.hidden || !document.hasFocus();
}
