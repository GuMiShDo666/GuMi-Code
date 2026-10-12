// Clean, relocatable Python distribution. Never copies the developer's venv or settings.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const ARCHIVE_URL = 'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.11.16%2B20260901-aarch64-apple-darwin-install_only.tar.gz';
const ARCHIVE_SHA256 = '50424fa409e8ae84b82a3052522f64695b47dff2158b70bb7358e0ebd6c085c9';
const runtimePath = (home = os.homedir()) => path.join(home, 'Library/Application Support/LocalDev/runtimes/gumcode-python-arm64');
const sha256 = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
function validateLock(file) {
  const lines = fs.readFileSync(file, 'utf8').split(/\r?\n/).map(s => s.trim()).filter(s => s && !s.startsWith('#'));
  if (!lines.length || lines.some(s => !/^[a-zA-Z0-9_.-]+==[a-zA-Z0-9_.+!-]+$/.test(s))) throw new Error('Runtime requirements must contain only exact package versions');
}
function validateRuntime(runtime, lock) {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, 'gumcode-runtime.json'), 'utf8'));
  if (manifest.archiveSha256 !== ARCHIVE_SHA256 || manifest.requirementsSha256 !== sha256(lock)) throw new Error('Python runtime is stale; prepare it again in a new directory');
  execFileSync(path.join(runtime, 'bin/python3'), ['-I', '-B', '-c', 'import platform,sys,ssl,sqlite3,openai,pydantic,pypdfium2,PIL; assert platform.machine() == "arm64"; assert sys.version_info[:3] == (3,11,16)'], { stdio: 'pipe' });
}
function prepare({ root = path.join(__dirname, '..'), target = runtimePath() } = {}) {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('目前仅支持 Apple 芯片 Mac');
  const lock = path.join(root, 'requirements-runtime.lock');
  validateLock(lock);
  if (fs.existsSync(target)) { validateRuntime(target, lock); console.log(`Python runtime ready: ${target}`); return target; }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  const staging = fs.mkdtempSync(path.join(path.dirname(target), '.gumcode-build-'));
  try {
    const archive = path.join(staging, 'python.tar.gz');
    execFileSync('/usr/bin/curl', ['--fail', '--location', '--retry', '2', '--connect-timeout', '20', '--max-time', '300', '--output', archive, ARCHIVE_URL], { stdio: 'inherit' });
    if (sha256(archive) !== ARCHIVE_SHA256) throw new Error('Python archive checksum mismatch');
    execFileSync('/usr/bin/tar', ['-xzf', archive, '-C', staging]);
    const runtime = path.join(staging, 'python');
    // Isolated pip ignores user config and never builds native packages on the recipient Mac.
    execFileSync(path.join(runtime, 'bin/python3'), ['-I', '-B', '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--no-cache-dir', '--no-compile', '--only-binary=:all:', '--index-url', 'https://pypi.org/simple', '-r', lock], { stdio: 'inherit', env: { PATH: '/usr/bin:/bin', HOME: staging, TMPDIR: staging, PIP_CONFIG_FILE: '/dev/null' } });
    // Console entrypoints contain the build prefix. GumCode invokes modules with python3.
    for (const entry of fs.readdirSync(path.join(runtime, 'bin'))) if (!/^python(?:3(?:\.11)?)?$/.test(entry)) fs.rmSync(path.join(runtime, 'bin', entry), { recursive: true, force: true });
    function clearBytecode(dir) { for (const e of fs.readdirSync(dir, { withFileTypes: true })) { const file = path.join(dir, e.name); if (e.name === '__pycache__') fs.rmSync(file, { recursive: true }); else if (e.isDirectory()) clearBytecode(file); } }
    clearBytecode(runtime);
    fs.writeFileSync(path.join(runtime, 'gumcode-runtime.json'), JSON.stringify({ python: '3.11.16', platform: 'darwin-arm64', archiveUrl: ARCHIVE_URL, archiveSha256: ARCHIVE_SHA256, requirementsSha256: sha256(lock) }, null, 2) + '\n');
    validateRuntime(runtime, lock);
    fs.renameSync(runtime, target);
    console.log(`Python runtime ready: ${target}`);
    return target;
  } finally { fs.rmSync(staging, { recursive: true, force: true }); }
}
if (require.main === module) { try { if (process.argv.length > 2) throw new Error('Usage: node scripts/prepare-gumcode-runtime.cjs'); prepare(); } catch (error) { console.error(error.message); process.exitCode = 1; } }
module.exports = { runtimePath, sha256, validateLock, validateRuntime, prepare, ARCHIVE_SHA256 };
