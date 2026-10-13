export type Entry = { fid: string; name: string; directory: boolean; size: number | null; path: string; location: [string, string][] };
export type Preferences = { output_dir: string; connections: number; limit_rate: number | null; width: number; height: number };
export type Task = { id: string; name: string; state: string; detail: string; target: string; valid: boolean; revision?: number;
  direction?: 'upload' | 'download'; local_dir?: string; needs_authorization?: boolean; share_url?: string };
export type Event = { id: string | null; event: string; data: unknown };
export type Cut = {id:string;entries:Entry[];blocked:boolean};
export type Favorite = {fid:string;name:string};
export type AppearanceSource = 'system'|'dark'|'light';
export type Appearance = {source:AppearanceSource;dark:boolean;warning:string};
export type ClientSettings = {closeBehavior:'tray'|'quit';notifications:boolean};
export type ClientSettingsResult = {settings:ClientSettings;warning:string};
export const METHODS = ['hello','account','browse','locate','search','search.cancel','share.parse','share.open','share.browse',
  'preferences.save','login','login.cancel','download','download.stop','tasks','tasks.retry',
  'file.info','file.mkdir','file.rename','file.move','file.delete','upload','tasks.cancel','tasks.clear','tasks.authorize',
  'share.list','share.link','share.create','share.revoke','share.save','recycle.list','recycle.restore','recycle.remove',
  'file.cut','file.paste','file.clearCut','favorites.list','favorites.add','favorites.remove','favorites.open','logout'] as const;
export type Method = typeof METHODS[number];
export interface Bridge {
  call(method: Method, params?: Record<string, unknown>, id?: string): Promise<any>;
  onEvent(listener: (event: Event) => void): () => void;
  chooseFolder(): Promise<string | null>;
  chooseFiles(): Promise<string[]>;
  openFolder(path: string): Promise<void>;
  copyText(text: string): Promise<void>;
  getAppearance():Promise<Appearance>;
  setAppearance(source:AppearanceSource):Promise<Appearance>;
  getClientSettings():Promise<ClientSettingsResult>;
  setClientSettings(settings:ClientSettings):Promise<ClientSettingsResult>;
  quit():Promise<void>;
  officialLogin(mode:'mobile'|'wechat'):Promise<{nickname?:string;logged_in?:boolean;cancelled?:boolean;warning?:string}>;
}
declare global { interface Window { quark: Bridge } }
