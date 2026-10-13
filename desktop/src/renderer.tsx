import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Entry, Event, Preferences, Task } from './types';
import './style.css';
import { FileDialog, FileAction } from './FileDialog';

const labels: Record<string, string> = { waiting: '等待中', running: '传输中', done: '已完成', failed: '失败', stopped: '已暂停' };
const units = (bytes: number | null) => bytes === null ? '—' : bytes < 1024 ? `${bytes} B` :
  bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KiB` : bytes < 1073741824 ? `${(bytes / 1048576).toFixed(1)} MiB` : `${(bytes / 1073741824).toFixed(2)} GiB`;
function FileIcon({ folder }: { folder: boolean }) {
  return <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    {folder ? <path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3V7Z" fill="#fff0c6" stroke="#c49b37" strokeWidth="1.5" /> :
      <><path d="M6 3h8l4 4v14H6V3Z" fill="#eef3ff" stroke="#8193ba" strokeWidth="1.5" /><path d="M14 3v5h4M9 12h6M9 16h6" stroke="#8193ba" strokeWidth="1.5" /></>}
  </svg>;
}
function App() {
  const [page, setPage] = useState('drive');
  const [nickname, setNickname] = useState('未登录');
  const [rows, setRows] = useState<Entry[]>([]);
  const [trail, setTrail] = useState<[string, string][]>([['0', '我的网盘']]);
  const [share, setShare] = useState<string | null>(null);
  const [sharePath, setSharePath] = useState('/');
  const [shareText, setShareText] = useState('');
  const [passcode, setPasscode] = useState('');
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState('global');
  const [kind, setKind] = useState('all');
  const [extensions, setExtensions] = useState('');
  const [extensionGroups, setExtensionGroups] = useState<Record<string, string[]>>({});
  const [sort, setSort] = useState<{ key: 'name' | 'size' | 'path'; descending: boolean } | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [searching, setSearching] = useState(false);
  const [searchView, setSearchView] = useState(false);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState('正在启动网盘后端…');
  const [preferences, setPreferences] = useState<Preferences>({ output_dir: '', connections: 4, limit_rate: null, width: 1180, height: 780 });
  const [threads, setThreads] = useState('4');
  const [rate, setRate] = useState('0');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [transferring, setTransferring] = useState(false);
  const [qr, setQr] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);
  const [fileDialog,setFileDialog] = useState<{ action:FileAction; entries:Entry[] } | null>(null);
  const [context,setContext] = useState<{ row:Entry; x:number; y:number } | null>(null);
  const view = useRef(0);
  const searchId = useRef<string | null>(null);
  const loginId = useRef<string | null>(null);
  const shareRevision = useRef(0);
  const codeRevision = useRef(0);
  const scroll = useRef<HTMLDivElement>(null);
  const call = window.quark.call;
  function acceptTasks(next: Task[]) {
    setTasks(old => next.map(task => {
      const previous = old.find(row => row.id === task.id);
      return previous && (previous.revision || 0) > (task.revision || 0) ? previous : task;
    }));
  }
  async function action<T>(work: () => Promise<T>): Promise<T | undefined> {
    try { return await work(); } catch (error) { setStatus(error instanceof Error ? error.message : '操作失败'); }
  }
  function cancelSearch() {
    searchId.current = null; setSearching(false);
    void action(() => call('search.cancel'));
  }
  async function browse(chain: [string, string][] = trail) {
    cancelSearch(); const generation = ++view.current; setLoading(true); setSearchView(false); setChecked(new Set());
    const result = await action<Entry[]>(() => call('browse', { fid: chain.at(-1)![0] }));
    if (generation === view.current) {
      setLoading(false);
      if (result) { setRows(result); setTrail(chain); setShare(null); setQuery(''); setStatus(`已读取 ${result.length} 项`); }
    }
  }
  async function openRow(row: Entry) {
    if (share) {
      if (!row.directory) return;
      const generation = ++view.current; cancelSearch(); setLoading(true);
      const result = await action<Entry[]>(() => call('share.browse', { handle: share, path: row.path }));
      if (generation === view.current) { setLoading(false); if (result) { setRows(result); setSharePath(row.path); setQuery(''); setChecked(new Set()); } }
    } else if (searchView && !row.location.length) {
      const generation = view.current;
      setStatus('正在定位文件…');
      const found = await action<Entry>(() => call('locate', { entry: row }));
      if (!found || generation !== view.current) return;
      setRows(old => old.map(entry => entry.fid === row.fid ? found : entry));
      if (row.directory) await browse(found.location); else setStatus(found.path);
    } else if (row.directory) {
      await browse(row.location.length ? row.location : [...trail, [row.fid, row.name]]);
    } else if (row.path) setStatus(row.path);
  }
  async function shareUp() {
    if (!share || sharePath === '/') return;
    const parent = sharePath.slice(0, sharePath.lastIndexOf('/')) || '/';
    const generation = ++view.current; setLoading(true);
    const result = await action<Entry[]>(() => call('share.browse', { handle: share, path: parent }));
    if (generation === view.current) { setLoading(false); if (result) { setRows(result); setSharePath(parent); setQuery(''); setChecked(new Set()); } }
  }
  async function runSearch() {
    if (!query.trim()) { setStatus('请输入搜索关键词'); return; }
    const id = crypto.randomUUID(); searchId.current = id; ++view.current;
    setSearching(true); setSearchView(true); setChecked(new Set()); setStatus('正在搜索…');
    let received = false;
    const off = window.quark.onEvent(event => {
      if (event.id !== id || searchId.current !== id || event.event !== 'search.page') return;
      const data = event.data as { entries: Entry[]; scanned: number };
      if (data.entries.length) {
        const first = !received;
        setRows(old => first ? data.entries : [...old, ...data.entries]); received = true;
      }
      setStatus(scope === 'global' ? '正在读取搜索结果…' : `正在搜索，已扫描 ${data.scanned} 个目录`);
    });
    try {
      await call('search', { query: query.trim(), fid: scope === 'global' ? '0' : trail.at(-1)![0], recursive: scope !== 'global' }, id);
      if (searchId.current === id) { if (!received) setRows([]); setStatus('搜索完成'); }
    } catch (error) {
      if (searchId.current === id) setStatus(`${error instanceof Error ? error.message : '搜索失败'}${received ? '；结果不完整' : ''}`);
    } finally { off(); if (searchId.current === id) { searchId.current = null; setSearching(false); } }
  }
  async function openShare() {
    const generation = ++view.current; cancelSearch(); setLoading(true);
    const result = await action<{ handle: string; entries: Entry[] }>(() => call('share.open', { text: shareText, passcode }));
    if (generation !== view.current) return;
    setLoading(false);
    if (result) { setShare(result.handle); setSharePath('/'); setRows(result.entries); setChecked(new Set()); setQuery(''); setSearchView(false); setPage('drive'); setStatus('分享只读浏览；下载后清理临时转存'); }
  }
  async function saveSettings(folder = preferences.output_dir) {
    const n = Number(threads), limit = Number(rate);
    if (!threads.trim() || !Number.isInteger(n) || n < 1 || n > 16 || !rate.trim() || !Number.isFinite(limit) || (limit !== 0 && (limit < .01 || limit > 10240))) {
      setStatus('线程数为 1–16；限速为 0 或 0.01–10240 MiB/s'); return false;
    }
    const next = { ...preferences, output_dir: folder, connections: n, limit_rate: limit ? Math.floor(limit * 1048576) : null };
    const saved = await action<{ preferences: Preferences; warning: string }>(() => call('preferences.save', next));
    if (saved) { setPreferences(saved.preferences); setStatus(saved.warning || '设置已保存'); return true; } return false;
  }
  useEffect(() => {
    const off = window.quark.onEvent((event: Event) => {
      if (event.event === 'task') {
        const task = event.data as Task;
        setTasks(old => old.some(row => row.id === task.id) ? old.map(row => row.id === task.id ? task : row) : [...old, task]);
      } else if (event.event === 'batch.finished') { setTransferring(false); setStatus('本批次已结束，请查看逐项结果'); }
      else if (event.event === 'login.qr' && event.id === loginId.current) setQr(event.data as string);
      else if (event.event === 'warning') setStatus(event.data as string);
      else if (event.event === 'backend.error') { setStatus(event.data as string); setTransferring(false); setSearching(false); }
    });
    void action(async () => {
      const hello = await call('hello');
      if (hello.protocol !== 1) throw new Error('后端协议版本不支持');
      setPreferences(hello.preferences); setThreads(String(hello.preferences.connections)); setRate(String(hello.preferences.limit_rate ? Math.max(.01, hello.preferences.limit_rate / 1048576) : 0));
      setExtensionGroups(hello.extension_groups || {});
      await browse([['0', '我的网盘']]);
      if (hello.warning) setStatus(hello.warning);
      void action(async () => setNickname((await call('account')).nickname));
    });
    return off;
  }, []);
  const filtered = useMemo(() => {
    let result = rows.filter(row => (!query || row.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())) &&
      (kind === 'all' || row.directory === (kind === 'directories')));
    if (extensions.trim()) {
      const suffixes = extensionGroups[extensions] || extensions.toLocaleLowerCase().split(/[\s,，;；、]+/).filter(Boolean).map(value => value.replace(/^\./, ''));
      if (extensions === '无扩展名') result = result.filter(row => !row.directory && (row.name.lastIndexOf('.') <= 0 || row.name.endsWith('.')));
      else {
      if (suffixes.some(value => !/^[a-z0-9]+(?:\.[a-z0-9]+)*$/.test(value))) return [];
      result = result.filter(row => !row.directory && suffixes.some(value => row.name.toLocaleLowerCase().endsWith('.' + value)));
      }
    }
    if (sort) result = [...result].sort((a,b) => {
      if (a.directory !== b.directory) return a.directory ? -1 : 1;
      if (sort.key === 'size') {
        if (a.directory) return a.name.localeCompare(b.name);
        if (a.size === null || b.size === null) return a.size === b.size ? 0 : a.size === null ? 1 : -1;
        return (a.size-b.size) * (sort.descending ? -1 : 1);
      }
      return a[sort.key].localeCompare(b[sort.key], undefined, { sensitivity: 'base' }) * (sort.descending ? -1 : 1);
    });
    return result;
  }, [rows, query, kind, extensions, sort, extensionGroups]);
  const virtualizer = useVirtualizer({ count: filtered.length, getScrollElement: () => scroll.current, estimateSize: () => 46, overscan: 10 });
  const selected = filtered.filter(row => checked.has(row.fid));
  function fileAction(action:FileAction, entries = selected) { setContext(null); setFileDialog({ action,entries }); }
  async function copyPaths(entries = selected) {
    await action(async () => {
      const generation = view.current;
      const located: Entry[] = [];
      for(const row of entries) located.push(row.path ? row : await call('locate',{ entry:row }));
      if(generation !== view.current) return;
      await window.quark.copyText(located.map(row => row.path).join('\n'));
      setStatus(`已复制 ${located.length} 项路径`);
    });
  }
  const all = filtered.length > 0 && selected.length === filtered.length;
  function toggle(fid: string) { setChecked(old => { const next = new Set(old); if (next.has(fid)) next.delete(fid); else next.add(fid); return next; }); }
  function sortBy(key: 'name' | 'size' | 'path') {
    if (key === 'path' && searchView && scope === 'global') { setStatus('全盘搜索路径按需定位，暂不支持路径排序'); return; }
    setSort({ key, descending: sort?.key === key ? !sort.descending : false });
  }
  async function login() {
    cancelSearch(); ++view.current; const id = crypto.randomUUID(); loginId.current = id; setLoggingIn(true); setNickname('正在登录');
    const result = await action<{ nickname: string }>(() => call('login', {}, id));
    if (loginId.current === id) { setLoggingIn(false); setQr(null); if (result) { setNickname(result.nickname); acceptTasks(await call('tasks'));  await browse([['0','我的网盘']]); } else setNickname('昵称暂不可用'); }
  }
  return <div className="app">
    <aside><div className="brand"><span className="brand-mark">Q</span><div>夸克网盘<small>桌面客户端</small></div></div>
      <nav aria-label="主导航">{[['drive','我的文件'],['share','分享链接'],['tasks','传输任务'],['settings','设置']].map(([key,label]) =>
        <button key={key} className={page === key ? 'nav active' : 'nav'} onClick={() => { setPage(key); if(key === 'drive' && share) void browse([['0','我的网盘']]); }}>{label}{key === 'tasks' && tasks.length > 0 && <span>{tasks.length}</span>}</button>)}</nav>
      <div className="account"><span className="avatar">{nickname.slice(0,1)}</span><div className="account-name" title={nickname}>{nickname}<small>夸克网盘账号</small></div></div>
      <button className="login-button" disabled={transferring || loggingIn} onClick={() => void login()}>扫码登录</button>
    </aside>
    <main><header><div><h1>{({drive: share ? '分享文件' : '我的文件', share:'分享链接',tasks:'传输任务',settings:'设置'} as Record<string,string>)[page]}</h1>
      <p>{page === 'drive' ? `${rows.length} 项 · ${share ? sharePath : trail.map(item => item[1]).join(' / ')}` : page === 'tasks' ? '下载进度与逐项结果' : page === 'share' ? '粘贴链接或完整分享文案' : '下载与本地保存偏好'}</p></div>
      <span className="connection">{transferring ? '正在传输' : '就绪'}</span></header>
      {page === 'drive' && <>
        <div className="toolbar"><button disabled={loading || (!!share && sharePath === '/')} onClick={() => void (share ? shareUp() : browse(trail.slice(0, -1).length ? trail.slice(0,-1) : trail))}>返回上级</button>
          <button disabled={loading} onClick={() => void (share ? openShare() : searchView ? runSearch() : browse())}>刷新</button>
          {searchView && <button onClick={() => void browse()}>返回目录</button>}
          <div className="search-input"><input aria-label="搜索关键词" placeholder="搜索文件名称" value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !share) void runSearch(); }} /></div>
          <select aria-label="搜索范围" disabled={!!share} value={scope} onChange={event => setScope(event.target.value)}><option value="global">全盘</option><option value="subtree">当前目录及子目录</option></select>
          <button className="primary" disabled={!!share} onClick={() => void runSearch()}>搜索</button>
          {searching && <button onClick={() => { cancelSearch(); setStatus('搜索已取消；已读结果可能不完整'); }}>取消搜索</button>}</div>
        <div className="filters"><select aria-label="类型筛选" value={kind} onChange={event => setKind(event.target.value)}><option value="all">全部类型</option><option value="files">仅文件</option><option value="directories">仅目录</option></select>
          <input aria-label="格式筛选" list="formats" placeholder="格式：mp4, mkv, tar.gz" value={extensions} onChange={event => setExtensions(event.target.value)} /><datalist id="formats">{[...Object.keys(extensionGroups),'无扩展名'].map(value => <option key={value} value={value}/>)}</datalist><span>显示 {filtered.length} 项{selected.length ? ` · 已选 ${selected.length} 项` : ''}</span></div>
        {!share && <div className="file-actions"><button disabled={loading || searching || transferring} onClick={() => fileAction('mkdir',[])}>新建目录</button>
          <button disabled={loading || searching || transferring || selected.length !== 1} onClick={() => fileAction('rename')}>重命名</button>
          <button disabled={loading || searching || transferring || !selected.length} onClick={() => fileAction('move')}>移动</button>
          <button disabled={loading || searching || transferring || !selected.length} onClick={() => fileAction('delete')}>移入回收站</button>
          <button disabled={selected.length !== 1} onClick={() => fileAction('info')}>属性</button><button disabled={!selected.length} onClick={() => void copyPaths()}>复制路径</button></div>}
        <div className="file-header"><input aria-label="全选可见文件" type="checkbox" checked={all} onChange={() => setChecked(old => { const next = new Set(old); filtered.forEach(row => all ? next.delete(row.fid) : next.add(row.fid)); return next; })} />
          <button onClick={() => sortBy('name')}>名称{sort?.key === 'name' ? sort.descending ? ' ↓' : ' ↑' : ''}</button><button onClick={() => sortBy('size')}>大小</button><span>类型</span><button onClick={() => sortBy('path')}>路径</button></div>
        <div className="file-scroll" ref={scroll}>{loading && <div className="empty">正在读取目录…</div>}
          {!loading && !filtered.length && <div className="empty">{searching ? '正在搜索，结果将陆续显示…' : rows.length ? '没有符合筛选条件的项目' : '暂无文件'}</div>}
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>{virtualizer.getVirtualItems().map(item => { const row = filtered[item.index]; return <div className={checked.has(row.fid) ? 'file-row selected' : 'file-row'} key={row.fid} style={{ transform: `translateY(${item.start}px)` }} onDoubleClick={() => void openRow(row)} onContextMenu={event => { event.preventDefault(); if(!checked.has(row.fid)) setChecked(new Set([row.fid])); setContext({row,x:Math.min(event.clientX,window.innerWidth-180),y:Math.min(event.clientY,window.innerHeight-210)}); }}>
            <input type="checkbox" aria-label={`选择 ${row.name}`} checked={checked.has(row.fid)} onChange={() => toggle(row.fid)} />
            <button className="file-name" title={row.name} onClick={() => void openRow(row)}><FileIcon folder={row.directory} /><span>{row.name}</span></button>
            <span>{row.directory ? '—' : units(row.size)}</span><span>{row.directory ? '目录' : '文件'}</span><span title={row.path}>{row.path || (searchView ? '待定位' : '—')}</span></div>; })}</div></div>
        <footer><button onClick={() => void action(async () => { const folder = await window.quark.chooseFolder(); if (folder) await saveSettings(folder); })}>保存目录</button>
          <button className="folder-path" title={preferences.output_dir} onClick={() => void action(() => window.quark.openFolder(preferences.output_dir))}>{preferences.output_dir || '尚未选择保存目录'}</button>
          <button className="primary" disabled={searching || loading || transferring || !selected.length || !preferences.output_dir} onClick={() => void action(async () => {
            if (!await saveSettings()) return; setTransferring(true);
            try { const result = await call('download', { entries: selected, handle: share, path: sharePath }); acceptTasks(result); setPage('tasks'); }
            catch (error) { setTransferring(false); throw error; }
          })}>下载{selected.length ? `（${selected.length}）` : ''}</button></footer>
      </>}
      {page === 'share' && <section className="form-section"><h2>打开分享</h2><p>支持纯链接、带 pwd 的链接及包含提取码的分享文案。</p>
        <label>分享链接或文案<textarea value={shareText} onChange={event => { shareRevision.current++; setShareText(event.target.value); }} onBlur={() => { const text = shareText, revision = shareRevision.current, code = codeRevision.current; void action(async () => { if (!text.trim()) return; const parsed = await call('share.parse', { text }); if (revision === shareRevision.current && code === codeRevision.current && parsed.passcode) setPasscode(parsed.passcode); }); }} /></label>
        <label>提取码 <small>可手工修改</small><input aria-label="提取码" value={passcode} onChange={event => { codeRevision.current++; setPasscode(event.target.value); }} /></label><button className="primary" disabled={loading} onClick={() => void openShare()}>打开分享</button>
        <p className="note">浏览不会转存。分享下载使用临时目录，下载结束、失败或停止后清理。</p></section>}
      {page === 'tasks' && <><div className="toolbar"><button disabled={transferring || !tasks.some(task => task.valid && task.state === 'failed')} onClick={() => void action(async () => { setTransferring(true); try { acceptTasks(await call('tasks.retry', { ids: tasks.filter(task => task.state === 'failed').map(task => task.id) })); } catch(error) { setTransferring(false); throw error; } })}>重试失败</button>
        <button disabled={transferring || !tasks.some(task => task.valid && ['failed','stopped','waiting'].includes(task.state))} onClick={() => void action(async () => { setTransferring(true); try { acceptTasks(await call('tasks.retry')); } catch(error) { setTransferring(false); throw error; } })}>继续未完成</button>
        <button disabled={!transferring} onClick={() => void action(() => call('download.stop'))}>停止下载</button></div>
        <div className="task-list">{!tasks.length && <div className="empty">尚无传输任务，请在文件页勾选后下载</div>}{tasks.map(task => <div className="task-row" key={task.id}><FileIcon folder={false}/><div><strong>{task.name}</strong><p>{task.detail || task.target}</p></div><span className={'task-state ' + task.state}>{labels[task.state] || task.state}</span>
          <button disabled={transferring || !task.valid || !['failed','stopped','waiting'].includes(task.state)} onClick={() => void action(async () => { setTransferring(true); try { acceptTasks(await call('tasks.retry', { ids: [task.id] })); } catch(error) { setTransferring(false); throw error; } })}>重试</button>
          <button onClick={() => void action(() => window.quark.openFolder(task.target.replace(/[\\/][^\\/]+$/, '')))}>打开目录</button></div>)}</div></>}
      {page === 'settings' && <section className="form-section"><h2>下载设置</h2><label>线程数 <small>1–16，默认 4</small><input type="number" min="1" max="16" disabled={transferring} value={threads} onChange={event => setThreads(event.target.value)} /></label>
        <label>总限速 <small>MiB/s，0 表示不限速</small><input type="number" min="0" step="0.1" disabled={transferring} value={rate} onChange={event => setRate(event.target.value)} /></label>
        <label>保存目录<input value={preferences.output_dir} readOnly /></label><button disabled={transferring} onClick={() => void action(async () => { const folder = await window.quark.chooseFolder(); if (folder) await saveSettings(folder); })}>选择目录</button>
        <button className="primary" disabled={transferring} onClick={() => void saveSettings()}>保存设置</button><p className="note">文件队列逐项执行，线程数作用于单个文件。实际速度受账号与服务端限制。</p></section>}
      <div className="status" role="status">{status}</div>
    </main>
    {context && <><div className="menu-dismiss" onClick={() => setContext(null)}/><div className="context-menu" role="menu" style={{left:context.x,top:context.y}}>
      {!share && <><button disabled={transferring || searching || selected.length !== 1} onClick={() => fileAction('rename')}>重命名</button><button disabled={transferring || searching} onClick={() => fileAction('move')}>移动</button><button disabled={transferring || searching} onClick={() => fileAction('delete')}>移入回收站</button><button onClick={() => fileAction('info',[context.row])}>属性</button></>}
      <button onClick={() => { const entries = selected; setContext(null); void copyPaths(entries); }}>复制路径</button></div></>}
    {fileDialog && <FileDialog action={fileDialog.action} entries={fileDialog.entries} parent={trail} close={() => setFileDialog(null)} done={() => { void (searchView ? runSearch() : browse()); }}/ >}
    {loggingIn && <div className="modal-backdrop"><div className="modal"><h2>扫码登录</h2><p>使用手机夸克 App 扫描二维码</p>{qr ? <img src={qr} alt="夸克登录二维码" /> : <div className="qr-loading">正在获取二维码…</div>}
      <button onClick={() => { loginId.current = null; setLoggingIn(false); setQr(null); setNickname('未登录'); void action(() => call('login.cancel')); }}>取消登录</button></div></div>}
  </div>;
}
createRoot(document.getElementById('root')!).render(<App />);
