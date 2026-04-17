import { buildXDC, eruda, mockWebxdc } from "webxdc-vite-plugins";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    // quick-unlock is currently disabled (one-tap unlock judged too
    // dangerous to deploy by default). The .xdc lives in apps-disabled/
    // so the bot's `apps/*.xdc` glob does not pick it up. Move the file
    // back to "../" if you decide to re-enable.
    buildXDC({ outDir: "../../apps-disabled/", outFileName: "quick-unlock.xdc" }),
    eruda(),
    mockWebxdc("./node_modules/webxdc-vite-plugins/src/webxdc.js"),
  ],
});
