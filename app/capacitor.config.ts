import type { CapacitorConfig } from "@capacitor/cli";

const config: CapacitorConfig = {
  appId: "app.xingyao.personal",
  appName: "星瑶",
  webDir: "../web/dist",
  android: {
    allowMixedContent: false,
  },
  server: {
    androidScheme: "https",
  },
};

export default config;
