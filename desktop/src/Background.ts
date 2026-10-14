import { Menu, Tray } from 'electron';
import { Task } from './types';

/** Native background actions use the same authorized queue as the window. */
export class Background {
  readonly tray:Tray;
  busy=false;
  available=true;
  quitting=false;
  notifications=true;
  private pending=false;
  private tasks=new Map<string,Task>();
  private notifiedBatch='';
  constructor(icon:string,private open:(transfers?:boolean) => void,
    private request:(method:string) => Promise<any>,private send:(event:string,data:unknown) => void,
    private quit:() => void) {
    this.tray=new Tray(icon);
    this.tray.on('click',() => this.open());
    this.tray.on('balloon-click',() => this.open(true));
    this.update();
  }
  snapshot(tasks:Task[]) {
    this.tasks=new Map(tasks.map(task => {
      const previous=this.tasks.get(task.id);
      return [task.id,previous && (previous.revision || 0) > (task.revision || 0) ? previous : task];
    }));
    this.update();
  }
  event(event:string,data:any) {
    if(event === 'task') {
      const previous=this.tasks.get(data.id);
      if(previous && (previous.revision || 0) > (data.revision || 0)) return;
      this.tasks.set(data.id,data);
      if(previous?.state !== data.state || previous?.valid !== data.valid || previous?.needs_authorization !== data.needs_authorization) this.update();
    } else if(event === 'batch.started') {this.busy=true;this.update();}
    else if(event === 'batch.finished') {
      this.busy=false;this.update();
      const {done=0,failed=0}=data.counts || {};
      if(this.notifications && !this.quitting && !data.stopped && done+failed > 0 && data.batch_id !== this.notifiedBatch) {
        this.notifiedBatch=data.batch_id;
        try {this.tray.displayBalloon({title:failed ? '传输已结束，部分任务失败' : '传输完成',content:`本批次成功 ${done} 项，失败 ${failed} 项。点击查看传输任务。`,iconType:failed ? 'warning' : 'info',noSound:true,respectQuietTime:true});}
        catch {this.send('warning','系统通知未能显示，请在传输页查看结果');}
      }
    }
  }
  update() {
    const enabled=this.available && !this.quitting && !this.pending;
    const resumable=[...this.tasks.values()].some(task => task.valid && !task.needs_authorization && ['waiting','failed','stopped'].includes(task.state));
    this.tray.setToolTip(`夸克网盘客户端 · ${this.quitting ? '正在退出' : !this.available ? '后端已退出' : this.busy ? '正在传输' : '就绪'}`);
    this.tray.setContextMenu(Menu.buildFromTemplate([
      {id:'open',label:'打开窗口',enabled:!this.quitting,click:() => this.open()},
      {id:'transfers',label:'传输任务',enabled:!this.quitting,click:() => this.open(true)},
      {type:'separator'},
      {id:'pause',label:'暂停全部传输',enabled:enabled && this.busy,click:() => void this.action('download.stop')},
      {id:'resume',label:'继续未完成任务',enabled:enabled && !this.busy && resumable,click:() => void this.action('tasks.retry')},
      {type:'separator'},
      {id:'quit',label:'退出客户端',enabled:!this.quitting,click:() => this.quit()},
    ]));
  }
  private async action(method:string) {
    if(this.pending || this.quitting || !this.available) return;
    this.pending=true;this.update();
    try {
      const result=await this.request(method);
      if(method === 'tasks.retry') {this.snapshot(result);this.send('tasks.changed',result);}
      this.send('feedback',method === 'download.stop' ? '正在暂停队列…' : '未完成任务已加入队列');
    } catch(error) {
      this.open(true);this.send('warning',error instanceof Error ? error.message : '托盘操作失败');
    } finally {this.pending=false;this.update();}
  }
  destroy() {this.tray.destroy();}
}
