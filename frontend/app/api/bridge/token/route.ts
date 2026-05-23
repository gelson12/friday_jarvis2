// Bridge token endpoint — desktop-bridges fetch a fresh LiveKit JWT from here
// on startup (and on reconnect). The raw LiveKit API secret stays in Railway
// env vars; no PC ever has to hold it. Bridges authenticate to this endpoint
// with a shared BRIDGE_TOKEN that grants only "mint me a JWT for the control
// room under my own machine identity" — a much smaller blast radius than the
// raw key+secret if it leaks.

import { NextResponse } from 'next/server';
import { AccessToken, type VideoGrant } from 'livekit-server-sdk';

const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;
const BRIDGE_TOKEN = process.env.BRIDGE_TOKEN;
const CONTROL_ROOM = process.env.JARVIS_CONTROL_ROOM ?? 'jarvis-control';

// JWT lifetime. Bridges refresh on reconnect, so 24h is plenty and still
// limits the window of a stolen-JWT replay attack.
const TOKEN_TTL_SECONDS = 24 * 60 * 60;

export const revalidate = 0;

export async function POST(req: Request) {
  try {
    if (!LIVEKIT_URL) {
      return new NextResponse('LIVEKIT_URL not set', { status: 500 });
    }
    if (!API_KEY || !API_SECRET) {
      return new NextResponse('LIVEKIT_API_KEY / SECRET not set', { status: 500 });
    }
    if (!BRIDGE_TOKEN) {
      // The endpoint is shipped before the env var is configured — keep the
      // worker healthy and tell the bridge what's wrong.
      return new NextResponse('bridge endpoint not configured (BRIDGE_TOKEN missing)', {
        status: 503,
      });
    }

    // Bearer-token auth, compared in constant time.
    const auth = req.headers.get('authorization') ?? '';
    const supplied = /^Bearer\s+(.+)$/i.exec(auth)?.[1]?.trim() ?? '';
    if (!supplied || !timingSafeEqualStr(supplied, BRIDGE_TOKEN)) {
      return new NextResponse('unauthorized', { status: 401 });
    }

    const body = (await req.json().catch(() => ({}))) as { machine?: unknown };
    const machine = String(body.machine ?? '').trim().toLowerCase();
    if (!/^[a-z0-9_-]{1,32}$/.test(machine)) {
      return new NextResponse("body must include a valid 'machine' name (a-z0-9_-)", {
        status: 400,
      });
    }

    const at = new AccessToken(API_KEY, API_SECRET, {
      identity: `desktop-bridge-${machine}`,
      name: `Desktop Bridge (${machine})`,
      ttl: `${TOKEN_TTL_SECONDS}s`,
    });
    const grant: VideoGrant = {
      room: CONTROL_ROOM,
      roomJoin: true,
      canPublish: true,
      canPublishData: true,
      canSubscribe: true,
    };
    at.addGrant(grant);
    const token = await at.toJwt();

    return NextResponse.json(
      {
        serverUrl: LIVEKIT_URL,
        token,
        room: CONTROL_ROOM,
        ttlSeconds: TOKEN_TTL_SECONDS,
      },
      { headers: { 'Cache-Control': 'no-store' } },
    );
  } catch (err) {
    console.error('[api/bridge/token] error:', err);
    const msg = err instanceof Error ? err.message : 'internal error';
    return new NextResponse(msg, { status: 500 });
  }
}

// Constant-time string compare so an attacker can't infer the BRIDGE_TOKEN
// from response-time differences.
function timingSafeEqualStr(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let result = 0;
  for (let i = 0; i < a.length; i += 1) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}
