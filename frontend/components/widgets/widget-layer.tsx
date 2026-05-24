'use client';

// Full-screen layer that renders every open widget plus the gesture
// cursor. Mounted once near the app root. `pointer-events-none` so it
// never blocks the session UI underneath; each WidgetShell re-enables
// pointer events for itself.
//
// Sits at z-40 — above the session content, below the z-50 control bar
// so the mic / leave buttons stay reachable.

import { useCallback, useState } from 'react';
import { AnimatePresence } from 'motion/react';
import { useGestureControl } from '@/hooks/useGestureControl';
import { useJarvisUIChannel } from '@/hooks/useJarvisUIChannel';
import { useJarvisUIStatePublisher } from '@/hooks/useJarvisUIStatePublisher';
import { useUiCommandChannel } from '@/hooks/useUiCommandChannel';
import type { CursorState } from '@/lib/jarvis-ui/gestures';
import { useJarvisUI } from '@/lib/jarvis-ui/store';
import { GestureCursor } from './gesture-cursor';
import { GestureModeOverlay } from './gesture-mode-overlay';
import { getWidgetComponent } from './registry';
import { WidgetShell } from './widget-shell';

export function WidgetLayer() {
  useJarvisUIChannel();
  useJarvisUIStatePublisher();
  useUiCommandChannel();
  const { widgets, highlightId, close, focus, move } = useJarvisUI();
  const [cursor, setCursor] = useState<CursorState | null>(null);

  // Clamp moves so a widget's title bar always stays grabbable.
  const clampedMove = useCallback(
    (id: string, x: number, y: number) => {
      const vw = typeof window !== 'undefined' ? window.innerWidth : 1920;
      const vh = typeof window !== 'undefined' ? window.innerHeight : 1080;
      move(id, Math.min(Math.max(x, -40), vw - 80), Math.min(Math.max(y, 0), vh - 56));
    },
    [move]
  );

  useGestureControl(setCursor);

  return (
    <div className="pointer-events-none fixed inset-0 z-40">
      <AnimatePresence>
        {widgets.map((widget) => {
          const Widget = getWidgetComponent(widget.kind);
          return (
            <WidgetShell
              key={widget.id}
              widget={widget}
              highlighted={widget.id === highlightId}
              onClose={() => close(widget.id)}
              onFocus={() => focus(widget.id)}
              onMove={(x, y) => clampedMove(widget.id, x, y)}
            >
              <Widget widget={widget} />
            </WidgetShell>
          );
        })}
      </AnimatePresence>
      <GestureModeOverlay />
      <GestureCursor cursor={cursor} />
    </div>
  );
}
