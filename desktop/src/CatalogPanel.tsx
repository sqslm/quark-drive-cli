import React, { useEffect, useRef, useState } from 'react';
import { useVirtualizer } from '@tanstack/react-virtual';
import { shareText } from './SharingDialog';
import { Feedback } from './Feedback';
import { ClientSettings } from './types';
import { shareExpiry } from './shareExpiry';

type Row = {share_id?:string;record_id?:string;title?:string;name?:string;share_url?:string;expired_type?:number;expired_at?:number;directory?:boolean;size?:number;parent?:string;deleted_at?:number};
export function CatalogPanel({mode,busy,onStatus,expiryMode}: {mode:'shares'|'recycle';busy:boolean;onStatus:Feedback;expiryMode:ClientSettings['shareExpiryMode']}) {
  const [rows,setRows] = useState<Row[]>([]);
  const [selected,setSelected] = useState<Set<string>>(new Set());
  const [loading,setLoading] = useState(false);
  const [error,setError] = useState('');
  const [ready,setReady] = useState(false);
  const [confirm,setConfirm] = useState<'revoke'|'restore'|'remove'|null>(null);
  const [confirmed,setConfirmed] = useState(false);
  const [results,setResults] = useState<{record_id?:string;share_id?:string;name:string;detail:string;state:string}[]|null>(null);
  const [stage,setStage] = useState('');
  const [now,setNow] = useState(Date.now);
  const scroll = useRef<HTMLDivElement>(null);
  const generation = useRef(0);
  const key = (row:Row) => String(mode === 'shares' ? row.share_id : row.record_id);
  const virtual = useVirtualizer({count:rows.length,getScrollElement:()=>scroll.current,estimateSize:()=>76,overscan:8});
  const picked = rows.filter(row => selected.has(key(row)));
  async function refresh() {
    const id = ++generation.current;setLoading(true);setReady(false);setError('');
    try {
      const response = await window.quark.call(mode === 'shares' ? 'share.list' : 'recycle.list');
      if(id === generation.current) {setRows(mode === 'shares' ? response.items : response);setSelected(new Set());setReady(true);}
    } catch(error) {if(id === generation.current) setError(error instanceof Error ? error.message : '列表读取失败');}
    finally {if(id === generation.current) setLoading(false);}
  }
  useEffect(() => {void refresh();return () => {++generation.current;};},[mode]);
  useEffect(() => {
    if(mode !== 'shares') return;
    const tick = () => setNow(Date.now());
    const interval = window.setInterval(tick,1000);
    window.addEventListener('focus',tick);document.addEventListener('visibilitychange',tick);
    return () => {window.clearInterval(interval);window.removeEventListener('focus',tick);document.removeEventListener('visibilitychange',tick);};
  },[mode]);
  async function submit() {
    setLoading(true);setError('');
    const id=crypto.randomUUID();
    setStage(`正在核验 ${picked.length} 个选中项目…`);
    const off=window.quark.onEvent(event => {if(event.id === id && ['recycle.progress','share.progress'].includes(event.event)) setStage(String(event.data));});
    try {
      const response = await window.quark.call(confirm === 'revoke' ? 'share.revoke' : confirm === 'restore' ? 'recycle.restore' : 'recycle.remove',{entries:picked},id);
      setResults(response);
      const done=new Set<string>(response.filter((row:{state:string}) => row.state === 'done').map((row:Row) => key(row)));
      setRows(old => old.filter(row => !done.has(key(row))));
      setSelected(old => new Set([...old].filter(value => !done.has(value))));
      if(response.some((row:{state:string}) => row.state === 'unknown')) {
        setReady(false);setError('有未确认的操作结果，请刷新核实后再操作。');
      }
      const failed = response.some((row:{state:string}) => row.state !== 'done');onStatus(failed ? '部分项目未完成，请查看逐项结果' : '操作已完成，请查看逐项结果', failed ? 'warning' : 'success');
    } catch(error) {const text=error instanceof Error ? error.message : '操作失败';setError(text);onStatus(text,'error');}
    finally {off();setStage('');setLoading(false);}
  }
  async function copy(row:Row) {
    setLoading(true);setError('');
    try {const result = await window.quark.call('share.link',{share_id:row.share_id});await window.quark.copyText(shareText(result));onStatus('已复制分享文案','success');}
    catch(error) {const text=error instanceof Error ? error.message : '复制分享失败';setError(text);onStatus(text,'error');}
    finally {setLoading(false);}
  }
  const disabled = busy || loading || !ready || !picked.length;
  function open(operation:'revoke'|'restore'|'remove') {setConfirm(operation);setResults(null);setConfirmed(false);}
  const title = confirm === 'revoke' ? '取消选中分享' : confirm === 'restore' ? '恢复选中记录' : '永久删除选中记录';
  return <><div className="toolbar"><button disabled={loading} onClick={() => void refresh()}>刷新</button>
    {mode === 'shares' ? <button disabled={disabled} onClick={() => open('revoke')}>取消选中分享</button> : <><button disabled={disabled} onClick={() => open('restore')}>恢复选中记录</button><button disabled={disabled} onClick={() => open('remove')}>永久删除选中记录</button></>}
    <span className="catalog-count">{rows.length} 项 · 已选 {picked.length} 项</span></div>
    {error && <p className="error catalog-error" role="alert">{error}</p>}
    <div className="catalog-header"><input type="checkbox" aria-label="全选当前记录" disabled={!ready || loading} checked={rows.length > 0 && picked.length === rows.length} onChange={() => setSelected(picked.length === rows.length ? new Set() : new Set(rows.map(key)))}/><span>{mode === 'shares' ? '分享名称 / 链接' : '名称 / 原目录'}</span><span>{mode === 'shares' ? '剩余有效期' : '类型 / 大小'}</span><span>操作</span></div>
    <div className="catalog-list" ref={scroll}>{!rows.length && <div className="empty">{loading ? '正在读取…' : error ? '读取失败，请刷新重试' : mode === 'shares' ? '暂无本人分享' : '回收站为空'}</div>}<div style={{height:virtual.getTotalSize(),position:'relative'}}>{virtual.getVirtualItems().map(item => {const row = rows[item.index];return <div className="catalog-row" key={key(row)} style={{transform:`translateY(${item.start}px)`}}>
      <input type="checkbox" disabled={loading || !ready} aria-label={`选择记录 ${row.title || row.name}`} checked={selected.has(key(row))} onChange={() => setSelected(old => {const next = new Set(old);next.has(key(row)) ? next.delete(key(row)) : next.add(key(row));return next;})}/>
      <div><strong>{row.title || row.name || row.share_id}</strong><p title={row.share_url || row.parent}>{mode === 'shares' ? row.share_url || '复制时读取链接' : row.parent ? `原目录 FID：${row.parent}` : '原目录信息不可用'}</p></div>
      <span title={mode === 'shares' ? shareExpiry(row,now,expiryMode).title : undefined}>{mode === 'shares' ? shareExpiry(row,now,expiryMode).text : row.directory ? '目录' : `${row.size ?? '—'} B`}</span>
      {mode === 'shares' ? <button disabled={loading} onClick={() => void copy(row)}>复制分享文案</button> : <span>已删除</span>}</div>;})}</div></div>
    {confirm && <div className="modal-backdrop"><section className="modal file-modal" role="dialog" aria-modal="true" aria-label={title}><h2>{title}</h2>
      {loading && stage && <p className="catalog-progress" aria-live="polite">{stage}</p>}
      {!results && <><p>已选择 {picked.length} 项：{picked.map(row => row.title || row.name).join('、')}</p><p>{confirm === 'remove' ? '这些记录将永久删除，无法从回收站恢复。' : confirm === 'restore' ? '恢复到原目录，同名冲突不覆盖。' : '选中链接将失效，网盘文件保留。'}</p>
        {confirm === 'remove' && <label className="confirm-checkbox"><input type="checkbox" disabled={loading} checked={confirmed} onChange={event => setConfirmed(event.target.checked)}/>我确认永久删除这些记录</label>}</>}
      {results && <div className="operation-results">{results.map((row,index) => <p className={row.state !== 'done' ? 'error' : ''} key={index}>{row.name}：{row.detail}</p>)}</div>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="dialog-actions"><button disabled={loading} onClick={() => setConfirm(null)}>{results ? '关闭' : '取消'}</button>{!results && <button className="primary" disabled={loading || busy || (confirm === 'remove' && !confirmed)} onClick={() => void submit()}>{loading ? '处理中…' : '确认'}</button>}</div></section></div>}
  </>;
}
