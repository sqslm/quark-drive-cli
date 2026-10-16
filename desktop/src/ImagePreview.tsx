import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Entry } from './types';
import { ImageData, imageKey, images } from './Images';

function useImage(entry:Entry,handle:string|null,thumbnail:boolean,active=true) {
  const key=imageKey(entry,handle,thumbnail);
  const [retryRequest,setRetryRequest]=useState({key,attempt:0});
  const attempt=retryRequest.key === key ? retryRequest.attempt : 0;
  const [state,setState]=useState<{key:string;data?:ImageData;error?:string}>({key});
  useEffect(() => {
    if(!active) return;
    setState({key});
    return images.request(key,{entry,handle,thumbnail,refresh:attempt>0},!thumbnail,
      data => setState({key,data}),error => setState({key,error:error.message}));
  },[key,attempt,active]);
  return {...(state.key === key ? state : {}),retry:() => setRetryRequest(old => ({key,attempt:(old.key === key ? old.attempt : 0)+1}))};
}

export function ImageThumbnail({entry,handle,active}:{entry:Entry;handle:string|null;active:boolean}) {
  const image=useImage(entry,handle,true,active);
  return <span className="image-thumbnail" title={image.error ? '缩略图加载失败，点击文件名打开预览后重试' : '打开图片预览'}>
    {image.data ? <img src={image.data.src} alt="" draggable={false}/> : <span aria-hidden="true">{image.error ? '◇' : '▧'}</span>}
  </span>;
}

export function ImagePreview({entries,start,handle,close}:{entries:Entry[];start:string;handle:string|null;close:()=>void}) {
  const [index,setIndex]=useState(Math.max(0,entries.findIndex(entry => entry.fid === start)));
  const [zoom,setZoom]=useState(1);
  const [broken,setBroken]=useState(false);
  const entry=entries[index];
  const image=useImage(entry,handle,false);
  const dialog=useRef<HTMLElement>(null);
  const stage=useRef<HTMLDivElement>(null);
  useEffect(() => {const previous=document.activeElement as HTMLElement|null;dialog.current?.focus();return () => {previous?.isConnected && previous.focus();};},[]);
  useEffect(() => {setZoom(1);setBroken(false);stage.current?.scrollTo(0,0);},[entry.fid]);
  function changeZoom(value:number) {setZoom(Math.min(4,Math.max(.25,value)));}
  useEffect(() => {
    // A loading transition can disable the focused zoom button. Keep gallery
    // shortcuts and the focus trap active even when the browser moves focus.
    const keydown=(event:KeyboardEvent) => {
      event.stopPropagation();
      if(event.key === 'Escape') {event.preventDefault();close();}
      else if(event.key === 'ArrowLeft' && index>0) {event.preventDefault();setIndex(index-1);}
      else if(event.key === 'ArrowRight' && index<entries.length-1) {event.preventDefault();setIndex(index+1);}
      else if(event.key === '+' || event.key === '=') changeZoom(zoom+.25);
      else if(event.key === '-') changeZoom(zoom-.25);
      else if(event.key === 'Tab') {
        const buttons=Array.from(dialog.current!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'));
        const current=buttons.indexOf(document.activeElement as HTMLButtonElement);
        if(event.shiftKey && current<=0) {event.preventDefault();buttons.at(-1)?.focus();}
        else if(!event.shiftKey && (current<0 || current===buttons.length-1)) {event.preventDefault();buttons[0]?.focus();}
      }
    };
    document.addEventListener('keydown',keydown,true);
    return () => document.removeEventListener('keydown',keydown,true);
  },[index,zoom,close,entries.length]);
  return createPortal(<div className="modal-backdrop image-backdrop" onClick={event => {if(event.target === event.currentTarget) close();}}>
    <section className="image-preview" role="dialog" aria-modal="true" aria-label="图片预览" tabIndex={-1} ref={dialog}>
      <header><div><strong title={entry.name}>{entry.name}</strong><small>{index+1} / {entries.length} · 预览图</small></div><button aria-label="关闭预览" onClick={close}>×</button></header>
      <div className="image-stage" ref={stage}>
        {image.data && !broken ? <div className="image-canvas" style={{width:`${Math.max(1,zoom)*100}%`,height:`${Math.max(1,zoom)*100}%`}}><img src={image.data.src} alt={entry.name} style={{width:`${Math.min(1,zoom)*100}%`,height:`${Math.min(1,zoom)*100}%`}} draggable={false} onError={() => setBroken(true)}/></div> : <div className="image-message" role="status">{image.error || broken ? <><p>{image.error || '图片显示失败'}</p><button onClick={() => {setBroken(false);image.retry();dialog.current?.focus();}}>重新加载图片</button></> : '正在加载图片…'}</div>}
      </div>
      <footer><button aria-label="上一张" disabled={index===0} onClick={() => setIndex(index-1)}>← 上一张</button><div><button aria-label="缩小" disabled={zoom<=.25 || !image.data} onClick={() => changeZoom(zoom-.25)}>−</button><button aria-label="适应窗口" onClick={() => setZoom(1)}>{Math.round(zoom*100)}%</button><button aria-label="放大" disabled={zoom>=4 || !image.data} onClick={() => changeZoom(zoom+.25)}>＋</button></div><button aria-label="下一张" disabled={index===entries.length-1} onClick={() => setIndex(index+1)}>下一张 →</button></footer>
      <p className="image-hint">← → 切换 · ＋ / − 缩放 · Esc 关闭{image.data && ` · ${image.data.width} × ${image.data.height}`}</p>
    </section>
  </div>,document.body);
}
