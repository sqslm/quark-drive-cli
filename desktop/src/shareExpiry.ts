export type Expiration = {expired_type?:number;expired_at?:number};
export function shareExpiry(row:Expiration,now:number) {
  if(row.expired_type === 1) return {text:'永久',title:'永久有效'};
  const value = row.expired_at;
  if(typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return {text:'到期时间未知',title:'服务端未提供有效到期时间'};
  // Current Quark catalogs use epoch milliseconds; accept epoch seconds too.
  const deadline = value < 100_000_000_000 ? value * 1000 : value;
  if(deadline > 8_640_000_000_000_000) return {text:'到期时间未知',title:'服务端到期时间无效'};
  const title = `到期：${new Date(deadline).toLocaleString('zh-CN',{hour12:false})}`;
  const remaining = deadline-now;
  return {text:remaining <= 0 ? '已过期' : remaining < 3_600_000 ? '不足 1 小时' : `剩余 ${Math.floor(remaining/3_600_000)} 小时`,title};
}
