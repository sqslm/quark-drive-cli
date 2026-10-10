# Quark Drive 项目介绍视频

33 秒、1280×720、30 fps 的 Remotion 项目介绍短片。画面根据本仓库的 CLI 和 PotPlayer 扩展功能制作；配乐由 `scripts/score.mjs` 原创生成。

```sh
cd video
npm ci
npm run score
npm run studio
npm run render
```

最终文件为 `video/quark-drive-intro.mp4`。修改 `src/video.tsx` 后重新运行 `npm run render` 即可导出。PotPlayer 扩展只支持 Windows 10/11 x64，片中已注明。
