const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFile}=require('node:fs/promises');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');
const http=require('node:http');
const {chromium}=require('@playwright/test');

const tick=() => new Promise(resolve => setImmediate(resolve));
test('image queue bounds concurrency, prioritizes previews, drops abandoned work and isolates accounts',async () => {
  const code=ts.transpileModule(await readFile(path.resolve(__dirname,'../src/Images.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const exports={};vm.runInNewContext(code,{exports,window:{quark:{call:async () => {}}}});
  const calls=[],received=[];
  const queue=new exports.ImageQueue(params => new Promise((resolve,reject) => calls.push({params,resolve,reject})),20);
  const request=(key,preview=false) => queue.request(key,{key},preview,data => received.push([key,data.src]),error => received.push([key,error.message]));
  request('a');request('b');const cancel=request('abandoned');request('preview',true);request('b');cancel();
  assert.deepEqual(calls.map(call => call.params.key),['a','b']);
  calls[0].resolve({src:'11111',width:1,height:1});await tick();
  assert.equal(calls[2].params.key,'preview');
  calls[1].resolve({src:'22222',width:1,height:1});calls[2].resolve({src:'33333',width:1,height:1});await tick();
  assert.equal(calls.length,3);assert.equal(received.filter(([key]) => key==='b').length,2);
  request('preview');assert.equal(calls.length,3,'memory cache reused');
  request('a');assert.equal(calls.length,4,'LRU evicted oldest bytes');
  queue.clear();request('new-account');assert.equal(calls.length,5);
  calls[3].resolve({src:'old',width:1,height:1});calls[4].reject(new Error('可重试'));await tick();
  assert.equal(received.filter(([key]) => key==='a').length,1,'late old-account response ignored');
  request('new-account');assert.equal(calls.length,6,'errors can retry');calls[5].resolve({src:'new',width:1,height:1});await tick();queue.clear();
});

test('cloud previews load visible images, zoom and retry; text refreshes safely and ignores late reads',async () => {
  const root=path.resolve(__dirname,'../.webpack/x64/renderer');
  const server=http.createServer(async (request,response) => {
    const filename=path.resolve(root,'.'+(request.url==='/' ? '/index.html' : request.url.split('?')[0]));
    if(!filename.startsWith(root+path.sep)) {response.writeHead(403).end();return;}
    try {response.setHeader('Content-Type',filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.png') ? 'image/png' : 'text/html');response.end(await readFile(filename));}catch {response.writeHead(404).end();}
  });
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try {
    const page=await browser.newPage({viewport:{width:1180,height:780}});
    const errors=[];page.on('pageerror',error => errors.push(error.message));
    await page.addInitScript(() => {
      const preferences={output_dir:'C:\\Downloads',connections:4,limit_rate:null,width:1180,height:780};
      const rows=Array.from({length:1000},(_,n) => ({fid:String(n),name:`图片${String(n).padStart(4,'0')}.png`,directory:false,size:100,path:'',location:[],modified_at:100}));
      const textRows=['配置.json','说明.txt','README.md','空文件.txt','失败.txt','迟到.txt'].map((name,n) => ({...rows[0],fid:'t'+n,name}));
      const pixel='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+juyQAAAAASUVORK5CYII=';
      window.imageCalls=[];window.imageActive=0;window.imageMax=0;window.failPreview=false;window.textVersion=1;window.textCalls=[];
      window.quark={copyText:async text => {window.copiedPreview=text;},onEvent:() => () => {},getAppearance:async () => ({source:'light',dark:false,warning:''}),getClientSettings:async () => ({settings:{closeBehavior:'tray',notifications:false},warning:''}),call:async (method,params={}) => {
        if(method==='hello') return {protocol:1,client_version:'0.5.22',signed_in:true,preferences,extension_groups:{}};
        if(method==='account') return {nickname:'图片测试',logged_in:true};
        if(method==='browse') return window.showTextRows ? textRows : rows;
        if(method==='tasks' || method==='favorites.list') return [];
        if(method==='image.read') {
          window.imageCalls.push(params);++window.imageActive;window.imageMax=Math.max(window.imageMax,window.imageActive);
          await new Promise(resolve => setTimeout(resolve,35));--window.imageActive;
          if(window.failPreview && !params.thumbnail) throw new Error('图片加载失败，请重试');
          return {src:pixel,width:1,height:1};
        }
        if(method==='text.read') {
          window.textCalls.push(params.entry.name);
          if(params.entry.name==='迟到.txt') await new Promise(resolve => {window.releaseText=resolve;});
          if(params.entry.name==='失败.txt' || window.failTextRefresh) throw new Error("Error invoking remote method 'quark:call': Error: 操作失败：文本读取失败");
          const text=params.entry.name==='配置.json' ? JSON.stringify({version:window.textVersion}) : params.entry.name==='空文件.txt' ? '' : params.entry.name==='README.md' ? '# Markdown 标题\n正文' : '<img src=x onerror="window.injected=true"><script>window.injected=true</script>\n'+('长文本\n'.repeat(1000));
          return {text,encoding:'UTF-8',size:text.length,modified_at:100};
        }
        return true;
      }};
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/main_window/index.html`);
    await page.waitForFunction(() => window.imageCalls.length>2 && document.querySelector('.image-thumbnail img'));
    assert.ok(await page.locator('.file-row').count()<50);
    assert.ok(await page.evaluate(() => window.imageCalls.length)<25,'does not fetch 1000 images');
    assert.equal(await page.evaluate(() => window.imageMax),2);
    await page.evaluate(() => {window.showTextRows=true;});await page.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByRole('button',{name:'配置.json',exact:true}).click();
    const textDialog=page.getByRole('dialog',{name:'文本预览',exact:true});
    await textDialog.getByLabel('文件内容').filter({hasText:'{"version":1}'}).waitFor();
    await page.evaluate(() => {window.textVersion=2;});await textDialog.getByRole('button',{name:'刷新内容'}).click();
    await textDialog.getByLabel('文件内容').filter({hasText:'{"version":2}'}).waitFor();
    await textDialog.getByRole('button',{name:'复制内容'}).click();assert.equal(await page.evaluate(() => window.copiedPreview),'{"version":2}');
    await page.getByText('已复制文本内容',{exact:true}).waitFor();
    await page.evaluate(() => {window.failTextRefresh=true;});await textDialog.getByRole('button',{name:'刷新内容'}).click();
    await textDialog.getByRole('alert').waitFor();assert.match(await textDialog.getByRole('alert').innerText(),/上次读取/);
    assert.doesNotMatch(await textDialog.innerText(),/Error invoking remote/);
    await page.evaluate(() => {window.failTextRefresh=false;});await textDialog.getByRole('button',{name:'重新读取'}).click();
    await textDialog.getByRole('button',{name:'刷新内容'}).waitFor();await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.getByRole('button',{name:'说明.txt',exact:true}).click();await textDialog.getByLabel('文件内容').waitFor();
    assert.ok((await textDialog.getByLabel('文件内容').innerText()).includes('<script>'));
    assert.equal(await textDialog.locator('img,script').count(),0);assert.equal(await page.evaluate(() => window.injected),undefined);
    await textDialog.getByRole('checkbox',{name:'自动换行'}).uncheck();assert.equal(await textDialog.locator('.text-content.wrap').count(),0);
    await textDialog.getByLabel('文件内容').focus();await page.keyboard.press('Tab');assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')),'关闭文本预览');
    await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.getByRole('button',{name:'README.md',exact:true}).click({button:'right'});await page.getByRole('menuitem',{name:'预览文本'}).click();
    await textDialog.getByLabel('文件内容').filter({hasText:'# Markdown 标题'}).waitFor();
    await page.screenshot({path:path.resolve(__dirname,'../out/text-preview-check.png')});
    await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.getByRole('button',{name:'空文件.txt',exact:true}).click();await textDialog.getByText('文件内容为空',{exact:true}).waitFor();
    await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.getByRole('button',{name:'失败.txt',exact:true}).click();await textDialog.getByRole('alert').waitFor();
    assert.doesNotMatch(await textDialog.innerText(),/Error invoking remote/);await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.getByRole('button',{name:'迟到.txt',exact:true}).click();await page.waitForFunction(() => !!window.releaseText);
    await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.getByRole('button',{name:'配置.json',exact:true}).click();await page.evaluate(() => window.releaseText());
    await textDialog.getByLabel('文件内容').filter({hasText:'{"version":2}'}).waitFor();
    await page.keyboard.press('Escape');await textDialog.waitFor({state:'detached'});
    await page.evaluate(() => {window.showTextRows=false;});await page.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByRole('button',{name:'图片0000.png',exact:true}).waitFor();
    const rowHeight=await page.locator('.file-row').first().evaluate(node => node.getBoundingClientRect().height);
    assert.equal(rowHeight,46);
    await page.getByRole('button',{name:'图片0000.png',exact:true}).click();
    const dialog=page.getByRole('dialog',{name:'图片预览'});await dialog.waitFor();
    await dialog.locator('.image-canvas img').waitFor();
    assert.equal(await dialog.locator('.image-canvas img').evaluate(img => img.naturalWidth),1);
    const before=await dialog.locator('.image-canvas').boundingBox();
    await dialog.getByRole('button',{name:'放大',exact:true}).click();
    const after=await dialog.locator('.image-canvas').boundingBox();assert.ok(after.width>before.width);
    await dialog.getByRole('button',{name:'适应窗口'}).click();
    const fitted=await dialog.locator('.image-canvas img').boundingBox();
    await dialog.getByRole('button',{name:'缩小',exact:true}).click();
    const smaller=await dialog.locator('.image-canvas img').boundingBox();assert.ok(smaller.width<fitted.width,'zoom out changes rendered image size');
    await page.keyboard.press('ArrowRight');await dialog.locator('strong').filter({hasText:'图片0001.png'}).waitFor();
    await dialog.locator('.image-canvas img').waitFor();
    await page.evaluate(() => {window.failPreview=true;});await page.keyboard.press('ArrowRight');
    await dialog.getByRole('button',{name:'重新加载图片'}).waitFor();
    await page.evaluate(() => {window.failPreview=false;});await dialog.getByRole('button',{name:'重新加载图片'}).click();await dialog.locator('.image-canvas img').waitFor();
    assert.ok(await page.evaluate(() => window.imageCalls.some(call => call.refresh && !call.thumbnail)));
    await page.screenshot({path:path.resolve(__dirname,'../out/image-preview-check.png')});
    await page.keyboard.press('Escape');await dialog.waitFor({state:'detached'});
    assert.equal(await page.locator('.file-row').first().evaluate(node => node.getBoundingClientRect().height),46);
    await page.locator('.file-scroll').evaluate(node => {node.scrollTop=20000;});
    await page.waitForFunction(() => window.imageCalls.some(call => call.thumbnail && Number(call.entry.fid)>400));
    assert.equal(await page.evaluate(() => window.imageMax),2);
    await page.emulateMedia({colorScheme:'dark'});
    assert.deepEqual(errors,[]);
  } finally {await browser.close();await new Promise(resolve => server.close(resolve));}
});
