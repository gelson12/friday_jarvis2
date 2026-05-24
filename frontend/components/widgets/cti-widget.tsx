'use client';

// The OpenCTI intelligence panel ("Global Eyes"). An iframe pointing at
// the Railway-hosted OpenCTI instance — the worker supplies the URL in
// the payload (so it can deep-link a specific dashboard), with
// NEXT_PUBLIC_OPENCTI_URL as the build-time fallback.
//
// Path Y (chosen 2026-05-24): OpenCTI lives on Railway with a public
// HTTPS URL, so this iframe works from ANY browser (phone, ROG, laptop)
// — no mixed-content concerns because both pages are HTTPS.
//
// Auth: OpenCTI sets a persistent session cookie on first login. After
// the user has logged in once in the same browser, this iframe is happy.

import type { CTIPayload, WidgetComponentProps } from '@/lib/jarvis-ui/protocol';
import { WidgetStatus } from './widget-status';

function joinUrl(base: string, path?: string): string {
  const b = base.replace(/\/+$/, '');
  if (!path) return b;
  const p = path.startsWith('/') ? path : `/${path}`;
  return `${b}${p}`;
}

export function CTIWidget({ widget }: WidgetComponentProps) {
  const data = widget.payload as CTIPayload | undefined;
  const baseUrl =
    data?.url || process.env.NEXT_PUBLIC_OPENCTI_URL || '';

  if (!baseUrl) {
    return (
      <WidgetStatus text="OpenCTI URL not configured. Set NEXT_PUBLIC_OPENCTI_URL on the frontend service or pass `url` in the widget payload." />
    );
  }

  const src = joinUrl(baseUrl, data?.path);

  return (
    <iframe
      title="OpenCTI Intelligence"
      src={src}
      className="h-full w-full border-0"
      loading="lazy"
      referrerPolicy="no-referrer-when-downgrade"
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals allow-downloads allow-popups-to-escape-sandbox"
    />
  );
}
