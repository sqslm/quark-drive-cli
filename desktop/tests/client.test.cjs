const { test } = require('node:test');
const assert = require('node:assert/strict');
const { chromium, _electron } = require('@playwright/test');
const { mkdtemp, readFile, rm, mkdir, writeFile } = require('node:fs/promises');
const { tmpdir } = require('node:os');
const path = require('node:path');
const http = require('node:http');

test('renderer virtualizes files, streams searches, cancels, preserves manual codes and queues selected files', async () => {
  const root = path.resolve(__dirname, '../.webpack/x64/renderer');
  const server = http.createServer(async (request, response) => {
    const filename = path.resolve(root, '.' + (request.url === '/' ? '/index.html' : request.url.split('?')[0]));
    if (!filename.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
    try { response.setHeader('Content-Type', filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.png') ? 'image/png' : 'text/html'); response.end(await readFile(filename)); }
    catch { response.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1180, height: 780 },reducedMotion:'no-preference',colorScheme:'light',timezoneId:'Asia/Singapore' });
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      const listeners = new Set(); let cancelled = false; let tasks = [];let cut=null;let favorites=[];let loggedIn=true;
      window.testWebCancel=true;
      window.testClockMs = Date.now();Date.now = () => window.testClockMs;
      let shares = [], recycle = [{record_id:'trash-a',fid:'gone-a',name:'测试删除.txt',directory:false,size:4,parent:'0'}, {record_id:'trash-b',fid:'gone-b',name:'测试恢复.txt',directory:false,size:4,parent:'0'}];
      const rows = Array.from({ length: 10000 }, (_, index) => ({ fid: String(index), name: `知否 ${String(index).padStart(5,'0')}.txt`, directory: false, size: index + 1, path: '', location: [],modified_at:index === 0 ? null : index === 1 ? Date.UTC(2026,9,14,4,34) : Date.UTC(2025,0,1)+index*60000 }));
      const preferences = { output_dir: 'C:\\Downloads', connections: 4, limit_rate: null, width: 1180, height: 780 };
      window.testCalls = [];
      let appearanceSource='system';
      let clientSettings={closeBehavior:'tray',notifications:true};
      matchMedia('(prefers-color-scheme: dark)').addEventListener('change',event => {if(appearanceSource === 'system') listeners.forEach(listener => listener({id:null,event:'appearance',data:{source:'system',dark:event.matches,warning:''}}));});
      window.quark = {
        readDroppedFiles:async files => {
          window.testDropReads=(window.testDropReads || 0)+1;
          if(window.testHoldDrop) await new Promise(resolve => {window.releaseDrop=resolve;});
          if(window.testDropFailure) throw new Error('拖入项目无法访问');
          return files.map(file => ({path:'C:\\Dropped\\'+file.name,name:file.name,directory:file.name.endsWith('目录')}));
        },
        getClientSettings:async () => ({settings:clientSettings,warning:''}),
        setClientSettings:async settings => {clientSettings=settings;window.testCalls.push({method:'desktop.set',params:settings});return {settings,warning:window.testClientSettingsWarning ? '后台运行设置已生效，但未能保存；本次窗口内有效' : ''};},
        quit:async () => {listeners.forEach(listener => listener({id:null,event:'desktop.quitting',data:true}));},
        getAppearance:async () => ({source:appearanceSource,dark:matchMedia('(prefers-color-scheme: dark)').matches,warning:''}),
        setAppearance:async source => {appearanceSource=source;const result={source,dark:source === 'dark' || source === 'system' && matchMedia('(prefers-color-scheme: dark)').matches,warning:window.testAppearanceWarning ? '外观已切换，但未能保存；本次窗口内有效' : ''};listeners.forEach(listener => listener({id:null,event:'appearance',data:result}));return result;},
        onEvent: callback => { listeners.add(callback); return () => listeners.delete(callback); },
        chooseFolder: async () => 'C:\\Downloads', chooseFiles:async () => ['C:\\Uploads\\测试.txt'], openFolder: async () => {}, copyText:async text => { window.testCalls.push({method:'clipboard',params:{text}}); },
        officialLogin:async mode => {window.testCalls.push({method:'officialLogin',params:{mode}});return window.testWebCancel ? {cancelled:true} : {nickname:'新账号',logged_in:true};},
        call: async (method, params = {}, id) => {
          window.testCalls.push({ method, params });
          if(method === 'hello') return { protocol: 1,client_version:'0.5.24',signed_in:true, preferences, extension_groups:{视频:['mp4','mkv'],音频:['mp3'],文档:['txt']} };
          if(method === 'account') return { nickname: loggedIn ? '测试账号' : '未登录',logged_in:loggedIn };
          if(method === 'account.capacity') {
            const data={...(window.testCapacity || {used_bytes:50*1073741824,total_bytes:100*1073741824})};
            if(window.testHoldCapacity) await new Promise(resolve => {window.capacityReleases ||= [];window.capacityReleases.push(resolve);});
            if(window.testCapacityFailure) throw new Error('capacity unavailable');
            return data;
          }
          if(method === 'logout') {loggedIn=false;cut=null;tasks=tasks.map(row => ({...row,valid:false}));return {logged_in:false,nickname:'未登录'};}
          if(method === 'favorites.list') return favorites.map(row => ({...row}));
          if(method === 'favorites.add') {const [fid,name]=params.trail.at(-1);favorites=[...favorites.filter(row => row.fid !== fid),{fid,name}];return favorites;}
          if(method === 'favorites.remove') {favorites=favorites.filter(row => row.fid !== params.fid);return favorites;}
          if(method === 'favorites.open') return [['0','我的网盘'],[params.fid,favorites.find(row => row.fid === params.fid).name]];
          if(method === 'file.paths') return params.entries.map(row => ({fid:row.fid,path:'/'+row.name}));
          if(method === 'file.info') return {file_name:params.entry.name,size:3489662954,path:'/'+params.entry.name,directory:false};
          if(method === 'tasks') return tasks.map(task => ({...task}));
          if(method === 'browse') {
            if(window.testHoldBrowse) await new Promise(resolve => {window.releaseBrowse=resolve;});
            if(window.testBrowseFailure) throw new Error('测试目录读取失败');
            return rows.filter(row => (row.parent || '0') === (params.fid || '0')).map(row => ({...row}));
          }
          if(method === 'file.mkdir') { listeners.forEach(callback => callback({id,event:'file.progress',data:'正在创建目录…'}));listeners.forEach(callback => callback({id:'old-file-operation',event:'file.progress',data:'过期文件进度'}));if(window.testHoldMkdir) await new Promise(resolve => {window.releaseMkdir=resolve;});rows.push({fid:'new',name:params.name,directory:true,size:null,path:'',location:[]}); return {fid:'new'}; }
          if(method === 'file.rename') { const row = rows.find(row => row.fid === params.entries[0].fid); row.name = params.name; return [{ name:row.name,state:'done',detail:'操作完成'}]; }
          if(method === 'file.cut' || method === 'file.copy') {cut={id:method === 'file.copy' ? 'copy-current' : 'cut-current',entries:params.entries,blocked:false,mode:method === 'file.copy' ? 'copy' : 'cut'};return cut;}
          if(method === 'file.clearCut') {cut=null;return null;}
          if(method === 'file.paste' && cut.mode === 'copy') {listeners.forEach(callback => callback({id,event:'file.progress',data:'正在批量复制 1 项…'}));if(window.testHoldCopy) await new Promise(resolve => {window.releaseCopy=resolve;});const results=cut.entries.map(entry => {rows.push({...entry,fid:'copy-'+entry.fid,parent:params.target});return {fid:entry.fid,name:entry.name,state:'done',detail:'已复制到目标目录，源项目保留'};});return {results,clipboard:cut};}
          if(method === 'file.paste') {const results=cut.entries.map(entry => {rows.find(row => row.fid === entry.fid).parent=params.target;return {fid:entry.fid,name:entry.name,state:'done',detail:'已移动到目标目录'};});cut=null;return {results,clipboard:null};}
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
          if(method === 'share.create') { listeners.forEach(callback => callback({id,event:'share.progress',data:'正在等待服务器生成链接…'})); await new Promise(resolve => {window.releaseShareCreate = resolve;});const result={result:'created',share_id:'own-new',pwd_id:'new-public',title:params.entry.name,share_url:'https://pan.quark.cn/s/new-public',passcode:params.passcode,expired_type:4,expired_at:Date.now()+720*3600000};shares.push(result);return result; }
          if(method === 'share.list') return {items:shares.map(row => ({...row})),catalog_complete:true};
          if(method === 'share.link') return shares.find(row => row.share_id === params.share_id);
          if(method === 'share.revoke') {listeners.forEach(callback => callback({id,event:'share.progress',data:'正在批量取消 1 个分享…'}));if(window.testHoldRevoke) await new Promise(resolve => {window.releaseRevoke=resolve;});shares=shares.filter(row => !params.entries.some(entry => entry.share_id === row.share_id));return params.entries.map(row => ({share_id:row.share_id,name:row.title,state:'done',detail:'已取消分享'}));}
          if(method === 'recycle.list') return recycle.map(row => ({...row}));
          if(method === 'recycle.remove' || method === 'recycle.restore') {
            const unknown=method === 'recycle.remove' ? (window.testRecycleUnknownIds || []) : [];
            if(method === 'recycle.remove') {
              listeners.forEach(callback => callback({id,event:'recycle.progress',data:`已核验 ${params.entries.length} 条，正在批量永久删除…`}));
              listeners.forEach(callback => callback({id:'old-recycle-request',event:'recycle.progress',data:'过期删除进度'}));
              if(window.testHoldRecycleRemove) await new Promise(resolve => {window.releaseRecycleRemove=resolve;});
            }
            recycle=recycle.filter(row => !params.entries.some(entry => entry.record_id === row.record_id && !unknown.includes(row.record_id)));
            return params.entries.map(row => ({record_id:row.record_id,name:row.name,state:unknown.includes(row.record_id) ? 'unknown' : 'done',detail:unknown.includes(row.record_id) ? '已提交，未重发，请刷新核实' : method === 'recycle.remove' ? '已永久删除' : '已恢复到原目录'}));
          }
          if(method === 'share.save') return window.testSaveUnknown ? {saved:false,result:'unknown',detail:'结果未确认，请核实目标目录'} : {saved:true,item_count:params.entries.length,destination_path:'/'};
          if(method === 'preferences.save') return { preferences:params, persisted:!window.testSaveWarning, warning:window.testSaveWarning ? '设置仅在窗口内有效' : '' };
          if(method === 'download') { tasks.push({ id:'task', name: params.entries[0].name, state:'waiting', detail:'', target:'C:\\Downloads\\test.txt', valid:true,direction:'download',revision:1,scheduled:true }); return tasks.map(task => ({...task})); }
          if(method === 'upload') {
            if(window.testHoldUpload) await new Promise(resolve => {window.releaseUpload=resolve;});
            params.sources.forEach(source => {const name=source.split('\\').at(-1);tasks.push({id:'upload-'+tasks.length,name,state:'waiting',detail:'',target:'/'+name,valid:true,direction:'upload',revision:1,scheduled:true});});
            return tasks.map(task => ({...task}));
          }
          if(method === 'tasks.cancel') { tasks = tasks.map(task => params.ids.includes(task.id) ? {...task,state:'cancelled',revision:task.revision+1} : task); return tasks.map(task => ({...task})); }
          if(method === 'tasks.pause') {tasks=tasks.map(task => params.ids.includes(task.id) ? {...task,state:'stopped',scheduled:false,revision:task.revision+1} : task);return tasks.map(task => ({...task}));}
          if(method === 'tasks.remove') {tasks=tasks.filter(task => !params.ids.includes(task.id));return tasks.map(task => ({...task}));}
          if(method === 'tasks.retry') {tasks=tasks.map(task => (!params.ids || params.ids.includes(task.id)) ? {...task,state:'waiting',scheduled:true,revision:task.revision+1} : task);return tasks.map(task => ({...task}));}
          if(method === 'tasks.clear') {tasks=tasks.filter(task => !(['done','cancelled'].includes(task.state) && (!params.ids || params.ids.includes(task.id))));return tasks.map(task => ({...task}));}
          if(method === 'download.stop') { tasks = tasks.map(task => task.state === 'cancelled' ? task : {...task,state:'stopped',revision:task.revision+1}); tasks.forEach(task => listeners.forEach(callback => callback({id:null,event:'task',data:task}))); listeners.forEach(callback => callback({id:null,event:'batch.finished',data:{stopped:true}})); return true; }
          if(method === 'login') throw new Error('登录服务暂时不可用');
          return [];
        }
      };
      window.testEmit=event => listeners.forEach(listener => listener(event));
      window.testGetTasks=() => tasks.map(task => ({...task}));
      window.testSetRecycle=next => {recycle=next;};
      window.testSetTasks=next => {tasks=next;listeners.forEach(listener => listener({id:null,event:'tasks.changed',data:tasks}));};
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/main_window/index.html`);
    async function dragData(names) {
      return page.evaluateHandle(names => {const data=new DataTransfer();names.forEach(name => data.items.add(new File(['payload'],name)));return data;},names);
    }
    async function drop(names) {
      const data=await dragData(names);await page.locator('.file-list').dispatchEvent('drop',{dataTransfer:data});await data.dispose();
    }
    await page.getByText('显示 10000 项', { exact: true }).waitFor();
    const capacity=page.getByRole('region',{name:'网盘空间',exact:true});
    await capacity.getByText('50.0%',{exact:true}).waitFor();
    assert.equal(await capacity.getByRole('progressbar').getAttribute('aria-valuenow'),'50');
    await page.evaluate(() => {window.testCapacityFailure=true;});await capacity.getByRole('button',{name:'刷新网盘空间'}).click();
    await capacity.getByText('更新失败，显示上次用量',{exact:true}).waitFor();
    await capacity.getByText('50.0%',{exact:true}).waitFor();
    await page.evaluate(() => {window.testCapacityFailure=false;window.testCapacity={used_bytes:125*1073741824,total_bytes:100*1073741824};});
    await capacity.getByRole('button',{name:'刷新网盘空间'}).click();await capacity.getByText('125.0%',{exact:true}).waitFor();
    assert.equal(await capacity.getByRole('progressbar').getAttribute('aria-valuenow'),'100');
    await capacity.getByText('已超过当前容量',{exact:true}).waitFor();
    await page.evaluate(() => {window.testCapacity={used_bytes:0,total_bytes:0};});await capacity.getByRole('button',{name:'刷新网盘空间'}).click();
    await capacity.getByText('总容量为零，占比未知',{exact:true}).waitFor();
    assert.equal(await capacity.getByRole('progressbar').getAttribute('aria-valuenow'),null);
    await page.evaluate(() => {window.testHoldCapacity=true;window.testCapacity={used_bytes:1099511627776,total_bytes:2199023255552};});
    const capacityBefore=await page.evaluate(() => window.testCalls.filter(call => call.method==='account.capacity').length);
    await capacity.getByRole('button',{name:'刷新网盘空间'}).click();await page.waitForFunction(() => window.capacityReleases?.length===1);
    await page.evaluate(() => {for(let i=0;i<8;i++) window.testEmit({event:'capacity.changed',data:null});});
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method==='account.capacity').length),capacityBefore+1);
    await page.evaluate(() => {window.testHoldCapacity=false;window.capacityReleases.shift()();});
    await capacity.getByText('1.00 TB / 2.00 TB',{exact:true}).waitFor();
    await page.waitForFunction(() => !document.querySelector('[aria-label="刷新网盘空间"]').disabled);
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method==='account.capacity').length),capacityBefore+2);
    assert.ok(await page.locator('.file-row').count() < 60);
    const listBefore=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    await page.evaluate(() => {window.testHoldBrowse=true;});
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByText('正在读取目录…',{exact:true}).waitFor();
    const listDuring=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    assert.ok(Math.abs(listBefore.rowY-listDuring.rowY) < 1,'loading must not push existing rows down');
    assert.equal(listDuring.height,listBefore.height,'loading must not change the scrollable list height');
    await page.evaluate(() => {window.testHoldBrowse=false;window.releaseBrowse();});
    await page.getByText('正在读取目录…',{exact:true}).waitFor({state:'hidden'});
    await page.locator('.file-scroll').evaluate(element => {element.scrollTop=920;});
    await page.waitForFunction(() => document.querySelector('.file-row').getBoundingClientRect().y < document.querySelector('.file-scroll').getBoundingClientRect().y);
    const scrolledBefore=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,top:element.scrollTop,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    await page.evaluate(() => {window.testHoldBrowse=true;window.testBrowseFailure=true;});
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByText('正在读取目录…',{exact:true}).waitFor();
    const scrolledDuring=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,top:element.scrollTop,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    assert.deepEqual(scrolledDuring,scrolledBefore);
    assert.equal(await page.locator('.file-scroll').evaluate(element => element.inert),true);
    await page.evaluate(() => {window.testHoldBrowse=false;window.releaseBrowse();});
    await page.getByText('正在读取目录…',{exact:true}).waitFor({state:'hidden'});
    await page.locator('.toast.error').getByText('测试目录读取失败',{exact:true}).waitFor();
    assert.equal(await page.locator('.file-scroll').evaluate(element => element.scrollTop),scrolledBefore.top);
    assert.equal(await page.locator('.file-scroll').evaluate(element => element.inert),false);
    await page.evaluate(() => {window.testBrowseFailure=false;document.querySelector('.file-scroll').scrollTop=0;});
    assert.equal(await page.getByRole('button',{name:'重命名',exact:true}).count(),0);
    assert.equal(await page.getByLabel('格式筛选',{exact:true}).count(),0);
    await page.getByRole('button',{name:'筛选',exact:true}).click();
    await page.getByLabel('格式筛选',{exact:true}).selectOption('视频');
    await page.getByText('显示 0 项',{exact:true}).waitFor();
    assert.ok(await page.getByLabel('格式筛选',{exact:true}).locator('option').count() >= 5);
    await page.getByLabel('格式筛选',{exact:true}).selectOption('文档');
    await page.getByText('显示 10000 项',{exact:true}).waitFor();
    await page.getByLabel('格式筛选',{exact:true}).selectOption('');
    await page.getByRole('button',{name:'新建目录',exact:true}).click();
    await page.getByLabel('新名称').fill('新建测试');
    await page.evaluate(() => {window.testHoldMkdir=true;});
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.locator('.file-progress').getByText('正在创建目录…',{exact:true}).waitFor();
    assert.equal(await page.getByText('过期文件进度',{exact:true}).count(),0);
    assert.ok(await page.getByRole('button',{name:'处理中…',exact:true}).isDisabled());
    await page.evaluate(() => {window.testHoldMkdir=false;window.releaseMkdir();});
    await page.getByText('显示 10001 项',{exact:true}).waitFor();
    const created = await page.evaluate(() => window.testCalls.find(call => call.method === 'file.mkdir'));
    assert.equal(created.params.parent,'0'); assert.equal(created.params.name,'新建测试');
    await page.getByLabel('选择 知否 00000.txt',{exact:true}).check();
    const beforeTimeSort=await page.evaluate(() => window.testCalls.length);
    const timeSort=page.getByRole('button',{name:'按修改时间排序',exact:true});
    await timeSort.click();assert.equal(await timeSort.textContent(),'修改时间 ↓');
    assert.equal(await page.locator('.file-row').first().locator('.file-name').textContent(),'新建测试');
    assert.equal(await page.locator('.file-row').nth(1).locator('.file-name').textContent(),'知否 00001.txt');
    assert.equal(await page.locator('.file-row').nth(1).locator('.modified-time').textContent(),'2026-10-14 12:34');
    assert.equal(await page.locator('.file-row').first().locator('.modified-time').textContent(),'未知');
    assert.ok(await page.getByRole('button',{name:'下载（1）',exact:true}).isEnabled());
    await page.setViewportSize({width:820,height:600});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    const columns=await page.locator('.file-row').first().evaluate(element => Array.from(element.children).map(cell => ({x:cell.getBoundingClientRect().x,width:cell.getBoundingClientRect().width})));
    assert.equal(columns.length,6);assert.ok(columns.every((cell,index) => !index || cell.x >= columns[index-1].x+columns[index-1].width));
    await page.setViewportSize({width:1180,height:780});
    await mkdir(path.resolve(__dirname,'../test-results'),{recursive:true});
    await page.screenshot({path:path.resolve(__dirname,'../test-results/modification-time.png')});
    await page.locator('.file-scroll').evaluate(element => {element.scrollTop=element.scrollHeight;});
    await page.getByLabel('选择 知否 00000.txt',{exact:true}).waitFor();
    assert.ok(await page.getByLabel('选择 知否 00000.txt',{exact:true}).isChecked());
    assert.equal(await page.locator('.file-row').last().locator('.modified-time').textContent(),'未知');
    await timeSort.click();assert.equal(await timeSort.textContent(),'修改时间 ↑');
    await page.locator('.file-scroll').evaluate(element => {element.scrollTop=0;});
    await page.locator('.file-row').getByRole('button',{name:'知否 00002.txt',exact:true}).waitFor();
    assert.equal(await page.locator('.file-row').nth(1).locator('.file-name').textContent(),'知否 00002.txt');
    assert.ok(await page.locator('.file-row').count() < 60);
    await page.locator('.file-scroll').evaluate(element => {element.scrollTop=element.scrollHeight;});
    await page.getByLabel('选择 知否 00000.txt',{exact:true}).waitFor();
    assert.equal(await page.locator('.file-row').last().locator('.modified-time').textContent(),'未知');
    await page.locator('.file-header').getByRole('button',{name:'大小',exact:true}).click();
    await page.locator('.file-scroll').evaluate(element => {element.scrollTop=0;});
    await page.getByLabel('选择 知否 00000.txt',{exact:true}).uncheck();
    assert.equal(await page.evaluate(() => window.testCalls.length),beforeTimeSort);
    await page.getByLabel('选择 知否 00003.txt',{exact:true}).check();
    const beforeMenu = await page.evaluate(() => window.testCalls.length);
    await page.locator('.file-row').filter({hasText:'知否 00002.txt'}).click({button:'right'});
    await page.getByRole('menu',{name:'文件操作'}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.length),beforeMenu);
    await mkdir(path.resolve(__dirname,'../test-results'),{recursive:true});
    await page.screenshot({path:path.resolve(__dirname,'../test-results/context-menu.png')});
    await page.getByRole('menuitem',{name:/^剪切/}).click();
    await page.locator('.toast.success').getByText('已剪切 1 项，请打开目标目录后粘贴',{exact:true}).waitFor();
    assert.equal(await page.locator('.file-row.cut').count(),1);
    const cutCall=await page.evaluate(() => window.testCalls.find(call => call.method==='file.cut'));
    assert.deepEqual(cutCall.params.entries.map(row => row.fid),['2']);
    await page.locator('.file-header').getByRole('button',{name:/^名称/}).click();
    const beforeFavorite=await page.evaluate(() => window.testCalls.filter(call => ['browse','locate'].includes(call.method)).length);
    await page.locator('.file-row').filter({hasText:'新建测试'}).click({button:'right'});
    await page.getByRole('menuitem',{name:'设为常用文件夹',exact:true}).click();
    await page.locator('.favorites').getByRole('button',{name:'新建测试',exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => ['browse','locate'].includes(call.method)).length),beforeFavorite);
    const folderBefore=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    await page.evaluate(() => {window.testHoldBrowse=true;});
    await page.locator('.file-row').getByRole('button',{name:'新建测试',exact:true}).click();
    await page.getByText('正在读取目录…',{exact:true}).waitFor();
    const folderDuring=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    assert.deepEqual(folderDuring,folderBefore);
    await page.evaluate(() => {window.testHoldBrowse=false;window.releaseBrowse();});
    await page.getByText('暂无文件',{exact:true}).waitFor();
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'新建测试',exact:true}).waitFor();
    assert.ok(await page.locator('.breadcrumbs').evaluate(element => element.getBoundingClientRect().bottom < document.querySelector('.file-header').getBoundingClientRect().top));
    await page.screenshot({path:path.resolve(__dirname,'../test-results/favorites-breadcrumb.png')});
    await page.getByLabel('文件列表',{exact:true}).click({button:'right',position:{x:30,y:40}});
    await page.getByRole('menuitem',{name:/^粘贴/}).click();
    await page.getByRole('dialog',{name:'粘贴（移动）'}).getByText('目标：我的网盘 / 新建测试',{exact:true}).waitFor();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('知否 00002.txt：已移动到目标目录',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const pasteCall=await page.evaluate(() => window.testCalls.find(call => call.method==='file.paste'));
    assert.equal(pasteCall.params.id,'cut-current');assert.equal(pasteCall.params.target,'new');
    assert.equal(await page.locator('.cut-bar').count(),0);
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'我的网盘',exact:true}).click();
    await page.locator('.file-row').filter({hasText:'知否 00003.txt'}).waitFor();
    await page.locator('.file-row').filter({hasText:'知否 00003.txt'}).click({button:'right'});
    const beforeCopy=await page.evaluate(() => window.testCalls.length);
    await page.getByRole('menuitem',{name:/^复制\s*Ctrl\+C$/}).click();
    await page.locator('.toast.success').getByText('已复制 1 项，请打开目标目录后粘贴',{exact:true}).waitFor();
    assert.equal(await page.locator('.file-row.cut').count(),0);
    assert.deepEqual(await page.evaluate(n => window.testCalls.slice(n).map(row => row.method),beforeCopy),['file.copy']);
    await page.getByLabel('文件列表',{exact:true}).focus();await page.keyboard.press('Control+c');
    assert.equal(await page.evaluate(() => window.testCalls.filter(row => row.method === 'file.copy').length),2);
    await page.locator('.favorites').getByRole('button',{name:'新建测试',exact:true}).click();
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'新建测试',exact:true}).waitFor();
    await page.getByLabel('文件列表',{exact:true}).focus();await page.keyboard.press('Control+v');
    const copyDialog=page.getByRole('dialog',{name:'粘贴（复制）',exact:true});
    await copyDialog.getByText('确认后复制这些项目，保留源项目。同名冲突不覆盖。',{exact:true}).waitFor();
    await page.evaluate(() => {window.testHoldCopy=true;});
    await copyDialog.getByRole('button',{name:'确认',exact:true}).click();
    await copyDialog.getByText('正在批量复制 1 项…',{exact:true}).waitFor();
    assert.ok(await copyDialog.getByRole('button',{name:'处理中…',exact:true}).isDisabled());
    await page.evaluate(() => {window.testHoldCopy=false;window.releaseCopy();});
    await copyDialog.getByText('知否 00003.txt：已复制到目标目录，源项目保留',{exact:true}).waitFor();
    await copyDialog.getByRole('button',{name:'关闭',exact:true}).click();
    await page.locator('.file-row').getByRole('button',{name:'知否 00003.txt',exact:true}).waitFor();
    await page.getByRole('button',{name:'取消复制',exact:true}).click();
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'我的网盘',exact:true}).click();
    await page.locator('.file-row').getByRole('button',{name:'知否 00003.txt',exact:true}).waitFor();
    await page.locator('.favorites').getByRole('button',{name:'新建测试',exact:true}).click();
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'新建测试',exact:true}).waitFor();
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'我的网盘',exact:true}).click();
    await page.getByRole('button',{name:'移除常用 新建测试',exact:true}).click();
    await page.locator('.favorites').getByText('右键目录可添加',{exact:true}).waitFor();
    await page.locator('.file-row').filter({hasText:'知否 00003.txt'}).click({button:'right'});
    await page.getByRole('menuitem',{name:'详细信息',exact:true}).click();
    await page.getByRole('dialog',{name:'文件属性'}).getByText('3.25 GB',{exact:true}).waitFor();
    assert.equal(await page.getByText('大小（字节）',{exact:true}).count(),0);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.locator('.file-row').filter({hasText:'知否 00003.txt'}).click({button:'right'});
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('menu').count(),0);
    await page.getByLabel('文件列表',{exact:true}).focus();
    await page.keyboard.press('F2');
    await page.getByRole('dialog',{name:'重命名'}).waitFor();
    await page.getByLabel('新名称').fill('编辑输入不剪切云端文件');
    await page.getByLabel('新名称').press('Control+x');
    await page.getByLabel('新名称').fill('输入框复制');await page.getByLabel('新名称').press('Control+c');
    assert.equal(await page.evaluate(() => window.testCalls.filter(row => row.method === 'file.copy').length),2);
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method==='file.cut').length),1);
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await page.getByLabel('文件列表',{exact:true}).focus();
    await page.keyboard.press('Delete');
    await page.getByRole('dialog',{name:'移入回收站'}).waitFor();
    await page.getByRole('button',{name:'取消',exact:true}).click();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method==='file.delete').length),0);
    await page.getByLabel('选择 知否 00003.txt',{exact:true}).uncheck();
    await page.getByLabel('选择 知否 00001.txt',{exact:true}).check();
    await page.getByLabel('选择 知否 00003.txt',{exact:true}).check();
    await page.locator('.file-row').filter({hasText:'知否 00001.txt'}).click({button:'right'});
    await page.getByRole('menuitem',{name:'下载（2）',exact:true}).waitFor();
    await page.getByRole('menuitem',{name:/^复制路径/}).click();
    await page.locator('.toast.success').getByText('已复制 2 项路径',{exact:true}).waitFor();
    const pathRequests=await page.evaluate(() => window.testCalls.filter(call => call.method === 'file.paths'));
    assert.equal(pathRequests.length,1);
    assert.deepEqual(pathRequests[0].params.entries.map(row => row.fid),['1','3']);
    assert.ok(await page.evaluate(() => window.testCalls.some(call => call.method === 'clipboard' && call.params.text === '/知否 00001.txt\n/知否 00003.txt')));
    await page.keyboard.press('Escape');
    await page.getByLabel('选择 知否 00003.txt',{exact:true}).uncheck();
    await page.getByLabel('选择 知否 00001.txt',{exact:true}).check();
    await page.getByRole('button',{name:'更多操作 知否 00001.txt',exact:true}).click();
    await page.getByRole('menuitem',{name:'分享',exact:true}).click();
    await page.getByLabel('分享有效期').selectOption('30');
    await page.getByLabel('新分享提取码').fill('a123');
    await page.getByRole('button',{name:'确认创建',exact:true}).click();
    await page.locator('.share-progress').getByText('正在等待服务器生成链接…',{exact:true}).waitFor();
    await page.evaluate(() => window.releaseShareCreate());
    await page.getByRole('button',{name:'复制分享文案',exact:true}).click();
    await page.locator('.toast.success').getByText('已复制分享文案',{exact:true}).waitFor();
    assert.equal(await page.locator('.toast').evaluate(element => getComputedStyle(element).animationName),'toast-enter');
    await page.getByRole('button',{name:'复制分享文案',exact:true}).click();
    await page.locator('.toast.success').getByText('已复制分享文案',{exact:true}).waitFor();
    await mkdir(path.resolve(__dirname, '../test-results'), {recursive:true});
    await page.waitForFunction(() => {const element=document.querySelector('.toast');return element && getComputedStyle(element).opacity === '1';});
    assert.ok(await page.locator('.toast').evaluate(element => {const box=element.getBoundingClientRect();return !!document.elementFromPoint(box.x+box.width/2,box.y+box.height/2)?.closest('.toast');}));
    await page.screenshot({path:path.resolve(__dirname,'../test-results/feedback.png')});
    await page.getByRole('button',{name:'关闭提示',exact:true}).click();
    assert.equal(await page.locator('.toast').count(),0);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const shareCreated = await page.evaluate(() => window.testCalls.find(call => call.method === 'share.create'));
    assert.equal(shareCreated.params.entry.fid,'1');assert.equal(shareCreated.params.expire_days,30);assert.equal(shareCreated.params.passcode,'a123');
    assert.ok(await page.evaluate(() => window.testCalls.some(call => call.method === 'clipboard' && call.params.text.includes('提取码：a123'))));
    await page.getByRole('button',{name:'我的分享',exact:true}).click();
    await page.getByText('剩余 720 小时',{exact:true}).waitFor();
    await page.evaluate(() => {window.testClockMs+=10*3600000;});
    await page.getByText('剩余 710 小时',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'share.list').length),1);
    await page.getByLabel('选择记录 知否 00001.txt',{exact:true}).check();
    await page.getByRole('button',{name:'取消选中分享',exact:true}).click();
    await page.evaluate(() => {window.testHoldRevoke=true;});
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.locator('.catalog-progress').getByText('正在批量取消 1 个分享…',{exact:true}).waitFor();
    assert.ok(await page.getByRole('button',{name:'处理中…',exact:true}).isDisabled());
    await page.evaluate(() => {window.testHoldRevoke=false;window.releaseRevoke();});
    await page.getByText('知否 00001.txt：已取消分享',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'share.list').length),1);
    assert.equal(await page.getByLabel('选择记录 知否 00001.txt',{exact:true}).count(),0);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.getByRole('button',{name:'回收站',exact:true}).click();
    await page.getByLabel('选择记录 测试删除.txt',{exact:true}).check();
    await page.getByRole('button',{name:'永久删除选中记录',exact:true}).click();
    assert.ok(await page.getByRole('button',{name:'确认',exact:true}).isDisabled());
    await page.getByLabel('我确认永久删除这些记录').check();
    await page.evaluate(() => {window.testHoldRecycleRemove=true;});
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.locator('.catalog-progress').getByText('已核验 1 条，正在批量永久删除…',{exact:true}).waitFor();
    assert.ok(await page.getByRole('button',{name:'处理中…',exact:true}).isDisabled());
    assert.ok(await page.getByLabel('我确认永久删除这些记录').isDisabled());
    assert.equal(await page.getByText('过期删除进度',{exact:true}).count(),0);
    await page.evaluate(() => {window.testHoldRecycleRemove=false;window.releaseRecycleRemove();});
    await page.getByText('测试删除.txt：已永久删除',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const removed = await page.evaluate(() => window.testCalls.find(call => call.method === 'recycle.remove'));
    assert.deepEqual(removed.params.entries.map(row => row.record_id),['trash-a']);
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'recycle.list').length),1,'verified permanent deletion must not rescan the whole recycle bin again');
    await page.getByLabel('选择记录 测试恢复.txt',{exact:true}).check();
    await page.getByRole('button',{name:'恢复选中记录',exact:true}).click();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('测试恢复.txt：已恢复到原目录',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'recycle.list').length),1);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.evaluate(() => {window.testSetRecycle([
      {record_id:'partial-done',fid:'partial-a',name:'已确认删除.txt',directory:false,size:4,parent:'0'},
      {record_id:'partial-unknown',fid:'partial-b',name:'未确认删除.txt',directory:false,size:4,parent:'0'},
      {record_id:'unselected',fid:'keep-c',name:'未选中记录.txt',directory:false,size:4,parent:'0'},
    ]);window.testRecycleUnknownIds=['partial-unknown'];});
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByLabel('选择记录 已确认删除.txt',{exact:true}).check();
    await page.getByLabel('选择记录 未确认删除.txt',{exact:true}).check();
    const partialReads=await page.evaluate(() => window.testCalls.filter(call => call.method === 'recycle.list').length);
    await page.getByRole('button',{name:'永久删除选中记录',exact:true}).click();
    await page.getByLabel('我确认永久删除这些记录').check();
    await page.getByRole('button',{name:'确认',exact:true}).click();
    await page.getByText('未确认删除.txt：已提交，未重发，请刷新核实',{exact:true}).waitFor();
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    assert.equal(await page.getByLabel('选择记录 已确认删除.txt',{exact:true}).count(),0);
    assert.ok(await page.getByLabel('选择记录 未确认删除.txt',{exact:true}).isDisabled());
    assert.ok(await page.getByLabel('选择记录 未选中记录.txt',{exact:true}).isDisabled());
    assert.ok(await page.getByRole('button',{name:'永久删除选中记录',exact:true}).isDisabled());
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'recycle.list').length),partialReads);
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    assert.ok(await page.getByLabel('选择记录 未确认删除.txt',{exact:true}).isEnabled());
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'recycle.remove').length),2);

    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.getByRole('button',{name:'设置',exact:true}).click();
    assert.equal(await page.locator('.brand-logo').evaluate(image => image.complete && image.naturalWidth > 0),true);
    const threadSlider=page.getByRole('slider',{name:'拖动调整线程数',exact:true});
    await threadSlider.focus();await threadSlider.press('End');
    assert.equal(await page.getByRole('spinbutton',{name:'线程数',exact:true}).inputValue(),'16');
    await threadSlider.press('Home');
    assert.equal(await page.getByRole('spinbutton',{name:'线程数',exact:true}).inputValue(),'1');
    await page.getByRole('spinbutton',{name:'线程数',exact:true}).fill('8');
    assert.equal(await threadSlider.inputValue(),'8');
    assert.equal(await page.getByRole('spinbutton',{name:'线程数',exact:true}).evaluate(element => getComputedStyle(element).outlineStyle),'solid');
    const sliderBounds=await threadSlider.boundingBox();
    await page.mouse.move(sliderBounds.x+7.5+(sliderBounds.width-15)*7/15,sliderBounds.y+sliderBounds.height/2);
    await page.mouse.down();
    await page.mouse.move(sliderBounds.x+sliderBounds.width*.85,sliderBounds.y+sliderBounds.height/2,{steps:5});
    assert.equal(await threadSlider.evaluate(element => getComputedStyle(element).outlineStyle),'none');
    await page.mouse.up();
    const dragged=await threadSlider.inputValue();
    assert.ok(Number(dragged) > 8 && Number(dragged) <= 16);
    assert.equal(await page.getByRole('spinbutton',{name:'线程数',exact:true}).inputValue(),dragged);
    await page.getByRole('spinbutton',{name:'线程数',exact:true}).fill('8');
    const rateSlider=page.getByRole('slider',{name:'拖动调整总限速',exact:true});
    await rateSlider.focus();await rateSlider.press('End');
    assert.equal(await rateSlider.evaluate(element => getComputedStyle(element).outlineStyle),'none');
    assert.equal(await page.getByRole('spinbutton',{name:'总限速',exact:true}).inputValue(),'100');
    await rateSlider.press('Home');
    assert.equal(await page.getByRole('spinbutton',{name:'总限速',exact:true}).inputValue(),'0');
    await page.getByRole('spinbutton',{name:'总限速',exact:true}).fill('10.5');
    assert.equal(await rateSlider.inputValue(),'10.5');
    await page.getByRole('spinbutton',{name:'总限速',exact:true}).fill('0.01');
    assert.equal(await rateSlider.inputValue(),'0.01');
    await page.getByRole('spinbutton',{name:'总限速',exact:true}).fill('150');
    assert.equal(await rateSlider.inputValue(),'100');
    assert.equal(await page.getByRole('spinbutton',{name:'总限速',exact:true}).inputValue(),'150');
    await page.getByRole('spinbutton',{name:'总限速',exact:true}).fill('0');
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    const sliderSaved=await page.evaluate(() => window.testCalls.filter(call => call.method === 'preferences.save').at(-1).params);
    assert.equal(sliderSaved.connections,8);assert.equal(sliderSaved.limit_rate,null);
    await page.getByLabel('关闭窗口行为',{exact:true}).selectOption('quit');
    await page.locator('.toast.success').getByText('后台运行设置已保存',{exact:true}).waitFor();
    await page.getByLabel('传输完成通知',{exact:true}).uncheck();
    await page.waitForFunction(() => window.testCalls.filter(call => call.method === 'desktop.set').at(-1).params.notifications === false);
    await page.evaluate(() => {window.testClientSettingsWarning=true;});
    await page.getByLabel('关闭窗口行为',{exact:true}).selectOption('tray');
    await page.locator('.toast.warning').getByText('后台运行设置已生效，但未能保存；本次窗口内有效',{exact:true}).waitFor();
    await page.evaluate(() => {window.testClientSettingsWarning=false;});
    await page.getByLabel('传输完成通知',{exact:true}).check();
    await page.getByRole('spinbutton',{name:'线程数',exact:true}).scrollIntoViewIfNeeded();
    await page.screenshot({path:path.resolve(__dirname,'../test-results/light-settings.png')});
    await page.getByRole('radio',{name:'暗色',exact:true}).check();
    await page.locator('.toast.success').getByText('外观已保存',{exact:true}).waitFor();
    assert.equal(await page.locator('html').getAttribute('data-theme'),'dark');
    assert.equal(await page.locator('html').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(24, 24, 24)');
    assert.equal(await page.locator('.titlebar').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(25, 28, 30)');
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.login-button')).backgroundColor === 'rgb(38, 38, 38)');
    assert.equal(await page.locator('aside').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(25, 28, 30)');
    assert.equal(await page.getByRole('button',{name:'保存设置',exact:true}).evaluate(element => getComputedStyle(element).color),'rgb(25, 25, 25)');
    await page.screenshot({path:path.resolve(__dirname,'../test-results/dark-settings.png')});
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.locator('.file-row').filter({hasText:'知否 00003.txt'}).click({button:'right'});
    assert.equal(await page.getByRole('menu',{name:'文件操作'}).evaluate(element => getComputedStyle(element).backgroundColor),'rgb(38, 38, 38)');
    await page.screenshot({path:path.resolve(__dirname,'../test-results/dark-files.png')});
    await page.getByRole('menuitem',{name:'详细信息',exact:true}).click();
    await page.getByText('3.25 GB',{exact:true}).waitFor();
    assert.equal(await page.getByRole('dialog',{name:'文件属性'}).evaluate(element => getComputedStyle(element).backgroundColor),'rgb(38, 38, 38)');
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.getByRole('button',{name:'设置',exact:true}).click();
    await page.emulateMedia({colorScheme:'light'});
    assert.equal(await page.locator('html').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(24, 24, 24)');
    await page.getByRole('radio',{name:'跟随系统',exact:true}).check();
    assert.equal(await page.locator('html').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(255, 255, 255)');
    await page.emulateMedia({colorScheme:'dark'});
    await page.waitForFunction(() => document.documentElement.dataset.colorScheme === 'dark');
    assert.equal(await page.locator('html').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(24, 24, 24)');
    await page.getByRole('radio',{name:'浅色',exact:true}).check();
    assert.equal(await page.locator('html').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(255, 255, 255)');
    await page.setViewportSize({width:860,height:600});
    assert.ok(await page.locator('.theme-options').evaluate(element => element.getBoundingClientRect().right <= innerWidth));
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    await page.getByRole('button',{name:'保存设置',exact:true}).scrollIntoViewIfNeeded();
    assert.ok(await page.getByRole('button',{name:'保存设置',exact:true}).isVisible());
    await page.setViewportSize({width:1180,height:780});
    await page.evaluate(() => {window.testAppearanceWarning=true;});
    await page.getByRole('radio',{name:'跟随系统',exact:true}).check();
    await page.locator('.toast.warning').getByText('外观已切换，但未能保存；本次窗口内有效',{exact:true}).waitFor();
    await page.evaluate(() => {window.testAppearanceWarning=false;});
    await page.emulateMedia({colorScheme:'light'});
    await page.waitForFunction(() => document.documentElement.dataset.colorScheme === 'light');
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    await page.locator('.toast.success').getByText('设置已保存',{exact:true}).waitFor();
    await page.evaluate(() => {window.testSaveWarning=true;});
    await page.getByRole('button',{name:'保存设置',exact:true}).click();
    await page.locator('.toast.warning').getByText('设置仅在窗口内有效',{exact:true}).waitFor();
    await page.evaluate(() => {window.testSaveWarning=false;});
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.getByLabel('搜索关键词').fill('知否');
    await page.getByRole('button', { name:'搜索', exact:true }).click();
    await page.getByRole('button', { name:'取消搜索' }).waitFor();
    assert.ok(await page.getByLabel('搜索关键词').isEnabled());
    const beforeSearchSort=await page.evaluate(() => window.testCalls.length);
    await timeSort.click();
    assert.equal(await page.locator('.file-row').first().locator('.file-name').textContent(),'知否 00001.txt');
    assert.equal(await page.locator('.file-row').first().locator('.modified-time').textContent(),'2026-10-14 12:34');
    await page.locator('.file-header').getByRole('button',{name:'大小',exact:true}).click();
    assert.equal(await page.evaluate(() => window.testCalls.length),beforeSearchSort);
    await page.getByRole('button', { name:'取消搜索' }).click();
    await page.waitForTimeout(600);
    await page.getByText('显示 100 项', { exact:true }).waitFor();
    await page.getByLabel('选择 知否 00000.txt', { exact:true }).check();
    await page.getByRole('button', { name:'下载（1）', exact:true }).click();
    await page.getByRole('heading', { name:'传输任务' }).waitFor();
    const downloaded = await page.evaluate(() => window.testCalls.find(call => call.method === 'download').params.entries);
    assert.equal(downloaded.length, 1); assert.equal(downloaded[0].fid, '0');
    await page.getByRole('button',{name:'设置',exact:true}).click();
    assert.ok(await page.getByRole('button',{name:'保存设置',exact:true}).isDisabled());
    assert.ok(await page.getByRole('slider',{name:'拖动调整线程数',exact:true}).isDisabled());
    assert.ok(await page.getByRole('slider',{name:'拖动调整总限速',exact:true}).isDisabled());
    assert.ok(await page.getByRole('radio',{name:'暗色',exact:true}).isEnabled());
    await page.getByRole('radio',{name:'暗色',exact:true}).check();
    await page.getByRole('button',{name:/^传输任务/}).click();
    assert.ok(await page.getByText('正在传输',{exact:true}).isVisible());
    await page.screenshot({path:path.resolve(__dirname,'../test-results/dark-transfers.png')});
    await page.getByRole('button',{name:'设置',exact:true}).click();
    await page.getByRole('radio',{name:'浅色',exact:true}).check();
    await page.getByRole('button', {name:'我的文件',exact:true}).click();
    await page.getByRole('button',{name:'上传',exact:true}).click();
    await page.getByRole('button', {name:'上传文件',exact:true}).click();
    await page.locator('.toast.success').getByText('已加入 1 个上传任务',{exact:true}).waitFor();
    assert.ok(await page.getByRole('heading',{name:'我的文件',exact:true}).isVisible());
    await page.getByRole('button',{name:/^传输任务/}).click();
    await page.getByRole('heading', {name:'传输任务'}).waitFor();
    await page.getByText('测试.txt',{exact:true}).waitFor();
    const uploaded = await page.evaluate(() => window.testCalls.find(call => call.method === 'upload'));
    assert.equal(uploaded.params.parent,'0'); assert.deepEqual(uploaded.params.sources,['C:\\Uploads\\测试.txt']);
    await page.locator('.task-row').filter({hasText:'测试.txt'}).getByRole('button',{name:'暂停',exact:true}).click();
    await page.getByText('已暂停',{exact:true}).waitFor();
    await page.getByRole('button',{name:'暂停全部',exact:true}).click();
    await page.getByRole('group',{name:'传输方向'}).getByRole('button',{name:/^下载/}).click();
    await page.getByText('已暂停',{exact:true}).waitFor();
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    const uploadsBefore=await page.evaluate(() => window.testCalls.filter(call => call.method === 'upload').length);
    const readsBefore=await page.evaluate(() => window.testDropReads || 0);
    await drop(['搜索视图.txt']);
    await page.locator('.toast.warning').getByText('请先返回网盘目录，再拖入文件上传',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testDropReads || 0),readsBefore);
    await page.getByRole('button',{name:'返回目录',exact:true}).click();
    const hover=await dragData(['拖拽.txt','资料目录']);
    const beforeDrag=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    await page.locator('.file-list').dispatchEvent('dragenter',{dataTransfer:hover});
    await page.getByText('松开鼠标，准备上传',{exact:true}).waitFor();
    const duringDrag=await page.locator('.file-scroll').evaluate(element => ({height:element.scrollHeight,rowY:element.querySelector('.file-row').getBoundingClientRect().y}));
    assert.deepEqual(duringDrag,beforeDrag);
    await page.locator('.file-row').first().dispatchEvent('dragenter',{dataTransfer:hover});
    await page.locator('.file-row').first().dispatchEvent('dragleave',{dataTransfer:hover});
    assert.ok(await page.locator('.drop-overlay').isVisible());
    await page.screenshot({path:path.resolve(__dirname,'../test-results/drop-overlay.png')});
    await page.locator('.file-list').dispatchEvent('dragleave',{dataTransfer:hover});
    assert.equal(await page.locator('.drop-overlay').count(),0);await hover.dispose();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'upload').length),uploadsBefore);
    await drop(['拖拽.txt','资料目录']);
    const dropDialog=page.getByRole('dialog',{name:'确认拖拽上传',exact:true});
    await dropDialog.getByText('目标：我的网盘',{exact:true}).waitFor();
    await dropDialog.getByText('共 2 项 · 1 个目录',{exact:true}).waitFor();
    assert.equal(await page.locator('.app').evaluate(element => element.inert),true);
    await page.screenshot({path:path.resolve(__dirname,'../test-results/drop-confirmation.png')});
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'upload').length),uploadsBefore);
    await dropDialog.getByRole('button',{name:'取消',exact:true}).click();
    await page.evaluate(() => {window.testHoldDrop=true;});
    await drop(['迟到.txt']);await dropDialog.getByText('正在读取拖入项目…',{exact:true}).waitFor();
    await dropDialog.getByRole('button',{name:'取消',exact:true}).click();
    await page.evaluate(() => {window.testHoldDrop=false;window.releaseDrop();});
    await page.waitForTimeout(20);assert.equal(await dropDialog.count(),0);
    await page.evaluate(() => {window.testDropFailure=true;});
    await drop(['失效.txt']);await page.locator('.toast.error').getByText('拖入项目无法访问',{exact:true}).waitFor();
    assert.equal(await dropDialog.count(),0);await page.evaluate(() => {window.testDropFailure=false;});
    await drop(['拖拽.txt','资料目录']);
    await page.evaluate(() => {window.testHoldUpload=true;});
    await dropDialog.getByRole('button',{name:'确认上传',exact:true}).click();
    assert.ok(await dropDialog.getByRole('button',{name:'正在加入队列…',exact:true}).isDisabled());
    assert.ok(await dropDialog.getByRole('button',{name:'取消',exact:true}).isDisabled());
    await page.evaluate(() => window.testEmit({id:null,event:'navigate',data:'tasks'}));
    assert.ok(await dropDialog.isVisible());
    await page.evaluate(() => {window.testHoldUpload=false;window.releaseUpload();});
    await page.getByText('拖拽.txt',{exact:true}).waitFor();
    assert.equal(await dropDialog.count(),0);
    const mixed=await page.evaluate(() => window.testCalls.filter(call => call.method === 'upload').at(-1).params);
    assert.deepEqual(mixed,{sources:['C:\\Dropped\\拖拽.txt','C:\\Dropped\\资料目录'],parent:'0'});
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'upload').length),uploadsBefore+1);
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    const savesBeforeAppend=await page.evaluate(() => window.testCalls.filter(call => call.method === 'preferences.save').length);
    await drop(['追加.txt']);await dropDialog.getByRole('button',{name:'确认上传',exact:true}).click();
    await dropDialog.waitFor({state:'detached'});
    assert.ok(await page.getByRole('heading',{name:'我的文件',exact:true}).isVisible());
    await page.getByRole('button',{name:/^传输任务/}).click();
    await page.getByText('追加.txt',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'preferences.save').length),savesBeforeAppend);
    await page.getByRole('button',{name:'暂停全部',exact:true}).click();
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.locator('.file-row').getByRole('button',{name:'新建测试',exact:true}).click();
    await drop(['子目录上传.txt']);await dropDialog.getByText('目标：我的网盘 / 新建测试',{exact:true}).waitFor();
    await dropDialog.getByRole('button',{name:'确认上传',exact:true}).click();
    await dropDialog.waitFor({state:'detached'});
    assert.ok(await page.getByRole('heading',{name:'我的文件',exact:true}).isVisible());
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'新建测试',exact:true}).waitFor();
    await page.getByRole('button',{name:/^传输任务/}).click();
    await page.getByText('子目录上传.txt',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.filter(call => call.method === 'upload').at(-1).params.parent),'new');
    await page.getByRole('button',{name:'暂停全部',exact:true}).click();
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'我的网盘',exact:true}).click();
    await drop(Array.from({length:61},(_,index) => `批量${index}.txt`));
    await dropDialog.getByText('另有 11 项，确认后全部加入队列。',{exact:true}).waitFor();
    assert.equal(await dropDialog.locator('li').count(),50);
    await page.keyboard.press('Escape');assert.equal(await dropDialog.count(),0);
    await page.getByRole('button', { name:'分享链接', exact:true }).click();
    await page.getByLabel('分享链接或文案').fill('https://pan.quark.cn/s/abcdef 提取码 1234');
    await page.getByLabel('提取码', { exact:true }).click();
    await page.getByLabel('提取码', { exact:true }).fill('5678');
    await page.getByRole('button', { name:'打开分享', exact:true }).click();
    const opened = await page.evaluate(() => window.testCalls.find(call => call.method === 'share.open'));
    assert.equal(opened.params.passcode, '5678');
    const beforeShareSort=await page.evaluate(() => window.testCalls.length);
    await timeSort.click();
    assert.equal(await page.locator('.file-row').first().locator('.file-name').textContent(),'知否 00001.txt');
    assert.equal(await page.locator('.file-row').last().locator('.modified-time').textContent(),'未知');
    await page.locator('.file-header').getByRole('button',{name:'大小',exact:true}).click();
    assert.equal(await page.evaluate(() => window.testCalls.length),beforeShareSort);
    const beforeShareDrop=await page.evaluate(() => window.testDropReads || 0);
    await drop(['分享不能上传.txt']);
    await page.locator('.toast.warning').getByText('分享文件为只读，请进入我的文件上传',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testDropReads || 0),beforeShareDrop);
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
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.getByRole('button',{name:'筛选',exact:true}).click();
    await page.screenshot({path:path.resolve(__dirname,'../test-results/compact-toolbar.png')});
    await page.screenshot({path:path.resolve(__dirname,'../test-results/light-files.png')});
    const originalTasks=await page.evaluate(() => window.testGetTasks());
    await page.evaluate(() => window.testSetTasks([
      {id:'df',name:'下载失败.txt',direction:'download',state:'failed'},
      {id:'ds',name:'下载暂停.txt',direction:'download',state:'stopped'},
      {id:'dd',name:'下载成功.txt',direction:'download',state:'done',created_at:Date.UTC(2025,11,31,15,50),started_at:Date.UTC(2025,11,31,15,58),finished_at:Date.UTC(2025,11,31,16,2),elapsed_seconds:121},
      {id:'dc',name:'下载取消.txt',direction:'download',state:'cancelled'},
      {id:'legacy',name:'旧账号下载.txt',state:'failed',valid:false},
      {id:'uw',name:'上传等待.txt',direction:'upload',state:'waiting'},
      {id:'uf',name:'上传失败.txt',direction:'upload',state:'failed'},
      {id:'us',name:'上传暂停.txt',direction:'upload',state:'stopped'},
      {id:'ud',name:'上传成功.txt',direction:'upload',state:'done',target:'/资料/上传成功.txt',created_at:Date.UTC(2026,9,16,6,10),started_at:Date.UTC(2026,9,16,6,12),finished_at:Date.UTC(2026,9,16,7,42),elapsed_seconds:3661},
      {id:'uc',name:'上传取消.txt',direction:'upload',state:'cancelled'},
    ].map(task => ({valid:true,revision:1,detail:'',target:'C:/Downloads/'+task.name,...task}))));
    await page.getByRole('button',{name:/^传输任务/}).click();
    const directions=page.getByRole('group',{name:'传输方向'}),completion=page.getByRole('group',{name:'传输完成状态'});
    assert.equal(await directions.getByRole('button',{name:/^上传/}).getAttribute('aria-pressed'),'true');
    assert.equal(await directions.getByRole('button',{name:/^下载/}).locator('span').textContent(),'5');
    assert.equal(await completion.getByRole('button',{name:/^未完成/}).locator('span').textContent(),'4');
    assert.equal(await page.locator('.task-row').count(),4);
    assert.equal(await page.getByText('下载失败.txt',{exact:true}).count(),0);
    await directions.getByRole('button',{name:/^下载/}).click();
    assert.equal(await page.getByText('下载成功.txt',{exact:true}).count(),0);
    assert.equal(await page.locator('.task-row').first().locator('.task-controls button').count(),3);
    assert.ok(await page.locator('.task-row').filter({hasText:'旧账号下载.txt'}).getByRole('button',{name:'开始',exact:true}).isDisabled());
    await page.locator('.task-row').filter({hasText:'旧账号下载.txt'}).getByText('失败时间：未记录',{exact:true}).waitFor();
    await page.getByRole('button',{name:'开始全部',exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls.filter(row => row.method === 'tasks.retry').at(-1).params.ids),['df','ds','uw','uf','us']);
    await page.getByRole('button',{name:'暂停全部',exact:true}).click();
    await directions.getByRole('button',{name:/^上传/}).click();
    await page.locator('.task-row').filter({hasText:'上传取消.txt'}).getByRole('button',{name:'删除记录',exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls.filter(row => row.method === 'tasks.remove').at(-1).params.ids),['uc']);
    await page.evaluate(() => window.testSetTasks(window.testGetTasks().map(task => ['dd','ud','uw'].includes(task.id) ? {...task,state:'done',revision:task.revision+1} : task)));
    assert.equal(await completion.getByRole('button',{name:/^未完成/}).locator('span').textContent(),'2');
    assert.equal(await completion.getByRole('button',{name:/^已完成/}).locator('span').textContent(),'2');
    assert.equal(await page.getByText('上传等待.txt',{exact:true}).count(),0);
    await completion.getByRole('button',{name:/^已完成/}).click();
    assert.equal(await page.locator('.task-row').count(),2);
    const uploadedRecord=page.locator('.task-row').filter({hasText:'上传成功.txt'});
    await uploadedRecord.getByText('完成时间：2026-10-16 15:42',{exact:true}).waitFor();
    await uploadedRecord.getByText('累计耗时：1 小时 1 分 1 秒',{exact:true}).waitFor();
    await uploadedRecord.getByText('上传到：/资料/上传成功.txt',{exact:true}).waitFor();
    assert.match(await uploadedRecord.locator('.task-record-line').getAttribute('title'),/加入：2026-10-16 14:10\n首次开始：2026-10-16 14:12/);
    await page.locator('.task-row').filter({hasText:'上传成功.txt'}).getByRole('button',{name:'打开文件夹',exact:true}).click();
    await page.screenshot({path:path.resolve(__dirname,'../test-results/transfer-groups-light.png')});
    await page.evaluate(() => window.quark.setAppearance('dark'));
    await page.screenshot({path:path.resolve(__dirname,'../test-results/transfer-groups-dark.png')});
    await page.setViewportSize({width:860,height:600});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    assert.equal(await uploadedRecord.evaluate(row => row.querySelector('.task-record-target').getBoundingClientRect().bottom <= row.getBoundingClientRect().bottom),true);
    await page.setViewportSize({width:1180,height:780});
    await directions.getByRole('button',{name:/^下载/}).click();
    await page.getByText('下载成功.txt',{exact:true}).waitFor();
    await page.locator('.task-row').filter({hasText:'下载成功.txt'}).getByText('完成时间：2026-01-01 00:02',{exact:true}).waitFor();
    await page.getByRole('button',{name:'清理已完成记录',exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls.filter(row => row.method === 'tasks.clear').at(-1).params.ids),['dd']);
    await page.getByText('暂无已完成的下载任务',{exact:true}).waitFor();
    assert.ok(await page.getByRole('button',{name:'清理已完成记录',exact:true}).isDisabled());
    await directions.getByRole('button',{name:/^上传/}).click();
    await page.getByText('上传成功.txt',{exact:true}).waitFor();
    assert.equal(await page.locator('.task-row').count(),2);
    await page.getByRole('button',{name:'清理已完成记录',exact:true}).click();
    assert.deepEqual(await page.evaluate(() => window.testCalls.filter(row => row.method === 'tasks.clear').at(-1).params.ids),['uw','ud']);
    await page.getByText('暂无已完成的上传任务',{exact:true}).waitFor();
    await directions.getByRole('button',{name:/^下载/}).click();
    await page.getByText('暂无已完成的下载任务',{exact:true}).waitFor();
    assert.ok(await page.getByRole('button',{name:'清理已完成记录',exact:true}).isDisabled());
    await completion.getByRole('button',{name:/^未完成/}).click();
    await page.getByText('下载取消.txt',{exact:true}).waitFor();
    assert.ok(await page.locator('.task-row').filter({hasText:'旧账号下载.txt'}).getByRole('button',{name:'开始',exact:true}).isDisabled());
    await page.evaluate(() => window.testSetTasks(Array.from({length:1000},(_,index) => ({id:'large-'+index,name:'批量完成'+index,direction:'upload',state:'done',valid:true,revision:1,detail:'',target:'C:/Downloads/test'}))));
    await directions.getByRole('button',{name:/^上传/}).click();await completion.getByRole('button',{name:/^已完成/}).click();
    assert.equal(await completion.getByRole('button',{name:/^已完成/}).locator('span').textContent(),'1000');
    assert.ok(await page.locator('.task-row').count() < 60);
    const beforeProgressCalls=await page.evaluate(() => window.testCalls.length);
    await page.evaluate(() => {
      const MB=1048576;
      const progress=(name,transferred,confirmed,total,rate,connections=4,phase='transferring',recursive=false) => ({name,transferred,confirmed,total,rate,connections,phase,recursive});
      window.testSetTasks([
        {id:'pd',name:'大文件.bin',direction:'download',state:'running',progress:progress('大文件.bin',50*MB,40*MB,100*MB,10*MB)},
        {id:'pt',name:'测试目录',direction:'download',state:'running',progress:progress('子目录/视频.mkv',80*MB,70*MB,200*MB,10*MB,8,'transferring',true)},
        {id:'pu',name:'未知大小.bin',direction:'download',state:'running',progress:progress('未知大小.bin',5*MB,0,null,MB,1)},
        {id:'ps',name:'转存文件.bin',direction:'download',state:'running',progress:progress('转存文件.bin',0,0,null,0,1,'saving')},
        {id:'pc',name:'清理文件.bin',direction:'download',state:'running',progress:progress('清理文件.bin',0,0,null,0,1,'cleaning')},
        {id:'pl',name:'旧任务',direction:'download',state:'stopped',detail:'旧任务结果'},
        {id:'pa',name:'上传文件.bin',direction:'upload',state:'running',progress:progress('上传文件.bin',20*MB,10*MB,40*MB,5*MB,1)},
        {id:'ph',name:'准备上传.bin',direction:'upload',state:'running',progress:progress('准备上传.bin',0,0,100*MB,0,0,'hashing')},
        {id:'px',name:'授权下一段.bin',direction:'upload',state:'running',progress:progress('授权下一段.bin',10*MB,10*MB,40*MB,5*MB,1,'authorizing')},
        {id:'pe',name:'空文件.bin',direction:'upload',state:'done',progress:progress('空文件.bin',0,0,0,0,0,'completed')},
      ].map(task => ({valid:true,revision:1,detail:'',target:'C:/Downloads/'+task.name,...task})));
    });
    await directions.getByRole('button',{name:/^下载/}).click();await completion.getByRole('button',{name:/^未完成/}).click();
    const progressRow=name => page.locator('.task-row').filter({hasText:name});
    const activeProgress=progressRow('大文件.bin');
    assert.equal(await activeProgress.getByRole('progressbar').getAttribute('aria-valuenow'),'50');
    await activeProgress.getByText('已传输 50.0 MB / 100.0 MB',{exact:true}).waitFor();
    await activeProgress.getByText('速度 10.0 MB/s',{exact:true}).waitFor();
    await activeProgress.getByText('连接 4',{exact:true}).waitFor();
    await activeProgress.getByText('预计剩余 约 5 秒',{exact:true}).waitFor();
    const treeProgress=progressRow('测试目录');
    await treeProgress.getByText('目录总进度 · 当前文件：子目录/视频.mkv',{exact:true}).waitFor();
    assert.equal(await treeProgress.getByRole('progressbar').getAttribute('aria-valuenow'),'40');
    assert.equal(await progressRow('未知大小.bin').getByRole('progressbar').getAttribute('aria-valuenow'),null);
    await progressRow('未知大小.bin').getByText('预计剩余 —',{exact:true}).waitFor();
    await progressRow('转存文件.bin').getByText('正在临时转存',{exact:true}).waitFor();
    assert.equal(await progressRow('转存文件.bin').getByText('连接 1',{exact:true}).count(),0);
    await progressRow('清理文件.bin').getByText('正在清理临时目录',{exact:true}).waitFor();
    assert.equal(await progressRow('旧任务').getByRole('progressbar').count(),0);
    await page.screenshot({path:path.resolve(__dirname,'../test-results/transfer-progress-dark.png')});
    await page.evaluate(() => window.quark.setAppearance('light'));
    await page.screenshot({path:path.resolve(__dirname,'../test-results/transfer-progress-light.png')});
    await page.setViewportSize({width:860,height:600});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    const phaseFits=await activeProgress.evaluate(row => row.querySelector('.progress-phase').getBoundingClientRect().bottom <= row.getBoundingClientRect().bottom);
    assert.equal(phaseFits,true);
    const recordBounds=await activeProgress.evaluate(row => ({row:row.getBoundingClientRect().toJSON(),target:row.querySelector('.task-record-target').getBoundingClientRect().toJSON()}));
    await page.screenshot({path:path.resolve(__dirname,'../test-results/transfer-progress-narrow.png')});
    assert.ok(recordBounds.target.bottom<=recordBounds.row.bottom,JSON.stringify(recordBounds));
    await page.setViewportSize({width:1180,height:780});
    await page.evaluate(() => {const task=window.testGetTasks().find(task => task.id === 'pd');window.testEmit({id:null,event:'task',data:{...task,state:'stopped',revision:2}});});
    await activeProgress.locator('.task-state').getByText('已暂停',{exact:true}).waitFor();
    assert.equal(await activeProgress.getByRole('progressbar').getAttribute('aria-valuenow'),'40');
    await activeProgress.getByText('已确认 40.0 MB / 100.0 MB',{exact:true}).waitFor();
    await activeProgress.getByText('速度 —',{exact:true}).waitFor();await activeProgress.getByText('预计剩余 —',{exact:true}).waitFor();
    await page.evaluate(() => window.testEmit({id:null,event:'task',data:window.testGetTasks().find(task => task.id === 'pd')}));
    assert.equal(await activeProgress.getByRole('progressbar').getAttribute('aria-valuenow'),'40');
    await directions.getByRole('button',{name:/^上传/}).click();
    assert.equal(await progressRow('准备上传.bin').getByRole('progressbar').getAttribute('aria-valuenow'),null);
    assert.equal(await progressRow('准备上传.bin').getByRole('progressbar').getAttribute('aria-valuetext'),'正在计算文件哈希');
    assert.ok(await progressRow('准备上传.bin').getByRole('button',{name:'暂停',exact:true}).isEnabled());
    assert.equal(await progressRow('授权下一段.bin').getByRole('progressbar').getAttribute('aria-valuenow'),'25');
    await progressRow('授权下一段.bin').getByText('速度 5.0 MB/s',{exact:true}).waitFor();
    await progressRow('授权下一段.bin').locator('.progress-phase').getByText('正在上传',{exact:true}).waitFor();
    await progressRow('准备上传.bin').getByText('速度 —',{exact:true}).waitFor();
    await page.screenshot({path:path.resolve(__dirname,'../test-results/upload-progress-light.png')});
    for(const [index,phase] of ['hashing_context','transferring','part_confirmed','authorizing'].entries()) {
      await page.evaluate(({phase,index}) => {const task=window.testGetTasks().find(task => task.id === 'px');window.testEmit({id:null,event:'task',data:{...task,revision:index+2,progress:{...task.progress,phase}}});},{phase,index});
      await progressRow('授权下一段.bin').locator('.progress-phase').getByText('正在上传',{exact:true}).waitFor();
      await progressRow('授权下一段.bin').getByText('速度 5.0 MB/s',{exact:true}).waitFor();
      await progressRow('授权下一段.bin').getByText('预计剩余 约 6 秒',{exact:true}).waitFor();
    }
    for(const [index,phase] of ['verifying','merging','completed'].entries()) {
      await page.evaluate(({phase,index}) => {const task=window.testGetTasks().find(task => task.id === 'px');window.testEmit({id:null,event:'task',data:{...task,revision:index+6,progress:{...task.progress,phase,transferred:task.progress.total,confirmed:task.progress.total}}});},{phase,index});
      await progressRow('授权下一段.bin').locator('.progress-phase').getByText('正在完成上传…',{exact:true}).waitFor();
      await progressRow('授权下一段.bin').getByText('速度 5.0 MB/s',{exact:true}).waitFor();
      await progressRow('授权下一段.bin').getByText('预计剩余 —',{exact:true}).waitFor();
    }
    for(const [index,phase] of ['verifying','merging'].entries()) {
      await page.evaluate(({phase,index}) => {const task=window.testGetTasks().find(task => task.id === 'px');window.testEmit({id:null,event:'task',data:{...task,revision:index+9,progress:{...task.progress,phase,recursive:true}}});},{phase,index});
      await progressRow('授权下一段.bin').locator('.progress-phase').getByText('正在上传',{exact:true}).waitFor();
      await progressRow('授权下一段.bin').getByText('速度 5.0 MB/s',{exact:true}).waitFor();
    }
    await page.evaluate(() => {const task=window.testGetTasks().find(task => task.id === 'px');window.testEmit({id:null,event:'task',data:{...task,revision:11,pausing:true}});});
    await progressRow('授权下一段.bin').locator('.progress-phase').getByText('正在安全停止…',{exact:true}).waitFor();
    await progressRow('授权下一段.bin').getByText('速度 —',{exact:true}).waitFor();
    await progressRow('授权下一段.bin').getByText('预计剩余 —',{exact:true}).waitFor();
    await progressRow('上传文件.bin').getByText('速度 5.0 MB/s',{exact:true}).waitFor();
    await progressRow('上传文件.bin').getByText('预计剩余 约 4 秒',{exact:true}).waitFor();
    assert.equal(await progressRow('上传文件.bin').getByText('连接 1',{exact:true}).count(),0);
    await completion.getByRole('button',{name:/^已完成/}).click();
    assert.equal(await progressRow('空文件.bin').getByRole('progressbar').getAttribute('aria-valuenow'),'100');
    await progressRow('空文件.bin').getByText('已传输 0.000 KB / 0.000 KB',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testCalls.length),beforeProgressCalls);
    await page.evaluate(tasks => {window.testSetTasks(tasks);return window.quark.setAppearance('light');},originalTasks);
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    await page.getByRole('button',{name:'账号操作',exact:true}).click();
    await page.getByRole('menuitem',{name:'切换账号',exact:true}).click();
    await page.getByRole('dialog',{name:'切换账号',exact:true}).waitFor();
    for(const name of ['扫码登录','手机验证码登录','微信登录']) assert.ok(await page.getByRole('button',{name,exact:true}).isEnabled());
    await page.screenshot({path:path.resolve(__dirname,'../test-results/account-login-picker.png')});
    await page.getByRole('button',{name:'手机验证码登录',exact:true}).click();
    await page.locator('.account-name[title="测试账号"]').waitFor();
    await page.locator('.toast').getByText('登录已取消，原账号保持不变',{exact:true}).waitFor();
    await page.evaluate(() => {window.testWebCancel=false;});
    await page.evaluate(() => {window.testHoldCapacity=true;window.testCapacity={used_bytes:99,total_bytes:100};});
    await capacity.getByRole('button',{name:'刷新网盘空间'}).click();await page.waitForFunction(() => window.capacityReleases.length===1);
    await page.evaluate(() => {window.testCapacity={used_bytes:20,total_bytes:100};});
    await page.getByRole('button',{name:'切换账号',exact:true}).click();
    await page.getByRole('button',{name:'微信登录',exact:true}).click();
    await page.locator('.account-name[title="新账号"]').waitFor();
    await page.waitForFunction(() => window.capacityReleases.length===2);
    await page.evaluate(() => {window.testHoldCapacity=false;window.capacityReleases.shift()();});
    assert.equal(await capacity.getByRole('progressbar').count(),0,'old account response must not populate the new account');
    await page.evaluate(() => window.capacityReleases.shift()());await capacity.getByText('20.0%',{exact:true}).waitFor();
    assert.deepEqual(await page.evaluate(() => window.testCalls.filter(row => row.method==='officialLogin').map(row => row.params.mode)),['mobile','wechat']);
    await page.getByRole('button',{name:'账号操作',exact:true}).click();
    await page.getByRole('menuitem',{name:'退出账号',exact:true}).click();
    await page.getByText('登录后浏览和管理网盘文件',{exact:true}).waitFor();
    await capacity.getByText('登录后查看用量',{exact:true}).waitFor();
    assert.equal(await capacity.getByRole('progressbar').count(),0);
    assert.equal(await page.locator('.file-row').count(),0);
    assert.ok(await page.getByRole('button',{name:'新建目录',exact:true}).isDisabled());
    const beforeSignedOutDrop=await page.evaluate(() => window.testDropReads || 0);
    await drop(['未登录不能上传.txt']);await page.locator('.toast.warning').getByText('请先登录后上传',{exact:true}).waitFor();
    assert.equal(await page.evaluate(() => window.testDropReads || 0),beforeSignedOutDrop);
    await page.getByRole('button',{name:'登录账号',exact:true}).first().click();
    await page.getByRole('button',{name:'扫码登录',exact:true}).click();
    await page.locator('.toast.error').getByText('登录服务暂时不可用',{exact:true}).waitFor();
    await page.locator('.account-name[title="未登录"]').waitFor();
    assert.deepEqual(errors, []);
    await page.getByRole('button',{name:'设置',exact:true}).click();
    await page.getByRole('button',{name:'退出客户端',exact:true}).click();
    await page.getByRole('alertdialog',{name:'正在安全退出',exact:true}).waitFor();
    assert.equal(await page.locator('.app').evaluate(element => element.inert),true);
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
});

test('packaged Electron starts its bundled backend with sandbox and no external Python', async () => {
  const directory = await mkdtemp(path.join(tmpdir(), 'quark-electron-test-'));
  const executable = path.resolve(__dirname, '..', process.env.QUARK_DESKTOP_OUT || 'out', 'Quark Drive Client-win32-x64/QuarkDriveClient.exe');
  const launchOptions={ executablePath: executable, args:['--user-data-dir=' + path.join(directory,'electron')], env:{ ...process.env,
    QUARK_CONFIG_DIR: directory, QUARK_COOKIE:'', QUARK_COOKIE_FILE:'', QUARK_DESKTOP_PYTHON:'Z:\\no-python.exe' } };
  let app=await _electron.launch(launchOptions);
  try {
    const page = await app.firstWindow();
    await page.getByRole('heading', { name:'我的文件' }).waitFor();
    const hello = await page.evaluate(() => window.quark.call('hello'));
    assert.equal(hello.client_version,await app.evaluate(({app}) => app.getVersion()));
    assert.deepEqual(await page.evaluate(() => window.quark.call('tasks.pause',{ids:[]})),[]);
    assert.deepEqual(await page.evaluate(() => window.quark.call('tasks.remove',{ids:[]})),[]);
    await assert.rejects(page.evaluate(() => window.quark.call('text.read',{entry:{fid:'text',name:'test.txt',directory:false,size:4,path:'',location:[],modified_at:null}})),/QUARK_COOKIE|Cookie|登录/);
    await assert.rejects(page.evaluate(() => window.quark.call('text.open',{entry:{fid:'text',name:'test.txt',directory:false,size:4,path:'',location:[],modified_at:null}})),/QUARK_COOKIE|Cookie|登录/);
    await assert.rejects(page.evaluate(() => window.quark.call('account.capacity')),/QUARK_COOKIE|Cookie|登录/);
    await assert.rejects(page.evaluate(() => window.quark.call('text.save',{editor:'test',version:'v1',text:'hello'})),/QUARK_COOKIE|Cookie|登录/);
    await assert.rejects(page.evaluate(() => window.quark.setEditorDirty('invalid')),/编辑状态/);
    await page.evaluate(() => window.quark.setEditorDirty(false));
    await page.locator('.titlebar').getByText('v'+hello.client_version,{exact:true}).waitFor();
    await assert.rejects(page.evaluate(() => window.quark.readDroppedFiles([new File(['payload'],'网页生成.txt')])),/本地文件/);
    const localDropFile=path.join(directory,'本地拖拽.txt'),localDropFolder=path.join(directory,'本地目录');
    await writeFile(localDropFile,'payload','utf8');await mkdir(localDropFolder);await writeFile(path.join(localDropFolder,'内部文件.txt'),'nested','utf8');
    await page.evaluate(() => {
      const target=document.createElement('div');target.id='native-drop-test';target.textContent='本地拖拽路径验证';target.style.cssText='position:fixed;left:260px;top:260px;width:300px;height:160px;z-index:200;background:#eef3ff';
      for(const type of ['dragenter','dragover']) target.addEventListener(type,event => {event.preventDefault();event.dataTransfer.dropEffect='copy';});
      target.addEventListener('drop',event => {event.preventDefault();event.stopPropagation();window.realDropFiles=Array.from(event.dataTransfer.files);window.nativeDropResult=window.quark.readDroppedFiles(window.realDropFiles);});
      document.body.append(target);
    });
    const dragSession=await page.context().newCDPSession(page);
    for(const type of ['dragEnter','dragOver','drop']) await dragSession.send('Input.dispatchDragEvent',{type,x:350,y:330,data:{items:[],files:[localDropFile,localDropFolder],dragOperationsMask:1}});
    const nativeDropped=await page.evaluate(() => window.nativeDropResult);
    assert.deepEqual(nativeDropped,[{path:localDropFile,name:'本地拖拽.txt',directory:false},{path:localDropFolder,name:'本地目录',directory:true}]);
    const deduplicated=await page.evaluate(() => window.quark.readDroppedFiles([window.realDropFiles[0],window.realDropFiles[0]]));
    assert.equal(deduplicated.length,1);
    await rm(localDropFile);
    await assert.rejects(page.evaluate(() => window.quark.readDroppedFiles([window.realDropFiles[0]])),/无法访问/);
    await page.evaluate(() => document.getElementById('native-drop-test').remove());await dragSession.detach();
    assert.deepEqual((await page.evaluate(() => window.quark.getClientSettings())).settings,{closeBehavior:'tray',notifications:true});
    await assert.rejects(page.evaluate(() => window.quark.setClientSettings({closeBehavior:'unknown',notifications:true})),/设置无效/);
    await app.evaluate(({Tray}) => {
      const original=Tray.prototype.setContextMenu;
      Tray.prototype.setContextMenu=function(menu) {global.testTray=this;global.testTrayMenu=menu;return original.call(this,menu);};
    });
    await page.evaluate(() => window.quark.setClientSettings({closeBehavior:'tray',notifications:true}));
    assert.ok(await app.evaluate(() => global.testTray && !global.testTray.isDestroyed()));
    const packagedIcon=path.join(path.dirname(executable),'resources/app-icon.ico');
    const iconHeader=await readFile(packagedIcon);
    assert.equal(iconHeader.readUInt16LE(2),1);assert.ok(iconHeader.readUInt16LE(4) >= 6);
    assert.deepEqual(iconHeader,await readFile(path.resolve(__dirname,'../assets/app-icon.ico')));
    const iconCorners=await app.evaluate(({nativeImage},filename) => [16,24,32,48,64,128,256].map(size => {
      const bitmap=nativeImage.createFromPath(filename).resize({width:size,height:size}).toBitmap();
      return [3,(size-1)*4+3,(size*(size-1))*4+3,(size*size-1)*4+3].map(offset => bitmap[offset]);
    }),packagedIcon);
    assert.ok(iconCorners.every(corners => corners.every(alpha => alpha === 0)),'all ICO sizes must have transparent corners');
    await page.locator('.brand-logo').waitFor();
    await page.waitForFunction(() => document.querySelector('.brand-logo').naturalWidth > 0);
    await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].close());
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()),false);
    assert.equal((await page.evaluate(() => window.quark.call('hello'))).protocol,1);
    await app.evaluate(() => global.testTray.emit('click'));
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()),true);
    await app.evaluate(() => global.testTrayMenu.getMenuItemById('transfers').click());
    await page.getByRole('heading',{name:'传输任务',exact:true}).waitFor();
    await page.getByRole('button',{name:'设置',exact:true}).click();
    await page.getByLabel('关闭窗口行为',{exact:true}).selectOption('quit');
    await page.getByLabel('传输完成通知',{exact:true}).uncheck();
    await page.locator('.toast.success').getByText('后台运行设置已保存',{exact:true}).waitFor();
    await page.waitForFunction(() => !document.querySelector('[aria-label="传输完成通知"]').disabled);
    const savedDesktop=JSON.parse(await readFile(path.join(await app.evaluate(({app}) => app.getPath('userData')),'desktop.json'),'utf8'));
    assert.deepEqual(savedDesktop,{closeBehavior:'quit',notifications:false});
    await page.getByRole('button',{name:'我的文件',exact:true}).click();
    const initialAppearance=await page.evaluate(() => window.quark.getAppearance());
    assert.equal(initialAppearance.source,'system');
    assert.equal(initialAppearance.dark,await app.evaluate(({nativeTheme}) => nativeTheme.shouldUseDarkColors));
    await page.waitForFunction(dark => document.documentElement.dataset.colorScheme === (dark ? 'dark' : 'light'),initialAppearance.dark);
    assert.equal(await page.locator('.titlebar').evaluate(element => getComputedStyle(element).backgroundColor),initialAppearance.dark ? 'rgb(25, 28, 30)' : 'rgb(246, 248, 252)');
    await assert.rejects(page.evaluate(() => window.quark.setAppearance('invalid')),/外观选项无效/);
    const dark=await page.evaluate(() => window.quark.setAppearance('dark'));
    assert.equal(dark.warning,'');assert.equal(dark.dark,true);
    assert.equal(await app.evaluate(({nativeTheme}) => nativeTheme.themeSource),'dark');
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark' && document.documentElement.dataset.colorScheme === 'dark');
    assert.equal(await page.locator('.titlebar').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(25, 28, 30)');
    const savedAppearance=JSON.parse(await readFile(path.join(await app.evaluate(({app}) => app.getPath('userData')),'appearance.json'),'utf8'));
    assert.equal(savedAppearance.source,'dark');
    await page.evaluate(() => window.quark.setAppearance('light'));
    assert.equal(await app.evaluate(({nativeTheme}) => nativeTheme.shouldUseDarkColors),false);
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'light' && document.documentElement.dataset.colorScheme === 'light');
    assert.equal(await page.locator('.titlebar').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(246, 248, 252)');
    await page.evaluate(() => window.quark.setAppearance('system'));
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
    await assert.rejects(page.evaluate(() => window.quark.call('login.web.finish',{cookie:'never-import-from-renderer'})), /不支持/);
    for(const mode of ['mobile','wechat']) {
      const opening=app.waitForEvent('window');
      await page.evaluate(mode => {window.loginPromise=window.quark.officialLogin(mode);},mode);
      const official=await opening;
      await official.waitForURL('https://pan.quark.cn/');
      await official.locator(mode === 'mobile' ? 'iframe[src*="custom_login_type=mobile"]' : 'iframe[src*="/wechat_qrcode"]').waitFor();
      assert.equal(await official.evaluate(() => typeof window.quark),'undefined');
      const webPreferences=await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows().find(win => win.webContents.getURL().startsWith('https://pan.quark.cn/')).webContents.getLastWebPreferences());
      assert.equal(webPreferences.sandbox,true);assert.equal(webPreferences.contextIsolation,true);assert.equal(webPreferences.nodeIntegration,false);
      assert.ok(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows().every(win => !win.webContents.getURL().startsWith('https://pan.quark.cn/') || !win.webContents.session.isPersistent())));
      await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows().find(win => win.webContents.getURL().startsWith('https://pan.quark.cn/')).close());
      assert.equal((await page.evaluate(() => window.loginPromise)).cancelled,true);
    }
    await page.evaluate(() => window.quark.setAppearance('dark'));
    const userData=await app.evaluate(({app}) => app.getPath('userData'));
    await app.close();app=await _electron.launch(launchOptions);
    const restarted=await app.firstWindow();
    await restarted.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    assert.equal((await restarted.evaluate(() => window.quark.getAppearance())).source,'dark');
    assert.deepEqual((await restarted.evaluate(() => window.quark.getClientSettings())).settings,{closeBehavior:'quit',notifications:false});
    assert.equal(await app.evaluate(({nativeTheme}) => nativeTheme.themeSource),'dark');
    await restarted.waitForFunction(() => document.documentElement.dataset.colorScheme === 'dark');
    assert.equal(await restarted.locator('.titlebar').evaluate(element => getComputedStyle(element).backgroundColor),'rgb(25, 28, 30)');
    await app.close();
    await writeFile(path.join(userData,'appearance.json'),'{broken','utf8');
    app=await _electron.launch(launchOptions);
    const malformed=await app.firstWindow();
    await malformed.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    const fallback=await malformed.evaluate(() => window.quark.getAppearance());
    assert.equal(fallback.source,'system');assert.match(fallback.warning,/无法读取/);
    assert.equal(await readFile(path.join(userData,'appearance.json'),'utf8'),'{broken');
    await app.close();
    await rm(path.join(userData,'appearance.json'));
    await mkdir(path.join(userData,'appearance.json'));
    app=await _electron.launch(launchOptions);
    const unavailable=await app.firstWindow();
    await unavailable.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    assert.equal((await unavailable.evaluate(() => window.quark.getAppearance())).source,'system');
    const unsaved=await unavailable.evaluate(() => window.quark.setAppearance('dark'));
    assert.equal(unsaved.source,'dark');assert.match(unsaved.warning,/未能保存/);
    await app.close();
    await writeFile(path.join(userData,'desktop.json'),'{broken','utf8');
    app=await _electron.launch(launchOptions);
    const brokenSettings=await app.firstWindow();
    await brokenSettings.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    const defaults=await brokenSettings.evaluate(() => window.quark.getClientSettings());
    assert.deepEqual(defaults.settings,{closeBehavior:'tray',notifications:true});assert.match(defaults.warning,/无法读取/);
    assert.equal(await readFile(path.join(userData,'desktop.json'),'utf8'),'{broken');
    await app.close();await rm(path.join(userData,'desktop.json'));await mkdir(path.join(userData,'desktop.json'));
    app=await _electron.launch(launchOptions);
    const readonly=await app.firstWindow();
    await readonly.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    const unsavedDesktop=await readonly.evaluate(() => window.quark.setClientSettings({closeBehavior:'quit',notifications:false}));
    assert.deepEqual(unsavedDesktop.settings,{closeBehavior:'quit',notifications:false});assert.match(unsavedDesktop.warning,/未能保存/);
  } finally { await app.close(); await rm(directory, { recursive:true, force:true }); }
});


test('packaged Markdown loads lazy assets and saves through the real isolated preload',async () => {
  const directory=await mkdtemp(path.join(tmpdir(),'quark-editor-ui-'));
  const executable=path.resolve(__dirname,'..',process.env.QUARK_DESKTOP_OUT || 'out','Quark Drive Client-win32-x64/QuarkDriveClient.exe');
  const app=await _electron.launch({executablePath:executable,args:['--user-data-dir='+path.join(directory,'electron')],env:{...process.env,QUARK_CONFIG_DIR:directory,QUARK_COOKIE:'',QUARK_COOKIE_FILE:''}});
  let page;
  try {
    page=await app.firstWindow();await page.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    await app.evaluate(({ipcMain,app,dialog},output) => {
      const entry={fid:'markdown',name:'README.md',directory:false,size:10,path:'/README.md',location:[['0','我的网盘']],modified_at:100};
      globalThis.editorRequests=[];globalThis.editorPrompts=[];
      dialog.showMessageBox=async (_window,options) => {globalThis.editorPrompts.push(options.title);return {response:0};};
      ipcMain.removeHandler('quark:call');
      ipcMain.handle('quark:call',(_event,{method,params}) => {
        if(method==='hello') return {protocol:1,signed_in:true,preferences:{output_dir:output,connections:4,limit_rate:null},client_version:app.getVersion()};
        if(method==='account') return {nickname:'文本测试',logged_in:true};
        if(method==='browse') return [{...entry}];
        if(method==='text.open') return {text:'# 原文',encoding:'UTF-8',editor:'opened',version:'one',entry:{...entry}};
        if(method==='text.save') {globalThis.editorRequests.push(params);entry.fid='edited';return {version:'two',entry:{...entry},warning:'',blocked:false};}
        return [];
      });
    },directory);
    const errors=[];page.on('pageerror',error => errors.push(error.message));
    await page.reload();await page.getByRole('button',{name:'README.md',exact:true}).click();
    const editor=page.getByRole('dialog',{name:'文本预览',exact:true});
    await editor.getByLabel('文件内容').waitFor();
    await editor.getByRole('button',{name:'渲染格式',exact:true}).click();
    await editor.getByLabel('Markdown 渲染预览').getByRole('heading',{name:'原文',exact:true}).waitFor();
    await editor.getByLabel('文件内容').fill('# 即时更新\n\n**加粗内容**');
    await editor.getByLabel('Markdown 渲染预览').getByRole('heading',{name:'即时更新',exact:true}).waitFor();
    await page.evaluate(() => window.quark.quit());
    await page.waitForTimeout(100);
    assert.deepEqual(await app.evaluate(() => globalThis.editorPrompts),['文本尚未保存']);
    await page.keyboard.press('Control+s');
    await editor.getByRole('status').filter({hasText:'已同步到云端'}).waitFor();
    assert.deepEqual(await app.evaluate(() => globalThis.editorRequests),[{editor:'opened',version:'one',text:'# 即时更新\n\n**加粗内容**'}]);
    await page.evaluate(() => window.quark.setAppearance('dark'));
    await page.screenshot({path:path.resolve(__dirname,'../out/editor-packaged-check.png')});
    assert.deepEqual(errors,[]);
    await page.keyboard.press('Escape');await editor.waitFor({state:'detached'});
    await page.getByRole('button',{name:'README.md',exact:true}).waitFor();
  } finally {if(page) await page.evaluate(() => window.quark.setEditorDirty(false)).catch(() => {});await app.close();await rm(directory,{recursive:true,force:true});}
});

test('bundled backend restores transfer dates and shows unknown legacy times without inventing them',async () => {
  const directory=await mkdtemp(path.join(tmpdir(),'quark-task-dates-'));
  const executable=path.resolve(__dirname,'..',process.env.QUARK_DESKTOP_OUT || 'out','Quark Drive Client-win32-x64/QuarkDriveClient.exe');
  const timing={created_at:Date.UTC(2026,9,16,6,10),started_at:Date.UTC(2026,9,16,6,12),finished_at:Date.UTC(2026,9,16,7,42),elapsed_seconds:90};
  const old={id:'legacy',name:'旧记录.txt',state:'done',detail:'已下载',target:'C:\\Downloads\\旧记录.txt',revision:1,context:'other',direction:'download',request:{fid:'old',target:'C:\\Downloads\\旧记录.txt',recursive:false,overwrite:false,name:'旧记录.txt'},share_url:'',created_at:null,started_at:null,finished_at:null,elapsed_seconds:null};
  const uploaded={...old,id:'dated',name:'有日期.txt',detail:'已上传',direction:'upload',target:'/资料/有日期.txt',request:{source:'C:\\Uploads\\有日期.txt',parent:'0',recursive:false,name:'有日期.txt',limit_rate:null},...timing};
  await mkdir(path.join(directory,'downloader'));
  await writeFile(path.join(directory,'downloader/tasks.json'),JSON.stringify({version:2,tasks:[old,uploaded]}),'utf8');
  const app=await _electron.launch({executablePath:executable,args:['--user-data-dir='+path.join(directory,'electron')],env:{...process.env,QUARK_CONFIG_DIR:directory,QUARK_COOKIE:'',QUARK_COOKIE_FILE:'',QUARK_DESKTOP_PYTHON:'Z:\\no-python.exe'}});
  try {
    const page=await app.firstWindow();await page.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    const records=await page.evaluate(() => window.quark.call('tasks'));
    for(const key of Object.keys(timing)) assert.equal(records[1][key],timing[key]);
    assert.equal(records[0].finished_at,null);
    const session=await page.context().newCDPSession(page);await session.send('Emulation.setTimezoneOverride',{timezoneId:'Asia/Singapore'});
    await page.getByRole('button',{name:/^传输任务/}).click();
    await page.getByText('完成时间：2026-10-16 15:42',{exact:true}).waitFor();
    await page.getByText('累计耗时：1 分 30 秒',{exact:true}).waitFor();
    await page.getByText('上传到：/资料/有日期.txt',{exact:true}).waitFor();
    await page.evaluate(() => window.quark.setAppearance('dark'));
    await page.screenshot({path:path.resolve(__dirname,'../test-results/task-dates-packaged.png')});
    await page.getByRole('group',{name:'传输方向'}).getByRole('button',{name:/^下载/}).click();
    await page.getByText('完成时间：未记录',{exact:true}).waitFor();
    await page.getByText('累计耗时：未记录',{exact:true}).waitFor();
    await session.detach();
  } finally {await app.close();await rm(directory,{recursive:true,force:true});}
});

test('native file drops on rows and empty space upload into the directory currently displayed', async () => {
  const directory=await mkdtemp(path.join(tmpdir(),'quark-native-drop-'));
  const executable=path.resolve(__dirname,'..',process.env.QUARK_DESKTOP_OUT || 'out','Quark Drive Client-win32-x64/QuarkDriveClient.exe');
  const localFile=path.join(directory,'拖入.txt'),localFolder=path.join(directory,'拖入目录');
  await writeFile(localFile,'small payload','utf8');await mkdir(localFolder);
  const app=await _electron.launch({executablePath:executable,args:['--user-data-dir='+path.join(directory,'electron')],env:{...process.env,QUARK_CONFIG_DIR:directory,QUARK_COOKIE:'',QUARK_COOKIE_FILE:''}});
  try {
    const page=await app.firstWindow();await page.getByRole('heading',{name:'我的文件',exact:true}).waitFor();
    // Use the production UI, native File and preload validation; replace only cloud RPC replies.
    await app.evaluate(({ipcMain,app},output) => {
      globalThis.dropCalls=[];const tasks=[];
      const preferences={output_dir:output,connections:4,limit_rate:null,width:1180,height:780};
      ipcMain.removeHandler('quark:call');
      ipcMain.handle('quark:call',(_event,{method,params}) => {
        globalThis.dropCalls.push({method,params});
        if(method === 'hello') return {protocol:1,signed_in:true,preferences,client_version:app.getVersion()};
        if(method === 'account') return {nickname:'拖拽测试账号',logged_in:true};
        if(method === 'browse') return params.fid === '0' ? [{fid:'folder',name:'当前列表里的子目录',directory:true,size:null,path:'',location:[],modified_at:null}] : [];
        if(method === 'preferences.save') return {preferences:params,warning:''};
        if(method === 'upload') {
          params.sources.forEach(source => tasks.push({id:String(tasks.length),name:source.split('\\').at(-1),state:'waiting',detail:'',valid:true,direction:'upload',revision:1,scheduled:true,target:params.parent}));
          return [...tasks];
        }
        if(method === 'tasks') return [...tasks];
        return [];
      });
    },directory);
    await page.reload();
    const child=page.locator('.file-row').getByRole('button',{name:'当前列表里的子目录',exact:true});await child.waitFor();
    const session=await page.context().newCDPSession(page);
    async function nativeDrop(target,paths) {
      const box=await target.boundingBox();assert.ok(box);
      const point={x:box.x+box.width/2,y:box.y+box.height/2,data:{items:[],files:paths,dragOperationsMask:1}};
      await session.send('Input.dispatchDragEvent',{type:'dragEnter',...point});
      await session.send('Input.dispatchDragEvent',{type:'dragOver',...point});
      await page.getByText('松开鼠标，准备上传',{exact:true}).waitFor();
      await session.send('Input.dispatchDragEvent',{type:'drop',...point});
    }
    await nativeDrop(child,[localFile,localFolder]);
    const dialog=page.getByRole('dialog',{name:'确认拖拽上传',exact:true});
    await dialog.getByText('目标：我的网盘',{exact:true}).waitFor();
    await dialog.getByText('共 2 项 · 1 个目录',{exact:true}).waitFor();
    assert.equal(await app.evaluate(() => globalThis.dropCalls.filter(row => row.method === 'upload').length),0);
    await dialog.getByRole('button',{name:'确认上传',exact:true}).click();
    await dialog.waitFor({state:'detached'});
    assert.ok(await page.getByRole('heading',{name:'我的文件',exact:true}).isVisible());
    assert.deepEqual(await app.evaluate(() => globalThis.dropCalls.find(row => row.method === 'upload').params),{sources:[localFile,localFolder],parent:'0'});
    await page.getByRole('button',{name:'我的文件',exact:true}).click();await child.click();
    await page.getByText('暂无文件',{exact:true}).waitFor();
    await nativeDrop(page.locator('.file-list'),[localFile]);
    await dialog.getByText('目标：我的网盘 / 当前列表里的子目录',{exact:true}).waitFor();
    await dialog.getByRole('button',{name:'确认上传',exact:true}).click();
    await dialog.waitFor({state:'detached'});
    assert.ok(await page.getByRole('heading',{name:'我的文件',exact:true}).isVisible());
    await page.getByRole('navigation',{name:'文件路径'}).getByRole('button',{name:'当前列表里的子目录',exact:true}).waitFor();
    const uploads=await app.evaluate(() => globalThis.dropCalls.filter(row => row.method === 'upload').map(row => row.params));
    assert.deepEqual(uploads,[{sources:[localFile,localFolder],parent:'0'},{sources:[localFile],parent:'folder'}]);
    await session.detach();
  } finally {await app.close();await rm(directory,{recursive:true,force:true});}
});
