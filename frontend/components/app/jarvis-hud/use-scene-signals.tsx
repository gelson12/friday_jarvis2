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

// Mount only when audioTrack is defined. useTrackVolume crashes in prod
// when given undefined — pre-connect, the agent participant doesn't exist
// yet, so audioTrack is undefined and we must not call the hook.
function VolumeReader({
  audioTrack,
  volumeRef,
}: {
  audioTrack: TrackReference;
  volumeRef: React.MutableRefObject<number>;
}) {
  const volume = useTrackVolume(audioTrack, {
    fftSize: 512,
    smoothingTimeConstant: 0.55,
  });
  useEffect(() => {
    volumeRef.current = volume ?? 0;
  }, [volume, volumeRef]);
  return null;
}

function VoiceAssistantBridge({ stateRef, volumeRef }: SceneSignalsRefs) {
  const { state, audioTrack } = useVoiceAssistant();

  useEffect(() => {
    stateRef.current = (state ?? 'idle') as SceneState;
  }, [state, stateRef]);

  // Reset to zero when the track goes away so the orb doesn't freeze on
  // the last amplitude after a disconnect.
  useEffect(() => {
    if (!audioTrack) volumeRef.current = 0;
  }, [audioTrack, volumeRef]);

  return audioTrack ? (
    <VolumeReader audioTrack={audioTrack as TrackReference} volumeRef={volumeRef} />
  ) : null;
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
