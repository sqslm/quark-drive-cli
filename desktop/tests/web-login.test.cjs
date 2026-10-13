const {test}=require('node:test');
const assert=require('node:assert/strict');
const {readFile}=require('node:fs/promises');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');

test('official login limits navigation and selects only complete Quark credentials',async () => {
  const source=await readFile(path.resolve(__dirname,'../src/WebLogin.ts'),'utf8');
  const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
  const api={};vm.runInNewContext(code,{exports:api,require:name => name === 'electron' ? {} : require(name),URL,Map});
  for(const url of ['https://pan.quark.cn/','https://uop.quark.cn/cas/custom/login','https://open.weixin.qq.com/connect/qrconnect']) assert.equal(api.allowedLoginNavigation(url),true);
  for(const url of ['http://pan.quark.cn/','https://pan.quark.cn:444/','https://pan.quark.cn.evil.test','https://user@pan.quark.cn','file:///C:/cookie','https://evil.test']) assert.equal(api.allowedLoginNavigation(url),false);
  assert.equal(api.loginCookie([{domain:'.quark.cn',name:'__pus',value:'partial'}]),null);
  const cookies=[{domain:'.evil.test',name:'__puus',value:'foreign'},{domain:'.quark.cn',name:'__pus',value:'valid'},{domain:'pan.quark.cn',name:'__pus',value:'less-specific'},{domain:'.quark.cn',name:'__puus',value:'complete'},{domain:'.quark.cn',name:'invalid',value:'value; injection'}];
  assert.equal(api.loginCookie(cookies),'__pus=valid; __puus=complete');
});
