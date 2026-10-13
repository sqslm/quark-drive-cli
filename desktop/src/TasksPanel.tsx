import React, { useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Task } from './types';
import { Feedback } from './Feedback';

const states: Record<string,string> = { waiting:'等待中',running:'传输中',done:'已完成',failed:'失败',stopped:'已暂停',cancelled:'已取消' };
export function TasksPanel({ tasks,busy,onTasks,onStart,onEnd,onStatus }: {
  tasks:Task[]; busy:boolean; onTasks:(tasks:Task[]) => void; onStart:() => void; onEnd:() => void; onStatus:Feedback;
}) {
  const [authorization,setAuthorization] = useState<Task | null>(null);
  const [code,setCode] = useState('');
  const [authBusy,setAuthBusy] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count:tasks.length,getScrollElement:() => scroll.current,estimateSize:() => 98,overscan:6 });
  const eligible = (task:Task) => task.valid && ['failed','stopped','waiting'].includes(task.state);
  async function retry(ids?:string[]) {
    onStart();
    try { onTasks(await window.quark.call('tasks.retry',ids ? {ids} : {}));onStatus('任务已加入队列','success'); }
    catch(error) { onEnd(); onStatus(error instanceof Error ? error.message : '继续任务失败','error'); }
  }
  async function cancel(ids:string[]) {
    try { onTasks(await window.quark.call('tasks.cancel',{ids})); onStatus('已请求取消，正在运行的任务将在安全停止后确认'); }
    catch(error) { onStatus(error instanceof Error ? error.message : '取消失败','error'); }
  }
  async function authorize() {
    setAuthBusy(true);
    try {
      const share = await window.quark.call('share.open',{text:authorization!.share_url,passcode:code});
      onTasks(await window.quark.call('tasks.authorize',{handle:share.handle,ids:[authorization!.id]}));
      setAuthorization(null); setCode(''); onStatus('分享授权已更新，可以继续任务','success');
    } catch(error) { onStatus(error instanceof Error ? error.message : '分享授权失败','error'); }
    finally { setAuthBusy(false); }
  }
  return <><div className="toolbar"><button disabled={busy || !tasks.some(task => eligible(task) && task.state === 'failed')} onClick={() => void retry(tasks.filter(task => eligible(task) && task.state === 'failed').map(task => task.id))}>重试失败</button>
    <button disabled={busy || !tasks.some(eligible)} onClick={() => void retry()}>继续未完成</button><button disabled={!busy} onClick={() => void window.quark.call('download.stop').then(() => onStatus('正在暂停队列…')).catch(error => onStatus(error.message,'error'))}>暂停全部</button>
    <button disabled={busy || !tasks.some(task => ['done','cancelled'].includes(task.state))} onClick={() => void window.quark.call('tasks.clear').then(rows => {onTasks(rows);onStatus('已清理结束的任务记录','success');}).catch(error => onStatus(error.message,'error'))}>清理已结束记录</button></div>
    <div className="task-list" ref={scroll}>{!tasks.length && <div className="empty">尚无传输任务，请在文件页下载或上传</div>}
      <div style={{height:virtual.getTotalSize(),position:'relative'}}>{virtual.getVirtualItems().map(item => { const task = tasks[item.index]; return <div className="task-row virtual-task" key={task.id} style={{transform:`translateY(${item.start}px)`}}>
        <span className="task-direction">{task.direction === 'upload' ? '上传' : '下载'}</span><div><strong>{task.name}</strong><p title={task.detail || task.target}>{task.detail || task.target}</p></div><span className={'task-state ' + task.state}>{states[task.state] || task.state}</span>
        {task.needs_authorization ? <button disabled={busy} onClick={() => {setCode('');setAuthorization(task);}}>重新授权</button> : <button disabled={busy || !eligible(task)} onClick={() => void retry([task.id])}>继续</button>}
        <button disabled={['done','cancelled'].includes(task.state)} onClick={() => void cancel([task.id])}>取消</button>
        <button onClick={() => void window.quark.openFolder(task.local_dir || task.target.replace(/[\\/][^\\/]+$/,'')).catch(error => onStatus(error.message))}>打开目录</button></div>; })}</div></div>
    {authorization && <div className="modal-backdrop"><section className="modal" role="dialog" aria-label="重新授权分享"><h2>重新授权分享</h2><p>恢复任务需要重新获取分享授权</p><p className="share-url">{authorization.share_url}</p><input aria-label="恢复分享提取码" value={code} onChange={event => setCode(event.target.value)} placeholder="提取码（如有）"/>
      <div className="dialog-actions"><button disabled={authBusy} onClick={() => setAuthorization(null)}>取消</button><button className="primary" disabled={authBusy} onClick={() => void authorize()}>确认授权</button></div></section></div>}
  </>;
}
