import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createPortal } from 'react-dom';
import { useVirtualizer } from '@tanstack/react-virtual';
import { Entry, Event, Preferences, Task, Cut, Favorite, Appearance, AppearanceSource, ClientSettings, LocalUploadItem } from './types';
import logo from '../assets/app-icon.png';
import './style.css';
import './theme.css';
import { FileDialog, FileAction } from './FileDialog';
import { TasksPanel } from './TasksPanel';
import { CatalogPanel } from './CatalogPanel';
import { SharingDialog } from './SharingDialog';
import { FeedbackProvider, useFeedback, Tone } from './Feedback';
import { formatSize as units } from './size';

function modificationTime(value:number|null|undefined) {
  if(value == null) return '未知';
  const date=new Date(value);
  if(!Number.isFinite(date.getTime())) return '未知';
  const pad=(number:number) => String(number).padStart(2,'0');
  return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function FileIcon({ folder }: { folder: boolean }) {
  return <svg className={folder ? 'folder-icon' : 'document-icon'} width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true">
    {folder ? <path d="M3 7a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9H3V7Z" fill="var(--folder-fill)" stroke="var(--folder-stroke)" strokeWidth="1.5" /> :
      <><path d="M6 3h8l4 4v14H6V3Z" fill="var(--document-fill)" stroke="var(--document-stroke)" strokeWidth="1.5" /><path d="M14 3v5h4M9 12h6M9 16h6" stroke="var(--document-stroke)" strokeWidth="1.5" /></>}
  </svg>;
}
function NavigationIcon({name}:{name:string}) {
  const paths:Record<string,string>={
    drive:'M3 7h7l2 3h9v10H3V7Z M3 7V4h7l2 3',
    share:'M10 14l4-4 M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0',
    shares:'M8 12l8-5 M8 12l8 5 M7 12a2 2 0 1 1-4 0 2 2 0 0 1 4 0 M20 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0 M20 18a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
    recycle:'M4 6h16 M9 3h6 M6 6l1 15h10l1-15 M10 10v7 M14 10v7',
    tasks:'M7 3v14 M3 13l4 4 4-4 M17 21V7 M13 11l4-4 4 4',
    settings:'M4 6h16 M4 12h16 M4 18h16 M8 3v6 M16 9v6 M10 15v6',
    back:'M14 5l-7 7 7 7',
    refresh:'M20 7v5h-5 M20 12a8 8 0 1 0-2 6',
  };
  return <svg className="ui-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]}/></svg>;
}
function App() {
  const toast = useFeedback();
  const [page, setPage] = useState('drive');
  const [clientVersion,setClientVersion] = useState('');
  const [appearanceSource,setAppearanceSource] = useState<AppearanceSource>('system');
  const [appearanceSaving,setAppearanceSaving] = useState(false);
  const [clientSettings,setClientSettings] = useState<ClientSettings>({closeBehavior:'tray',notifications:true});
  const [clientSettingsSaving,setClientSettingsSaving] = useState(false);
  const [quitting,setQuitting] = useState(false);
  const [dragging,setDragging] = useState(false);
  const dragDepth=useRef(0);
  const dropGeneration=useRef(0);
  const dropSubmitting=useRef(false);
  const [dropUpload,setDropUpload]=useState<{parent:string;target:string;account:number;items:LocalUploadItem[]|null}|null>(null);
  const [dropBusy,setDropBusy]=useState(false);
  function clearDrag() {dragDepth.current=0;setDragging(false);}
  function cancelDrop() {++dropGeneration.current;setDropUpload(null);clearDrag();}
  async function changeClientSettings(next:ClientSettings) {
    const previous=clientSettings;
    setClientSettings(next);
    setClientSettingsSaving(true);
    try {
      const result=await window.quark.setClientSettings(next);setClientSettings(result.settings);
      notify(result.warning || '后台运行设置已保存',result.warning ? 'warning' : 'success');
    } catch(error) {setClientSettings(previous);notify(error instanceof Error ? error.message : '无法保存后台运行设置','error');}
    finally {setClientSettingsSaving(false);}
  }
  function acceptAppearance(next:Appearance) {
    document.documentElement.dataset.theme=next.source;
    document.documentElement.dataset.colorScheme=next.dark ? 'dark' : 'light';
    setAppearanceSource(next.source);
  }
  async function changeAppearance(source:AppearanceSource) {
    setAppearanceSaving(true);
    try {
      const next=await window.quark.setAppearance(source);acceptAppearance(next);
      notify(next.warning || '外观已保存',next.warning ? 'warning' : 'success');
    } catch(error) {notify(error instanceof Error ? error.message : '无法切换外观','error');}
    finally {setAppearanceSaving(false);}
  }
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
  const [sort, setSort] = useState<{ key: 'name' | 'size' | 'path' | 'modified_at'; descending: boolean } | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [searching, setSearching] = useState(false);
  const [searchView, setSearchView] = useState(false);
  const [resultsRecursive,setResultsRecursive] = useState(false);
  const [format,setFormat] = useState('');
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState('正在启动网盘后端…');
  function notify(text:string,tone:Tone='info') {setStatus(text);toast(text,tone);}
  const [preferences, setPreferences] = useState<Preferences>({ output_dir: '', connections: 4, limit_rate: null, width: 1180, height: 780 });
  const [threads, setThreads] = useState('4');
  const [rate, setRate] = useState('0');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [transferring, setTransferring] = useState(false);
  const [qr, setQr] = useState<string | null>(null);
  const [loggingIn, setLoggingIn] = useState(false);
  const [signedIn,setSignedIn] = useState(false);
  const [accountMenu,setAccountMenu] = useState(false);
  const [loginPicker,setLoginPicker] = useState(false);
  const [loginMode,setLoginMode] = useState<'qr'|'mobile'|'wechat'>('qr');
  const [loginCancelling,setLoginCancelling] = useState(false);
  const [showFilters,setShowFilters] = useState(false);
  const [fileDialog,setFileDialog] = useState<{ action:FileAction; entries:Entry[]; cutId?:string } | null>(null);
  const [cut,setCut] = useState<Cut|null>(null);
  const [favorites,setFavorites] = useState<Favorite[]>([]);
  const [favoriteBusy,setFavoriteBusy] = useState(false);
  const [sharingDialog,setSharingDialog] = useState<'create'|'save'|null>(null);
  const [accountRevision,setAccountRevision] = useState(0);
  const [context,setContext] = useState<{ row:Entry|null; x:number; y:number } | null>(null);
  const view = useRef(0);
  const searchId = useRef<string | null>(null);
  const loginId = useRef<string | null>(null);
  const profileGeneration = useRef(0);
  const shareRevision = useRef(0);
  const codeRevision = useRef(0);
  const scroll = useRef<HTMLDivElement>(null);
  const directoryKey = share ? `${share}:${sharePath}` : trail.at(-1)![0];
  useLayoutEffect(() => {scroll.current?.scrollTo({top:0});},[directoryKey]);
  const menu = useRef<HTMLDivElement>(null);
  useEffect(() => {if(context) menu.current?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();},[context]);
  const call = window.quark.call;
  async function loadFavorites() {
    const generation = profileGeneration.current;
    const next = await action<Favorite[]>(() => call('favorites.list'));
    if(next && generation === profileGeneration.current) setFavorites(next);
  }
  async function toggleFavorite(row:Entry) {
    setContext(null);setFavoriteBusy(true);
    const generation = profileGeneration.current;
    try {
      const exists = favorites.some(item => item.fid === row.fid);
      let chain = row.location.length ? row.location : [...trail,[row.fid,row.name]];
      if(!exists && searchView && !row.location.length) {
        const found:Entry = await call('locate',{entry:row});chain=found.location;
      }
      if(generation !== profileGeneration.current) return;
      const next:Favorite[] = await call(exists ? 'favorites.remove' : 'favorites.add',exists ? {fid:row.fid} : {trail:chain});
      if(generation === profileGeneration.current) {setFavorites(next);notify(exists ? '已移除常用文件夹' : '已添加常用文件夹','success');}
    } catch(error) {notify(error instanceof Error ? error.message : '无法保存常用文件夹','error');}
    finally {setFavoriteBusy(false);}
  }
  async function removeFavorite(fid:string) {
    setFavoriteBusy(true);const generation=profileGeneration.current;
    const next=await action<Favorite[]>(() => call('favorites.remove',{fid}));
    if(next && generation === profileGeneration.current) {setFavorites(next);notify('已移除常用文件夹','success');}
    setFavoriteBusy(false);
  }
  async function openFavorite(fid:string) {
    cancelSearch();const generation=++view.current;setLoading(true);setPage('drive');
    const chain=await action<[string,string][]>(() => call('favorites.open',{fid}));
    if(generation !== view.current) return;
    setLoading(false);
    if(chain) {await browse(chain);void loadFavorites();}
  }
  async function shareBrowse(path:string) {
    if(!share) return;
    cancelSearch();const generation=++view.current;setLoading(true);
    const result=await action<Entry[]>(() => call('share.browse',{handle:share,path}));
    if(generation === view.current) {setLoading(false);if(result) {setRows(result);setSharePath(path);setQuery('');setChecked(new Set());}}
  }
  function acceptTasks(next: Task[]) {
    setTasks(old => next.map(task => {
      const previous = old.find(row => row.id === task.id);
      return previous && (previous.revision || 0) > (task.revision || 0) ? previous : task;
    }));
  }
  async function action<T>(work: () => Promise<T>): Promise<T | undefined> {
    try { return await work(); } catch (error) { notify(error instanceof Error ? error.message : '操作失败','error'); }
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
      if (result) { setRows(result); setTrail(chain); setShare(null); setQuery(''); setKind('all'); setExtensions(''); setFormat(''); setStatus(`已读取 ${result.length} 项`); }
    }
  }
  async function openRow(row: Entry) {
    if(loading) return;
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
    if (!query.trim()) { notify('请输入搜索关键词','warning'); return; }
    const id = crypto.randomUUID(); searchId.current = id; ++view.current;
    setResultsRecursive(scope !== 'global');
    if(scope === 'global' && sort?.key === 'path') setSort(null);
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
      if (searchId.current === id) notify(`${error instanceof Error ? error.message : '搜索失败'}${received ? '；结果不完整' : ''}`,'error');
    } finally { off(); if (searchId.current === id) { searchId.current = null; setSearching(false); } }
  }
  async function openShare() {
    const generation = ++view.current; cancelSearch(); setLoading(true);
    const result = await action<{ handle: string; entries: Entry[] }>(() => call('share.open', { text: shareText, passcode }));
    if (generation !== view.current) return;
    setLoading(false);
    if (result) { setShare(result.handle); setSharePath('/'); setRows(result.entries); setChecked(new Set()); setQuery(''); setKind('all'); setFormat(''); setExtensions(''); setSearchView(false); setPage('drive'); notify('已打开分享，下载后清理临时转存','success'); }
  }
  async function saveSettings(folder = preferences.output_dir) {
    const n = Number(threads), limit = Number(rate);
    if (!threads.trim() || !Number.isInteger(n) || n < 1 || n > 16 || !rate.trim() || !Number.isFinite(limit) || (limit !== 0 && (limit < .01 || limit > 10240))) {
      notify('线程数为 1–16；限速为 0 或 0.01–10240 MiB/s','warning'); return false;
    }
    const next = { ...preferences, output_dir: folder, connections: n, limit_rate: limit ? Math.floor(limit * 1048576) : null };
    const saved = await action<{ preferences: Preferences; warning: string }>(() => call('preferences.save', next));
    if (saved) { setPreferences(saved.preferences); notify(saved.warning || '设置已保存',saved.warning ? 'warning' : 'success'); return true; } return false;
  }
  useEffect(() => {
    void action(async () => {const next=await window.quark.getAppearance();acceptAppearance(next);if(next.warning) notify(next.warning,'warning');});
    void action(async () => {const next=await window.quark.getClientSettings();setClientSettings(next.settings);if(next.warning) notify(next.warning,'warning');});
    const off = window.quark.onEvent((event: Event) => {
      if(event.event === 'appearance') acceptAppearance(event.data as Appearance);
      else if(event.event === 'desktop.quitting') {cancelDrop();setQuitting(true);}
      else if(event.event === 'navigate' && event.data === 'tasks') {if(!dropSubmitting.current) cancelDrop();cancelSearch();setPage('tasks');}
      else if(event.event === 'tasks.changed') acceptTasks(event.data as Task[]);
      else if(event.event === 'batch.started') setTransferring(true);
      else if(event.event === 'feedback') notify(event.data as string);
      else if (event.event === 'task') {
        const task = event.data as Task;
        setTasks(old => old.some(row => row.id === task.id) ? old.map(row => row.id === task.id && (task.revision || 0) >= (row.revision || 0) ? task : row) : [...old, task]);
      } else if (event.event === 'batch.finished') { setTransferring(false); notify('本批次已结束，请查看逐项结果'); }
      else if (event.event === 'login.qr' && event.id === loginId.current) setQr(event.data as string);
      else if (event.event === 'warning') notify(event.data as string,'warning');
      else if (event.event === 'backend.error') { notify(event.data as string,'error'); setTransferring(false); setSearching(false); }
    });
    void action(async () => {
      const hello = await call('hello');
      if (hello.protocol !== 1) throw new Error('后端协议版本不支持');
      setClientVersion(hello.client_version || '');
      setPreferences(hello.preferences); setThreads(String(hello.preferences.connections)); setRate(String(hello.preferences.limit_rate ? Math.max(.01, hello.preferences.limit_rate / 1048576) : 0));
      setSignedIn(hello.signed_in !== false);
      setExtensionGroups(hello.extension_groups || {});
      acceptTasks(await call("tasks"));
      if(hello.signed_in !== false) {await browse([['0', '我的网盘']]);void loadFavorites();} else setStatus('请登录夸克账号');
      if (hello.warning) setStatus(hello.warning);
      const generation = profileGeneration.current;
      void action(async () => { const account = await call('account'); if(generation === profileGeneration.current) {setNickname(account.nickname);setSignedIn(account.logged_in !== false);} });
    });
    return off;
  }, []);
  useEffect(() => {window.addEventListener('blur',clearDrag);return () => window.removeEventListener('blur',clearDrag);},[]);
  const filtered = useMemo(() => {
    let result = rows.filter(row => (!query || row.name.toLocaleLowerCase().includes(query.toLocaleLowerCase())) &&
      (kind === 'all' || row.directory === (kind === 'directories')));
    const filter = format === 'custom' ? extensions : format;
    if (filter.trim()) {
      const suffixes = extensionGroups[filter] || filter.toLocaleLowerCase().split(/[\s,，;；、]+/).filter(Boolean).map(value => value.replace(/^\./, ''));
      if (filter === '无扩展名') result = result.filter(row => !row.directory && (row.name.lastIndexOf('.') <= 0 || row.name.endsWith('.')));
      else {
      if (suffixes.some(value => !/^[a-z0-9]+(?:\.[a-z0-9]+)*$/.test(value))) return [];
      result = result.filter(row => !row.directory && suffixes.some(value => row.name.toLocaleLowerCase().endsWith('.' + value)));
      }
    }
    if (sort) result = [...result].sort((a,b) => {
      if (a.directory !== b.directory) return a.directory ? -1 : 1;
      if(sort.key === 'modified_at') {
        if(a.modified_at == null || b.modified_at == null) return a.modified_at == b.modified_at ? 0 : a.modified_at == null ? 1 : -1;
        return (a.modified_at-b.modified_at) * (sort.descending ? -1 : 1);
      }
      if (sort.key === 'size') {
        if (a.directory) return a.name.localeCompare(b.name);
        if (a.size === null || b.size === null) return a.size === b.size ? 0 : a.size === null ? 1 : -1;
        return (a.size-b.size) * (sort.descending ? -1 : 1);
      }
      return a[sort.key].localeCompare(b[sort.key], undefined, { sensitivity: 'base' }) * (sort.descending ? -1 : 1);
    });
    return result;
  }, [rows, query, kind, extensions, format, sort, extensionGroups]);
  const virtualizer = useVirtualizer({ count: filtered.length, getScrollElement: () => scroll.current, estimateSize: () => 46, overscan: 10 });
  const selected = filtered.filter(row => checked.has(row.fid));
  const cutIds = useMemo(() => new Set(cut?.entries.map(row => row.fid) || []),[cut]);
  const canModify = signedIn && !share && !transferring && !searching && !loading && !loggingIn;
  const dropBlocked=!signedIn ? '请先登录后上传' : share ? '分享文件为只读，请进入我的文件上传' : searchView ? '请先返回网盘目录，再拖入文件上传' : loading || searching ? '请等待目录读取或搜索完成' : loggingIn || fileDialog || sharingDialog || loginPicker || dropUpload ? '请先结束当前对话框' : '';
  const canDrop=page === 'drive' && !dropBlocked && !quitting;
  function fileDrag(event:React.DragEvent) {return event.dataTransfer.types.includes('Files');}
  function dragEnter(event:React.DragEvent) {
    if(!fileDrag(event)) return;
    event.preventDefault();event.stopPropagation();dragDepth.current++;setDragging(true);
  }
  function dragLeave(event:React.DragEvent) {
    if(!fileDrag(event)) return;
    event.stopPropagation();dragDepth.current=Math.max(0,dragDepth.current-1);
    if(!dragDepth.current) setDragging(false);
  }
  async function receiveDrop(event:React.DragEvent) {
    if(!fileDrag(event)) return;
    event.preventDefault();event.stopPropagation();clearDrag();
    if(!canDrop) {notify(dropBlocked || '请在我的文件目录中拖入上传','warning');return;}
    const files=Array.from(event.dataTransfer.files);
    if(!files.length) {notify('请拖入本地文件或目录','warning');return;}
    const generation=++dropGeneration.current;
    const intent={parent:trail.at(-1)![0],target:trail.map(([,name]) => name).join(' / '),account:profileGeneration.current,items:null};
    setContext(null);setDropUpload(intent);
    try {
      const items=await window.quark.readDroppedFiles(files);
      if(generation === dropGeneration.current) setDropUpload({...intent,items});
    } catch(error) {if(generation === dropGeneration.current) {cancelDrop();notify(error instanceof Error ? error.message : '无法读取拖入项目','error');}}
  }
  const canDownload = signedIn && !searching && !loading && !loggingIn && !!preferences.output_dir && selected.length > 0;
  const canPaste = canModify && !searchView && !!cut && !cut.blocked;
  function fileAction(action:FileAction, entries = selected) { setContext(null); setFileDialog({ action,entries }); }
  async function copyPaths(entries = selected) {
    await action(async () => {
      const generation = view.current;
      const located: Entry[] = [];
      for(const row of entries) located.push(row.path ? row : share ? {...row,path:sharePath.replace(/\/$/,'')+'/'+row.name} : await call('locate',{ entry:row }));
      if(generation !== view.current) return;
      await window.quark.copyText(located.map(row => row.path).join('\n'));
      notify(`已复制 ${located.length} 项路径`,'success');
    });
  }
  async function cutSelected() {
    setContext(null);
    if(!canModify || !selected.length) return;
    const generation = profileGeneration.current;
    const clipboard = await action<Cut>(() => call('file.cut',{entries:selected}));
    if(clipboard && generation === profileGeneration.current) {setCut(clipboard);notify(`已剪切 ${clipboard.entries.length} 项，请打开目标目录后粘贴`,'success');}
  }
  async function clearCut() {
    setContext(null);
    if(!cut) return;
    await action(async () => {setCut(await call('file.clearCut',{id:cut.id}));notify('已取消剪切');});
  }
  function paste() {
    setContext(null);
    if(canPaste && cut) setFileDialog({action:'paste',entries:cut.entries,cutId:cut.id});
  }
  async function downloadSelected() {
    setContext(null);
    if(!canDownload) {notify('请勾选项目并选择保存目录，目录读取完成后再下载','warning');return;}
    await action(async () => {
      const alreadyRunning = transferring;
      if(!alreadyRunning && !await saveSettings()) return;setTransferring(true);
      try {acceptTasks(await call('download',{entries:selected,handle:share,path:sharePath}));setPage('tasks');notify(`已加入 ${selected.length} 个下载任务`,'success');}
      catch(error) {if(!alreadyRunning) setTransferring(false);throw error;}
    });
  }
  function contextAt(event:React.MouseEvent,row:Entry|null) {
    event.preventDefault();event.stopPropagation();
    if(row && !checked.has(row.fid)) setChecked(new Set([row.fid]));
    setContext({row,x:Math.max(4,Math.min(event.clientX,window.innerWidth-230)),y:Math.max(39,Math.min(event.clientY,window.innerHeight-460))});
    scroll.current?.focus();
  }
  function shortcut(event:React.KeyboardEvent) {
    if(event.key === 'Escape' && dropUpload && !dropBusy) {cancelDrop();event.preventDefault();return;}
    if(event.key === 'Escape' && accountMenu) {setAccountMenu(false);event.preventDefault();return;}
    if(event.key === 'Escape' && loginPicker) {setLoginPicker(false);event.preventDefault();return;}
    if(event.key === 'Escape' && context) {setContext(null);scroll.current?.focus();event.preventDefault();return;}
    if(page !== 'drive' || fileDialog || sharingDialog || loginPicker || loggingIn || dropUpload || (event.target as HTMLElement).closest('input:not([type=checkbox]),textarea,select,[contenteditable=true]')) return;
    if((event.ctrlKey || event.metaKey) && !event.altKey) {
      const key=event.key.toLowerCase();
      if(key === 'x' && canModify && selected.length) {event.preventDefault();void cutSelected();}
      else if(key === 'v' && canPaste) {event.preventDefault();paste();}
      else if(key === 'c' && event.shiftKey && selected.length) {event.preventDefault();setContext(null);void copyPaths();}
      else if(key === 'a' && !searching && !loading) {event.preventDefault();setChecked(new Set(filtered.map(row => row.fid)));}
    } else if(event.key === 'F2' && canModify && selected.length === 1) {event.preventDefault();fileAction('rename');}
    else if((event.key === 'Delete' || event.key === 'Backspace') && canModify && selected.length) {event.preventDefault();fileAction('delete');}
    else if(event.key === 'Escape' && cut) {event.preventDefault();void clearCut();}
  }
  const all = filtered.length > 0 && selected.length === filtered.length;
  function toggle(fid: string) { setChecked(old => { const next = new Set(old); if (next.has(fid)) next.delete(fid); else next.add(fid); return next; }); }
  function sortBy(key: 'name' | 'size' | 'path' | 'modified_at') {
    if (key === 'path' && searchView && !resultsRecursive) { setStatus('全盘搜索路径按需定位，暂不支持路径排序'); return; }
    setSort({ key, descending: sort?.key === key ? !sort.descending : key === 'modified_at' });
  }
  async function enqueueUpload(sources:string[],parent:string) {
    const alreadyRunning = transferring;
    if(!alreadyRunning && !await saveSettings()) return;
    setTransferring(true);
    try {acceptTasks(await call('upload',{sources,parent}));setPage('tasks');notify(`已加入 ${sources.length} 个上传任务`,'success');}
    catch(error) {if(!alreadyRunning) setTransferring(false);throw error;}
  }
  async function confirmDrop() {
    if(!dropUpload?.items || dropSubmitting.current) return;
    if(dropUpload.account !== profileGeneration.current || !signedIn || loggingIn) {cancelDrop();notify('账号已变化，请重新拖入文件','warning');return;}
    dropSubmitting.current=true;setDropBusy(true);
    await action(() => enqueueUpload(dropUpload.items!.map(item => item.path),dropUpload.parent));
    dropSubmitting.current=false;setDropBusy(false);cancelDrop();
  }
  async function upload(directory:boolean) {
    const parent=trail.at(-1)![0],account=profileGeneration.current;
    await action(async () => {
      const sources = directory ? [await window.quark.chooseFolder()].filter(Boolean) : await window.quark.chooseFiles();
      if(!sources.length) return;
      if(account !== profileGeneration.current) throw new Error('账号已变化，请重新选择上传项目');
      await enqueueUpload(sources as string[],parent);
    });
  }
  function clearAccountView() {
    cancelDrop();
    cancelSearch();++view.current;++profileGeneration.current;setContext(null);setFileDialog(null);setSharingDialog(null);
    setFavorites([]);setCut(null);setRows([]);setChecked(new Set());setShare(null);setShareText('');setPasscode('');
    ++shareRevision.current;++codeRevision.current;
    setQuery('');setSearchView(false);setTrail([['0','我的网盘']]);setAccountRevision(old => old+1);setLoading(false);
  }
  async function exitAccount() {
    setAccountMenu(false);
    const result=await action<{warning?:string}>(() => call('logout'));
    if(!result) return;
    clearAccountView();setSignedIn(false);setNickname('未登录');setPage('drive');acceptTasks(await call('tasks'));
    notify(result.warning || '已退出本客户端',result.warning ? 'warning' : 'success');
  }
  async function login(mode:'qr'|'mobile'|'wechat'='qr') {
    cancelDrop();
    setLoginPicker(false);setAccountMenu(false);setLoginMode(mode);setLoginCancelling(false);setQr(null);
    cancelSearch();++view.current;++profileGeneration.current;setCut(null);setContext(null);setLoading(false);
    const id=crypto.randomUUID();loginId.current=id;setLoggingIn(true);
    const result=await action<{nickname?:string;cancelled?:boolean;warning?:string}>(() => mode === 'qr' ? call('login',{},id) : window.quark.officialLogin(mode));
    if(loginId.current !== id) return;
    setLoggingIn(false);setQr(null);
    if(result && !result.cancelled) {
      clearAccountView();setSignedIn(true);setNickname(result.nickname || '昵称暂不可用');setPage('drive');
      acceptTasks(await call('tasks'));await browse([['0','我的网盘']]);void loadFavorites();
      notify(result.warning || '登录成功',result.warning ? 'warning' : 'success');
    } else {if(signedIn) void loadFavorites();if(result?.cancelled) notify(signedIn ? '登录已取消，原账号保持不变' : '登录已取消');}
  }
  return <><div className="titlebar"><img src={logo} alt=""/><span>夸克网盘客户端{clientVersion && <small className="client-version">v{clientVersion}</small>}</span></div><div className="app" inert={quitting || !!dropUpload} onKeyDown={shortcut} onDragOver={event => {if(fileDrag(event)) {event.preventDefault();event.dataTransfer.dropEffect='none';}}} onDrop={event => {if(fileDrag(event)) {event.preventDefault();clearDrag();notify('请把文件或目录拖入我的文件列表','warning');}}}>
    <aside><div className="brand"><img className="brand-logo" src={logo} alt=""/><div>夸克网盘<small>桌面客户端</small></div></div>
      <nav aria-label="主导航">{[['drive','我的文件'],['share','分享链接'],['shares','我的分享'],['recycle','回收站'],['tasks','传输任务'],['settings','设置']].map(([key,label]) =>
        <button key={key} className={page === key ? 'nav active' : 'nav'} aria-current={page === key ? 'page' : undefined} onClick={() => { cancelSearch(); setPage(key); if(key === 'drive' && share) void browse([['0','我的网盘']]); }}><NavigationIcon name={key}/>{label}{key === 'tasks' && tasks.length > 0 && <span>{tasks.length}</span>}</button>)}</nav>
      <section className="favorites" aria-label="常用文件夹"><h2>常用文件夹</h2>{!favorites.length && <p>右键目录可添加</p>}{favorites.map(row => <div key={row.fid}><button title={row.name} disabled={loading || loggingIn} onClick={() => void openFavorite(row.fid)}>{row.name}</button><button aria-label={`移除常用 ${row.name}`} disabled={favoriteBusy || loggingIn} onClick={() => void removeFavorite(row.fid)}>×</button></div>)}</section>
      <div className="account-area"><button className="account" aria-label="账号操作" aria-expanded={accountMenu} disabled={transferring || loggingIn} onClick={() => setAccountMenu(old => !old)}><span className="avatar">{nickname.slice(0,1)}</span><span className="account-name" title={nickname}>{nickname}<small>{signedIn ? '夸克网盘账号' : '点击登录账号'}</small></span><span>⌃</span></button>
        {accountMenu && <div className="account-popup" role="menu" aria-label="账号菜单"><button role="menuitem" onClick={() => {setAccountMenu(false);setLoginPicker(true);}}>{signedIn ? '切换账号' : '登录账号'}</button>{signedIn && <button role="menuitem" title="退出本客户端并移除共享的本地扫码凭证" onClick={() => void exitAccount()}>退出账号</button>}</div>}
      </div>
      <button className="login-button" disabled={transferring || loggingIn} onClick={() => setLoginPicker(true)}>{signedIn ? '切换账号' : '登录账号'}</button>
    </aside>
    <main><header><div><h1>{({drive: share ? '分享文件' : '我的文件', share:'分享链接',shares:'我的分享',recycle:'回收站',tasks:'传输任务',settings:'设置'} as Record<string,string>)[page]}</h1>
      <p>{page === 'drive' ? `${rows.length} 项${searchView ? ' · 搜索结果' : ''}` : ({tasks:'分别查看上传、下载及完成情况',share:'粘贴链接或完整分享文案',shares:'管理已创建的分享链接',recycle:'恢复文件或清理已删除记录',settings:'按你的习惯，调整外观与传输偏好'} as Record<string,string>)[page]}</p></div>
      <span className="connection">{transferring ? '正在传输' : '就绪'}</span></header>
      {page === 'drive' && <>
        <div className="path-toolbar"><nav className="breadcrumbs" aria-label="文件路径">{(share ? [['/','分享文件'],...sharePath.split('/').filter(Boolean).map((name,index,parts) => ['/'+parts.slice(0,index+1).join('/'),name])] : trail).map(([fid,name],index,chain) => <React.Fragment key={fid}>{index > 0 && <span aria-hidden="true">/</span>}<button aria-current={index === chain.length-1 ? 'location' : undefined} disabled={loggingIn || !signedIn} onClick={() => void (share ? shareBrowse(fid) : browse(trail.slice(0,index+1)))}>{name}</button></React.Fragment>)}</nav>
          <button className="icon-button" aria-label="返回上级" title="返回上级" disabled={!signedIn || loading || (!!share ? sharePath === '/' : trail.length === 1)} onClick={() => void (share ? shareUp() : browse(trail.slice(0,-1)))}><NavigationIcon name="back"/></button>
          <button className="icon-button" aria-label="刷新" title="刷新" disabled={!signedIn || loading} onClick={() => void (share ? openShare() : searchView ? runSearch() : browse())}><NavigationIcon name="refresh"/></button>
        </div>
        <div className="toolbar file-toolbar">
          {!share && <><details className="upload-picker"><summary role="button" aria-label="上传" aria-disabled={!signedIn || loading || searching || loggingIn} onClick={event => {if(!signedIn || loading || searching || loggingIn) event.preventDefault();}}>＋ 上传 ▾</summary><div><button onClick={event => {event.currentTarget.closest('details')?.removeAttribute('open');void upload(false);}}>上传文件</button><button onClick={event => {event.currentTarget.closest('details')?.removeAttribute('open');void upload(true);}}>上传目录</button></div></details>
            <button disabled={!canModify} onClick={() => fileAction('mkdir',[])}>新建目录</button></>}
          <span className="file-count">显示 {filtered.length} 项{selected.length ? ` · 已选 ${selected.length} 项` : ''}</span>
          {searchView && <button onClick={() => void browse()}>返回目录</button>}
          <div className="search-input"><input aria-label="搜索关键词" placeholder="搜索文件名称" value={query} onChange={event => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !share) void runSearch(); }} /></div>
          <select aria-label="搜索范围" disabled={!!share || !signedIn} value={scope} onChange={event => setScope(event.target.value)}><option value="global">全盘</option><option value="subtree">当前目录及子目录</option></select>
          <button className="primary" disabled={!!share || !signedIn} onClick={() => void runSearch()}>搜索</button>
          {searching && <button onClick={() => { cancelSearch(); notify('搜索已取消；已读结果可能不完整'); }}>取消搜索</button>}
          <button aria-label="筛选" aria-expanded={showFilters} className={format || kind !== 'all' ? 'filter-active' : ''} onClick={() => setShowFilters(old => !old)}>筛选{format || kind !== 'all' ? ' ·' : ''}</button></div>
        {(showFilters || format || kind !== 'all') && <div className="filters"><select aria-label="类型筛选" value={kind} onChange={event => setKind(event.target.value)}><option value="all">全部类型</option><option value="files">仅文件</option><option value="directories">仅目录</option></select>
          <select aria-label="格式筛选" value={format} onChange={event => setFormat(event.target.value)}><option value="">全部格式</option>{[...Object.keys(extensionGroups),'无扩展名'].map(value => <option key={value} value={value}>{value}</option>)}<option value="custom">自定义格式</option></select>
          {format === 'custom' && <input aria-label="自定义扩展名" placeholder="mp4, mkv, tar.gz" value={extensions} onChange={event => setExtensions(event.target.value)}/>}
          {(format || kind !== 'all') && <button className="clear-filters" onClick={() => {setFormat('');setExtensions('');setKind('all');}}>清除筛选</button>}</div>}
        {share && <div className="file-actions"><button disabled={loading || searching || transferring || loggingIn || !selected.length} onClick={() => setSharingDialog('save')}>永久转存到网盘</button><span className="note">下载仍使用临时转存并清理</span></div>}
        <div className="file-header"><input aria-label="全选可见文件" type="checkbox" checked={all} onChange={() => setChecked(old => { const next = new Set(old); filtered.forEach(row => all ? next.delete(row.fid) : next.add(row.fid)); return next; })} />
          <button onClick={() => sortBy('name')}>名称{sort?.key === 'name' ? sort.descending ? ' ↓' : ' ↑' : ''}</button><button onClick={() => sortBy('size')}>大小</button><span>类型</span><button aria-label="按修改时间排序" aria-description={sort?.key === 'modified_at' ? sort.descending ? '最新在前' : '最早在前' : '点击按最新在前排序'} title="本地时间，点击切换新旧顺序" onClick={() => sortBy('modified_at')}>修改时间{sort?.key === 'modified_at' ? sort.descending ? ' ↓' : ' ↑' : ''}</button><button onClick={() => sortBy('path')}>路径</button></div>
        {cut && !share && <div className="cut-bar"><span>{cut.blocked ? '上次粘贴结果待核实' : `已剪切 ${cut.entries.length} 项，等待粘贴`}</span><button disabled={!canPaste} onClick={paste}>粘贴到当前目录</button><button onClick={() => void clearCut()}>取消剪切</button></div>}
        <div className="file-list" aria-busy={loading} onDragEnter={dragEnter} onDragLeave={dragLeave} onDragOver={event => {if(fileDrag(event)) {event.preventDefault();event.stopPropagation();event.dataTransfer.dropEffect=canDrop ? 'copy' : 'none';}}} onDrop={event => void receiveDrop(event)}><div className="file-scroll" ref={scroll} tabIndex={0} inert={loading} aria-label="文件列表" onContextMenu={event => contextAt(event,null)}>
          {!loading && !filtered.length && <div className="empty">{!signedIn ? <><p>登录后浏览和管理网盘文件</p><button className="primary" onClick={() => setLoginPicker(true)}>登录账号</button></> : searching ? '正在搜索，结果将陆续显示…' : rows.length ? '没有符合筛选条件的项目' : '暂无文件'}</div>}
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>{virtualizer.getVirtualItems().map(item => { const row = filtered[item.index]; return <div className={'file-row' + (checked.has(row.fid) ? ' selected' : '') + (cutIds.has(row.fid) && !share ? ' cut' : '')} key={row.fid} style={{ transform: `translateY(${item.start}px)` }} onDoubleClick={() => void openRow(row)} onContextMenu={event => contextAt(event,row)}>
            <input type="checkbox" aria-label={`选择 ${row.name}`} checked={checked.has(row.fid)} onChange={() => toggle(row.fid)} />
            <div className="file-title"><button className="file-name" title={row.name} onClick={() => void openRow(row)}><FileIcon folder={row.directory} /><span>{row.name}</span></button><div className="row-actions"><button title="更多操作" aria-label={`更多操作 ${row.name}`} onClick={event => contextAt(event,row)}>⋯</button></div></div>
            <span>{row.directory ? '—' : units(row.size)}</span><span>{row.directory ? '目录' : '文件'}</span><span className="modified-time" title={row.modified_at == null ? '服务器未提供有效修改时间' : '本地时间'}>{modificationTime(row.modified_at)}</span><span title={row.path}>{row.path || (searchView ? '待定位' : '—')}</span></div>; })}</div></div>
          {loading && <div className="list-loading" role="status">正在读取目录…</div>}
          {dragging && <div className={'drop-overlay'+(canDrop ? '' : ' blocked')} role="status"><strong>{canDrop ? '松开鼠标，准备上传' : dropBlocked}</strong>{canDrop && <p>目标：{trail.map(([,name]) => name).join(' / ')}</p>}<small>文件和目录均可，确认后加入上传队列</small></div>}</div>
        <footer><button disabled={transferring || loggingIn} onClick={() => void action(async () => { const folder = await window.quark.chooseFolder(); if (folder) await saveSettings(folder); })}>保存目录</button>
          <button className="folder-path" title={preferences.output_dir} onClick={() => void action(() => window.quark.openFolder(preferences.output_dir))}>{preferences.output_dir || '尚未选择保存目录'}</button>
          <button className="primary" disabled={!canDownload} onClick={() => void downloadSelected()}>{transferring ? "加入队列" : "下载"}{selected.length ? `（${selected.length}）` : ''}</button></footer>
      </>}
      {page === 'share' && <section className="form-section"><h2>打开分享</h2><p>支持纯链接、带 pwd 的链接及包含提取码的分享文案。</p>
        <label>分享链接或文案<textarea value={shareText} onChange={event => { shareRevision.current++; setShareText(event.target.value); }} onBlur={() => { const text = shareText, revision = shareRevision.current, code = codeRevision.current; void action(async () => { if (!text.trim()) return; const parsed = await call('share.parse', { text }); if (revision === shareRevision.current && code === codeRevision.current && parsed.passcode) setPasscode(parsed.passcode); }); }} /></label>
        <label>提取码 <small>可手工修改</small><input aria-label="提取码" value={passcode} onChange={event => { codeRevision.current++; setPasscode(event.target.value); }} /></label><button className="primary" disabled={loading} onClick={() => void openShare()}>打开分享</button>
        <p className="note">浏览不会转存。分享下载使用临时目录，下载结束、失败或停止后清理。</p></section>}
      {page === 'tasks' && <TasksPanel tasks={tasks} busy={transferring} onTasks={acceptTasks} onStart={() => setTransferring(true)} onEnd={() => setTransferring(false)} onStatus={notify}/>}
      {(page === 'shares' || page === 'recycle') && (signedIn ? <CatalogPanel key={`${page}-${accountRevision}`} mode={page === 'shares' ? 'shares' : 'recycle'} busy={transferring || loggingIn} onStatus={notify}/> : <div className="empty">请先登录账号</div>)}
      {page === 'settings' && <section className="form-section settings-section"><h2>外观</h2><p className="section-description">选择适合你的主题，让浏览更舒服。</p><div className="theme-options" role="group" aria-label="外观主题">{([['system','跟随系统'],['light','浅色'],['dark','暗色']] as [AppearanceSource,string][]).map(([source,label]) => <label className={'theme-option'+(appearanceSource === source ? ' chosen' : '')} key={source}><span className={'theme-preview '+source} aria-hidden="true"><i/><span><b/><b/><b/></span></span><span className="theme-option-label"><input type="radio" name="appearance" value={source} checked={appearanceSource === source} disabled={appearanceSaving} onChange={() => void changeAppearance(source)}/>{label}</span></label>)}</div><p className="note">立即生效并自动保存。跟随系统会自动响应 Windows 主题变化。</p><div className="settings-divider"/><h2>下载设置</h2>
        <div className="transfer-settings"><div className="transfer-setting"><div><label htmlFor="thread-value">线程数</label><p>单文件并发连接，默认 4</p></div><input id="thread-value" aria-label="线程数" type="number" min="1" max="16" step="1" disabled={transferring} value={threads} onChange={event => setThreads(event.target.value)}/><div className="setting-slider"><span>1</span><input aria-label="拖动调整线程数" type="range" min="1" max="16" step="1" disabled={transferring} value={Math.min(16,Math.max(1,Number(threads) || 1))} style={{'--range-fill':`${(Math.min(16,Math.max(1,Number(threads) || 1))-1)/15*100}%`} as React.CSSProperties} onChange={event => setThreads(event.target.value)}/><span>16</span></div></div>
        <div className="transfer-setting"><div><label htmlFor="rate-value">总限速</label><p>MiB/s，0 表示不限速</p></div><input id="rate-value" aria-label="总限速" type="number" min="0" max="10240" step="0.01" disabled={transferring} value={rate} onChange={event => setRate(event.target.value)} title="滑块范围 0–100，输入框支持 0.01–10240 MiB/s"/><div className="setting-slider"><span>0</span><input aria-label="拖动调整总限速" type="range" min="0" max="100" step="0.01" disabled={transferring} value={Math.min(100,Math.max(0,Number(rate) || 0))} style={{'--range-fill':`${Math.min(100,Math.max(0,Number(rate) || 0))}%`} as React.CSSProperties} onChange={event => setRate(event.target.value)}/><span>100</span></div></div></div>
        <label>保存目录<input value={preferences.output_dir} readOnly /></label><button disabled={transferring} onClick={() => void action(async () => { const folder = await window.quark.chooseFolder(); if (folder) await saveSettings(folder); })}>选择目录</button>
        <button className="primary" disabled={transferring} onClick={() => void saveSettings()}>保存设置</button><p className="note">上传和下载队列逐项执行，线程数作用于单个下载文件。限速对上传和下载均生效，实际速度受账号与服务端限制。限速滑块为 0–100，输入框可填写更高值。</p>
        <div className="settings-divider"/><h2>后台运行</h2><div className="desktop-options"><label>关闭窗口时<select aria-label="关闭窗口行为" value={clientSettings.closeBehavior} disabled={clientSettingsSaving} onChange={event => void changeClientSettings({...clientSettings,closeBehavior:event.target.value as ClientSettings['closeBehavior']})}><option value="tray">最小化到托盘，继续传输</option><option value="quit">安全退出客户端</option></select></label><label className="notification-option"><input aria-label="传输完成通知" type="checkbox" checked={clientSettings.notifications} disabled={clientSettingsSaving} onChange={event => void changeClientSettings({...clientSettings,notifications:event.target.checked})}/>传输完成时显示系统通知</label></div><p className="note">即时生效并自动保存。托盘可打开窗口、暂停或继续任务；退出会等待当前操作结束并保存进度。</p><button onClick={() => void action(() => window.quark.quit())}>退出客户端</button></section>}
      <div className="status" role="status">{status}</div>
    </main>
    {accountMenu && <div className="menu-dismiss" onClick={() => setAccountMenu(false)}/>}
    {context && <><div className="menu-dismiss" onClick={() => setContext(null)} onContextMenu={event => {event.preventDefault();setContext(null);}}/><div ref={menu} className="context-menu" role="menu" aria-label="文件操作" style={{left:context.x,top:context.y}} onKeyDown={event => {
      shortcut(event);
      if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)) {
        event.preventDefault();const buttons=Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || []);
        const index=buttons.indexOf(document.activeElement as HTMLButtonElement);
        const next=event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length-1 : (index+(event.key === 'ArrowDown' ? 1 : -1)+buttons.length)%buttons.length;
        buttons[next]?.focus();
      }
    }}>
      {context.row && <>
        {context.row.directory && <button role="menuitem" disabled={loading || searching || selected.length !== 1} onClick={() => {const row=context.row!;setContext(null);void openRow(row);}}>打开目录</button>}
        <button role="menuitem" disabled={!canDownload} onClick={() => void downloadSelected()}>下载{selected.length > 1 ? `（${selected.length}）` : ''}</button>
        {!share && <button role="menuitem" disabled={!canModify || selected.length !== 1} onClick={() => {setContext(null);setSharingDialog('create');}}>分享</button>}
        <hr/>{!share && <>
          <button role="menuitem" disabled={!canModify || !selected.length} onClick={() => void cutSelected()}>剪切<kbd>Ctrl+X</kbd></button>
          <button role="menuitem" disabled={!canPaste} onClick={paste}>粘贴<kbd>Ctrl+V</kbd></button><hr/>
          <button role="menuitem" disabled={!canModify || !selected.length} onClick={() => fileAction('delete')}>删除<kbd>Delete</kbd></button>
          <button role="menuitem" disabled={!canModify || selected.length !== 1} onClick={() => fileAction('rename')}>重命名<kbd>F2</kbd></button>
          <button role="menuitem" disabled={!canModify || !selected.length} onClick={() => fileAction('move')}>移动到…</button><hr/>
          {context.row.directory && <button role="menuitem" disabled={favoriteBusy || loggingIn || selected.length !== 1 || loading || searching} onClick={() => void toggleFavorite(context.row!)}>{favorites.some(row => row.fid === context.row!.fid) ? '取消常用文件夹' : '设为常用文件夹'}</button>}
          <button role="menuitem" disabled={selected.length !== 1 || loading || searching} onClick={() => fileAction('info',[context.row!])}>详细信息</button>
        </>}
        <button role="menuitem" disabled={loading || searching} onClick={() => {setContext(null);void copyPaths();}}>复制路径<kbd>Ctrl+Shift+C</kbd></button>
      </>}
      {!context.row && <><button role="menuitem" disabled={loading || searching} onClick={() => {setContext(null);void (share ? openShare() : browse());}}>刷新</button>{!share && <><button role="menuitem" disabled={!canModify} onClick={() => fileAction('mkdir',[])}>新建目录</button><button role="menuitem" disabled={!canPaste} onClick={paste}>粘贴<kbd>Ctrl+V</kbd></button></>}</>}
    </div></>}
    {fileDialog && <FileDialog action={fileDialog.action} entries={fileDialog.entries} cutId={fileDialog.cutId} onCut={setCut} parent={trail} close={() => setFileDialog(null)} done={() => { void (searchView ? runSearch() : browse()); }} onStatus={notify}/ >}
    {sharingDialog && <SharingDialog mode={sharingDialog} entries={selected} handle={share} path={sharePath} close={() => setSharingDialog(null)} onStatus={notify}/>}
    {dropUpload && createPortal(<div className="modal-backdrop"><section className="modal file-modal" role="dialog" aria-modal="true" aria-label="确认拖拽上传"><h2>上传到当前目录</h2><p>目标：{dropUpload.target}</p>{dropUpload.items ? <><p>共 {dropUpload.items.length} 项 · {dropUpload.items.filter(item => item.directory).length} 个目录</p><ul className="drop-items">{dropUpload.items.slice(0,50).map(item => <li key={item.path}><FileIcon folder={item.directory}/><span title={item.path}>{item.name}</span><small>{item.directory ? '目录' : '文件'}</small></li>)}</ul>{dropUpload.items.length > 50 && <p className="note">另有 {dropUpload.items.length-50} 项，确认后全部加入队列。</p>}<p className="note">同名冲突不会自动覆盖。目录内容在正式上传时读取。</p></> : <p>正在读取拖入项目…</p>}<div className="dialog-actions"><button autoFocus disabled={dropBusy} onClick={cancelDrop}>取消</button><button className="primary" disabled={dropBusy || !dropUpload.items} onClick={() => void confirmDrop()}>{dropBusy ? '正在加入队列…' : '确认上传'}</button></div></section></div>,document.body)}
    {loginPicker && <div className="modal-backdrop"><section className="modal login-picker" role="dialog" aria-modal="true" aria-label={signedIn ? '切换账号' : '登录账号'}><h2>{signedIn ? '切换账号' : '登录夸克账号'}</h2><p>选择官方支持的登录方式</p><button disabled={transferring} onClick={() => void login('qr')}>扫码登录</button><button disabled={transferring} onClick={() => void login('mobile')}>手机验证码登录</button><button disabled={transferring} onClick={() => void login('wechat')}>微信登录</button><p className="note">手机和微信登录在独立的夸克官方页面完成。{signedIn && '取消登录会保留原账号。'}</p><button onClick={() => setLoginPicker(false)}>取消</button></section></div>}
    {loggingIn && <div className="modal-backdrop"><div className="modal" role="dialog" aria-modal="true" aria-label={loginMode === 'qr' ? '扫码登录' : '官方网页登录'}><h2>{loginMode === 'qr' ? '扫码登录' : '官方网页登录'}</h2>{loginMode === 'qr' ? <><p>使用手机夸克 App 扫描二维码</p>{qr ? <img src={qr} alt="夸克登录二维码" /> : <div className="qr-loading">{loginCancelling ? '正在取消，等待当前请求结束…' : '正在获取二维码…'}</div>}<button disabled={loginCancelling} onClick={() => {setLoginCancelling(true);setQr(null);void action(() => call('login.cancel'));}}>{loginCancelling ? '正在取消…' : '取消登录'}</button></> : <p>请在官方窗口完成{loginMode === 'mobile' ? '手机验证码' : '微信'}登录，关闭该窗口可取消。</p>}</div></div>}
  </div>{quitting && <div className="modal-backdrop exit-backdrop"><section className="modal" role="alertdialog" aria-modal="true" aria-label="正在安全退出"><h2>正在安全退出…</h2><p>正在停止任务并保存进度，请稍候。</p><p className="note">当前网络请求或文件操作结束后，客户端会自动退出。</p></section></div>}</>;
}
createRoot(document.getElementById('root')!).render(<FeedbackProvider><App /></FeedbackProvider>);
