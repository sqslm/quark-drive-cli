import { app } from 'electron';
import { mkdir, readFile, rename, unlink, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { ClientSettings } from './types';

export const defaultClientSettings:ClientSettings={closeBehavior:'tray',notifications:true,shareExpiryMode:'hours'};
export function validClientSettings(value:unknown):value is ClientSettings {
  if(!value || typeof value !== 'object') return false;
  const data=value as ClientSettings;
  return (data.closeBehavior === 'tray' || data.closeBehavior === 'quit') && typeof data.notifications === 'boolean' && (data.shareExpiryMode === 'hours' || data.shareExpiryMode === 'daysHours');
}
const filename=() => path.join(app.getPath('userData'),'desktop.json');
export async function loadClientSettings() {
  try {
    const data={shareExpiryMode:'hours',...JSON.parse(await readFile(filename(),'utf8'))};
    if(!validClientSettings(data)) throw new Error('invalid settings');
    return {settings:{closeBehavior:data.closeBehavior,notifications:data.notifications,shareExpiryMode:data.shareExpiryMode},warning:''};
  } catch(error) {
    return {settings:{...defaultClientSettings},warning:(error as NodeJS.ErrnoException).code === 'ENOENT' ? '' : '后台运行设置无法读取，已使用默认值；可重新选择设置保存'};
  }
}
export async function saveClientSettings(settings:ClientSettings) {
  const destination=filename(),temporary=destination+'.tmp';
  await mkdir(path.dirname(destination),{recursive:true});
  try {await writeFile(temporary,JSON.stringify(settings)+'\n','utf8');await rename(temporary,destination);}
  finally {await unlink(temporary).catch(() => {});}
}
