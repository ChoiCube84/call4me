import type { NextConfig } from "next";

// Static export: FastAPI serves `out/` at the site root (same origin as the API), so
// there is no Node server in production. No dynamic route segments: pages take the
// call id as a query param (/live/?id=...).
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
