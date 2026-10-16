import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Entry } from './types';
import { previewError } from './Images';
import { useFeedback } from './Feedback';
import { formatSize } from './size';

export const isText = (entry:Entry) => !entry.directory && /\.(json|txt|md|markdown|log|csv|ya?ml|toml|ini|xml|conf)$/i.test(entry.name);
type TextData={text:string;encoding:string;size:number;modified_at:number|null};

export function TextPreview({entry,close}:{entry:Entry;close:()=>void}) {
  const [revision,refresh]=useState(0);
  const [wrap,setWrap]=useState(true);
  const [state,setState]=useState<{data:TextData|null;loading:boolean;error:string}>({data:null,loading:true,error:''});
  const dialog=useRef<HTMLElement>(null);
  const notify=useFeedback();
  useEffect(() => {
    let active=true;
    setState(old => ({...old,loading:true,error:''}));
    void window.quark.call('text.read',{entry}).then(data => {if(active) setState({data,loading:false,error:''});},
      error => {if(active) setState(old => ({...old,loading:false,error:previewError(error)}));});
    return () => {active=false;};
  },[entry,revision]);
  useEffect(() => {const previous=document.activeElement as HTMLElement|null;dialog.current?.focus();return () => {if(previous?.isConnected) previous.focus();};},[]);
  useEffect(() => {
    const keydown=(event:KeyboardEvent) => {
      event.stopPropagation();
      if(event.key === 'Escape') {event.preventDefault();close();}
      else if(event.key === 'Tab') {
        const controls=Array.from(dialog.current!.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),[tabindex="0"]'));
        const current=controls.indexOf(document.activeElement as HTMLElement);
        if(event.shiftKey && current<=0) {event.preventDefault();controls.at(-1)?.focus();}
        else if(!event.shiftKey && (current<0 || current === controls.length-1)) {event.preventDefault();controls[0]?.focus();}
      }
    };
    document.addEventListener('keydown',keydown,true);
    return () => document.removeEventListener('keydown',keydown,true);
  },[close]);
  async function copy() {
    if(!state.data) return;
    try {await window.quark.copyText(state.data.text);notify('已复制文本内容','success');}
    catch(error) {notify(previewError(error),'error');}
  }
  return createPortal(<div className="modal-backdrop image-backdrop" onClick={event => {if(event.target === event.currentTarget) close();}}>
    <section className="image-preview text-preview" role="dialog" aria-modal="true" aria-label="文本预览" tabIndex={-1} ref={dialog}>
      <header><div><strong title={entry.name}>{entry.name}</strong><small>云端只读 · 刷新时重新读取最新内容</small></div><button aria-label="关闭文本预览" onClick={close}>×</button></header>
      <div className="text-controls"><span role="status">{state.loading ? '正在读取云端内容…' : state.data ? `${state.data.encoding} · ${formatSize(state.data.size)}` : '读取失败'}</span><label><input type="checkbox" checked={wrap} onChange={event => setWrap(event.target.checked)}/>自动换行</label><button disabled={!state.data || state.loading} onClick={() => void copy()}>复制内容</button><button disabled={state.loading} onClick={() => refresh(value => value+1)}>{state.loading ? '读取中…' : state.error ? '重新读取' : '刷新内容'}</button></div>
      {state.error && <p className="text-error" role="alert">{state.error}{state.data && '；以下保留上次读取的内容'}</p>}
      {state.data ? state.data.text ? <pre className={'text-content'+(wrap ? ' wrap' : '')} tabIndex={0} aria-label="文件内容" aria-busy={state.loading}>{state.data.text}</pre> : <div className="image-message">文件内容为空</div> : <div className="image-message">{state.loading ? '正在读取云端内容…' : '请点击“重新读取”重试，也可以下载后查看。'}</div>}
      <p className="image-hint">只读预览 · 最多 2 MiB · Markdown 按源文显示 · Esc 关闭</p>
    </section>
  </div>,document.body);
}
