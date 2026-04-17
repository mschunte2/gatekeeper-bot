import { buildXDC, eruda, mockWebxdc } from "webxdc-vite-plugins";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    buildXDC({ outDir: "../", outFileName: "quick-unlock.xdc" }),
    eruda(),
    mockWebxdc("./node_modules/webxdc-vite-plugins/src/webxdc.js"),
  ],
});
