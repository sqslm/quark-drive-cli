import React, { useEffect, useState } from 'react';
import { Entry } from './types';
import { Feedback } from './Feedback';

export type FileAction = 'mkdir' | 'rename' | 'move' | 'delete' | 'info';
export function FileDialog({ action, entries, parent, done, close, onStatus }: {
  action: FileAction; entries: Entry[]; parent: [string,string][]; done: () => void; close: () => void; onStatus:Feedback;
}) {
  const [name,setName] = useState(action === 'rename' ? entries[0].name : '');
  const [trail,setTrail] = useState<[string,string][]>([['0','我的网盘']]);
  const [folders,setFolders] = useState<Entry[]>([]);
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [results,setResults] = useState<{ name:string; state:string; detail:string }[] | null>(null);
  const [info,setInfo] = useState<Record<string,unknown> | null>(null);
  const title = ({ mkdir:'新建目录',rename:'重命名',move:'移动到',delete:'移入回收站',info:'文件属性' })[action];
  async function loadFolders(chain: [string,string][]) {
    setBusy(true); setError('');
    try { const rows: Entry[] = await window.quark.call('browse',{ fid:chain.at(-1)![0] }); setFolders(rows.filter(row => row.directory)); setTrail(chain); }
    catch(error) { setError(error instanceof Error ? error.message : '无法读取目录'); }
    finally { setBusy(false); }
  }
  useEffect(() => {
    if(action === 'move') void loadFolders([['0','我的网盘']]);
    if(action === 'info') {
      setBusy(true);
      void window.quark.call('file.info',{ entry:entries[0] }).then(setInfo).catch(error => setError(error.message)).finally(() => setBusy(false));
    }
  }, []);
  async function submit() {
    setBusy(true); setError('');
    try {
      if(action === 'mkdir') {
        await window.quark.call('file.mkdir',{ parent:parent.at(-1)![0], name }); done(); close();onStatus('目录已创建','success');
      } else if(action !== 'info') {
        const result = await window.quark.call(`file.${action}`,{ entries,name,target:trail.at(-1)![0] });
        setResults(result); done();onStatus(result.some((row:{state:string}) => row.state !== 'done') ? '部分项目操作失败，请查看逐项结果' : '文件操作已完成', result.some((row:{state:string}) => row.state !== 'done') ? 'warning' : 'success');
      }
    } catch(error) { const text = error instanceof Error ? error.message : '操作失败';setError(text);onStatus(text,'error'); }
    finally { setBusy(false); }
  }
  return <div className="modal-backdrop"><section className="modal file-modal" role="dialog" aria-modal="true" aria-label={title}>
    <h2>{title}</h2>
    <p>{action === 'mkdir' ? `位置：${parent.map(item => item[1]).join(' / ')}` : `已选择 ${entries.length} 项：${entries.map(row => row.name).join('、')}`}</p>
    {(action === 'mkdir' || action === 'rename') && !results && <label>名称<input aria-label="新名称" autoFocus value={name} disabled={busy} onChange={event => setName(event.target.value)} onKeyDown={event => { if(event.key === 'Enter' && name.trim() && !busy) void submit(); }}/></label>}
    {action === 'move' && !results && <div className="directory-picker"><div><button disabled={busy || trail.length === 1} onClick={() => void loadFolders(trail.slice(0,-1))}>上级</button><span>{trail.map(item => item[1]).join(' / ')}</span></div>
      <div className="directory-list">{folders.map(row => <button key={row.fid} disabled={busy} onClick={() => void loadFolders([...trail,[row.fid,row.name]])}>{row.name}</button>)}{!folders.length && <p>没有子目录，可以选择当前目录</p>}</div><p>目标：{trail.map(item => item[1]).join(' / ')}</p></div>}
    {action === 'delete' && !results && <p>选中项目将移入回收站，可在回收站中恢复。</p>}
    {info && <dl className="properties">{Object.entries(info).map(([key,value]) => <React.Fragment key={key}><dt>{({file_name:'名称',path:'路径',size:'大小（字节）',directory:'目录',fid:'FID',pdir_fid:'父目录 FID',updated_at:'修改时间',created_at:'创建时间'} as Record<string,string>)[key] || key}</dt><dd>{String(value)}</dd></React.Fragment>)}</dl>}
    {results && <div className="operation-results">{results.map((row,index) => <p key={index} className={row.state === 'failed' ? 'error' : ''}>{row.name}：{row.detail}</p>)}</div>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="dialog-actions"><button disabled={busy} onClick={close}>{results || action === 'info' ? '关闭' : '取消'}</button>
      {!results && action !== 'info' && <button className="primary" disabled={busy || ((action === 'mkdir' || action === 'rename') && !name.trim())} onClick={() => void submit()}>{busy ? '处理中…' : action === 'move' ? '移到当前目录' : '确认'}</button>}
      {info && <button onClick={() => void window.quark.copyText(String(info.path || '')).then(() => onStatus('已复制路径','success')).catch(error => onStatus(error.message,'error'))}>复制路径</button>}</div>
  </section></div>;
}
