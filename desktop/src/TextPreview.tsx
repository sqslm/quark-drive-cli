import React, { Suspense, useDeferredValue, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Entry } from './types';
import { previewError } from './Images';
import { useFeedback } from './Feedback';
import MarkdownView from './MarkdownView';
export const isText = (entry:Entry) => !entry.directory && /\.(json|txt|md|markdown|log|csv|ya?ml|toml|ini|xml|conf)$/i.test(entry.name);
type Opened={text:string;encoding:string;editor:string;version:string;entry:Entry};

export function TextPreview({entry,close,onSaved}:{entry:Entry;close:()=>void;onSaved:(oldFid:string,entry:Entry)=>void}) {
  const [revision,refresh]=useState(0), [wrap,setWrap]=useState(true);
  const [mode,setMode]=useState<'source'|'rendered'>('source');
  const [draft,setDraft]=useState(''), [saved,setSaved]=useState('');
  const [loading,setLoading]=useState(true), [saving,setSaving]=useState(false), [paused,setPaused]=useState(false);
  const [hasContent,setHasContent]=useState(false);
  const [composing,setComposing]=useState(false);
  const [error,setError]=useState(''), [confirm,setConfirm]=useState<'close'|'refresh'|null>(null);
  const session=useRef<Opened|null>(null), current=useRef(entry), text=useRef(''), baseline=useRef('');
  const running=useRef(false), active=useRef(true);
  const dialog=useRef<HTMLElement>(null);
  const notify=useFeedback();
  const rendered=useDeferredValue(draft);
  const dirty=draft!==saved;
  const markdown=/\.(md|markdown)$/i.test(entry.name);
  useEffect(() => {
    active.current=true;
    return () => {active.current=false;void window.quark.setEditorDirty(false);};
  },[]);
  useEffect(() => {void window.quark.setEditorDirty(dirty || saving);},[dirty,saving]);
  useEffect(() => {
    let alive=true;let identifier:string|null=null;
    setLoading(true);setError('');setPaused(false);
    void window.quark.call('text.open',{entry:current.current}).then((data:Opened) => {
      identifier=data.editor;
      if(!alive) {void window.quark.call('text.close',{editor:identifier});return;}
      session.current=data;current.current=data.entry;text.current=baseline.current=data.text;
      setDraft(data.text);setSaved(data.text);setHasContent(true);setLoading(false);
    },error => {if(alive) {setLoading(false);setError(previewError(error));}});
    return () => {alive=false;session.current=null;if(identifier) void window.quark.call('text.close',{editor:identifier});};
  },[revision]);
  useEffect(() => {const previous=document.activeElement as HTMLElement|null;dialog.current?.focus();return () => {if(previous?.isConnected) previous.focus();};},[]);
  async function save() {
    const opened=session.current;
    if(!opened || running.current || composing || confirm || text.current===baseline.current) return;
    const sent=text.current, oldFid=opened.entry.fid;
    running.current=true;setSaving(true);setError('');
    try {
      const result=await window.quark.call('text.save',{editor:opened.editor,version:opened.version,text:sent});
      if(!active.current) return;
      opened.version=result.version;opened.entry=result.entry;current.current=result.entry;
      baseline.current=sent;setSaved(sent);setPaused(!!result.blocked);setError(result.warning || '');
      onSaved(oldFid,result.entry);
    } catch(error) {if(active.current) {setPaused(true);setError(previewError(error));}}
    finally {running.current=false;if(active.current) setSaving(false);}
  }
  useEffect(() => {
    if(!dirty || loading || saving || paused || confirm || composing) return;
    const timer=window.setTimeout(() => void save(),1000);
    return () => window.clearTimeout(timer);
  },[draft,saved,loading,saving,paused,confirm,composing]);
  function request(action:'close'|'refresh') {
    if(running.current) {notify('正在保存，请等待云端确认后再关闭或刷新','info');return;}
    if(text.current!==baseline.current) {setConfirm(action);return;}
    if(action==='close') close();else refresh(value => value+1);
  }
  useEffect(() => {
    const keydown=(event:KeyboardEvent) => {
      event.stopPropagation();
      if((event.ctrlKey || event.metaKey) && event.key.toLowerCase()==='s') {event.preventDefault();void save();}
      else if(event.key==='Escape') {event.preventDefault();if(confirm) setConfirm(null);else request('close');}
      else if(event.key==='Tab') {
        const controls=Array.from(dialog.current!.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),[tabindex="0"]'));
        const index=controls.indexOf(document.activeElement as HTMLElement);
        if(event.shiftKey && index<=0) {event.preventDefault();controls.at(-1)?.focus();}
        else if(!event.shiftKey && (index<0 || index===controls.length-1)) {event.preventDefault();controls[0]?.focus();}
      }
    };
    document.addEventListener('keydown',keydown,true);
    return () => document.removeEventListener('keydown',keydown,true);
  });
  async function copy() {
    try {await window.quark.copyText(text.current);notify('已复制文本内容','success');}
    catch(error) {notify(previewError(error),'error');}
  }
  return createPortal(<div className="modal-backdrop image-backdrop" onClick={event => {if(event.target===event.currentTarget) request('close');}}>
    <section className="image-preview text-preview" role="dialog" aria-modal="true" aria-label="文本预览" tabIndex={-1} ref={dialog}>
      <header><div><strong title={entry.name}>{entry.name}</strong><small>在线编辑 · 停止输入约 1 秒后保存 · Ctrl+S 立即保存</small></div><button aria-label="关闭文本预览" onClick={() => request('close')}>×</button></header>
      <div className="text-controls"><span role="status">{loading ? '正在读取云端内容…' : saving ? '正在保存到云端…' : paused ? '自动保存已暂停' : dirty ? '有未保存的修改' : session.current ? '已同步到云端' : '读取失败'}{session.current && ` · ${session.current.encoding}`}</span>
        {markdown && <div className="markdown-modes" role="group" aria-label="Markdown 显示格式"><button aria-pressed={mode==='source'} onClick={() => setMode('source')}>原格式</button><button aria-pressed={mode==='rendered'} onClick={() => setMode('rendered')}>渲染格式</button></div>}
        <label><input type="checkbox" checked={wrap} onChange={event => setWrap(event.target.checked)}/>自动换行</label>
        <button disabled={!hasContent || loading} onClick={() => void copy()}>复制内容</button><button disabled={!session.current || loading || saving || composing || !!confirm || !dirty} onClick={() => void save()}>保存到云端</button><button disabled={loading || saving} onClick={() => request('refresh')}>{loading ? '读取中…' : error ? '重新读取' : '刷新内容'}</button></div>
      {error && <p className="text-error" role="alert">{error}{hasContent ? '；当前内容保留，草稿可复制' : ''}</p>}
      {confirm && <div className="editor-confirm" role="alertdialog" aria-label="未保存的修改"><span>还有未保存的修改。确认放弃{confirm==='close' ? '并关闭' : '并重新读取'}？</span><button onClick={() => setConfirm(null)}>继续编辑</button><button onClick={() => {const action=confirm;setConfirm(null);if(action==='close') close();else refresh(value => value+1);}}>放弃修改</button></div>}
      {hasContent && !loading ? <div className={'text-workspace'+(markdown && mode==='rendered' ? ' split' : '')}>
        <textarea className={'text-content text-editor'+(wrap ? ' wrap' : '')} aria-label="文件内容" value={draft} disabled={!!confirm} spellCheck={false} maxLength={2097152} wrap={wrap ? 'soft' : 'off'} onCompositionStart={() => setComposing(true)} onCompositionEnd={() => setComposing(false)} onChange={event => {text.current=event.target.value;setDraft(event.target.value);}}/>
        {markdown && mode==='rendered' && <article className="markdown-render" aria-label="Markdown 渲染预览"><Suspense fallback={<p>正在加载 Markdown 渲染…</p>}><MarkdownView text={rendered}/></Suspense></article>}
      </div> : <div className="image-message">{loading ? '正在读取云端内容…' : '请点击“重新读取”重试，也可以下载后查看。'}</div>}
      <p className="image-hint">最多 2 MiB · 保留原编码 · 替换保存会更换 FID，旧版留回收站 · 分享可能受影响</p>
    </section>
  </div>,document.body);
}
