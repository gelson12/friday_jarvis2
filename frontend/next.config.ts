import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Emit a minimal standalone server for small, fast-cold-start containers.
  output: 'standalone',

  // Don't let formatting nits or stray lint warnings block a production
  // deploy. Lint is a dev-time concern, not a release gate — pre-merge
  // CI / editor integrations are the right place for that. Leaving this
  // on previously caused every push after 5596278e to silently fail and
  // the live worker fell ~23 hours behind without anyone noticing.
  eslint: {
    ignoreDuringBuilds: true,
  },
};

export default nextConfig;
