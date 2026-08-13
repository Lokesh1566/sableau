// Minimal Electron shell used only as a Chromium host exposing CDP.
// It exists so this project can run a real browser in environments where the
// Playwright browser download is unavailable. Nothing app specific lives here.
const { app, BrowserWindow } = require('electron');
const port = process.env.SABLEAU_CDP_PORT || '9222';
app.commandLine.appendSwitch('remote-debugging-port', port);
app.commandLine.appendSwitch('remote-allow-origins', '*');
app.disableHardwareAcceleration();
app.whenReady().then(() => {
  new BrowserWindow({
    width: 1280,
    height: 900,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true }
  }).loadURL('about:blank');
});
app.on('window-all-closed', () => app.quit());
