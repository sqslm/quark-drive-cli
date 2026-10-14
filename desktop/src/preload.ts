import { contextBridge, ipcRenderer, webUtils } from 'electron';
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
  chooseFiles: () => ipcRenderer.invoke('quark:choose-files'),
  readDroppedFiles:files => {
    const paths=files.map(file => webUtils.getPathForFile(file));
    if(!paths.length || paths.some(value => !value)) return Promise.reject(new Error('请拖入本地文件或目录'));
    return ipcRenderer.invoke('quark:dropped-files',paths);
  },
  openFolder: (path) => ipcRenderer.invoke('quark:open-folder', path),
  copyText: (text) => ipcRenderer.invoke('quark:copy-text', text),
  getAppearance: () => ipcRenderer.invoke('quark:appearance.get'),
  setAppearance: source => ipcRenderer.invoke('quark:appearance.set',source),
  getClientSettings: () => ipcRenderer.invoke('quark:desktop.get'),
  setClientSettings: settings => ipcRenderer.invoke('quark:desktop.set',settings),
  quit: () => ipcRenderer.invoke('quark:quit'),
  officialLogin: mode => ipcRenderer.invoke('quark:official-login',mode),
};
contextBridge.exposeInMainWorld('quark', bridge);
