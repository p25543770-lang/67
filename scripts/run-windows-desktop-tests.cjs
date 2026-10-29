"use strict";
// Run the same desktop test files individually on Windows. A stuck subprocess
// must fail the build with its filename rather than hanging installer CI.
const fs = require("node:fs");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");

const dir = path.resolve(__dirname, "../desktop/tests");
const files = fs.readdirSync(dir).filter((name) => name.endsWith(".test.cjs")).sort();
if (!files.length) throw new Error("No desktop tests found");

function run(file) {
  return new Promise((resolve) => {
    console.log(`\nDesktop tests: ${file}`);
    const child = spawn(process.execPath, ["--test", path.join(dir, file)], { stdio: "inherit" });
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      console.error(`::error::Desktop test ${file} timed out after 25 seconds`);
      if (process.platform === "win32") {
        // node --test owns a child runner; terminate the entire tree.
        spawnSync("taskkill", ["/pid", String(child.pid), "/t", "/f"], { timeout: 5000, stdio: "inherit" });
      } else {
        child.kill("SIGKILL");
      }
    }, 25000);
    child.on("error", (error) => {
      clearTimeout(timer);
      console.error(`::error::Could not run desktop test ${file}: ${error.message}`);
      resolve(false);
    });
    child.on("exit", (code, signal) => {
      clearTimeout(timer);
      const ok = !timedOut && code === 0;
      if (!ok && !timedOut) console.error(`::error::Desktop test ${file} failed (code ${code}, signal ${signal})`);
      resolve(ok);
    });
  });
}

(async () => {
  for (const file of files) {
    if (!await run(file)) {
      process.exitCode = 1;
      return;
    }
  }
  console.log(`All ${files.length} desktop test files passed.`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
