import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Emit a minimal standalone server for small, fast-cold-start containers.
  output: 'standalone',
};

export default nextConfig;
