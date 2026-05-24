// Module-level pub-sub for the gesture-mode toggle. The worker sends
// {"type":"gesture_mode","enabled":bool} over the ui-command data
// topic; useUiCommandChannel forwards the boolean here, and the
// GestureModeOverlay (mounted inside WidgetLayer) subscribes to render
// the fullscreen mirrored camera view when active.
//
// Same pattern as widget-volume-bus.ts. Avoids React Context plumbing
// for a single global boolean.

type Listener = (enabled: boolean) => void;

let current = false;
const listeners = new Set<Listener>();

export function getGestureMode(): boolean {
  return current;
}

export function setGestureMode(enabled: boolean): void {
  if (current === enabled) return;
  current = enabled;
  for (const fn of listeners) {
    try {
      fn(enabled);
    } catch (err) {
      console.warn('[gesture-mode] listener threw', err);
    }
  }
}

export function subscribeGestureMode(fn: Listener): () => void {
  listeners.add(fn);
  // Fire once with the current state so the listener doesn't have
  // to read the initial value separately.
  fn(current);
  return () => {
    listeners.delete(fn);
  };
}
