import React, { useEffect, useRef, useState } from 'react';
import { Entry } from './types';
import { Feedback } from './Feedback';

export function shareText(result: {title?:string; share_url?:string; passcode?:string}) {
  return [result.title,result.share_url,result.passcode ? `提取码：${result.passcode}` : ''].filter(Boolean).join('\n');
}

export function SharingDialog({mode,entries,handle,path,close,onStatus}: {
  mode:'create'|'save'; entries:Entry[]; handle:string|null; path:string; close:()=>void; onStatus:Feedback;
}) {
  const [busy,setBusy] = useState(false);
  const [days,setDays] = useState('7');
  const [code,setCode] = useState('');
  const [trail,setTrail] = useState<[string,string][]>([['0','我的网盘']]);
  const [folders,setFolders] = useState<Entry[]>([]);
  const [ready,setReady] = useState(mode === 'create');
  const [error,setError] = useState('');
  const [result,setResult] = useState<any>(null);
  const [stage,setStage] = useState('');
  const mounted = useRef(true);
  async function browse(chain:[string,string][]) {
    setBusy(true); setReady(false); setError('');
    try {
      const rows:Entry[] = await window.quark.call('browse',{fid:chain.at(-1)![0]});
      if(mounted.current) {setFolders(rows.filter(row => row.directory));setTrail(chain);setReady(true);}
    } catch(error) { if(mounted.current) setError(error instanceof Error ? error.message : '无法读取目标目录'); }
    finally { if(mounted.current) setBusy(false); }
  }
  useEffect(() => { if(mode === 'save') void browse(trail); return () => {mounted.current = false;}; },[]);
  async function submit() {
    setBusy(true);setError('');
    setStage(mode === 'create' ? '正在核对选中项目…' : '正在转存选中项目…');
    const id = crypto.randomUUID();
    const off = window.quark.onEvent(event => {if(event.id === id && event.event === 'share.progress') setStage(String(event.data));});
    try {
      const response = mode === 'create'
        ? await window.quark.call('share.create',{entry:entries[0],expire_days:Number(days),passcode:code,handle},id)
        : await window.quark.call('share.save',{entries,handle,path,target:trail.at(-1)![0]});
      setResult(response);
      onStatus(mode === 'create' ? response.result === 'created' ? '分享已创建并核验' : '分享结果未完整确认，请查看对话框' : response.saved ? `已永久转存 ${response.item_count} 项` : '转存结果未确认，请检查目标目录',response.result === 'created' || response.saved ? 'success' : 'warning');
    } catch(error) { const text=error instanceof Error ? error.message : '操作失败';setError(text);onStatus(text,'error'); }
    finally {off();setBusy(false);setStage('');}
  }
  const title = mode === 'create' ? '创建分享' : '永久转存到网盘';
  return <div className="modal-backdrop"><section className="modal file-modal" role="dialog" aria-modal="true" aria-label={title}>
    <h2>{title}</h2><p>已选择 {entries.length} 项：{entries.map(row => row.name).join('、')}</p>
    {busy && stage && <p className="share-progress" aria-live="polite">{stage}</p>}
    {!result && mode === 'create' && <><label>有效期<select aria-label="分享有效期" value={days} disabled={busy} onChange={event => setDays(event.target.value)}><option value="1">1 天</option><option value="7">7 天</option><option value="30">30 天</option><option value="0">永久</option></select></label>
      <label>提取码<input aria-label="新分享提取码" placeholder="留空公开分享，或四位字母/数字" maxLength={4} value={code} disabled={busy} onChange={event => setCode(event.target.value)}/></label></>}
    {!result && mode === 'save' && <><p>这些项目将保留在你的网盘中。</p><div className="directory-picker"><div><button disabled={busy || trail.length === 1} onClick={() => void browse(trail.slice(0,-1))}>上级</button><span>{trail.map(item => item[1]).join(' / ')}</span></div><div className="directory-list">{folders.map(row => <button key={row.fid} disabled={busy} onClick={() => void browse([...trail,[row.fid,row.name]])}>{row.name}</button>)}{ready && !folders.length && <p>没有子目录</p>}</div><p>目标：{trail.map(item => item[1]).join(' / ')}</p></div></>}
    {result && mode === 'create' && <><p className={result.result !== 'created' ? 'error' : ''}>{result.result === 'created' ? '已创建并核验选中项目' : result.detail || '分享结果未确认，请刷新我的分享核实'}</p>{result.share_url && <pre className="share-copy">{shareText(result)}</pre>}</>}
    {result && mode === 'save' && <p className={!result.saved ? 'error' : ''}>{result.saved ? `已转存 ${result.item_count} 项到 ${result.destination_path}` : result.detail || '转存结果未确认，请检查目标目录'}</p>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="dialog-actions"><button disabled={busy} onClick={close}>{result ? '关闭' : '取消'}</button>
      {result?.share_url && <button onClick={() => void window.quark.copyText(shareText(result)).then(() => {setError('');onStatus('已复制分享文案','success');}).catch(error => {setError(error.message);onStatus(error.message,'error');})}>复制分享文案</button>}
      {!result && <button className="primary" disabled={busy || !ready || (mode === 'create' && !!code && !/^[A-Za-z0-9]{4}$/.test(code))} onClick={() => void submit()}>{busy ? '处理中…' : mode === 'create' ? '确认创建' : '确认永久转存'}</button>}</div>
  </section></div>;
}
