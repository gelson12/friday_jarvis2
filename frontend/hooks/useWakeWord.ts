// Hands-free wake-word for the welcome screen.
//
// Runs continuous browser SpeechRecognition while `enabled`. When a final
// transcript contains the wake phrase ("Friday" / "Hey Friday") it calls
// `onWake()` once — the welcome view uses that to start the LiveKit
// session without a button press.
//
// Gotchas pre-handled (learned the hard way on the OpenJarvis build):
//   - SpeechRecognition vs webkitSpeechRecognition (Chrome/Safari shim).
//   - "no-speech" / "audio-capture" errors are non-fatal — they fire on
//     silence; we just let onend restart the recogniser.
//   - On a cold first visit mic permission is 'prompt'; recognition.start()
//     errors 'not-allowed' and the instance latches dead. The effect
//     therefore DEPENDS ON micPermissionState, so when the user grants the
//     mic the effect re-runs and builds a fresh, working recogniser.
//   - Echo guard: ignore transcripts while speechSynthesis is speaking.

'use client';

import { useEffect, useRef, useState } from 'react';

interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<{
    0: { transcript: string };
    isFinal: boolean;
  }>;
}

interface SpeechRecognitionErrorEventLike {
  error: string;
}

interface SpeechRecognitionLike {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  start(): void;
  stop(): void;
  onresult: ((ev: SpeechRecognitionEventLike) => void) | null;
  onerror: ((ev: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
}

declare global {
  interface Window {
    SpeechRecognition?: { new (): SpeechRecognitionLike };
    webkitSpeechRecognition?: { new (): SpeechRecognitionLike };
  }
}

// Loose match — any utterance containing "Friday" or "Hey Friday".
const WAKE_RE = /\b(hey\s+)?friday\b/i;

type MicState = 'granted' | 'denied' | 'prompt' | 'unknown';

interface UseWakeWordOptions {
  /** Listen only while true (e.g. while the session is disconnected). */
  enabled: boolean;
  /** Fired once when the wake phrase is heard. */
  onWake: () => void;
  lang?: string;
}

/**
 * Continuous wake-word listener. Safe to call unconditionally; pass
 * `enabled: false` to keep it dormant (Rules of Hooks friendly).
 */
export function useWakeWord({
  enabled,
  onWake,
  lang = 'en-US',
}: UseWakeWordOptions): void {
  const onWakeRef = useRef(onWake);
  onWakeRef.current = onWake;

  const firedRef = useRef(false);
  const [micState, setMicState] = useState<MicState>('unknown');

  // Track mic permission so the recogniser effect can re-arm on grant.
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('permissions' in navigator)) {
      return;
    }
    let cancelled = false;
    let status: PermissionStatus | null = null;
    const sync = () => {
      if (!cancelled && status) setMicState(status.state as MicState);
    };
    navigator.permissions
      .query({ name: 'microphone' as PermissionName })
      .then((s) => {
        if (cancelled) return;
        status = s;
        sync();
        s.addEventListener('change', sync);
      })
      .catch(() => {
        /* Permissions API unsupported — leave 'unknown'. */
      });
    return () => {
      cancelled = true;
      status?.removeEventListener('change', sync);
    };
  }, []);

  useEffect(() => {
    if (!enabled) return;
    if (typeof window === 'undefined') return;
    const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Ctor) return; // Web Speech API unsupported — manual button only.
    if (micState === 'denied') return; // would just error-loop.

    firedRef.current = false;
    const recognition = new Ctor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = lang;

    let stopped = false;

    recognition.onresult = (ev: SpeechRecognitionEventLike) => {
      const synth =
        typeof window !== 'undefined' ? window.speechSynthesis : null;
      if (synth && synth.speaking) return; // echo guard

      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const result = ev.results[i];
        if (!result.isFinal) continue;
        const transcript = result[0]?.transcript ?? '';
        if (WAKE_RE.test(transcript) && !firedRef.current) {
          firedRef.current = true;
          onWakeRef.current();
          return;
        }
      }
    };

    recognition.onerror = (ev: SpeechRecognitionErrorEventLike) => {
      if (ev.error === 'no-speech' || ev.error === 'audio-capture') return;
      if (ev.error === 'not-allowed') stopped = true;
    };

    recognition.onend = () => {
      if (stopped || firedRef.current) return;
      try {
        recognition.start();
      } catch {
        /* already started — ignore */
      }
    };

    try {
      recognition.start();
    } catch {
      /* ignore */
    }

    return () => {
      stopped = true;
      try {
        recognition.stop();
      } catch {
        /* ignore */
      }
    };
    // micState IS a dependency: a flip to 'granted' rebuilds a fresh,
    // working recogniser after the cold-start 'not-allowed' latch.
  }, [enabled, lang, micState]);
}
