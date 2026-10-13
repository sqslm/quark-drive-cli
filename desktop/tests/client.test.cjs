const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium, _electron } = require('@playwright/test');
const { mkdtemp, readFile, rm, mkdir } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const http = require('node:http');

test('renderer virtualizes files, streams searches, cancels, preserves manual codes and queues selected files', async () => {
  const root = path.resolve(__dirname, '../.webpack/x64/renderer');
  const server = http.createServer(async (request, response) => {
    const filename = path.resolve(root, '.' + (request.url === '/' ? '/index.html' : request.url.split('?')[0]));
    if (!filename.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
    try { response.setHeader('Content-Type', filename.endsWith('.js') ? 'application/javascript' : 'text/html'); response.end(await readFile(filename)); }
    catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1180, height: 780 } });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const listeners = new Set(); let cancelled = false; let tasks = [];
      let shares = [], recycle = [{record_id:'trash-a',fid:'gone-a',name:'测试删除.txt',directory:false,size:4,parent:'0'}, {record_id:'trash-b',fid:'gone-b',name:'测试恢复.txt',directory:false,size:4,parent:'0'}];
      const rows = Array.from({ length: 10000 }, (_, index) => ({ fid: String(index), name: `知否 ${String(index).padStart(5,'0')}.txt`, directory: false, size: index + 1, path: '', location: [] }));
      const preferences = { output_dir: 'C:\\Downloads', connections: 4, limit_rate: null, width: 1180, height: 780 };
      window.testCalls = [];
      window.quark = {
        onEvent: callback => { listeners.add(callback); return () => listeners.delete(callback); },
        chooseFolder: async () => 'C:\\Downloads', chooseFiles:async () => ['C:\\Uploads\\测试.txt'], openFolder: async () => {}, copyText:async text => { window.testCalls.push({method:'clipboard',params:{text}}); },
        call: async (method, params = {}, id) => {
          window.testCalls.push({ method, params });
          if(method === 'hello') return { protocol: 1, preferences, extension_groups:{视频:['mp4','mkv'],音频:['mp3'],文档:['txt']} };
          if(method === 'account') return { nickname: '测试账号' };
          if(method === 'tasks') return tasks.map(task => ({...task}));
          if(method === 'browse') return rows.map(row => ({...row}));
          if(method === 'file.mkdir') { rows.push({fid:'new',name:params.name,directory:true,size:null,path:'',location:[]}); return {fid:'new'}; }
          if(method === 'file.rename') { const row = rows.find(row => row.fid === params.entries[0].fid); row.name = params.name; return [{ name:row.name,state:'done',detail:'操作完成'}]; }
          if(method === 'locate') return { ...params.entry, path: '/知否/' + params.entry.name, location: [['0','我的网盘']] };
          if(method === 'search.cancel') { cancelled = true; return true; }
          if(method === 'search') {
            cancelled = false;
            listeners.forEach(callback => callback({ id, event:'search.page', data:{ entries: rows.slice(0,100), scanned:0 } }));
            await new Promise(resolve => setTimeout(resolve, 500));
            if (!cancelled) listeners.forEach(callback => callback({ id, event:'search.page', data:{ entries: rows.slice(100,105), scanned:0 } }));
            return { cancelled };
          }
          if(method === 'share.parse') return { url:'https://pan.quark.cn/s/abcdef', passcode:'1234' };
          if(method === 'share.open') return { handle:'share', entries:rows.slice(0,2) };
          if(method === 'share.create') { const result={result:'created',share_id:'own-new',pwd_id:'new-public',title:params.entry.name,share_url:'https://pan.quark.cn/s/new-public',passcode:params.passcode,expired_type:4};shares.push(result);return result; }
          if(method === 'share.list') return {items:shares.map(row => ({...row})),catalog_complete:true};
          if(method === 'share.link') return shares.find(row => row.share_id === params.share_id);
          if(method === 'share.revoke') {shares=shares.filter(row => !params.entries.some(entry => entry.share_id === row.share_id));return params.entries.map(row => ({name:row.title,state:'done',detail:'已取消分享'}));}
          if(method === 'recycle.list') return recycle.map(row => ({...row}));
          if(method === 'recycle.remove' || method === 'recycle.restore') {recycle=recycle.filter(row => !params.entries.some(entry => entry.record_id === row.record_id));return params.entries.map(row => ({name:row.name,state:'done',detail:method === 'recycle.remove' ? '已永久删除' : '已恢复到原目录'}));}
          if(method === 'share.save') return window.testSaveUnknown ? {saved:false,result:'unknown',detail:'结果未确认，请核实目标目录'} : {saved:true,item_count:params.entries.length,destination_path:'/'};
          if(method === 'preferences.save') return { preferences:params, persisted:true, warning:'' };
          if(method === 'download') { tasks.push({ id:'task', name: params.entries[0].name, state:'waiting', detail:'', target:'C:\\Downloads\\test.txt', valid:true,direction:'download',revision:1 }); return tasks.map(task => ({...task})); }
          if(method === 'upload') { tasks.push({ id:'upload-task',name:'测试.txt',state:'waiting',detail:'',target:'/测试.txt',valid:true,direction:'upload',revision:1 }); return tasks.map(task => ({...task})); }
          if(method === 'tasks.cancel') { tasks = tasks.map(task => params.ids.includes(task.id) ? {...task,state:'cancelled',revision:task.revision+1} : task); return tasks.map(task => ({...task})); }
          if(method === 'download.stop') { tasks = tasks.map(task => task.state === 'cancelled' ? task : {...task,state:'stopped',revision:task.revision+1}); tasks.forEach(task => listeners.forEach(callback => callback({id:null,event:'task',data:task}))); listeners.forEach(callback => callback({id:null,event:'batch.finished',data:{stopped:true}})); return true; }
          if(method === 'login') throw new Error('登录服务暂时不可用');
          return [];
        }
      };
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/main_window/index.html`);
    await page.getByText('显示 10000 项', { exact: true }).waitFor();
    assert.ok(await page.locator('.file-row').count() < 60);
    await page.getByLabel('格式筛选',{exact:true}).selectOption('视频');
    await page.getByText('显示 0 项',{exact:true}).waitFor();
    assert.ok(await page.getByLabel('格式筛选',{exact:true}).locator('option').count() >= 5);
    await page.getByLabel('格式筛选',{exact:true}).selectOption('文档');
    await page.getByText('显示 10000 项',{exact:true}).waitFor();
    await page.getByLabel('格式筛选',{exact:true}).selectOption('');
    await page.getByRole('button',{name:'新建目录',exact:true}).click();
    await page.getByLabel('新名称').fill('新建测试');
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('显示 10001 项',{exact:true}).waitFor();
    const created = await page.evaluate(() => window.testCalls.find(call => call.method === 'file.mkdir'));
    assert.equal(created.params.parent,'0'); assert.equal(created.params.name,'新建测试');
    await page.getByLabel('选择 知否 00001.txt',{exact:true}).check();
    await page.getByRole('button',{name:'创建分享',exact:true}).click();
    await page.getByLabel('分享有效期').selectOption('30');
    await page.getByLabel('新分享提取码').fill('a123');
    await page.getByRole('button',{name:'确认创建',exact:true}).click();
    await page.getByRole('button',{name:'复制分享文案',exact:true}).click();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const shareCreated = await page.evaluate(() => window.testCalls.find(call => call.method === 'share.create'));
    assert.equal(shareCreated.params.entry.fid,'1');assert.equal(shareCreated.params.expire_days,30);assert.equal(shareCreated.params.passcode,'a123');
    assert.ok(await page.evaluate(() => window.testCalls.some(call => call.method === 'clipboard' && call.params.text.includes('提取码：a123'))));
    await page.getByRole('button',{name:'我的分享',exact:true}).click();
    await page.getByLabel('选择记录 知否 00001.txt',{exact:true}).check();
    await page.getByRole('button',{name:'取消选中分享',exact:true}).click();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('知否 00001.txt：已取消分享',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.getByRole('button',{name:'回收站',exact:true}).click();
    await page.getByLabel('选择记录 测试删除.txt',{exact:true}).check();
    await page.getByRole('button',{name:'永久删除选中记录',exact:true}).click();
    assert.ok(await page.getByRole('button',{name:'确认',exact:true}).isDisabled());
    await page.getByLabel('我确认永久删除这些记录').check();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('测试删除.txt：已永久删除',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const removed = await page.evaluate(() => window.testCalls.find(call => call.method === 'recycle.remove'));
    assert.deepEqual(removed.params.entries.map(row => row.record_id),['trash-a']);
    await page.getByLabel('选择记录 测试恢复.txt',{exact:true}).check();
    await page.getByRole('button',{name:'恢复选中记录',exact:true}).click();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('测试恢复.txt：已恢复到原目录',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.getByLabel('搜索关键词').fill('知否');
    await page.getByRole('button', { name:'搜索', exact:true }).click();
    await page.getByRole('button', { name:'取消搜索' }).waitFor();
    assert.ok(await page.getByLabel('搜索关键词').isEnabled());
    await page.getByRole('button', { name:'取消搜索' }).click();
    await page.waitForTimeout(600);
    await page.getByText('显示 100 项', { exact:true }).waitFor();
    await page.getByLabel('选择 知否 00000.txt', { exact:true }).check();
    await page.getByRole('button', { name:'下载（1）', exact:true }).click();
    await page.getByRole('heading', { name:'传输任务' }).waitFor();
    const downloaded = await page.evaluate(() => window.testCalls.find(call => call.method === 'download').params.entries);
    assert.equal(downloaded.length, 1); assert.equal(downloaded[0].fid, '0');
    await page.getByRole('button', {name:'我的文件',exact:true}).click();
    await page.getByRole('button', {name:'上传文件',exact:true}).click();
    await page.getByRole('heading', {name:'传输任务'}).waitFor();
    await page.getByText('测试.txt',{exact:true}).waitFor();
    const uploaded = await page.evaluate(() => window.testCalls.find(call => call.method === 'upload'));
    assert.equal(uploaded.params.parent,'0'); assert.deepEqual(uploaded.params.sources,['C:\\Uploads\\测试.txt']);
    await page.locator('.task-row').filter({hasText:'测试.txt'}).getByRole('button',{name:'取消',exact:true}).click();
    await page.getByText('已取消',{exact:true}).waitFor();
    await page.getByRole('button',{name:'暂停全部',exact:true}).click();
    await page.getByText('已暂停',{exact:true}).waitFor();
    await page.getByRole('button', { name:'分享链接', exact:true }).click();
    await page.getByLabel('分享链接或文案').fill('https://pan.quark.cn/s/abcdef 提取码 1234');
    await page.getByLabel('提取码', { exact:true }).click();
    await page.getByLabel('提取码', { exact:true }).fill('5678');
    await page.getByRole('button', { name:'打开分享', exact:true }).click();
    const opened = await page.evaluate(() => window.testCalls.find(call => call.method === 'share.open'));
    assert.equal(opened.params.passcode, '5678');
    await page.getByLabel('选择 知否 00001.txt',{exact:true}).check();
    await page.getByRole('button',{name:'永久转存到网盘',exact:true}).click();
    await page.getByRole('button',{name:'确认永久转存',exact:true}).click();
    await page.getByText('已转存 1 项到 /',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const saved = await page.evaluate(() => window.testCalls.find(call => call.method === 'share.save'));
    assert.equal(saved.params.handle,'share');assert.equal(saved.params.target,'0');assert.deepEqual(saved.params.entries.map(row => row.fid),['1']);
    await page.evaluate(() => {window.testSaveUnknown = true;});
    await page.getByRole('button',{name:'永久转存到网盘',exact:true}).click();
    await page.getByRole('button',{name:'确认永久转存',exact:true}).click();
    await page.getByText('结果未确认，请核实目标目录',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'确认永久转存',exact:true}).count(),0);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await mkdir(path.resolve(__dirname, '../test-results'), { recursive:true });
    await page.screenshot({ path:path.resolve(__dirname, '../test-results/client.png') });
    assert.deepEqual(errors, []);
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});

test('packaged Electron starts its bundled backend with sandbox and no external Python', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'quark-electron-test-'));
  const executable = path.resolve(__dirname, '..', process.env.QUARK_DESKTOP_OUT || 'out', 'Quark Drive Client-win32-x64/QuarkDriveClient.exe');
  const app = await _electron.launch({ executablePath: executable, args:['--user-data-dir=' + path.join(directory,'electron')], env:{ ...process.env,
    QUARK_CONFIG_DIR: directory, QUARK_COOKIE:'', QUARK_COOKIE_FILE:'', QUARK_DESKTOP_PYTHON:'Z:\\no-python.exe' } });
  try {
    const page = await app.firstWindow();
    await page.getByRole('heading', { name:'我的文件' }).waitFor();
    const hello = await page.evaluate(() => window.quark.call('hello'));
    assert.equal(await page.locator('.titlebar').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(246, 248, 252)');
    assert.ok(await page.evaluate(() => navigator.windowControlsOverlay?.visible));
    assert.equal(hello.protocol, 1);
    assert.equal(await page.evaluate(() => typeof require), 'undefined');
    const preferences = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences());
    assert.equal(preferences.contextIsolation, true); assert.equal(preferences.sandbox, true); assert.equal(preferences.nodeIntegration, false);
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].maximize());
    await page.waitForTimeout(100);
    assert.ok(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isMaximized()));
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].unmaximize());
    await page.waitForTimeout(100);
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isMaximized()),false);
    await assert.rejects(page.evaluate(() => window.quark.call('os.system')), /不支持/);
  } finally { await app.close(); await rm(directory, { recursive:true, force:true }); }
});
