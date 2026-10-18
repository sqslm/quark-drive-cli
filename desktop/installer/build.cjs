const { existsSync } = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { version } = require('../package.json');

const desktop = path.resolve(__dirname, '..');
const output = path.resolve(desktop, process.env.QUARK_DESKTOP_OUT || `out/v${version}-installer`);
const packaged = path.join(output, 'Quark Drive Client-win32-x64');
const compiler = process.env.ISCC_PATH || path.join(process.env.LOCALAPPDATA || '', 'Programs', 'Inno Setup 7', 'ISCC.exe');

if (!existsSync(compiler)) throw new Error('未找到 Inno Setup 7 编译器，请使用 ISCC_PATH 指定 ISCC.exe。');
for (const file of ['QuarkDriveClient.exe', 'resources/app.asar', 'resources/QuarkBackend/QuarkBackend.exe']) {
  if (!existsSync(path.join(packaged, file))) throw new Error(`缺少已打包程序：${file}。请先使用相同 QUARK_DESKTOP_OUT 运行 npm run package。`);
}

const result = spawnSync(compiler, [
  '--no-ide-signtools',
  `--define=AppVersion=${version}`,
  `--define=PackageDir=${packaged}`,
  `--output-dir=${path.join(output, 'wizard')}`,
  path.join(__dirname, 'windows.iss'),
], { stdio: 'inherit', windowsHide: true });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
