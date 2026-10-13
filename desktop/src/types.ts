export type Entry = { fid: string; name: string; directory: boolean; size: number | null; path: string; location: [string, string][] };
export type Preferences = { output_dir: string; connections: number; limit_rate: number | null; width: number; height: number };
export type Task = { id: string; name: string; state: string; detail: string; target: string; valid: boolean; revision?: number };
export type Event = { id: string | null; event: string; data: unknown };
export const METHODS = ['hello','account','browse','locate','search','search.cancel','share.parse','share.open','share.browse',
  'preferences.save','login','login.cancel','download','download.stop','tasks','tasks.retry'] as const;
export type Method = typeof METHODS[number];
export interface Bridge {
  call(method: Method, params?: Record<string, unknown>, id?: string): Promise<any>;
  onEvent(listener: (event: Event) => void): () => void;
  chooseFolder(): Promise<string | null>;
  openFolder(path: string): Promise<void>;
}
declare global { interface Window { quark: Bridge } }
