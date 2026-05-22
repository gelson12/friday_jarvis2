'use client';

// The conversation transcript as a floating, draggable panel — the same
// AgentChatTranscript the main view uses, re-housed inside a widget so
// the user can move it around the JARVIS screen.

import { useAgent, useSessionContext, useSessionMessages } from '@livekit/components-react';
import { AgentChatTranscript } from '@/components/agents-ui/agent-chat-transcript';

export function ChatWidget() {
  const session = useSessionContext();
  const { messages } = useSessionMessages(session);
  const { state: agentState } = useAgent();

  return (
    <AgentChatTranscript
      agentState={agentState}
      messages={messages}
      className="h-full w-full [&>div>div]:px-3 [&>div>div]:py-3"
    />
  );
}
