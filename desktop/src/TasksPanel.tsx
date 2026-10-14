import React, { useMemo, useRef, useState } from 'react';
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
  const [direction,setDirection] = useState<'download'|'upload'>(() => tasks.at(-1)?.direction === 'upload' ? 'upload' : 'download');
  const [completed,setCompleted] = useState(() => {
    const latestDirection=tasks.at(-1)?.direction === 'upload' ? 'upload' : 'download';
    return tasks.length > 0 && !tasks.some(task => (task.direction === 'upload' ? 'upload' : 'download') === latestDirection && task.state !== 'done');
  });
  const counts=useMemo(() => {
    const next={download:{pending:0,done:0},upload:{pending:0,done:0}};
    tasks.forEach(task => {next[task.direction === 'upload' ? 'upload' : 'download'][task.state === 'done' ? 'done' : 'pending']++;});
    return next;
  },[tasks]);
  const visible=useMemo(() => tasks.filter(task => (task.direction === 'upload' ? 'upload' : 'download') === direction && (task.state === 'done') === completed),[tasks,direction,completed]);
  const scroll = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count:visible.length,getScrollElement:() => scroll.current,estimateSize:() => 98,overscan:6 });
  function selectDirection(next:'download'|'upload') {setDirection(next);if(scroll.current) scroll.current.scrollTop=0;}
  function selectCompleted(next:boolean) {setCompleted(next);if(scroll.current) scroll.current.scrollTop=0;}
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
  return <><div className="transfer-tabs" role="group" aria-label="传输方向">{(['download','upload'] as const).map(value => <button key={value} aria-pressed={direction === value} className={direction === value ? 'active' : ''} onClick={() => selectDirection(value)}>{value === 'download' ? '下载' : '上传'}<span>{counts[value].pending+counts[value].done}</span></button>)}</div>
    <div className="transfer-filters" role="group" aria-label="传输完成状态"><button aria-pressed={!completed} className={!completed ? 'active' : ''} onClick={() => selectCompleted(false)}>未完成<span>{counts[direction].pending}</span></button><button aria-pressed={completed} className={completed ? 'active' : ''} onClick={() => selectCompleted(true)}>已完成<span>{counts[direction].done}</span></button></div>
    <div className="toolbar"><button disabled={busy || !visible.some(task => eligible(task) && task.state === 'failed')} onClick={() => void retry(visible.filter(task => eligible(task) && task.state === 'failed').map(task => task.id))}>重试失败</button>
    <button disabled={busy || !visible.some(eligible)} onClick={() => void retry(visible.filter(eligible).map(task => task.id))}>继续未完成</button><button disabled={!busy} onClick={() => void window.quark.call('download.stop').then(() => onStatus('正在暂停队列…')).catch(error => onStatus(error.message,'error'))}>暂停全部</button>
    <button disabled={busy || !visible.some(task => ['done','cancelled'].includes(task.state))} onClick={() => void window.quark.call('tasks.clear',{ids:visible.filter(task => ['done','cancelled'].includes(task.state)).map(task => task.id)}).then(rows => {onTasks(rows);onStatus('已清理当前分类中结束的任务记录','success');}).catch(error => onStatus(error.message,'error'))}>清理已结束记录</button></div>
    <div className="task-list" ref={scroll}>{!visible.length && <div className="empty">{!tasks.length ? '尚无传输任务，请在文件页下载或上传' : `暂无${completed ? '已完成' : '未完成'}的${direction === 'upload' ? '上传' : '下载'}任务`}</div>}
      <div style={{height:virtual.getTotalSize(),position:'relative'}}>{virtual.getVirtualItems().map(item => { const task = visible[item.index]; return <div className="task-row virtual-task" key={task.id} style={{transform:`translateY(${item.start}px)`}}>
        <span className="task-direction">{task.direction === 'upload' ? '上传' : '下载'}</span><div><strong>{task.name}</strong><p title={task.detail || task.target}>{task.detail || task.target}</p></div><span className={'task-state ' + task.state}>{states[task.state] || task.state}</span>
        {task.needs_authorization ? <button disabled={busy} onClick={() => {setCode('');setAuthorization(task);}}>重新授权</button> : <button disabled={busy || !eligible(task)} onClick={() => void retry([task.id])}>继续</button>}
        <button disabled={['done','cancelled'].includes(task.state)} onClick={() => void cancel([task.id])}>取消</button>
        <button onClick={() => void window.quark.openFolder(task.local_dir || task.target.replace(/[\\/][^\\/]+$/,'')).catch(error => onStatus(error.message))}>打开目录</button></div>; })}</div></div>
    {authorization && <div className="modal-backdrop"><section className="modal" role="dialog" aria-label="重新授权分享"><h2>重新授权分享</h2><p>恢复任务需要重新获取分享授权</p><p className="share-url">{authorization.share_url}</p><input aria-label="恢复分享提取码" value={code} onChange={event => setCode(event.target.value)} placeholder="提取码（如有）"/>
      <div className="dialog-actions"><button disabled={authBusy} onClick={() => setAuthorization(null)}>取消</button><button className="primary" disabled={authBusy} onClick={() => void authorize()}>确认授权</button></div></section></div>}
  </>;
}
