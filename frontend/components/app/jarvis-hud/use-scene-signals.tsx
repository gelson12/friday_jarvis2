'use client';

import { useContext, useEffect, useRef } from 'react';
import {
  type AgentState,
  RoomContext,
  type TrackReference,
  useTrackVolume,
  useVoiceAssistant,
} from '@livekit/components-react';

export type SceneState = AgentState | 'disconnected';

export interface SceneSignalsRefs {
  stateRef: React.MutableRefObject<SceneState>;
  volumeRef: React.MutableRefObject<number>;
}

function VoiceAssistantBridge({ stateRef, volumeRef }: SceneSignalsRefs) {
  const { state, audioTrack } = useVoiceAssistant();
  const volume = useTrackVolume(audioTrack as TrackReference, {
    fftSize: 512,
    smoothingTimeConstant: 0.55,
  });

  useEffect(() => {
    stateRef.current = (state ?? 'idle') as SceneState;
  }, [state, stateRef]);

  useEffect(() => {
    volumeRef.current = volume ?? 0;
  }, [volume, volumeRef]);

  return null;
}

/**
 * Returns refs that are continuously updated with the LiveKit agent state and
 * the smoothed mic volume, plus a Bridge React node the caller mounts inside
 * its tree. Safe to mount outside a LiveKitRoom — when no room context exists,
 * the bridge renders nothing and the refs stay at 'disconnected' / 0.
 *
 * The scene reads stateRef.current / volumeRef.current inside useFrame so the
 * React tree never re-renders at audio rate.
 */
export function useSceneSignals() {
  const stateRef = useRef<SceneState>('disconnected');
  const volumeRef = useRef<number>(0);
  const room = useContext(RoomContext);

  const Bridge = room ? <VoiceAssistantBridge stateRef={stateRef} volumeRef={volumeRef} /> : null;

  return { stateRef, volumeRef, Bridge };
}
