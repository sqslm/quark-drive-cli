import React, { useMemo, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Task } from './types';
import { Feedback } from './Feedback';
import { formatSize } from './size';

const TASK_ROW_HEIGHT=60;
const TASK_HEADER_HEIGHT=36;

const states: Record<string,string> = { waiting:'等待中',running:'传输中',done:'已完成',failed:'失败',stopped:'已暂停',cancelled:'已取消' };
const phases:Record<string,string>={starting:'正在开始',resuming:'正在续传',transferring:'正在传输',part_confirmed:'分段已确认',checkpoint:'进度已保存',completed:'数据已传输，等待完成确认',non_resumable:'单线程传输，停止后需重新下载',saving:'正在临时转存',cleaning:'正在清理临时目录',hashing:'正在计算文件哈希',hashing_context:'正在准备分片上下文',authorizing:'正在获取上传授权',verifying:'正在核验上传结果',merging:'正在合并上传分片'};
function recordDate(value:number|null|undefined) {
  if(value == null || !Number.isFinite(value) || value < 0) return '未记录';
  const date=new Date(value);
  if(!Number.isFinite(date.getTime())) return '未记录';
  const pad=(n:number) => String(n).padStart(2,'0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function elapsedTime(value:number|null|undefined) {
  if(value == null || !Number.isFinite(value) || value < 0) return '未记录';
  const seconds=Math.floor(value);
  if(seconds===0) return value>0 ? '不足 1 秒' : '0 秒';
  const parts=[];
  if(seconds>=3600) parts.push(`${Math.floor(seconds/3600)} 小时`);
  if(seconds%3600>=60) parts.push(`${Math.floor(seconds%3600/60)} 分`);
  if(seconds%60) parts.push(`${seconds%60} 秒`);
  return parts.join(' ');
}
function TaskRecordInfo({task}:{task:Task}) {
  const detail=`加入：${recordDate(task.created_at)}\n首次开始：${recordDate(task.started_at)}\n累计耗时：${elapsedTime(task.elapsed_seconds)}\n${task.direction === 'upload' ? '上传到' : '下载到'}：${task.target}\n时间按本机时区显示`;
  const size=task.progress ? formatSize(task.progress.total) : task.detail.match(/[：:]\s*(\d+(?:\.\d+)?\s*(?:B|KiB|MiB|GiB|TiB|KB|MB|GB|TB))$/)?.[1] || '—';
  return <>
    <span className="task-cell task-number task-total" role="cell">{size}</span>
    <div className="task-cell task-record-line" role="cell" title={detail}><span aria-label={`完成时间：${recordDate(task.finished_at)}`}>{recordDate(task.finished_at)}</span><small className="task-subline" aria-label={`累计耗时：${elapsedTime(task.elapsed_seconds)}`}>耗时：{elapsedTime(task.elapsed_seconds)}</small></div>
    <span className="task-cell task-state done" role="cell">已完成</span>
  </>;
}
function TransferProgress({task}:{task:Task}) {
  const progress=task.progress;
  const active=task.state === 'running' && !task.pausing && !task.removing;
  const uploadStarted=task.direction === 'upload' && !!progress && progress.transferred > 0;
  const preparing=!!progress && !uploadStarted && ['hashing','hashing_context','authorizing','verifying','merging'].includes(progress.phase);
  const rate=active && !preparing && progress && Number.isFinite(progress.rate) && progress.rate > 0 ? progress.rate : 0;
  const rateText=rate ? `${formatSize(rate)}/s` : '—';
  let status=states[task.state] || task.state;
  if(task.pausing || task.removing) status='正在安全停止…';
  else if(active) {
    const finishing=uploadStarted && ((progress!.total !== null && progress!.total > 0 && progress!.confirmed >= progress!.total) || (!progress!.recursive && ['verifying','merging','completed'].includes(progress!.phase)));
    status=finishing ? '正在完成上传…' : preparing || !progress ? '准备中' : ['saving','cleaning'].includes(progress.phase) ? phases[progress.phase] : task.direction === 'upload' ? '正在上传' : '正在下载';
  }
  const detail=task.detail || (progress ? phases[progress.phase] : '') || status;
  const bytes=progress ? active ? progress.transferred : progress.confirmed : null;
  const total=progress?.total ?? null;
  const percent=bytes !== null && total !== null && total > 0 ? `${Math.floor(Math.min(100,Math.max(0,bytes/total*100)))}%` : '—';
  const amount=progress ? `${formatSize(bytes)} / ${formatSize(total)}` : '';

  return <>
    <div className="task-cell task-number task-progress" role="cell" title={amount}><span>{percent}</span>{amount && <small className="task-subline">{amount}</small>}</div>
    <span className="task-cell task-number task-rate" role="cell" aria-label={`速度 ${rateText}`}>{rateText}</span>
    <span className={'task-cell task-state '+task.state} role="cell" title={detail}>{status}</span>
  </>;
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
  const [query,setQuery]=useState('');
  const keyword=query.trim().toLocaleLowerCase();
  const counts=useMemo(() => {
    const next={download:{pending:0,done:0},upload:{pending:0,done:0}};
    tasks.forEach(task => {next[task.direction === 'upload' ? 'upload' : 'download'][task.state === 'done' ? 'done' : 'pending']++;});
    return next;
  },[tasks]);
  const visible=useMemo(() => tasks.filter(task => (task.direction === 'upload' ? 'upload' : 'download') === direction && (task.state === 'done') === completed && task.name.toLocaleLowerCase().includes(keyword)),[tasks,direction,completed,keyword]);
  const completedInDirection=useMemo(() => tasks.filter(task => (task.direction === 'upload' ? 'upload' : 'download') === direction && task.state === 'done'),[tasks,direction]);
  const scroll = useRef<HTMLDivElement>(null);
  const virtual = useVirtualizer({ count:visible.length,getScrollElement:() => scroll.current,estimateSize:() => TASK_ROW_HEIGHT,scrollMargin:TASK_HEADER_HEIGHT,overscan:6,getItemKey:index => visible[index].id });
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
    <div className="toolbar task-toolbar"><button disabled={commandBusy || !tasks.some(eligible)} onClick={() => void command(() => start(tasks.filter(eligible).map(task => task.id)))}>开始全部</button>
      <button disabled={commandBusy || !busy} onClick={() => void command(async () => {await window.quark.call('download.stop');onStatus('正在暂停整个队列…');})}>暂停全部</button>
      <button disabled={commandBusy || busy || !completedInDirection.length} onClick={() => void command(async () => {onTasks(await window.quark.call('tasks.clear',{ids:completedInDirection.map(task => task.id)}));onStatus(`已清理${direction === 'upload' ? '上传' : '下载'}已完成记录`,'success');})}>清理已完成记录</button>
      <input type="search" className="task-search" aria-label="按文件名筛选传输记录" placeholder="筛选文件名" value={query} onChange={event => {setQuery(event.target.value);if(scroll.current) scroll.current.scrollTop=0;}}/>
    </div>
    <div className="task-list" ref={scroll}>
      <div className={"task-table "+(completed ? "task-completed" : "task-pending")} role="table" aria-label={`${direction === 'upload' ? '上传' : '下载'}任务`} aria-colcount={5} aria-rowcount={visible.length+1}>
        <div className="task-header task-grid" role="row" aria-rowindex={1}>
          <span className="task-name" role="columnheader">文件名</span>
          {completed ? <><span className="task-number" role="columnheader">大小</span><span role="columnheader">传输时间</span><span role="columnheader">完成状态</span></> : <><span className="task-number" role="columnheader">进度</span><span className="task-number" role="columnheader">{direction === 'upload' ? '上传速度' : '下载速度'}</span><span role="columnheader">状态</span></>}
          <span className="task-controls" role="columnheader">操作</span>
        </div>
        {!visible.length && <div className="empty">{keyword ? '没有匹配的传输记录' : !tasks.length ? '尚无传输任务，请在文件页下载或上传' : `暂无${completed ? '已完成' : '未完成'}的${direction === 'upload' ? '上传' : '下载'}任务`}</div>}
        <div role="rowgroup" style={{height:virtual.getTotalSize(),position:'relative'}}>{virtual.getVirtualItems().map(item => { const task = visible[item.index]; return <div className="task-row virtual-task task-grid" role="row" aria-rowindex={item.index+2} key={task.id} style={{transform:`translateY(${item.start-TASK_HEADER_HEIGHT}px)`}}>
          <div className="task-name" role="cell"><strong title={task.name}>{task.name}</strong>
            <span className={'task-subline'+(task.state === 'failed' ? ' task-error' : '')} title={task.state === 'failed' ? task.detail : task.target}>{task.state === 'failed' ? task.detail || '传输失败，请重试' : !completed && task.progress?.recursive && task.state === 'running' ? `当前文件：${task.progress.name}` : `${task.direction === 'upload' ? '上传到' : '下载到'}：${task.target}`}</span>
          </div>
          {completed ? <TaskRecordInfo task={task}/> : <TransferProgress task={task}/>}
        <div className="task-controls" role="cell">
          {!completed && <button className="task-icon-button" title={task.needs_authorization ? '重新授权并开始' : task.pausing ? '正在暂停' : task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? '暂停' : '开始'} aria-label={task.needs_authorization ? '重新授权并开始' : task.pausing ? '正在暂停' : task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? '暂停' : '开始'} disabled={commandBusy || task.pausing || task.removing || (!eligible(task) && !task.needs_authorization && task.state !== 'running' && !(task.state === 'waiting' && task.scheduled))} onClick={() => {
            if(task.needs_authorization) {setCode('');setAuthorization(task);}
            else void command(() => task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? pause([task.id]) : start([task.id]));
          }}><TaskActionIcon kind={task.state === 'running' || (task.state === 'waiting' && task.scheduled) ? 'pause' : 'play'}/></button>}
          <button className="task-icon-button" title="打开文件夹" aria-label="打开文件夹" onClick={() => void command(async () => {await window.quark.openFolder(task.local_dir || task.target.replace(/[\\/][^\\/]+$/,''));})}><TaskActionIcon kind="folder"/></button>
          <button className="task-icon-button" title="删除记录（保留文件）" aria-label="删除记录" disabled={commandBusy || task.removing} onClick={() => void command(() => remove(task))}><TaskActionIcon kind="trash"/></button>
        </div></div>; })}</div></div></div>
    {authorization && <div className="modal-backdrop"><section className="modal" role="dialog" aria-label="重新授权分享"><h2>重新授权分享</h2><p>恢复任务需要重新获取分享授权</p><p className="share-url">{authorization.share_url}</p><input aria-label="恢复分享提取码" value={code} onChange={event => setCode(event.target.value)} placeholder="提取码（如有）"/>
      <div className="dialog-actions"><button disabled={authBusy} onClick={() => setAuthorization(null)}>取消</button><button className="primary" disabled={authBusy} onClick={() => void authorize()}>确认授权</button></div></section></div>}
  </>;
}
