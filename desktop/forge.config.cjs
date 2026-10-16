const path = require('node:path');
module.exports = {
  outDir: process.env.QUARK_DESKTOP_OUT || 'out',
  packagerConfig: {
    asar: true,
    executableName: 'QuarkDriveClient',
    icon:path.resolve(__dirname,'assets/app-icon.ico'),
    extraResource: [path.resolve(__dirname, '../dist/QuarkBackend'),path.resolve(__dirname,'assets/app-icon.ico'),path.resolve(__dirname,'assets/markdown-notices.txt')],
  },
  makers: [{ name: '@electron-forge/maker-zip', platforms: ['win32'] }],
  plugins: [{ name: '@electron-forge/plugin-webpack', config: {
    mainConfig: './webpack.main.cjs',
    renderer: { config: './webpack.renderer.cjs', entryPoints: [{
      html: './src/index.html', js: './src/renderer.tsx', name: 'main_window', preload: { js: './src/preload.ts' },
    }] },
    devContentSecurityPolicy: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://localhost:*; object-src 'none'",
  } }],
};
