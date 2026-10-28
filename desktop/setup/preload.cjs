"use strict";
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("depthStudioSetup", {
  getState: () => ipcRenderer.invoke("setup:state"),
  onEvent: (callback) => {
    const handler = (_event, payload) => callback(payload);
    ipcRenderer.on("setup:event", handler);
    return () => ipcRenderer.removeListener("setup:event", handler);
  },
  run: (mode) => ipcRenderer.invoke("setup:run", { mode }),
  revealLog: () => ipcRenderer.invoke("setup:reveal-log"),
  readLog: () => ipcRenderer.invoke("setup:read-log"),
  copyLog: () => ipcRenderer.invoke("setup:copy-log"),
  continueDemo: () => ipcRenderer.invoke("setup:continue-demo"),
  openApp: () => ipcRenderer.invoke("setup:open-app"),
  quit: () => ipcRenderer.invoke("setup:quit"),
});
