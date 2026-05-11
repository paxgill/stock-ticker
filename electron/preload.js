const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  toggleAlwaysOnTop: () => ipcRenderer.invoke('toggle-always-on-top'),
  setCompactMode: () => ipcRenderer.invoke('set-compact'),
  setFullMode: () => ipcRenderer.invoke('set-full'),
  isAlwaysOnTop: () => ipcRenderer.invoke('get-always-on-top'),
  sendQuotes: (quotes) => ipcRenderer.send('quotes-update', quotes),
  isElectron: true,
});
