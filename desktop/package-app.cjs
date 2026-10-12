const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { runtimePath, validateRuntime } = require('./scripts/prepare-gumcode-runtime.cjs');
const root = __dirname;
const source = path.join(root, 'node_modules/electron/dist/Electron.app');
// Keep signed bundles outside iCloud Documents: its provider rewrites Finder metadata during signing.
const args = process.argv.slice(2);
if (args.length && (args.length !== 2 || args[0] !== '--output' || !path.isAbsolute(args[1]) || !args[1].endsWith('.app'))) throw new Error('Usage: node package-app.cjs [--output /absolute/path/GuMi-Code.app]');
const target = args[1] || path.join(root, 'release/GuMi-Code.app');
if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('目前仅支持 Apple 芯片 Mac');
const runtime = runtimePath();
if (!fs.existsSync(runtime)) throw new Error('先运行 npm run prepare:runtime');
validateRuntime(runtime, path.join(root, 'requirements-runtime.lock'));
if (!fs.existsSync(path.join(root, 'dist/index.html'))) throw new Error('先运行 npm run build');
if (fs.existsSync(target)) throw new Error(`${target} 已存在；保留或手动移走后再打包`);
fs.mkdirSync(path.dirname(target), { recursive: true });
execFileSync('/usr/bin/ditto', ['--norsrc', '--noextattr', source, target]);
for (const notice of ['LICENSE', 'LICENSES.chromium.html']) {
  fs.copyFileSync(path.join(path.dirname(source), notice), path.join(target, 'Contents/Resources', notice));
}
const payload = path.join(target, 'Contents/Resources/app');
fs.mkdirSync(payload, { recursive: true });
for (const item of ['dist', 'electron']) fs.cpSync(path.join(root, item), path.join(payload, item), { recursive: true, filter: file => path.basename(file) !== '__pycache__' && !file.endsWith('.pyc') });
const engineTarget = path.join(payload, 'engine');
fs.mkdirSync(engineTarget, { recursive: true });
for (const item of ['src', 'pyproject.toml', 'README.md', 'README_en.md']) {
 const from = path.resolve(root, '..', item);
 fs.cpSync(from, path.join(engineTarget,item), {recursive:true, filter:file=>{
  const info=fs.lstatSync(file); if(info.isSymbolicLink())throw Error('Engine source must not contain symlinks');
  return info.isDirectory() ? path.basename(file)!=='__pycache__' : item!=='src'||file.endsWith('.py');
 }});
}
fs.copyFileSync(path.join(root,'requirements-runtime.lock'),path.join(engineTarget,'requirements-runtime.lock'));
fs.cpSync(path.join(root,'third-party'),path.join(payload,'third-party'),{recursive:true});

fs.cpSync(runtime, path.join(payload, 'runtime/python'), { recursive: true, verbatimSymlinks: true });
validateRuntime(path.join(payload, 'runtime/python'), path.join(engineTarget, 'requirements-runtime.lock'));
const metadata = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
fs.writeFileSync(path.join(payload, 'package.json'), JSON.stringify({ name: metadata.name, productName: 'GuMi-Code', version: metadata.version, main: metadata.main, description: metadata.description, author: metadata.author }));
for (const item of ['README.md']) if (fs.existsSync(path.join(root, item))) fs.copyFileSync(path.join(root, item), path.join(payload, item));
const plist = path.join(target, 'Contents/Info.plist');
// Electron treats an executable still named Electron as a development runtime.
const executableName = 'GuMi-Code';
fs.renameSync(path.join(target, 'Contents/MacOS/Electron'), path.join(target, 'Contents/MacOS', executableName));
fs.copyFileSync(path.join(root, 'public/app-icon.icns'), path.join(target, 'Contents/Resources/gumi-code.icns'));
for (const [key, value] of Object.entries({ CFBundleExecutable: executableName, CFBundleName: 'GuMi-Code', CFBundleDisplayName: 'GuMi-Code', CFBundleIdentifier: 'io.gumishdo.code', CFBundleIconFile: 'gumi-code.icns' })) {
  execFileSync('/usr/libexec/PlistBuddy', ['-c', `Set :${key} ${value}`, plist]);
}
// Finder metadata added by synced folders is not code and cannot be included in a signature.
execFileSync('/usr/bin/xattr', ['-dr', 'com.apple.FinderInfo', target]);
// Python native extensions are not in standard framework folders; sign each Mach-O first.
function signNativeFiles(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) signNativeFiles(file);
    else if (entry.isFile()) {
      const fd = fs.openSync(file, 'r');
      const magic = Buffer.alloc(4);
      try { fs.readSync(fd, magic, 0, 4, 0); } finally { fs.closeSync(fd); }
      if (['cffaedfe', 'cefaedfe', 'feedfacf', 'feedface', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca'].includes(magic.toString('hex'))) {
        const arches = execFileSync('/usr/bin/lipo', ['-archs', file], { encoding: 'utf8' });
        if (!arches.split(/\s+/).includes('arm64')) throw new Error(`Native dependency has no arm64 slice: ${path.basename(file)}`);
        execFileSync('/usr/bin/codesign', ['--force', '--sign', '-', file], { stdio: 'pipe' });
      }
    }
  }
}
signNativeFiles(path.join(payload, 'runtime/python'));
execFileSync('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', target], { stdio: 'inherit' });
execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', target], { stdio: 'inherit' });
validateRuntime(path.join(payload, 'runtime/python'), path.join(engineTarget, 'requirements-runtime.lock'));
execFileSync(path.join(payload, 'runtime/python/bin/python3'), ['-I', '-B', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import gum_code.desktop', path.join(engineTarget, 'src')], { stdio: 'pipe' });
console.log(target);
