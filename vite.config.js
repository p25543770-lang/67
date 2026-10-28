import { defineConfig } from "vite";

const bridgePort = Number(process.env.DEPTH_STUDIO_PORT || 8765);
const bridgeProxy = {
  target: `http://127.0.0.1:${bridgePort}`,
  changeOrigin: false,
  configure(proxy) {
    proxy.on("error", (_error, _request, response) => {
      if (response.headersSent) return;
      response.writeHead(503, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      });
      response.end(
        JSON.stringify({
          error:
            "Локальный мост не запущен. Демо работает без Python; для Kinect запустите server.py.",
        }),
      );
    });
  },
};

export default defineConfig({
  build: {
    reportCompressedSize: false,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("/node_modules/three/")) return "three-vendor";
          if (id.includes("/node_modules/lucide/")) return "icons-vendor";
        },
      },
    },
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: true,
    proxy: { "/api": bridgeProxy },
  },
  preview: {
    host: "0.0.0.0",
    allowedHosts: true,
    proxy: { "/api": bridgeProxy },
  },
});
