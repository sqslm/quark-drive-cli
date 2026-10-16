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
import { defaultClientSettings, loadClientSettings, saveClientSettings, validClientSettings } from './ClientSettings';
import { Background } from './Background';

declare const MAIN_WINDOW_WEBPACK_ENTRY: string;
declare const MAIN_WINDOW_PRELOAD_WEBPACK_ENTRY: string;
protocol.registerSchemesAsPrivileged([{ scheme: 'quark', privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
let window: BrowserWindow;
let backend: ChildProcessWithoutNullStreams;
const pending = new Map<string, { method:string;resolve: (data: unknown) => void; reject: (error: Error) => void }>();
let stopping = false;
let editorDirty=false;
let confirmingQuit=false;
let quitAllowed=false;
let backendClosed=false;
let background:Background;
let clientSettings={...defaultClientSettings};
let clientSettingsWarning='';
let clientSettingsSaving=false;
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
function send(event:string,data:unknown) {if(window && !window.isDestroyed()) window.webContents.send('quark:event',{id:null,event,data});}
function showWindow(transfers=false) {
  if(!window || window.isDestroyed()) return;
  if(window.isMinimized()) window.restore();window.show();window.focus();
  if(transfers) send('navigate','tasks');
}
app.on('second-instance', () => showWindow());
async function finishQuit(code:number|null) {
  if(code !== null && code !== 0) await dialog.showMessageBox(window,{type:'warning',title:'后端异常退出',message:'后端未正常退出，请在重启后核实未完成任务与检查点。',buttons:['退出客户端']});
  quitAllowed=true;background?.destroy();app.quit();
}
async function requestQuit() {
  if(stopping || confirmingQuit) return;
  if(editorDirty) {
    confirmingQuit=true;showWindow();
    try {
      const result=await dialog.showMessageBox(window,{type:'warning',title:'文本尚未保存',message:'编辑器有未保存的修改或保存正在进行。返回编辑可等待保存完成；放弃修改将退出客户端。',buttons:['返回编辑','放弃未保存修改并退出'],defaultId:0,cancelId:0});
      if(result.response!==1) return;
    } finally {confirmingQuit=false;}
  }
  stopping=true;
  if(background) {background.quitting=true;background.update();}
  showWindow();send('desktop.quitting',true);
  // EOF asks Python to stop, finish active operations and persist checkpoints.
  // Keep the UI process alive until Python and its streams have closed.
  if(!backend || backendClosed) void finishQuit(0);
  else backend.stdin.end();
}
function failPending(message: string) {
  for (const job of pending.values()) job.reject(new Error(message));
  pending.clear();
  if(background) {background.available=false;background.busy=false;background.update();}
  showWindow();
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
        background?.event(message.event,message.data);
        if (!window.isDestroyed()) window.webContents.send('quark:event', message);
      } else if (pending.has(message.id)) {
        const job = pending.get(message.id)!; pending.delete(message.id);
        if (message.error) job.reject(new Error(message.error)); else {
          if(['tasks','download','upload','tasks.retry','tasks.cancel','tasks.pause','tasks.remove','tasks.clear','tasks.authorize'].includes(job.method)) background?.snapshot(message.result);
          job.resolve(message.result);
        }
      }
    } catch { failPending('后端协议异常，请重新启动客户端'); }
  });
  // Do not expose raw Python diagnostics or exception messages in the renderer.
  backend.stderr.on('data', () => {});
  backend.on('error', () => {if(!stopping) failPending('无法启动网盘后端');});
  backend.on('exit', () => { if (!stopping) failPending('网盘后端已退出，请重新启动客户端'); });
  backend.on('close',code => {backendClosed=true;if(stopping) void finishQuit(code);});
  backend.stdin.on('error', () => {if(!stopping) failPending('网盘后端连接已关闭');});
}
function trusted(event: Electron.IpcMainInvokeEvent) {
  if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) throw new Error('无效调用来源');
  if(stopping) throw new Error('正在退出客户端，请等待当前任务安全停止');
}
function backendRequest(method:string,params:Record<string,unknown>={},id=randomUUID()) {
  if (!backend || backend.exitCode !== null || !backend.stdin.writable) return Promise.reject(new Error('网盘后端不可用'));
  return new Promise<any>((resolve,reject) => {
    pending.set(id,{method,resolve,reject});
    backend.stdin.write(JSON.stringify({id,method,params})+'\n');
  });
}
app.whenReady().then(async () => {
  const savedAppearance=await loadAppearance();
  appearanceWarning=savedAppearance.warning;
  nativeTheme.themeSource=savedAppearance.source;
  const savedClientSettings=await loadClientSettings();
  clientSettings=savedClientSettings.settings;clientSettingsWarning=savedClientSettings.warning;
  const colors=appearanceColors(nativeTheme.shouldUseDarkColors);
  const icon=app.isPackaged ? path.join(process.resourcesPath,'app-icon.ico') : path.resolve(app.getAppPath(),'assets/app-icon.ico');
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
    title: `夸克网盘客户端 v${app.getVersion()}`,icon, backgroundColor: colors.background, titleBarStyle:'hidden',
    titleBarOverlay:{color:colors.title,symbolColor:colors.symbol,height:34},
    webPreferences: { preload: MAIN_WINDOW_PRELOAD_WEBPACK_ENTRY, contextIsolation: true, nodeIntegration: false, sandbox: true } });
  window.setMenuBarVisibility(false);
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', event => event.preventDefault());
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  background=new Background(icon,showWindow,backendRequest,send,requestQuit);
  background.notifications=clientSettings.notifications;
  window.on('close',event => {
    if(quitAllowed) return;
    event.preventDefault();
    if(stopping) return;
    if(clientSettings.closeBehavior === 'tray') window.hide();else requestQuit();
  });
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
  ipcMain.handle('quark:desktop.get',event => {trusted(event);return {settings:clientSettings,warning:clientSettingsWarning};});
  ipcMain.handle('quark:desktop.set',async (event,settings) => {
    trusted(event);
    if(!validClientSettings(settings)) throw new Error('后台运行设置无效');
    if(clientSettingsSaving) throw new Error('正在保存设置，请稍后再试');
    clientSettingsSaving=true;
    clientSettings={closeBehavior:settings.closeBehavior,notifications:settings.notifications};
    background.notifications=settings.notifications;
    try {await saveClientSettings(clientSettings);clientSettingsWarning='';}
    catch {clientSettingsWarning='后台运行设置已生效，但未能保存；本次窗口内有效';}
    finally {clientSettingsSaving=false;}
    background.update();
    return {settings:clientSettings,warning:clientSettingsWarning};
  });
  ipcMain.handle('quark:quit',event => {trusted(event);requestQuit();});
  ipcMain.handle('quark:editor.dirty',(event,dirty) => {trusted(event);if(typeof dirty!=='boolean') throw new Error('编辑状态无效');editorDirty=dirty;});
  ipcMain.handle('quark:call', (event, message) => {
    trusted(event);
    if (!message || typeof message.id !== 'string' || message.id.length > 128 || pending.has(message.id) || !METHODS.includes(message.method)) throw new Error('无效操作');
    return backendRequest(message.method,message.params,message.id).then(result =>
      message.method === 'hello' ? {...result,client_version:app.getVersion()} : result);
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
  ipcMain.handle('quark:dropped-files',async (event,values) => {
    trusted(event);
    if(!Array.isArray(values) || !values.length || values.some(value => typeof value !== 'string' || !path.isAbsolute(value))) throw new Error('拖入的项目必须是本地文件或目录');
    return Promise.all([...new Set<string>(values)].map(async value => {
      let info;
      try {info=await stat(value);}
      catch {throw new Error('拖入的项目不存在或无法访问，请重新选择');}
      if(!info.isFile() && !info.isDirectory()) throw new Error('只能上传文件或目录');
      return {path:value,name:path.basename(value),directory:info.isDirectory()};
    }));
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
app.on('before-quit',event => {if(!quitAllowed) {event.preventDefault();requestQuit();}});
