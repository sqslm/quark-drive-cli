import { app } from 'electron';
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { AppearanceSource } from './types';

export function validAppearance(value:unknown):value is AppearanceSource {
  return value === 'system' || value === 'dark' || value === 'light';
}
const filename=() => path.join(app.getPath('userData'),'appearance.json');
export async function loadAppearance():Promise<{source:AppearanceSource;warning:string}> {
  try {
    const data=JSON.parse(await readFile(filename(),'utf8'));
    if(!validAppearance(data?.source)) throw new Error('invalid appearance');
    return {source:data.source,warning:''};
  } catch(error) {
    return {source:'system',warning:(error as NodeJS.ErrnoException).code === 'ENOENT' ? '' : '外观设置无法读取，已跟随系统；可重新选择外观保存'};
  }
}
export async function saveAppearance(source:AppearanceSource) {
  const destination=filename(),temporary=destination+'.tmp';
  await mkdir(path.dirname(destination),{recursive:true});
  try {await writeFile(temporary,JSON.stringify({source})+'\n','utf8');await rename(temporary,destination);}
  finally {await unlink(temporary).catch(() => {});}
}
export function appearanceColors(dark:boolean) {
  return dark ? {background:'#181818',title:'#191c1e',symbol:'#ececec'} : {background:'#ffffff',title:'#f6f8fc',symbol:'#35435c'};
}
