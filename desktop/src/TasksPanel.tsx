import React, { useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Task } from './types';
import { Feedback } from './Feedback';
import { formatSize } from './size';

const states: Record<string,string> = { waiting:'等待中',running:'传输中',done:'已完成',failed:'失败',stopped:'已暂停',cancelled:'已取消' };
const phases:Record<string,string>={starting:'正在开始',resuming:'正在续传',transferring:'正在传输',part_confirmed:'分段已确认',checkpoint:'进度已保存',completed:'数据已传输，等待完成确认',non_resumable:'单线程传输，停止后需重新下载',saving:'正在临时转存',cleaning:'正在清理临时目录',hashing:'正在计算文件哈希',hashing_context:'正在准备分片上下文',authorizing:'正在获取上传授权',verifying:'正在核验上传结果',merging:'正在合并上传分片'};
function remainingTime(seconds:number) {
  const value=Math.ceil(seconds);
  if(value >= 3600) return `约 ${Math.floor(value/3600)} 小时 ${Math.floor(value%3600/60)} 分`;
  if(value >= 60) return `约 ${Math.floor(value/60)} 分 ${value%60} 秒`;
  return `约 ${value} 秒`;
}
function TransferProgress({task}:{task:Task}) {
  const progress=task.progress;
  if(!progress) return <p title={task.target}>{task.pausing || task.removing ? '正在安全停止…' : task.state === 'running' ? '正在准备传输…' : task.state === 'waiting' ? '等待进入传输队列' : task.detail || task.target}</p>;
  const active=task.state === 'running' && !task.pausing && !task.removing;
  const bytes=active || task.state === 'done' ? progress.transferred : progress.confirmed;
  const total=progress.total;
  const known=total !== null && Number.isFinite(total) && total > 0;
  const uploadStarted=task.direction === 'upload' && progress.transferred > 0;
  const uploadFinishing=uploadStarted && ((known && progress.confirmed >= total) || (!progress.recursive && ['verifying','merging','completed'].includes(progress.phase)));
  const preparing=!uploadStarted && ['hashing','hashing_context','authorizing','verifying','merging'].includes(progress.phase);
  const phase=uploadStarted ? uploadFinishing ? '正在完成上传…' : '正在上传' : phases[progress.phase] || '正在处理';
  const fraction=preparing && active && bytes === 0 ? null : known ? Math.min(100,Math.max(0,bytes/total*100)) : total === 0 && task.state === 'done' ? 100 : null;
  const rate=active && !preparing && Number.isFinite(progress.rate) && progress.rate > 0 ? progress.rate : 0;
  const eta=known && rate > 0 && bytes < total && !uploadFinishing && !['saving','cleaning','completed'].includes(progress.phase) ? (total-bytes)/rate : null;
  const connections=active && !preparing && (task.direction || 'download') === 'download' && !['saving','cleaning','completed'].includes(progress.phase);
  return <div className="task-progress">
    <p className="progress-file" title={progress.name}>{active && !['saving','cleaning'].includes(progress.phase) ? `${progress.recursive ? '目录总进度 · ' : ''}当前文件：${progress.name}` : progress.recursive ? '目录总进度' : '文件传输进度'}</p>
    <div className="progress-line"><div className={'progress-track'+(fraction === null && active ? ' indeterminate' : '')} role="progressbar" aria-label={`${task.name}传输进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={fraction ?? undefined} aria-valuetext={fraction === null ? preparing ? phase : total === 0 ? '空文件' : '总量未知' : `${Math.floor(fraction*10)/10}%`}><span style={{width:fraction === null ? undefined : `${fraction}%`}}/></div><span>{fraction === null ? preparing ? phase : total === 0 ? '空文件' : '总量未知' : `${Math.floor(fraction*10)/10}%`}</span></div>
    <div className="progress-metrics"><span>{active || task.state === 'done' ? '已传输' : '已确认'} {formatSize(bytes)} / {formatSize(total)}</span><span>速度 {rate ? `${formatSize(rate)}/s` : '—'}</span>{connections && <span>连接 {progress.connections}</span>}<span>预计剩余 {eta !== null && Number.isFinite(eta) ? remainingTime(eta) : '—'}</span></div>
    <p className="progress-phase">{task.removing || task.pausing ? '正在安全停止…' : active ? phase : task.detail || states[task.state]}</p>
  </div>;
}
function TaskActionIcon({kind}:{kind:'play'|'pause'|'folder'|'trash'}) {
  const paths={play:'M8 5l11 7-11 7V5Z',pause:'M8 5v14M16 5v14',folder:'M3 7V5h7l2 3h9v11H3V7Z',trash:'M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 10v7M14 10v7'};
  return <svg width="18" height="18" viewBox="0 0 24 24" fill={kind === 'play' ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth={kind === 'pause' ? 3 : 1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[kind]}/></svg>;
}
export function TasksPanel({ tasks,busy,onTasks,onStart,onEnd,onStatus }: {
  tasks:Task[]; busy:boolean; onTasks:(tasks:Task[]) => void; onStart:() => void; onEnd:() => void; onStatus:Feedback;
}) {
  const [authorization,setAuthorization] = useState<Task | null>(null);
  const [code,setCode] = useState('');
  const [authBusy,setAuthBusy] = useState(false);
  const commandPending=useRef(false);
  const [commandBusy,setCommandBusy]=useState(false);
  async function command(work:()=>Promise<void>) {
    if(commandPending.current) return;
    commandPending.current=true;setCommandBusy(true);
    try {await work();} catch(error) {onStatus(error instanceof Error ? error.message : '任务操作失败','error');}
    finally {commandPending.current=false;setCommandBusy(false);}
  }
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
  const completedInDirection=useMemo(() => tasks.filter(task => (task.direction === 'upload' ? 'upload' : 'download') === direction && task.state === 'done'),[tasks,direction]);
  const scroll = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count:visible.length,getScrollElement:() => scroll.current,estimateSize:() => 142,overscan:6 });
  function selectDirection(next:'download'|'upload') {setDirection(next);if(scroll.current) scroll.current.scrollTop=0;}
  function selectCompleted(next:boolean) {setCompleted(next);if(scroll.current) scroll.current.scrollTop=0;}
  const eligible = (task:Task) => task.valid && !task.pausing && !task.removing && ['failed','stopped','waiting'].includes(task.state) && !(task.state === 'waiting' && task.scheduled);
  async function start(ids:string[]) {
    const alreadyRunning=busy;onStart();
    try {onTasks(await window.quark.call('tasks.retry',{ids}));onStatus('任务已加入队列','success');}
    catch(error) {if(!alreadyRunning) onEnd();throw error;}
  }
  async function pause(ids:string[]) {
    onTasks(await window.quark.call('tasks.pause',{ids}));onStatus('正在暂停选中任务，队列中的其他任务继续运行');
  }
  async function remove(task:Task) {
    onTasks(await window.quark.call('tasks.remove',{ids:[task.id]}));
    onStatus(task.scheduled || task.state === 'running' ? '正在安全停止并删除记录；文件保留' : '已删除任务记录，文件保留','success');
  }
  async function authorize() {
    setAuthBusy(true);
    try {
      const share = await window.quark.call('share.open',{text:authorization!.share_url,passcode:code});
      onTasks(await window.quark.call('tasks.authorize',{handle:share.handle,ids:[authorization!.id]}));
      const taskId=authorization!.id;
      setAuthorization(null); setCode(''); await start([taskId]);
    } catch(error) { onStatus(error instanceof Error ? error.message : '分享授权失败','error'); }
    finally { setAuthBusy(false); }
  }
  return <><div className="transfer-tabs" role="group" aria-label="传输方向">{(['download','upload'] as const).map(value => <button key={value} aria-pressed={direction === value} className={direction === value ? 'active' : ''} onClick={() => selectDirection(value)}>{value === 'download' ? '下载' : '上传'}<span>{counts[value].pending+counts[value].done}</span></button>)}</div>
    <div className="transfer-filters" role="group" aria-label="传输完成状态"><button aria-pressed={!completed} className={!completed ? 'active' : ''} onClick={() => selectCompleted(false)}>未完成<span>{counts[direction].pending}</span></button><button aria-pressed={completed} className={completed ? 'active' : ''} onClick={() => selectCompleted(true)}>已完成<span>{counts[direction].done}</span></button></div>
    <div className="toolbar"><button disabled={commandBusy || !tasks.some(eligible)} onClick={() => void command(() => start(tasks.filter(eligible).map(task => task.id)))}>开始全部</button>
      <button disabled={commandBusy || !busy} onClick={() => void command(async () => {await window.quark.call('download.stop');onStatus('正在暂停整个队列…');})}>暂停全部</button>
      <button disabled={commandBusy || busy || !completedInDirection.length} onClick={() => void command(async () => {onTasks(await window.quark.call('tasks.clear',{ids:completedInDirection.map(task => task.id)}));onStatus(`已清理${direction === 'upload' ? '上传' : '下载'}已完成记录`,'success');})}>清理已完成记录</button></div>
    <div className="task-list" ref={scroll}>{!visible.length && <div className="empty">{!tasks.length ? '尚无传输任务，请在文件页下载或上传' : `暂无${completed ? '已完成' : '未完成'}的${direction === 'upload' ? '上传' : '下载'}任务`}</div>}
      <div style={{height:virtual.getTotalSize(),position:'relative'}}>{virtual.getVirtualItems().map(item => { const task = visible[item.index]; return <div className="task-row virtual-task" key={task.id} style={{transform:`translateY(${item.start}px)`}}>
        <span className="task-direction">{task.direction === 'upload' ? '上传' : '下载'}</span><div><strong title={task.name}>{task.name}</strong><TransferProgress task={task}/></div><span className={'task-state ' + task.state}>{states[task.state] || task.state}</span>
        <div className="task-controls">
          <button className="task-icon-button" title={task.needs_authorization ? '重新授权并开始' : task.pausing ? '正在暂停' : task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? '暂停' : '开始'} aria-label={task.needs_authorization ? '重新授权并开始' : task.pausing ? '正在暂停' : task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? '暂停' : '开始'} disabled={commandBusy || task.pausing || task.removing || (!eligible(task) && !task.needs_authorization && task.state !== 'running' && !(task.state === 'waiting' && task.scheduled))} onClick={() => {
            if(task.needs_authorization) {setCode('');setAuthorization(task);}
            else void command(() => task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? pause([task.id]) : start([task.id]));
          }}><TaskActionIcon kind={task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? 'pause' : 'play'}/></button>
          <button className="task-icon-button" title="打开文件夹" aria-label="打开文件夹" onClick={() => void command(async () => {await window.quark.openFolder(task.local_dir || task.target.replace(/[\\/][^\\/]+$/,''));})}><TaskActionIcon kind="folder"/></button>
          <button className="task-icon-button" title="删除记录（保留文件）" aria-label="删除记录" disabled={commandBusy || task.removing} onClick={() => void command(() => remove(task))}><TaskActionIcon kind="trash"/></button>
        </div></div>; })}</div></div>
    {authorization && <div className="modal-backdrop"><section className="modal" role="dialog" aria-label="重新授权分享"><h2>重新授权分享</h2><p>恢复任务需要重新获取分享授权</p><p className="share-url">{authorization.share_url}</p><input aria-label="恢复分享提取码" value={code} onChange={event => setCode(event.target.value)} placeholder="提取码（如有）"/>
      <div className="dialog-actions"><button disabled={authBusy} onClick={() => setAuthorization(null)}>取消</button><button className="primary" disabled={authBusy} onClick={() => void authorize()}>确认授权</button></div></section></div>}
  </>;
}
