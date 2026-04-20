import { buildXDC, eruda, mockWebxdc } from "webxdc-vite-plugins";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    // quick-lock is disabled: the .xdc lives in apps-disabled/ so the
    // bot's `apps/*.xdc` glob does not pick it up. Move the built file
    // back to "../" if you decide to re-enable it.
    buildXDC({ outDir: "../../apps-disabled/", outFileName: "quick-lock.xdc" }),
    eruda(),
    mockWebxdc("./node_modules/webxdc-vite-plugins/src/webxdc.js"),
  ],
});
