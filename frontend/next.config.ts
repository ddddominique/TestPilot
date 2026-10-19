import type { NextConfig } from "next";

const config: NextConfig = {
  async rewrites() {
    const backend = (process.env.API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
    return [{ source: "/backend/:path*", destination: `${backend}/:path*` }];
  },
};
export default config;
