import React, { useEffect, useRef, useState } from 'react';
import { formatSize } from './size';

type Capacity={used_bytes:number;total_bytes:number};
const size=(value:number) => value>=1099511627776 ? `${(value/1099511627776).toFixed(2)} TB` : formatSize(value);

export function CapacityMeter({enabled}:{enabled:boolean}) {
  const [capacity,setCapacity]=useState<Capacity|null>(null);
  const [loading,setLoading]=useState(false),[failed,setFailed]=useState(false);
  const refresh=useRef<(()=>void)|null>(null);
  useEffect(() => {
    setCapacity(null);setFailed(false);
    if(!enabled) {setLoading(false);return;}
    let disposed=false,running=false,again=false;
    async function load() {
      if(running) {again=true;return;}
      running=true;setLoading(true);
      do {
        again=false;
        try {
          const data=await window.quark.call('account.capacity');
          if(!data || !Number.isSafeInteger(data.used_bytes) || !Number.isSafeInteger(data.total_bytes) || data.used_bytes<0 || data.total_bytes<0) throw new Error('invalid capacity');
          if(!disposed) {setCapacity(data);setFailed(false);}
        } catch {if(!disposed) setFailed(true);}
      } while(again && !disposed);
      running=false;if(!disposed) setLoading(false);
    }
    refresh.current=() => void load();
    const off=window.quark.onEvent(event => {if(event.event==='capacity.changed' || event.event==='batch.finished') void load();});
    void load();
    return () => {disposed=true;refresh.current=null;off();};
  },[enabled]);
  const data=enabled ? capacity : null;
  const percent=data && data.total_bytes>0 ? data.used_bytes/data.total_bytes*100 : null;
  return <section className="capacity-meter" aria-label="网盘空间">
    <div className="capacity-heading"><span>网盘空间</span><span>{percent===null ? '' : `${percent.toFixed(1)}%`}</span><button disabled={!enabled || loading} title="刷新网盘空间" aria-label="刷新网盘空间" onClick={() => refresh.current?.()}>↻</button></div>
    {data && <><div className={'capacity-track'+(percent!==null && percent>=90 ? ' full' : '')} role="progressbar" aria-label="网盘空间占比" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent===null ? undefined : Math.min(100,percent)} aria-valuetext={percent===null ? '总容量为零，占比未知' : `已用 ${percent.toFixed(1)}%`}><span style={{width:percent===null ? '0%' : `${Math.min(100,percent)}%`}}/></div><p title={`已用 ${size(data.used_bytes)} / 总容量 ${size(data.total_bytes)}`}>{size(data.used_bytes)} / {size(data.total_bytes)}</p></>}
    <small>{!enabled ? '登录后查看用量' : failed ? data ? '更新失败，显示上次用量' : '空间暂不可用，点击刷新重试' : loading ? '正在更新用量…' : data && data.used_bytes>data.total_bytes ? '已超过当前容量' : data && data.total_bytes===0 ? '总容量为零，占比未知' : '已用 / 总容量'}</small>
  </section>;
}
