'use client';

// Fullscreen mirrored camera preview shown while gesture mode is active.
// Lets the user SEE their hand so the existing useGestureControl
// (which reads from a detached off-screen video) translates gestures
// into widget interactions intuitively.
//
// The gesture cursor at z-[100000] (rendered by WidgetLayer) floats
// over this overlay, so users see both the camera and the cursor.

import { useEffect, useState } from 'react';
import { Track } from 'livekit-client';
import { useTracks, VideoTrack } from '@livekit/components-react';
import { subscribeGestureMode } from '@/lib/jarvis-ui/gesture-mode-bus';

export function GestureModeOverlay() {
  const [active, setActive] = useState(false);
  useEffect(() => subscribeGestureMode(setActive), []);
  const tracks = useTracks([Track.Source.Camera]);
  const cam = tracks.find((t) => t.participant?.isLocal && t.publication);

  if (!active) return null;

  return (
    <div className="pointer-events-none fixed inset-0 z-30 flex items-center justify-center bg-black/85 p-6">
      <div
        className="relative h-full w-full overflow-hidden rounded-2xl border-2 shadow-[0_0_40px_rgba(60,223,255,0.25)]"
        style={{
          borderColor: '#3CDFFF',
          maxHeight: 'min(80vh, 900px)',
          maxWidth: 'min(95%, 1600px)',
        }}
      >
        {cam ? (
          <VideoTrack
            trackRef={cam}
            className="h-full w-full object-cover"
            // Selfie mirror — wave right hand, it goes right on screen.
            style={{ transform: 'scaleX(-1)' }}
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center font-mono text-xs uppercase tracking-[0.34em] text-[#3CDFFF]">
            Camera warming up…
          </div>
        )}
        <div className="absolute left-4 top-3 rounded bg-black/55 px-2 py-1 font-mono text-[10px] uppercase tracking-[0.28em] text-[#3CDFFF] backdrop-blur">
          ● Gesture Mode — say &ldquo;turn off gesture mode&rdquo; to exit
        </div>
      </div>
    </div>
  );
}
