module.exports = {
  appId: "com.desktoppet.pet",
  productName: "桌面宠物",
  directories: { output: "release" },
  files: [
    "package.json",
    "src/**",
    "assets/pet/**",
    "assets/bubbles/**",
    "assets/fallback/**"
  ],
  asar: true,
  mac: {
    target: [
      { target: "dmg", arch: ["arm64", "x64"] },
      { target: "zip", arch: ["arm64", "x64"] }
    ],
    identity: null,
    category: "public.app-category.entertainment"
  },
  win: {
    target: [
      { target: "nsis", arch: ["x64"] },
      { target: "portable", arch: ["x64"] }
    ]
  },
  nsis: { oneClick: true, perMachine: false }
};
