import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { RoomAgentDispatch, RoomConfiguration } from '@livekit/protocol';

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

// NOTE: you are expected to define the following environment variables in `.env.local`:
const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;
// Name of the Python worker (see agent.py WorkerOptions agent_name=).
// Default keeps dispatch working when the env var is unset.
const AGENT_NAME = process.env.AGENT_NAME ?? 'friday';

// don't cache the results
export const revalidate = 0;

export async function POST(req: Request) {
  try {
    if (LIVEKIT_URL === undefined) {
      throw new Error('LIVEKIT_URL is not defined');
    }
    if (API_KEY === undefined) {
      throw new Error('LIVEKIT_API_KEY is not defined');
    }
    if (API_SECRET === undefined) {
      throw new Error('LIVEKIT_API_SECRET is not defined');
    }

    // Parse room config from request body. The client may send an
    // explicit room_config (sandbox path) or none at all
    // (TokenSource.endpoint path). Either way, we ensure the room
    // configuration carries an agent dispatch grant — that's the
    // only thing that causes LiveKit Cloud to bring our worker into
    // the user's room. Previously, when the client didn't send a
    // room_config, the resulting room had no agent dispatch and the
    // worker would register but never receive a job.
    type JsonValue = string | number | boolean | null | { [k: string]: JsonValue } | JsonValue[];
    let body: { room_config?: JsonValue } = {};
    try {
      body = await req.json();
    } catch {
      // Empty body — fine, we'll build the room_config server-side.
    }
    const roomConfig = body?.room_config
      ? RoomConfiguration.fromJson(body.room_config, { ignoreUnknownFields: true })
      : new RoomConfiguration();

    // Force-attach the agent dispatch if the client didn't provide one.
    if (!roomConfig.agents || roomConfig.agents.length === 0) {
      roomConfig.agents = [
        new RoomAgentDispatch({ agentName: AGENT_NAME }),
      ];
    }

    // Generate participant token
    const participantName = 'user';
    const participantIdentity = `voice_assistant_user_${Math.floor(Math.random() * 10_000)}`;
    const roomName = `voice_assistant_room_${Math.floor(Math.random() * 10_000)}`;

    const participantToken = await createParticipantToken(
      { identity: participantIdentity, name: participantName },
      roomName,
      roomConfig
    );

    // Return connection details
    const data: ConnectionDetails = {
      serverUrl: LIVEKIT_URL,
      roomName,
      participantName,
      participantToken,
    };
    const headers = new Headers({
      'Cache-Control': 'no-store',
    });
    return NextResponse.json(data, { headers });
  } catch (error) {
    if (error instanceof Error) {
      console.error(error);
      return new NextResponse(error.message, { status: 500 });
    }
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  roomConfig: RoomConfiguration | undefined
): Promise<string> {
  const at = new AccessToken(API_KEY, API_SECRET, {
    ...userInfo,
    ttl: '15m',
  });
  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
  };
  at.addGrant(grant);

  if (roomConfig) {
    at.roomConfig = roomConfig;
  }

  return at.toJwt();
}
