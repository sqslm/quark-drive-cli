const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mkdtemp,readFile,writeFile,rm}=require('node:fs/promises');
const {tmpdir}=require('node:os');
const path=require('node:path');
const vm=require('node:vm');
const ts=require('typescript');

test('share lifetime preference migrates old settings and persists both modes',async () => {
  const directory=await mkdtemp(path.join(tmpdir(),'quark-client-settings-'));
  try {
    const source=await readFile(path.resolve(__dirname,'../src/ClientSettings.ts'),'utf8');
    const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
    const api={};vm.runInNewContext(code,{exports:api,require:name => name==='electron' ? {app:{getPath:() => directory}} : require(name)});
    const filename=path.join(directory,'desktop.json');
    assert.equal((await api.loadClientSettings()).settings.shareExpiryMode,'hours');
    const legacy={closeBehavior:'quit',notifications:false};
    await writeFile(filename,JSON.stringify(legacy));
    const loaded=await api.loadClientSettings();
    assert.equal(loaded.warning,'');assert.equal(loaded.settings.shareExpiryMode,'hours');
    assert.equal(loaded.settings.closeBehavior,'quit');assert.equal(loaded.settings.notifications,false);
    assert.deepEqual(JSON.parse(await readFile(filename,'utf8')),legacy,'reading must not rewrite old settings');
    for(const mode of ['daysHours','hours']) {
      const settings={...legacy,shareExpiryMode:mode};
      assert.equal(api.validClientSettings(settings),true);
      await api.saveClientSettings(settings);
      assert.equal((await api.loadClientSettings()).settings.shareExpiryMode,mode);
      assert.deepEqual(JSON.parse(await readFile(filename,'utf8')),settings);
    }
    for(const mode of ['',null,42,'days']) assert.equal(api.validClientSettings({...legacy,shareExpiryMode:mode}),false);
    await writeFile(filename,JSON.stringify({...legacy,shareExpiryMode:'days'}));
    assert.match((await api.loadClientSettings()).warning,/无法读取/);
    assert.equal(JSON.parse(await readFile(filename,'utf8')).shareExpiryMode,'days');
  } finally {await rm(directory,{recursive:true,force:true});}
});
