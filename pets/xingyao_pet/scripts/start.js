const { spawn } = require("child_process");
const path = require("path");
const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
const electron = require("electron");
const child = spawn(electron, ["."], {
  cwd: path.resolve(__dirname, ".."),
  env,
  stdio: "inherit"
});
child.on("exit", (code) => process.exit(code ?? 0));
