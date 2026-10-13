import { BrowserWindow, session, shell } from 'electron';
import { randomUUID } from 'node:crypto';

type Request = (method:string,params?:Record<string,unknown>) => Promise<any>;
export function allowedLoginNavigation(value:string) {
  try {const url=new URL(value);return url.protocol === 'https:' && !url.port && !url.username && !url.password && ['pan.quark.cn','uop.quark.cn','open.weixin.qq.com'].includes(url.hostname);}
  catch {return false;}
}
export function loginCookie(cookies:{domain?:string;name:string;value:string}[]) {
  const selected=new Map<string,{rank:number;value:string}>();
  for(const cookie of cookies) {
    const domain=(cookie.domain || '').replace(/^\./,'').toLowerCase();
    if(!['quark.cn','pan.quark.cn','drive-pc.quark.cn','drive.quark.cn'].includes(domain) || !cookie.name || !cookie.value) continue;
    if(/[\r\n;]/.test(cookie.name+cookie.value)) continue;
    const rank=domain === 'quark.cn' ? 2 : domain === 'drive-pc.quark.cn' ? 1 : 0;
    if(!selected.has(cookie.name) || rank >= selected.get(cookie.name)!.rank) selected.set(cookie.name,{rank,value:cookie.value});
  }
  if(!selected.has('__pus') || !selected.has('__puus')) return null;
  return [...selected].map(([name,{value}]) => `${name}=${value}`).join('; ');
}

export async function officialLogin(parent:BrowserWindow,mode:'mobile'|'wechat',request:Request) {
  const {token}=await request('login.web.begin');
  const loginSession=session.fromPartition(`quark-login-${randomUUID()}`);
  loginSession.setPermissionRequestHandler((_contents,_permission,callback) => callback(false));
  let login:BrowserWindow;
  try {
    login=new BrowserWindow({parent,width:1060,height:760,minWidth:860,minHeight:620,
      title:`夸克官方登录 · ${mode === 'mobile' ? '手机验证码' : '微信'}`,backgroundColor:'#fff',
      webPreferences:{session:loginSession,contextIsolation:true,nodeIntegration:false,sandbox:true}});
  } catch(error) {await request('login.web.cancel',{token});throw error;}
  login.setMenuBarVisibility(false);
  return new Promise<any>((resolve,reject) => {
    let settled=false,checking=false,accepting=false,recheck=false;
    let timer:ReturnType<typeof setTimeout>|null=null;
    let lastCandidate='';
    function finish(result:any,error?:Error) {
      if(settled) return;settled=true;
      if(timer) clearTimeout(timer);
      loginSession.cookies.removeListener('changed',changed);
      if(!login.isDestroyed()) login.destroy();
      void loginSession.clearStorageData().catch(() => {});
      if(error) reject(error);else resolve(result);
    }
    async function changed() {
      if(settled) return;
      if(checking) {recheck=true;return;}
      if(accepting) return;
      checking=true;
      try {
        const candidate=loginCookie(await loginSession.cookies.get({}));
        if(!candidate || candidate === lastCandidate || settled) return;
        lastCandidate=candidate;accepting=true;
        // During verification a close waits for the actual result, so the UI
        // cannot report cancellation after a credential was already committed.
        const result=await request('login.web.finish',{token,cookie:candidate});
        finish(result);
      } catch(error) {
        accepting=false;
        if(!settled && !login.isDestroyed()) {
          const message=error instanceof Error ? error.message : '凭证尚未验证';
          login.setTitle(`夸克官方登录 · ${message}`);
          if(!parent.isDestroyed()) parent.webContents.send('quark:event',{id:null,event:'warning',data:`官方登录尚未完成：${message}`});
        }
      } finally {checking=false;if(recheck && !settled) {recheck=false;void changed();}}
    }
    loginSession.cookies.on('changed',changed);
    login.on('close',event => {if(accepting) event.preventDefault();});
    login.on('closed',() => {
      if(!settled) void request('login.web.cancel',{token}).then(() => finish({cancelled:true})).catch(() => finish(null,new Error('取消官方登录失败，请重新启动客户端')));
    });
    login.webContents.on('will-navigate',(event,url) => {if(!allowedLoginNavigation(url)) event.preventDefault();});
    login.webContents.on('will-redirect',(event,url) => {if(!allowedLoginNavigation(url)) event.preventDefault();});
    login.webContents.setWindowOpenHandler(({url}) => {
      if(/^https:\/\/terms\.alicdn\.com\//.test(url)) void shell.openExternal(url);
      return {action:'deny'};
    });
    login.webContents.on('dom-ready',() => {
      // Only select the observed official login tab; inputs stay entirely on
      // the official page, without a preload or application credential bridge.
      const label=mode === 'mobile' ? '手机登录' : '微信登录';
      void login.webContents.executeJavaScript(`Array.from(document.querySelectorAll('span,div,a')).find(el => el.textContent?.trim() === ${JSON.stringify(label)} && el.children.length === 0)?.click()`)
        .catch(() => {});
      if(timer) clearTimeout(timer);
      timer=setTimeout(() => void changed(),200);
    });
    void login.loadURL('https://pan.quark.cn/').catch(async () => {
      if(!settled) {await request('login.web.cancel',{token}).catch(() => {});finish(null,new Error('无法打开夸克官方登录页面，请检查网络后重试'));}
    });
  });
}
