const path = require('node:path');
const os = require('node:os');

function getGumCodeRuntime({ root = path.resolve(__dirname, '..'), packaged = false, home = os.homedir() } = {}) {
  return {
    source: packaged ? path.join(root, 'engine/src') : path.resolve(root, '../src'),
    python: path.join(packaged ? path.join(root, 'runtime/python') : path.join(home, 'Library/Application Support/LocalDev/runtimes/gumcode-python-arm64'), 'bin/python3'),
  };
}

function pythonEnvironment(source, inherited = process.env) {
  const env = { ...inherited };
  for (const key of Object.keys(env)) if (key.startsWith('PYTHON') || key === 'VIRTUAL_ENV' || key === '__PYVENV_LAUNCHER__') delete env[key];
  return { ...env, PYTHONPATH: source, PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONSAFEPATH: '1' };
}

module.exports = { getGumCodeRuntime, pythonEnvironment };
