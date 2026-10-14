const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFile}=require('node:fs/promises');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const {EventEmitter}=require('node:events');
const {PassThrough,Writable}=require('node:stream');

async function source(name) {
  return ts.transpileModule(await readFile(path.resolve(__dirname,'../src/'+name+'.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
}
function fakeTrayApi() {
  const instances=[];
  class Tray extends EventEmitter {
    constructor(icon) {super();this.icon=icon;this.balloons=[];this.updates=0;instances.push(this);}
    setToolTip(value) {this.tooltip=value;}
    setContextMenu(menu) {this.menu=menu;this.updates++;}
    displayBalloon(value) {if(this.failBalloon) throw new Error('notification unavailable');this.balloons.push(value);}
    destroy() {this.destroyed=true;}
  }
  return {Tray,Menu:{buildFromTemplate:items => ({items})},instances};
}
async function backgroundApi(electron) {
  const api={};vm.runInNewContext(await source('Background'),{exports:api,require:name => name === 'electron' ? electron : require(name),Map});return api;
}
const tick=() => new Promise(resolve => setImmediate(resolve));
const task=(id,state='stopped',options={}) => ({id,name:id,state,valid:true,...options});

test('tray actions reuse the queue, block duplicate resume, and skip progress menu rebuilds',async () => {
  const electron=fakeTrayApi(),{Background}=await backgroundApi(electron);
  const requests=[],events=[],opens=[];let release;
  const background=new Background('logo.ico',transfers => opens.push(transfers),method => {requests.push(method);return new Promise(resolve => {release=resolve;});},(event,data) => events.push({event,data}),() => events.push({event:'quit'}));
  const tray=background.tray,menu=id => tray.menu.items.find(item => item.id === id);
  background.snapshot([task('a'),task('share','stopped',{needs_authorization:true}),task('old','waiting',{valid:false})]);
  assert.equal(requests.length,0);assert.equal(menu('resume').enabled,true);assert.equal(menu('pause').enabled,false);
  menu('resume').click();menu('resume').click();
  assert.deepEqual(requests,['tasks.retry']);assert.equal(menu('resume').enabled,false);
  background.event('batch.started',{batch_id:'one'});
  release([task('a','running')]);await tick();
  assert.equal(menu('pause').enabled,true);assert.equal(menu('resume').enabled,false);
  assert.ok(events.some(event => event.event === 'tasks.changed'));
  background.event('task',task('a','running'));
  const updates=tray.updates;
  for(let index=0;index<100;index++) background.event('task',task('a','running',{detail:'progress '+index}));
  assert.equal(tray.updates,updates);
  menu('pause').click();assert.equal(requests.at(-1),'download.stop');release(true);await tick();
  tray.emit('click');menu('transfers').click();assert.deepEqual(opens,[undefined,true]);
  background.event('task',task('a','stopped',{valid:false}));
  background.event('batch.finished',{batch_id:'one',stopped:true,counts:{stopped:1}});
  assert.equal(menu('resume').enabled,false);
  background.available=false;background.update();assert.equal(menu('pause').enabled,false);assert.equal(menu('resume').enabled,false);
  background.available=true;
  background.event('task',task('a','done',{revision:5}));
  background.event('task',task('a','waiting',{revision:4}));
  assert.equal(menu('resume').enabled,false,'an older event must not revive a completed task');
  background.snapshot([task('a','waiting',{revision:4})]);
  assert.equal(menu('resume').enabled,false,'a delayed response must not revive an already completed task');
  background.event('tasks.changed',[task('paused','stopped')]);
  assert.equal(menu('resume').enabled,true);
  background.event('tasks.changed',[]);
  assert.equal(menu('resume').enabled,false,'a removed task must not remain in the tray snapshot');
  assert.equal(menu('quit').enabled,true);menu('quit').click();assert.equal(events.at(-1).event,'quit');
  background.destroy();assert.equal(tray.destroyed,true);
});

test('completion notifications contain only this batch, support click, and respect pause and preference',async () => {
  const electron=fakeTrayApi(),{Background}=await backgroundApi(electron),opens=[],warnings=[];
  const background=new Background('logo.ico',transfers => opens.push(transfers),async () => [],(event,data) => warnings.push({event,data}),() => {});
  const complete=(id,options={}) => background.event('batch.finished',{batch_id:id,stopped:false,counts:{done:2,failed:1,cancelled:1},...options});
  complete('a');complete('a');assert.equal(background.tray.balloons.length,1);
  assert.match(background.tray.balloons[0].content,/成功 2 项，失败 1 项/);
  background.tray.emit('balloon-click');assert.deepEqual(opens,[true]);
  complete('paused',{stopped:true});complete('empty',{counts:{done:0,failed:0,cancelled:2}});
  background.notifications=false;complete('disabled');background.notifications=true;
  background.quitting=true;complete('quitting');background.quitting=false;
  assert.equal(background.tray.balloons.length,1);
  background.tray.failBalloon=true;complete('unavailable');assert.equal(warnings.at(-1).event,'warning');
  assert.equal(background.available,true);
});

async function runtime(closeBehavior='tray') {
  const electron=fakeTrayApi(),app=new EventEmitter(),ipc=new Map(),windows=[];
  app.requestSingleInstanceLock=() => true;app.whenReady=async () => {};
  app.isPackaged=false;app.getAppPath=() => path.resolve(__dirname,'..');
  app.getVersion=() => '0.5.12';
  app.quit=() => {
    if(app.quitted) return;
    const event={preventDefault(){this.prevented=true;}};app.emit('before-quit',event);
    if(event.prevented) return;
    app.quitted=true;windows.forEach(window => window.close());
  };
  class Window extends EventEmitter {
    constructor() {super();this.visible=true;this.sent=[];this.webContents={mainFrame:{},send:(channel,message) => this.sent.push(message),setWindowOpenHandler(){},on(){},session:{setPermissionRequestHandler(){}}};windows.push(this);}
    isDestroyed() {return !!this.destroyed;}
    isMinimized() {return false;}
    show() {this.visible=true;}
    hide() {this.visible=false;}
    focus() {}
    setMenuBarVisibility() {}
    setBackgroundColor() {}
    setTitleBarOverlay() {}
    loadURL() {return Promise.resolve();}
    close() {const event={preventDefault(){this.prevented=true;}};this.emit('close',event);if(!event.prevented) this.destroyed=true;}
  }
  Object.assign(electron,{app,BrowserWindow:Window,ipcMain:{handle:(name,handler) => ipc.set(name,handler)},nativeTheme:Object.assign(new EventEmitter(),{themeSource:'system',shouldUseDarkColors:false}),protocol:{registerSchemesAsPrivileged(){}},dialog:{showMessageBox:async () => {}},clipboard:{},shell:{},net:{}});
  const child=new EventEmitter();child.stdout=new PassThrough();child.stderr=new PassThrough();child.requests=[];
  child.exitCode=null;child.stdin=new Writable({write(chunk,encoding,done){child.requests.push(JSON.parse(chunk.toString()));done();}});
  child.stdin.on('finish',() => {child.ended=true;});
  const {Background}=await backgroundApi(electron);
  const context={exports:{},require:name => {
    if(name === 'electron') return electron;
    if(name === 'node:child_process') return {spawn:() => child};
    if(name === './Background') return {Background};
    if(name === './types') return {METHODS:['hello','tasks','download.stop','tasks.retry']};
    if(name === './WebLogin') return {};
    if(name === './Appearance') return {loadAppearance:async () => ({source:'system',warning:''}),appearanceColors:() => ({background:'#fff',title:'#fff',symbol:'#111'})};
    if(name === './ClientSettings') return {defaultClientSettings:{closeBehavior,notifications:true},loadClientSettings:async () => ({settings:{closeBehavior,notifications:true},warning:''})};
    return require(name);
  },process,URL,Response,Map,MAIN_WINDOW_WEBPACK_ENTRY:'http://localhost/',MAIN_WINDOW_PRELOAD_WEBPACK_ENTRY:'preload.js'};
  vm.runInNewContext(await source('main'),context);await tick();await tick();
  const window=windows[0],tray=electron.instances[0];
  const send=(event,data) => child.stdout.write(JSON.stringify({id:null,event,data})+'\n');
  const call=(method,params={}) => ipc.get('quark:call')({sender:window.webContents,senderFrame:window.webContents.mainFrame},{id:'test-request',method,params});
  return {app,window,tray,child,send,call,cleanup(){child.stdout.end();child.stderr.end();if(!app.quitted) {app.quit();child.emit('close',0);}}};
}

test('closing to tray keeps backend alive and a second instance restores the same window',async () => {
  const r=await runtime();try {
    r.window.close();assert.equal(r.window.visible,false);assert.equal(r.window.destroyed,undefined);assert.equal(r.child.ended,undefined);
    r.send('batch.started',{batch_id:'one'});assert.equal(r.tray.menu.items.find(item => item.id === 'pause').enabled,true);
    r.app.emit('second-instance');assert.equal(r.window.visible,true);
    r.window.hide();r.child.emit('exit',1);assert.equal(r.window.visible,true);
    assert.equal(r.tray.menu.items.find(item => item.id === 'resume').enabled,false);
    assert.ok(r.window.sent.some(message => message.event === 'backend.error'));
  } finally {r.cleanup();}
});

test('safe exit ends stdin once, waits for backend close, blocks new work and suppresses notifications',async () => {
  const r=await runtime('quit');try {
    r.send('batch.started',{batch_id:'one'});
    r.window.close();await tick();
    assert.equal(r.child.ended,true);assert.equal(r.app.quitted,undefined);assert.equal(r.window.destroyed,undefined);
    assert.ok(r.window.sent.some(message => message.event === 'desktop.quitting'));
    assert.throws(() => r.call('tasks.retry'),/正在退出/);
    r.app.quit();assert.equal(r.app.quitted,undefined);
    r.send('batch.finished',{batch_id:'one',stopped:true,counts:{done:1,stopped:2}});
    assert.equal(r.tray.balloons.length,0);
    r.child.emit('close',0);await tick();
    assert.equal(r.app.quitted,true);assert.equal(r.window.destroyed,true);assert.equal(r.tray.destroyed,true);
  } finally {r.cleanup();}
});
