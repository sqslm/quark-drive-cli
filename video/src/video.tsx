import React from 'react';
import {AbsoluteFill, Audio, Sequence, interpolate, staticFile, useCurrentFrame} from 'remotion';

const C = {
  bg: '#07151B',
  panel: '#10242B',
  panel2: '#143039',
  white: '#EDF4F0',
  muted: '#77949A',
  mint: '#A7F1D9',
  orange: '#FFAB87',
  line: '#315159',
};

const font = '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif';
const mono = 'Consolas, "SFMono-Regular", monospace';
const ease = (f: number, a: number, b: number) => interpolate(f, [a, b], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
const fade = (f: number, d: number) => ease(f, 0, 22) * (1 - ease(f, d - 26, d));
const move = (f: number, a: number, b: number, from: number, to = 0) => from + (to - from) * ease(f, a, b);

const Grid: React.FC<{frame: number}> = ({frame}) => (
  <AbsoluteFill style={{overflow: 'hidden', pointerEvents: 'none'}}>
    <div style={{position: 'absolute', inset: 0, background: 'radial-gradient(circle at 73% 42%, #11343D 0%, #07151B 52%)'}} />
    <svg width="1280" height="720" style={{position: 'absolute', opacity: 0.27, transform: `translateX(${(frame * 0.12) % 80}px)`}}>
      {Array.from({length: 18}, (_, i) => <line key={`v${i}`} x1={i * 80 - 80} y1="0" x2={i * 80 - 80} y2="720" stroke={C.line} strokeWidth="1" />)}
      {Array.from({length: 11}, (_, i) => <line key={`h${i}`} x1="0" y1={i * 80 - 40} x2="1280" y2={i * 80 - 40} stroke={C.line} strokeWidth="1" />)}
    </svg>
    <div style={{position: 'absolute', inset: 34, border: '1px solid rgba(167,241,217,.10)', borderRadius: 3}} />
  </AbsoluteFill>
);

const Kicker: React.FC<{children: React.ReactNode; light?: boolean}> = ({children, light}) => (
  <div style={{fontFamily: mono, color: light ? C.mint : C.muted, fontSize: 15, letterSpacing: 3.5, fontWeight: 700}}>{children}</div>
);

const Title: React.FC<{children: React.ReactNode; size?: number; style?: React.CSSProperties}> = ({children, size = 66, style}) => (
  <div style={{fontFamily: font, fontSize: size, fontWeight: 700, letterSpacing: -3, lineHeight: 1.2, color: C.white, ...style}}>{children}</div>
);

const Pill: React.FC<{children: React.ReactNode; color?: string}> = ({children, color = C.mint}) => (
  <div style={{display: 'inline-flex', alignItems: 'center', gap: 9, border: `1px solid ${color}55`, color, borderRadius: 30, padding: '10px 17px', fontSize: 15, fontFamily: font, background: `${color}0D`}}>
    <span style={{width: 6, height: 6, borderRadius: 6, background: color}} />{children}
  </div>
);

const QMark: React.FC<{size?: number; frame?: number}> = ({size = 100, frame = 0}) => (
  <svg width={size} height={size} viewBox="0 0 100 100" style={{overflow: 'visible'}}>
    <circle cx="48" cy="46" r="30" fill="none" stroke={C.mint} strokeWidth="7" strokeDasharray={`${174 * ease(frame, 0, 28)} 190`} strokeLinecap="round" transform="rotate(-92 48 46)" />
    <path d="M66 67 L83 84" stroke={C.mint} strokeWidth="7" strokeLinecap="round" strokeDasharray={`${25 * ease(frame, 18, 37)} 25`} />
    <circle cx="48" cy="46" r="4" fill={C.orange} opacity={0.5 + 0.5 * Math.sin(frame / 10)} />
  </svg>
);

const Scene: React.FC<{duration: number; children: (frame: number) => React.ReactNode}> = ({duration, children}) => {
  const frame = useCurrentFrame();
  return <AbsoluteFill style={{opacity: fade(frame, duration), backgroundColor: C.bg, overflow: 'hidden'}}><Grid frame={frame} />{children(frame)}</AbsoluteFill>;
};

const Intro: React.FC = () => <Scene duration={165}>{f => <>
  <div style={{position: 'absolute', left: 88, top: 66, opacity: ease(f, 10, 32)}}><Kicker>QUARK DRIVE / A BETTER FLOW</Kicker></div>
  <div style={{position: 'absolute', left: 112, top: 176, transform: `translateY(${move(f, 0, 38, 32)}px)`, opacity: ease(f, 0, 30)}}>
    <Kicker light>01 / INTRODUCING</Kicker>
    <div style={{height: 30}} />
    <Title size={80}>你的云盘，<br /><span style={{color: C.mint}}>有了新的轨道。</span></Title>
    <div style={{height: 30}} />
    <div style={{color: C.muted, fontFamily: font, fontSize: 24, letterSpacing: 1}}>夸克网盘，从命令行到播放器。</div>
  </div>
  <div style={{position: 'absolute', left: 844, top: 169, width: 306, height: 306}}>
    {[0, 1, 2].map((i) => <div key={i} style={{position: 'absolute', inset: i * 32, border: `1px solid ${i === 0 ? C.line : '#3A6268'}`, borderRadius: '50%', transform: `rotate(${f * (i % 2 ? -0.4 : 0.5)}deg) scale(${0.82 + 0.18 * ease(f, 0, 45)})`, opacity: 0.65 - i * 0.12}} />)}
    <div style={{position: 'absolute', inset: 103, display: 'grid', placeItems: 'center'}}><QMark size={100} frame={f} /></div>
    {[0, 1, 2].map((i) => {
      const a = f * (0.007 + i * 0.002) + i * 2.1;
      const r = 152 - i * 32;
      return <div key={i} style={{position: 'absolute', left: 153 + r * Math.cos(a), top: 153 + r * Math.sin(a), width: 8 - i, height: 8 - i, borderRadius: '50%', background: i === 1 ? C.orange : C.mint, boxShadow: `0 0 18px ${C.mint}`}} />;
    })}
  </div>
  <div style={{position: 'absolute', bottom: 71, left: 90, display: 'flex', gap: 16, opacity: ease(f, 55, 80)}}><Pill>Python CLI</Pill><Pill>PotPlayer 扩展</Pill></div>
  <div style={{position: 'absolute', bottom: 78, right: 93, font: `14px ${mono}`, color: C.muted}}>WINDOWS / LINUX</div>
</>}</Scene>;

const FileGlyph: React.FC<{kind?: 'file' | 'folder'; color?: string}> = ({kind = 'file', color = C.mint}) => kind === 'folder' ?
  <svg width="28" height="24" viewBox="0 0 28 24"><path d="M2 7V4a2 2 0 0 1 2-2h8l3 4h9a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7Z" fill={`${color}22`} stroke={color} strokeWidth="1.6" /></svg> :
  <svg width="24" height="28" viewBox="0 0 24 28"><path d="M3 2h11l7 7v16a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V2Z" fill={`${color}15`} stroke={color} strokeWidth="1.5"/><path d="M14 2v7h7" fill="none" stroke={color} strokeWidth="1.5"/></svg>;

const Terminal: React.FC<{frame: number}> = ({frame}) => {
  const lines = [
    {t: '$ quark tree /工作资料 --depth 2', color: C.mint},
    {t: '├─ 项目 / 设计 / 归档', color: C.white},
    {t: '├─ 会议记录.pdf', color: C.white},
    {t: '└─ 2026-素材 / 影像', color: C.white},
    {t: '$ quark search 报告 --in /工作资料', color: C.mint},
  ];
  return <div style={{width: 580, height: 348, border: `1px solid ${C.line}`, borderRadius: 18, background: '#091A20', boxShadow: '0 34px 90px #0008', overflow: 'hidden', transform: `perspective(1000px) rotateY(-5deg) translateY(${move(frame, 0, 35, 55)}px)`}}>
    <div style={{height: 51, borderBottom: `1px solid ${C.line}`, display: 'flex', alignItems: 'center', gap: 9, paddingLeft: 22, background: '#10252C'}}><span style={{width: 9, height: 9, borderRadius: 9, background: C.orange}}/><span style={{width: 9, height: 9, borderRadius: 9, background: C.mint}}/><span style={{width: 9, height: 9, borderRadius: 9, background: C.line}}/><span style={{marginLeft: 18, font: `13px ${mono}`, color: C.muted}}>quark / workspace</span></div>
    <div style={{padding: '29px 30px', display: 'flex', flexDirection: 'column', gap: 17}}>{lines.map((line, i) => <div key={i} style={{font: `17px ${mono}`, color: line.color, opacity: ease(frame, 20 + i * 14, 32 + i * 14), transform: `translateX(${move(frame, 20 + i * 14, 34 + i * 14, 10)}px)`}}>{line.t}</div>)}</div>
  </div>;
};

const Organize: React.FC = () => <Scene duration={190}>{f => <>
  <div style={{position: 'absolute', left: 91, top: 88, transform: `translateY(${move(f, 0, 31, 28)}px)`}}><Kicker light>02 / FIND YOUR ORDER</Kicker><div style={{height: 24}}/><Title>让每个文件，<br/>回到该在的位置。</Title><div style={{height: 24}}/><div style={{color: C.muted, fontFamily: font, fontSize: 21}}>浏览 · 搜索 · 批量整理</div></div>
  <div style={{position: 'absolute', right: 76, top: 116}}><Terminal frame={f}/></div>
  <div style={{position: 'absolute', left: 95, bottom: 109, display: 'flex', alignItems: 'center', gap: 22}}>
    {[['资料', 'folder'], ['报告.pdf', 'file'], ['视频', 'folder']].map(([n, kind], i) => <div key={n} style={{border: `1px solid ${C.line}`, background: C.panel, borderRadius: 14, width: 154, height: 89, display: 'flex', alignItems: 'center', gap: 13, padding: 17, color: C.white, font: `15px ${font}`, opacity: ease(f, 44 + i * 13, 63 + i * 13), transform: `translateY(${move(f, 44 + i * 13, 72 + i * 13, 55)}px)`}}><FileGlyph kind={kind as 'file' | 'folder'} />{n}</div>)}
  </div>
  <div style={{position: 'absolute', left: 90, bottom: 57, width: 420, height: 1, background: C.line}}/>
</>}</Scene>;

const Share: React.FC = () => <Scene duration={185}>{f => <>
  <div style={{position: 'absolute', top: 90, left: 91}}><Kicker light>03 / FROM LINK TO LIBRARY</Kicker><div style={{height: 23}}/><Title>分享链接，<br/><span style={{color: C.mint}}>精准抵达。</span></Title><div style={{height: 22}}/><div style={{font: `20px ${font}`, color: C.muted}}>查看内容，筛选文件，再转存。</div></div>
  <svg width="1280" height="720" style={{position: 'absolute', pointerEvents: 'none'}}><path d="M477 422 C605 300 660 546 820 417" stroke={C.mint} strokeWidth="2" fill="none" strokeDasharray="7 11" opacity={ease(f, 20, 70) * .7}/><path d="M803 405 L825 416 L807 431" stroke={C.mint} strokeWidth="2" fill="none" opacity={ease(f, 20, 70)}/></svg>
  <div style={{position: 'absolute', left: 105, top: 392, width: 360, height: 169, borderRadius: 18, background: C.panel, border: `1px solid ${C.line}`, padding: 25, transform: `translateX(${move(f, 10, 37, -54)}px)`, opacity: ease(f, 10, 37)}}>
    <Kicker>SHARED LINK</Kicker><div style={{font: `18px ${mono}`, color: C.white, marginTop: 23}}>pan.quark.cn/s/...</div><div style={{display: 'flex', gap: 9, marginTop: 23}}><Pill>*.pdf</Pill><Pill>*.mp4</Pill></div>
  </div>
  <div style={{position: 'absolute', right: 113, top: 345, width: 336, height: 256, borderRadius: 18, background: '#17343A', border: `1px solid ${C.mint}77`, padding: 26, boxShadow: `0 0 90px ${C.mint}18`, transform: `translateX(${move(f, 55, 90, 65)}px)`, opacity: ease(f, 55, 90)}}>
    <Kicker light>MY DRIVE</Kicker><div style={{font: `21px ${font}`, color: C.white, marginTop: 20}}>／工作资料／精选</div><div style={{height: 1, background: C.line, margin: '20px 0'}}/><div style={{display: 'flex', gap: 17, alignItems: 'center', color: C.white, font: `17px ${font}`}}><FileGlyph/>设计规范.pdf</div><div style={{display: 'flex', gap: 17, alignItems: 'center', marginTop: 17, color: C.white, font: `17px ${font}`}}><FileGlyph color={C.orange}/>发布片.mp4</div>
  </div>
  <div style={{position: 'absolute', left: 568, top: 356, borderRadius: 50, width: 50, height: 50, display: 'grid', placeItems: 'center', background: C.mint, color: C.bg, fontSize: 27, fontFamily: mono, opacity: ease(f, 44, 75), transform: `translate(${move(f, 44, 98, -90, 112)}px, ${Math.sin(f / 13) * 13}px)`}}>›</div>
  <div style={{position: 'absolute', left: 92, bottom: 63}}><Pill>支持批量转存与定时任务</Pill></div>
</>}</Scene>;

const Transfer: React.FC = () => <Scene duration={185}>{f => <>
  <div style={{position: 'absolute', left: 91, top: 88}}><Kicker light>04 / MOVE WITH CONFIDENCE</Kicker><div style={{height: 24}}/><Title>本地与云端，<br/>自由往返。</Title><div style={{height: 21}}/><div style={{font: `21px ${font}`, color: C.muted}}>上传 · 下载 · 备份 · 断点续传</div></div>
  <div style={{position: 'absolute', left: 96, top: 365, width: 276, height: 210, borderRadius: 20, border: `1px solid ${C.line}`, background: C.panel, display: 'grid', placeItems: 'center', transform: `translateY(${move(f, 10, 45, 45)}px)`}}><div style={{textAlign: 'center'}}><div style={{fontSize: 72, color: C.white, fontFamily: mono}}>▣</div><div style={{font: `18px ${font}`, color: C.white}}>本地文件</div><div style={{font: `13px ${mono}`, color: C.muted, marginTop: 9}}>LOCAL</div></div></div>
  <div style={{position: 'absolute', right: 96, top: 365, width: 276, height: 210, borderRadius: 20, border: `1px solid ${C.mint}77`, background: C.panel2, display: 'grid', placeItems: 'center', transform: `translateY(${move(f, 24, 55, 45)}px)`}}><div style={{textAlign: 'center'}}><div style={{fontSize: 72, color: C.mint, fontFamily: mono}}>☁</div><div style={{font: `18px ${font}`, color: C.white}}>夸克网盘</div><div style={{font: `13px ${mono}`, color: C.muted, marginTop: 9}}>CLOUD</div></div></div>
  <svg width="1280" height="720" style={{position: 'absolute'}}><path d="M389 443 H886" stroke={C.line} strokeWidth="2"/><path d="M871 432 L889 443 L871 454" stroke={C.mint} strokeWidth="2" fill="none"/><path d="M889 505 H389" stroke={C.line} strokeWidth="2"/><path d="M404 494 L386 505 L404 516" stroke={C.orange} strokeWidth="2" fill="none"/>{Array.from({length: 5},(_,i) => <circle key={i} cx={390 + ((f * 5 + i * 108) % 492)} cy="443" r={i % 2 ? 3 : 5} fill={C.mint} opacity={.3 + i * .13}/>)}{Array.from({length: 4},(_,i) => <circle key={i} cx={886 - ((f * 3.7 + i * 145) % 492)} cy="505" r="3" fill={C.orange} opacity={.55}/>)}</svg>
  <div style={{position: 'absolute', left: 470, top: 383, width: 330, color: C.muted, textAlign: 'center', font: `15px ${mono}`, letterSpacing: 1}}>UPLOAD / DOWNLOAD</div>
  <div style={{position: 'absolute', left: 516, top: 552, border: `1px solid ${C.line}`, borderRadius: 30, padding: '10px 17px', background: C.bg, color: C.mint, font: `14px ${font}`, opacity: ease(f, 50, 80)}}>↻　中断后继续</div>
</>}</Scene>;

const Player: React.FC = () => <Scene duration={200}>{f => <>
  <div style={{position: 'absolute', left: 91, top: 82}}><Kicker light>05 / WATCH WITHOUT WAITING</Kicker><div style={{height: 24}}/><Title>云端影片，<br/><span style={{color: C.mint}}>直接开播。</span></Title><div style={{height: 22}}/><div style={{font: `20px ${font}`, color: C.muted}}>PotPlayer 扩展 · 在线播放 · 续看进度</div></div>
  <div style={{position: 'absolute', right: 88, top: 87, width: 590, height: 443, border: `1px solid ${C.line}`, borderRadius: 18, overflow: 'hidden', background: '#091A20', boxShadow: '0 36px 100px #0009', transform: `perspective(1000px) rotateY(-4deg) translateY(${move(f, 0, 42, 50)}px)`}}>
    <div style={{height: 40, display: 'flex', alignItems: 'center', paddingLeft: 19, gap: 8, background: '#112931'}}><span style={{width: 7, height: 7, borderRadius: 5, background: C.orange}}/><span style={{width: 7, height: 7, borderRadius: 5, background: C.line}}/><span style={{width: 7, height: 7, borderRadius: 5, background: C.line}}/><span style={{marginLeft: 19, font: `12px ${mono}`, color: C.muted}}>POTPLAYER　/　我的网盘</span></div>
    <div style={{height: 332, position: 'relative', overflow: 'hidden', background: 'radial-gradient(circle at 54% 49%, #2D6C6B 0%, #143B45 34%, #071923 75%)'}}>
      {[0,1,2,3].map(i => <div key={i} style={{position: 'absolute', width: 270 + i * 115, height: 270 + i * 115, border: `1px solid ${i === 0 ? C.mint : '#7BCDC5'}`, borderRadius: '50%', left: 294 - (270 + i * 115)/2, top: 165 - (270 + i * 115)/2, opacity: .5 - i * .09, transform: `rotate(${f * (i % 2 ? -0.17 : .2)}deg) scaleY(.55)`}}/>)}
      <div style={{position: 'absolute', top: 84, left: 228, width: 134, height: 134, borderRadius: '50%', background: '#D6EBD9', filter: 'blur(2px)', boxShadow: '0 0 105px #A7F1D96A', transform: `scale(${1 + Math.sin(f / 16) * .04})`}}/>
      <div style={{position: 'absolute', top: 130, left: 274, width: 45, height: 45, border: '1px solid #D9F4EA', borderRadius: '50%', display: 'grid', placeItems: 'center', color: C.white, fontSize: 24, paddingLeft: 3}}>▶</div>
      <div style={{position: 'absolute', bottom: 24, left: 26, right: 26, height: 3, background: '#FFFFFF44'}}><div style={{width: `${34 + ease(f, 40, 145) * 33}%`, height: 3, background: C.mint}}/></div>
      <div style={{position: 'absolute', bottom: 39, left: 26, color: C.white, font: `13px ${mono}`}}>EPISODE 07　 ·　 续播中</div>
    </div>
    <div style={{height: 70, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 25px', font: `15px ${font}`, color: C.white}}><span>星际旅程 · 第 07 集</span><span style={{color: C.mint}}>字幕　SRT / ASS</span></div>
  </div>
  <div style={{position: 'absolute', left: 93, bottom: 108, display: 'flex', gap: 12, flexWrap: 'wrap', maxWidth: 470}}><Pill>搜索网盘</Pill><Pill>收藏目录</Pill><Pill>最近观看</Pill></div>
  <div style={{position: 'absolute', right: 92, bottom: 91, font: `13px ${font}`, color: C.muted}}>PotPlayer 扩展仅支持 Windows 10/11 x64</div>
</>}</Scene>;

const Outro: React.FC = () => <Scene duration={165}>{f => <>
  <div style={{position: 'absolute', left: 100, top: 124, width: 160, height: 160, opacity: ease(f, 0, 35)}}><QMark size={160} frame={f + 36}/></div>
  <div style={{position: 'absolute', left: 100, top: 327, transform: `translateY(${move(f, 0, 32, 34)}px)`}}><Kicker light>QUARK DRIVE CLI</Kicker><div style={{height: 25}}/><Title size={68}>让云盘，回到<br/><span style={{color: C.mint}}>你的工作流。</span></Title></div>
  <div style={{position: 'absolute', right: 98, top: 208, width: 352, height: 262, borderLeft: `1px solid ${C.line}`, paddingLeft: 45, display: 'flex', flexDirection: 'column', gap: 29, justifyContent: 'center', opacity: ease(f, 40, 75)}}>
    {['扫码登录', '文件管理与自动化', '云端视频在线播放'].map((v,i) => <div key={v} style={{font: `22px ${font}`, color: C.white, display: 'flex', alignItems: 'center', gap: 17, transform: `translateX(${move(f, 40 + i * 10, 70 + i * 10, 20)}px)`}}><span style={{color: C.mint, font: `17px ${mono}`}}>0{i+1}</span>{v}</div>)}
  </div>
  <div style={{position: 'absolute', left: 100, bottom: 87, display: 'flex', gap: 18, alignItems: 'center', opacity: ease(f, 80, 112)}}><div style={{width: 54, height: 1, background: C.mint}}/><span style={{font: `17px ${mono}`, color: C.mint}}>quark</span><span style={{font: `14px ${font}`, color: C.muted}}>命令行，也可以很有想象力。</span></div>
  <div style={{position: 'absolute', right: 99, bottom: 84, font: `12px ${mono}`, color: C.muted}}>OPEN SOURCE PROJECT / 2026</div>
</>}</Scene>;

export const QuarkDriveIntro: React.FC = () => <AbsoluteFill style={{backgroundColor: C.bg}}>
  <Sequence from={0} durationInFrames={165}><Intro/></Sequence>
  <Sequence from={145} durationInFrames={190}><Organize/></Sequence>
  <Sequence from={315} durationInFrames={185}><Share/></Sequence>
  <Sequence from={480} durationInFrames={185}><Transfer/></Sequence>
  <Sequence from={645} durationInFrames={200}><Player/></Sequence>
  <Sequence from={825} durationInFrames={165}><Outro/></Sequence>
  <Audio src={staticFile('score.wav')} volume={0.7}/>
</AbsoluteFill>;
