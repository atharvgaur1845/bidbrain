import type { NextConfig } from "next";

// Static export: the same build is deployed to Vercel and served by the API
// container (FastAPI mounts frontend/out at /), so one HF Space can host both.
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
