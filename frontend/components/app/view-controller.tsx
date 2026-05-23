'use client';

import { useEffect, useRef } from 'react';
import { useTheme } from 'next-themes';
import { AnimatePresence, motion } from 'motion/react';
import { useSessionContext } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { AgentSessionView_01 } from '@/components/agents-ui/blocks/agent-session-view-01';
import { WelcomeView } from '@/components/app/welcome-view';

const MotionWelcomeView = motion.create(WelcomeView);
const MotionSessionView = motion.create(AgentSessionView_01);

const VIEW_MOTION_PROPS = {
  variants: {
    visible: {
      opacity: 1,
    },
    hidden: {
      opacity: 0,
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
  transition: {
    duration: 0.5,
    ease: 'linear',
  },
};

interface ViewControllerProps {
  appConfig: AppConfig;
}

export function ViewController({ appConfig }: ViewControllerProps) {
  const { isConnected, start } = useSessionContext();
  const { resolvedTheme } = useTheme();

  // Auto-connect on page load — no button click. The browser asks for
  // mic permission once (LiveKit WebRTC init); after the first Allow it
  // is automatic on every visit. Wake/sleep ("Hey Friday" / "goodbye
  // Friday") is gated server-side in the worker, not in the browser.
  //
  // Auto-RECONNECT if the session drops (LiveKit job process exits on
  // participant disconnect — every refresh / network hiccup ends the
  // job, and without a manual "Talk to Friday" button the user would
  // be stranded on the welcome view. We retry with a small delay so
  // we don't hammer the API in a tight loop on a permanent failure.
  const startedRef = useRef(false);
  const wasConnectedRef = useRef(false);
  useEffect(() => {
    if (isConnected) {
      wasConnectedRef.current = true;
      return;
    }
    // Not connected. Either initial mount, or we dropped.
    if (!startedRef.current) {
      startedRef.current = true;
      void start();
      return;
    }
    if (wasConnectedRef.current) {
      // We were connected and lost it — retry after a brief delay.
      const t = window.setTimeout(() => {
        void start();
      }, 2000);
      return () => window.clearTimeout(t);
    }
  }, [isConnected, start]);

  return (
    <AnimatePresence mode="wait">
      {/* Welcome view */}
      {!isConnected && (
        <MotionWelcomeView
          key="welcome"
          {...VIEW_MOTION_PROPS}
          startButtonText={appConfig.startButtonText}
          onStartCall={start}
        />
      )}
      {/* Session view */}
      {isConnected && (
        <MotionSessionView
          key="session-view"
          {...VIEW_MOTION_PROPS}
          supportsChatInput={appConfig.supportsChatInput}
          supportsVideoInput={appConfig.supportsVideoInput}
          supportsScreenShare={appConfig.supportsScreenShare}
          isPreConnectBufferEnabled={appConfig.isPreConnectBufferEnabled}
          audioVisualizerType={appConfig.audioVisualizerType}
          audioVisualizerColor={
            resolvedTheme === 'dark'
              ? appConfig.audioVisualizerColorDark
              : appConfig.audioVisualizerColor
          }
          audioVisualizerColorShift={appConfig.audioVisualizerColorShift}
          audioVisualizerBarCount={appConfig.audioVisualizerBarCount}
          audioVisualizerGridRowCount={appConfig.audioVisualizerGridRowCount}
          audioVisualizerGridColumnCount={appConfig.audioVisualizerGridColumnCount}
          audioVisualizerRadialBarCount={appConfig.audioVisualizerRadialBarCount}
          audioVisualizerRadialRadius={appConfig.audioVisualizerRadialRadius}
          audioVisualizerWaveLineWidth={appConfig.audioVisualizerWaveLineWidth}
          className="fixed inset-0"
        />
      )}
    </AnimatePresence>
  );
}
