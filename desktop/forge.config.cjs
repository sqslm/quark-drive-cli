const path = require('node:path');
module.exports = {
  outDir: process.env.QUARK_DESKTOP_OUT || 'out',
  packagerConfig: {
    asar: true,
    executableName: 'QuarkDriveClient',
    icon:path.resolve(__dirname,'assets/app-icon.ico'),
    extraResource: [path.resolve(__dirname, '../dist/QuarkBackend'),path.resolve(__dirname,'assets/app-icon.ico'),path.resolve(__dirname,'assets/markdown-notices.txt')],
  },
  makers: [
    { name: '@electron-forge/maker-zip', platforms: ['win32'] },
    { name: '@electron-forge/maker-squirrel', platforms: ['win32'], config: {
      name: 'QuarkDriveClient',
      title: 'Quark Drive Client',
      authors: 'Quark Drive Client',
      owners: 'Quark Drive Client',
      description: 'Windows installer for Quark Drive Client',
      copyright: 'Copyright Quark Drive Client',
      version: '0.5.25',
      exe: 'QuarkDriveClient.exe',
      setupExe: 'QuarkDriveClient-0.5.25-Setup.exe',
      setupIcon: path.resolve(__dirname, 'assets/app-icon.ico'),
      usePackageJson: false,
    } },
  ],
  plugins: [{ name: '@electron-forge/plugin-webpack', config: {
    mainConfig: './webpack.main.cjs',
    renderer: { config: './webpack.renderer.cjs', entryPoints: [{
      html: './src/index.html', js: './src/renderer.tsx', name: 'main_window', preload: { js: './src/preload.ts' },
    }] },
    devContentSecurityPolicy: "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://localhost:*; object-src 'none'",
  } }],
};
