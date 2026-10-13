import { app, BrowserWindow, clipboard, dialog, ipcMain, nativeTheme, net, protocol, shell } from 'electron';
import { spawn, ChildProcessWithoutNullStreams } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { stat } from 'node:fs/promises';
import { METHODS } from './types';
import { randomUUID } from 'node:crypto';
import { officialLogin } from './WebLogin';
import { appearanceColors, loadAppearance, saveAppearance, validAppearance } from './Appearance';

declare const MAIN_WINDOW_WEBPACK_ENTRY: string;
declare const MAIN_WINDOW_PRELOAD_WEBPACK_ENTRY: string;
protocol.registerSchemesAsPrivileged([{ scheme: 'quark', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
let window: BrowserWindow;
let backend: ChildProcessWithoutNullStreams;
const pending = new Map<string, { resolve: (data: unknown) => void; reject: (error: Error) => void }>();
let stopping = false;
let webLoginRunning = false;
let appearanceWarning = '';
let appearanceSaving = false;
function appearance() {return {source:nativeTheme.themeSource,dark:nativeTheme.shouldUseDarkColors,warning:appearanceWarning};}
function syncAppearance() {
  if(!window || window.isDestroyed()) return;
  const colors=appearanceColors(nativeTheme.shouldUseDarkColors);
  window.setBackgroundColor(colors.background);
  window.setTitleBarOverlay({color:colors.title,symbolColor:colors.symbol,height:34});
  window.webContents.send('quark:event',{id:null,event:'appearance',data:appearance()});
}
if (!app.requestSingleInstanceLock()) app.exit(0);
app.on('second-instance', () => { if (window && !window.isDestroyed()) { window.restore(); window.focus(); } });
function failPending(message: string) {
  for (const job of pending.values()) job.reject(new Error(message));
  pending.clear();
  if (window && !window.isDestroyed()) window.webContents.send('quark:event', { id: null, event: 'backend.error', data: message });
}
function startBackend() {
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' };
  const command = app.isPackaged ? path.join(process.resourcesPath, 'QuarkBackend', 'QuarkBackend.exe') : (process.env.QUARK_DESKTOP_PYTHON || 'python');
  const args = app.isPackaged ? [] : ['-m', 'quark_cli.desktop'];
  if (!app.isPackaged) Object.assign(env, { PYTHONPATH: path.resolve(app.getAppPath(), '../src') });
  backend = spawn(command, args, { env, windowsHide: true, stdio: 'pipe' });
  createInterface({ input: backend.stdout, crlfDelay: Infinity }).on('line', line => {
    try {
      const message = JSON.parse(line);
      if (message.event) {
        if (!window.isDestroyed()) window.webContents.send('quark:event', message);
      } else if (pending.has(message.id)) {
        const job = pending.get(message.id)!; pending.delete(message.id);
        if (message.error) job.reject(new Error(message.error)); else job.resolve(message.result);
      }
    } catch { failPending('后端协议异常，请重新启动客户端'); }
  });
  // Do not expose raw Python diagnostics or exception messages in the renderer.
  backend.stderr.on('data', () => {});
  backend.on('error', () => failPending('无法启动网盘后端'));
  backend.on('exit', () => { if (!stopping) failPending('网盘后端已退出，请重新启动客户端'); });
  backend.stdin.on('error', () => failPending('网盘后端连接已关闭'));
}
function trusted(event: Electron.IpcMainInvokeEvent) {
  if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) throw new Error('无效调用来源');
}
function backendRequest(method:string,params:Record<string,unknown>={},id=randomUUID()) {
  if (!backend || backend.exitCode !== null || !backend.stdin.writable) return Promise.reject(new Error('网盘后端不可用'));
  return new Promise<any>((resolve,reject) => {
    pending.set(id,{resolve,reject});
    backend.stdin.write(JSON.stringify({id,method,params})+'\n');
  });
}
app.whenReady().then(async () => {
  const savedAppearance=await loadAppearance();
  appearanceWarning=savedAppearance.warning;
  nativeTheme.themeSource=savedAppearance.source;
  const colors=appearanceColors(nativeTheme.shouldUseDarkColors);
  if (app.isPackaged) {
    const root = path.dirname(path.dirname(fileURLToPath(MAIN_WINDOW_WEBPACK_ENTRY)));
    protocol.handle('quark', request => {
      const url = new URL(request.url);
      const target = path.resolve(root, '.' + decodeURIComponent(url.pathname));
      if (url.host !== 'app' || !target.startsWith(root + path.sep)) return new Response(null, { status: 403 });
      return net.fetch(pathToFileURL(target).toString());
    });
  }
  window = new BrowserWindow({ width: 1180, height: 780, minWidth: 860, minHeight: 600,
    title: '夸克网盘客户端', backgroundColor: colors.background, titleBarStyle:'hidden',
    titleBarOverlay:{color:colors.title,symbolColor:colors.symbol,height:34},
    webPreferences: { preload: MAIN_WINDOW_PRELOAD_WEBPACK_ENTRY, contextIsolation: true, nodeIntegration: false, sandbox: true } });
  window.setMenuBarVisibility(false);
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  nativeTheme.on('updated',syncAppearance);
  ipcMain.handle('quark:appearance.get',event => {trusted(event);return appearance();});
  ipcMain.handle('quark:appearance.set',async (event,source) => {
    trusted(event);
    if(!validAppearance(source)) throw new Error('外观选项无效');
    if(appearanceSaving) throw new Error('正在保存外观，请稍后再试');
    appearanceSaving=true;
    nativeTheme.themeSource=source;
    syncAppearance();
    try {await saveAppearance(source);appearanceWarning='';}
    catch {appearanceWarning='外观已切换，但未能保存；本次窗口内有效';}
    finally {appearanceSaving=false;}
    return appearance();
  });
  ipcMain.handle('quark:call', (event, message) => {
    trusted(event);
    if (!message || typeof message.id !== 'string' || message.id.length > 128 || pending.has(message.id) || !METHODS.includes(message.method)) throw new Error('无效操作');
    return backendRequest(message.method,message.params,message.id);
  });
  ipcMain.handle('quark:official-login',async (event,mode) => {
    trusted(event);
    if(!['mobile','wechat'].includes(mode) || webLoginRunning) throw new Error('请先结束当前登录');
    webLoginRunning=true;
    try {return await officialLogin(window,mode,backendRequest);}
    finally {webLoginRunning=false;}
  });
  ipcMain.handle('quark:choose-folder', async event => {
    trusted(event);
    const result = await dialog.showOpenDialog(window, { properties: ['openDirectory'] });
    return result.canceled ? null : result.filePaths[0];
  });
  ipcMain.handle('quark:open-folder', async (event, folder: string) => {
    trusted(event);
    if (typeof folder !== 'string' || !path.isAbsolute(folder) || !(await stat(folder)).isDirectory()) throw new Error('保存目录不存在');
    const error = await shell.openPath(folder); if (error) throw new Error('无法打开保存目录');
  });
  ipcMain.handle('quark:choose-files', async event => {
    trusted(event);
    const result = await dialog.showOpenDialog(window, { properties: ['openFile', 'multiSelections'] });
    return result.canceled ? [] : result.filePaths;
  });
  startBackend();
  ipcMain.handle('quark:copy-text', (event, text: string) => {
    trusted(event);
    if (typeof text !== 'string') throw new Error('复制内容无效');
    clipboard.writeText(text);
  });
  await window.loadURL(app.isPackaged ? 'quark://app/main_window/index.html' : MAIN_WINDOW_WEBPACK_ENTRY);
});
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => { stopping = true; backend?.stdin.end(); });
