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

test('visible image rows load lazily; preview zooms, navigates, retries, closes and keeps layout stable',async () => {
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
      const pixel='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+juyQAAAAASUVORK5CYII=';
      window.imageCalls=[];window.imageActive=0;window.imageMax=0;window.failPreview=false;
      window.quark={onEvent:() => () => {},getAppearance:async () => ({source:'light',dark:false,warning:''}),getClientSettings:async () => ({settings:{closeBehavior:'tray',notifications:false},warning:''}),call:async (method,params={}) => {
        if(method==='hello') return {protocol:1,client_version:'0.5.21',signed_in:true,preferences,extension_groups:{}};
        if(method==='account') return {nickname:'图片测试',logged_in:true};
        if(method==='browse') return rows;
        if(method==='tasks' || method==='favorites.list') return [];
        if(method==='image.read') {
          window.imageCalls.push(params);++window.imageActive;window.imageMax=Math.max(window.imageMax,window.imageActive);
          await new Promise(resolve => setTimeout(resolve,35));--window.imageActive;
          if(window.failPreview && !params.thumbnail) throw new Error('图片加载失败，请重试');
          return {src:pixel,width:1,height:1};
        }
        return true;
      }};
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/main_window/index.html`);
    await page.waitForFunction(() => window.imageCalls.length>2 && document.querySelector('.image-thumbnail img'));
    assert.ok(await page.locator('.file-row').count()<50);
    assert.ok(await page.evaluate(() => window.imageCalls.length)<25,'does not fetch 1000 images');
    assert.equal(await page.evaluate(() => window.imageMax),2);
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
