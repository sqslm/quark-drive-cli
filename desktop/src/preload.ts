import { contextBridge, ipcRenderer } from 'electron';
import { Bridge, Event, Method, METHODS } from './types';
const bridge: Bridge = {
  call(method: Method, params = {}, id = crypto.randomUUID()) {
    if (!METHODS.includes(method)) return Promise.reject(new Error('不支持的操作'));
    return ipcRenderer.invoke('quark:call', { id, method, params });
  },
  onEvent(listener) {
    const handler = (_event: unknown, value: Event) => listener(value);
    ipcRenderer.on('quark:event', handler);
    return () => ipcRenderer.removeListener('quark:event', handler);
  },
  chooseFolder: () => ipcRenderer.invoke('quark:choose-folder'),
  openFolder: (path) => ipcRenderer.invoke('quark:open-folder', path),
};
contextBridge.exposeInMainWorld('quark', bridge);
