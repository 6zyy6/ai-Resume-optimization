import { defineConfig } from "@tarojs/cli";

export default defineConfig({
  projectName: "ai-resume-assistant",
  date: "2026-07-29",
  designWidth: 375,
  deviceRatio: { 375: 2 },
  sourceRoot: "src",
  outputRoot: "dist",
  framework: "react",
  compiler: "webpack5",
  cache: { enable: true },
  mini: {
    postcss: {
      pxtransform: { enable: true },
      cssModules: { enable: false },
    },
  },
});
