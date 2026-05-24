'use client';

// YouTube results: the selected video embedded up top, the rest as a
// clickable list below. Uses the YouTube IFrame Player API so the worker
// can mute / set volume on demand via the widget_volume bus — a static
// iframe embed exposes no programmatic controls.

import { useEffect, useRef, useState } from 'react';
import type { WidgetComponentProps, YouTubePayload } from '@/lib/jarvis-ui/protocol';
import { subscribeWidgetVolume } from '@/lib/jarvis-ui/widget-volume-bus';
import { WidgetStatus } from './widget-status';

// Minimal shape of the YT.Player methods we actually call.
interface YTPlayer {
  mute(): void;
  unMute(): void;
  setVolume(level: number): void;
  loadVideoById(id: string): void;
  destroy(): void;
}

interface YTGlobal {
  Player: new (
    element: HTMLElement | string,
    options: {
      videoId: string;
      playerVars?: Record<string, string | number>;
      events?: {
        onReady?: (event: { target: YTPlayer }) => void;
      };
    }
  ) => YTPlayer;
}

declare global {
  interface Window {
    YT?: YTGlobal;
    onYouTubeIframeAPIReady?: () => void;
  }
}

// Resolves once the YT IFrame API script has loaded. Cached so every
// player mount after the first reuses the same promise.
let ytApiPromise: Promise<YTGlobal> | null = null;

function loadYouTubeApi(): Promise<YTGlobal> {
  if (typeof window === 'undefined') {
    return new Promise(() => {});
  }
  if (window.YT && window.YT.Player) {
    return Promise.resolve(window.YT);
  }
  if (ytApiPromise) return ytApiPromise;
  ytApiPromise = new Promise<YTGlobal>((resolve) => {
    const existing = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => {
      existing?.();
      if (window.YT) resolve(window.YT);
    };
    const tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
  });
  return ytApiPromise;
}

export function YouTubeWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as YouTubePayload | undefined;
  const videos = data?.videos ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  const playerHostRef = useRef<HTMLDivElement | null>(null);
  const playerRef = useRef<YTPlayer | null>(null);

  // Fall back to the first video if the picked one isn't in this list
  // (the payload may have been replaced by a fresh search).
  const selected =
    picked && videos.some((v) => v.videoId === picked)
      ? picked
      : videos[0]?.videoId ?? null;

  // ── Player lifecycle ────────────────────────────────────────────
  useEffect(() => {
    if (!selected || !playerHostRef.current) return;
    let cancelled = false;
    loadYouTubeApi().then((YT) => {
      if (cancelled || !playerHostRef.current) return;
      if (playerRef.current) {
        try {
          playerRef.current.loadVideoById(selected);
        } catch {
          /* player got into a bad state — fall through to rebuild */
        }
        return;
      }
      playerRef.current = new YT.Player(playerHostRef.current, {
        videoId: selected,
        playerVars: {
          autoplay: 1,
          modestbranding: 1,
          rel: 0,
          enablejsapi: 1,
        },
      });
    });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // Tear the player down when the widget unmounts.
  useEffect(
    () => () => {
      try {
        playerRef.current?.destroy();
      } catch {
        /* nothing useful to do */
      }
      playerRef.current = null;
    },
    []
  );

  // ── widget_volume from the worker ───────────────────────────────
  useEffect(() => {
    const unsub = subscribeWidgetVolume('youtube', (cmd) => {
      const p = playerRef.current;
      if (!p) return;
      try {
        if (cmd.action === 'mute') p.mute();
        else if (cmd.action === 'unmute') {
          p.unMute();
          if (typeof cmd.level === 'number') p.setVolume(cmd.level);
        } else if (cmd.action === 'set') {
          const level = Math.max(0, Math.min(100, cmd.level ?? 50));
          if (level === 0) p.mute();
          else {
            p.unMute();
            p.setVolume(level);
          }
        }
      } catch (err) {
        console.warn('[youtube-widget] volume command failed', err);
      }
    });
    return unsub;
  }, []);

  if (!data) return <WidgetStatus text="Searching YouTube…" />;
  if (videos.length === 0) {
    return <WidgetStatus text={`No videos for "${data.query}".`} />;
  }

  return (
    <div className="flex h-full flex-col">
      <div className="relative aspect-video w-full shrink-0 bg-black">
        {/* The IFrame API replaces this div with the player iframe. */}
        <div key={selected ?? 'none'} ref={playerHostRef} className="h-full w-full" />
        {/* Always-available escape hatch when the embed is blocked by the channel. */}
        {selected && (
          <a
            href={`https://www.youtube.com/watch?v=${selected}`}
            target="_blank"
            rel="noopener noreferrer"
            className="absolute right-2 top-2 rounded bg-black/70 px-2 py-1 text-[10px] font-mono text-[#3CDFFF] backdrop-blur hover:bg-black/90 hover:text-white"
          >
            Watch on YouTube ↗
          </a>
        )}
      </div>
      <ul className="min-h-0 flex-1 divide-y divide-[#3CDFFF]/10 overflow-auto">
        {videos.map((v) => (
          <li key={v.videoId} className="flex items-center">
            <button
              type="button"
              onClick={() => setPicked(v.videoId)}
              className={`flex flex-1 items-center gap-2 px-2 py-1.5 text-left transition-colors hover:bg-[#3CDFFF]/10 ${
                v.videoId === selected ? 'bg-[#3CDFFF]/15' : ''
              }`}
            >
              {v.thumbnail && (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={v.thumbnail}
                  alt=""
                  className="h-9 w-16 shrink-0 rounded object-cover"
                />
              )}
              <span className="min-w-0">
                <span className="line-clamp-1 text-xs text-[#cdf2fb]">{v.title}</span>
                <span className="line-clamp-1 font-mono text-[10px] text-[#5fb0c6]">
                  {v.channel}
                </span>
              </span>
            </button>
            <a
              href={`https://www.youtube.com/watch?v=${v.videoId}`}
              target="_blank"
              rel="noopener noreferrer"
              title="Open this video on YouTube"
              className="shrink-0 px-2 text-[#5fb0c6] hover:text-[#3CDFFF]"
              onClick={(e) => e.stopPropagation()}
            >
              ↗
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}
