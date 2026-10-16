import { Entry } from './types';

export type ImageData = {src:string;width:number;height:number};
type Job = {key:string;params:Record<string,unknown>;preview:boolean;listeners:Set<{resolve:(data:ImageData)=>void;reject:(error:Error)=>void}>;running:boolean;generation:number};
export const isImage = (entry:Entry) => !entry.directory && /\.(jpe?g|png|webp)$/i.test(entry.name);

// Only mounted, visible rows hold a lease. Navigation releases queued work;
// already issued requests finish without delivering data to abandoned views.
export class ImageQueue {
  private cache = new Map<string,ImageData>();
  private bytes = 0;
  private jobs = new Map<string,Job>();
  private active = 0;
  private generation = 0;
  constructor(private call:(params:Record<string,unknown>)=>Promise<ImageData>,private limit=16*1024*1024) {}
  clear() {
    ++this.generation;this.cache.clear();this.bytes=0;
    for(const job of this.jobs.values()) {job.listeners.clear();}
    this.jobs.clear();
  }
  request(key:string,params:Record<string,unknown>,preview:boolean,resolve:(data:ImageData)=>void,reject:(error:Error)=>void) {
    const cached=this.cache.get(key);
    if(cached && !params.refresh) {this.cache.delete(key);this.cache.set(key,cached);resolve(cached);return () => {};}
    if(cached) {this.cache.delete(key);this.bytes-=cached.src.length*2;}
    let job=this.jobs.get(key);
    if(!job) {job={key,params,preview,listeners:new Set(),running:false,generation:this.generation};this.jobs.set(key,job);}
    const listener={resolve,reject};job.listeners.add(listener);this.pump();
    return () => {job!.listeners.delete(listener);if(!job!.running && !job!.listeners.size) this.jobs.delete(key);};
  }
  private pump() {
    while(this.active < 2) {
      const next=Array.from(this.jobs.values()).filter(job => !job.running && job.listeners.size).sort((a,b) => Number(b.preview)-Number(a.preview))[0];
      if(!next) break;
      next.running=true;++this.active;
      void this.call(next.params).then(data => {
        if(next.generation !== this.generation) return;
        const size=data.src.length*2;
        if(next.listeners.size && size <= this.limit) {
          while(this.bytes+size > this.limit && this.cache.size) {const first=this.cache.keys().next().value!;this.bytes-=this.cache.get(first)!.src.length*2;this.cache.delete(first);}
          this.cache.set(next.key,data);this.bytes+=size;
        }
        for(const listener of next.listeners) listener.resolve(data);
      },error => {for(const listener of next.listeners) listener.reject(error instanceof Error ? error : new Error('图片加载失败'));}).finally(() => {
        --this.active;if(this.jobs.get(next.key) === next) this.jobs.delete(next.key);this.pump();
      });
    }
  }
}
export const images=new ImageQueue(params => window.quark.call('image.read',params));
export function imageKey(entry:Entry,handle:string|null,thumbnail:boolean) {
  return JSON.stringify([handle,entry.fid,entry.name,entry.size,entry.modified_at,thumbnail]);
}
