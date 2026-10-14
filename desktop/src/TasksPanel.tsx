import React, { useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Task } from './types';
import { Feedback } from './Feedback';
import { formatSize } from './size';

const states: Record<string,string> = { waiting:'等待中',running:'传输中',done:'已完成',failed:'失败',stopped:'已暂停',cancelled:'已取消' };
const phases:Record<string,string>={starting:'正在开始',resuming:'正在续传',transferring:'正在传输',part_confirmed:'分段已确认',checkpoint:'进度已保存',completed:'数据已传输，等待完成确认',non_resumable:'单线程传输，停止后需重新下载',saving:'正在临时转存',cleaning:'正在清理临时目录'};
function remainingTime(seconds:number) {
  const value=Math.ceil(seconds);
  if(value >= 3600) return `约 ${Math.floor(value/3600)} 小时 ${Math.floor(value%3600/60)} 分`;
  if(value >= 60) return `约 ${Math.floor(value/60)} 分 ${value%60} 秒`;
  return `约 ${value} 秒`;
}
function TransferProgress({task}:{task:Task}) {
  const progress=task.progress;
  if(!progress) return <p title={task.target}>{task.state === 'running' ? '正在准备传输…' : task.state === 'waiting' ? '等待进入传输队列' : task.detail || task.target}</p>;
  const active=task.state === 'running';
  const phase=phases[progress.phase] || '正在处理';
  const bytes=active || task.state === 'done' ? progress.transferred : progress.confirmed;
  const total=progress.total;
  const known=total !== null && Number.isFinite(total) && total > 0;
  const fraction=known ? Math.min(100,Math.max(0,bytes/total*100)) : total === 0 && task.state === 'done' ? 100 : null;
  const rate=active && Number.isFinite(progress.rate) && progress.rate > 0 ? progress.rate : 0;
  const eta=known && rate > 0 && bytes < total && !['saving','cleaning','completed'].includes(progress.phase) ? (total-bytes)/rate : null;
  const connections=active && (task.direction || 'download') === 'download' && !['saving','cleaning','completed'].includes(progress.phase);
  return <div className="task-progress">
    <p className="progress-file" title={progress.name}>{active && !['saving','cleaning'].includes(progress.phase) ? `${progress.recursive ? '目录总进度 · ' : ''}当前文件：${progress.name}` : progress.recursive ? '目录总进度' : '文件传输进度'}</p>
    <div className="progress-line"><div className={'progress-track'+(fraction === null && active ? ' indeterminate' : '')} role="progressbar" aria-label={`${task.name}传输进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={fraction ?? undefined} aria-valuetext={fraction === null ? total === 0 ? '空文件' : '总量未知' : `${Math.floor(fraction*10)/10}%`}><span style={{width:fraction === null ? undefined : `${fraction}%`}}/></div><span>{fraction === null ? total === 0 ? '空文件' : '总量未知' : `${Math.floor(fraction*10)/10}%`}</span></div>
    <div className="progress-metrics"><span>{active || task.state === 'done' ? '已传输' : '已确认'} {formatSize(bytes)} / {formatSize(total)}</span><span>速度 {rate ? `${formatSize(rate)}/s` : '—'}</span>{connections && <span>连接 {progress.connections}</span>}<span>预计剩余 {eta !== null && Number.isFinite(eta) ? remainingTime(eta) : '—'}</span></div>
    <p className="progress-phase">{active ? phase : task.detail || states[task.state]}</p>
  </div>;
}
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
  const virtual = useVirtualizer({ count:visible.length,getScrollElement:() => scroll.current,estimateSize:() => 142,overscan:6 });
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
        <span className="task-direction">{task.direction === 'upload' ? '上传' : '下载'}</span><div><strong title={task.name}>{task.name}</strong><TransferProgress task={task}/></div><span className={'task-state ' + task.state}>{states[task.state] || task.state}</span>
        {task.needs_authorization ? <button disabled={busy} onClick={() => {setCode('');setAuthorization(task);}}>重新授权</button> : <button disabled={busy || !eligible(task)} onClick={() => void retry([task.id])}>继续</button>}
        <button disabled={['done','cancelled'].includes(task.state)} onClick={() => void cancel([task.id])}>取消</button>
        <button onClick={() => void window.quark.openFolder(task.local_dir || task.target.replace(/[\\/][^\\/]+$/,'')).catch(error => onStatus(error.message))}>打开目录</button></div>; })}</div></div>
    {authorization && <div className="modal-backdrop"><section className="modal" role="dialog" aria-label="重新授权分享"><h2>重新授权分享</h2><p>恢复任务需要重新获取分享授权</p><p className="share-url">{authorization.share_url}</p><input aria-label="恢复分享提取码" value={code} onChange={event => setCode(event.target.value)} placeholder="提取码（如有）"/>
      <div className="dialog-actions"><button disabled={authBusy} onClick={() => setAuthorization(null)}>取消</button><button className="primary" disabled={authBusy} onClick={() => void authorize()}>确认授权</button></div></section></div>}
  </>;
}
