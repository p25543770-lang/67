const { app, BrowserWindow, shell } = require('electron');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
let bridge, web;
const root = path.join(app.getAppPath(), 'dist');
function startWeb() {
  web = http.createServer((req, res) => {
    if (req.url.startsWith('/api/')) {
      const proxy = http.request({ hostname: '127.0.0.1', port: 8765, path: req.url, method: req.method, headers: req.headers }, upstream => {
        res.writeHead(upstream.statusCode, upstream.headers); upstream.pipe(res);
      });
      proxy.on('error', () => { if (!res.headersSent) { res.writeHead(503, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: 'Сервер Kinect не запущен. Установите Python и зависимости согласно README.' })); } });
      req.pipe(proxy); return;
    }
    const urlPath = new URL(req.url, 'http://localhost').pathname;
    const file = path.resolve(root, '.' + (urlPath === '/' ? '/index.html' : urlPath));
    if (!file.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
    const types = { '.html': 'text/html; charset=utf-8', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
    fs.readFile(file, (err, data) => { if (err) { res.writeHead(404); res.end(); return; } res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' }); res.end(data); });
  });
  return new Promise(resolve => web.listen(0, '127.0.0.1', resolve));
}
app.whenReady().then(async () => {
  const script = path.join(process.resourcesPath, 'server.py');
  bridge = spawn('python', [script], { windowsHide: true, stdio: 'ignore' });
  bridge.on('error', () => {}); // Demo still works without Python.
  await startWeb();
  const win = new BrowserWindow({ width: 1440, height: 900, minWidth: 800, minHeight: 600, title: 'Depth Studio', webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true } });
  win.webContents.setWindowOpenHandler(({ url }) => { if (url.startsWith('https://')) shell.openExternal(url); return { action: 'deny' }; });
  win.loadURL(`http://127.0.0.1:${web.address().port}/`);
});
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { if (bridge) bridge.kill(); if (web) web.close(); });
