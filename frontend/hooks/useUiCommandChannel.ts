'use client';

// Receives structured UI commands from the worker over the LiveKit
// data channel (topic "ui-command"). Handles voice-controlled camera
// on/off plus gesture-mode toggle. Mirrors OpenJarvis's UiCommandBridge
// but as a hook so it drops in next to the other channel hooks in
// WidgetLayer (the same place useJarvisUIChannel and
// useJarvisUIStatePublisher mount).
//
// The worker parses the intent server-side and sends structured JSON;
// we never parse transcription text here.

import { useCallback, useRef } from 'react';
import {
  useDataChannel,
  useLocalParticipant,
} from '@livekit/components-react';
import { setGestureMode } from '@/lib/jarvis-ui/gesture-mode-bus';

const UI_COMMAND_TOPIC = 'ui-command';

interface UiCommand {
  type?: string;
  enabled?: boolean;
}

export function useUiCommandChannel(): void {
  const { localParticipant } = useLocalParticipant();
  const lpRef = useRef(localParticipant);
  lpRef.current = localParticipant;

  const onMessage = useCallback((msg: { payload: Uint8Array }) => {
    let data: UiCommand;
    try {
      data = JSON.parse(new TextDecoder().decode(msg.payload));
    } catch {
      return;
    }
    if (data?.type === 'camera') {
      void lpRef.current?.setCameraEnabled(Boolean(data.enabled));
    } else if (data?.type === 'gesture_mode') {
      setGestureMode(Boolean(data.enabled));
    }
  }, []);

  useDataChannel(UI_COMMAND_TOPIC, onMessage);
}
