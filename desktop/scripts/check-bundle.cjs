// Offline relocation check: synthetic model, empty HOME, system-only PATH.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { spawn, execFileSync } = require('node:child_process');

async function main() {
  const app = process.argv[2];
  if (!app || !path.isAbsolute(app) || !app.endsWith('.app')) throw new Error('Usage: node scripts/check-gumcode-bundle.cjs /absolute/path/App.app');
  const temp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'gumcode-portable-')));
  const moved = path.join(temp, 'Moved App.app');
  let server;
  try {
    execFileSync('/usr/bin/ditto', ['--norsrc', '--noextattr', app, moved]);
    execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', moved]);
    const root = path.join(moved, 'Contents/Resources/app');
    const { getGumCodeRuntime, pythonEnvironment } = require(path.join(root, 'electron/gum-code-runtime.cjs'));
    const runtime = getGumCodeRuntime({ root, packaged: true, home: path.join(temp, 'home') });
    const home = path.join(temp, 'home'), workspace = path.join(temp, 'workspace');
    fs.mkdirSync(home); fs.mkdirSync(workspace);
    const env = pythonEnvironment(runtime.source, { HOME: home, PATH: '/usr/bin:/bin', TMPDIR: temp });
    function run(request) {
      return new Promise((resolve, reject) => {
        const child = spawn(runtime.python, ['-u', '-m', 'gum_code.desktop'], { cwd: temp, env, stdio: ['pipe','pipe','pipe'] });
        let output = '';
        const timeout = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('Protocol timeout')); }, 30000);
        child.stdout.on('data', b => { output += b; });
        child.stderr.resume();
        child.on('error', reject);
        child.on('close', code => { clearTimeout(timeout); try { assert.equal(code, 0, `engine ${request.action} exit: ${output}`); const events = output.trim().split('\n').map(JSON.parse); assert(events.some(e => e.type === 'finished' && e.status === 'completed')); resolve(events); } catch (e) { reject(e); } });
        child.stdin.write(JSON.stringify(request)+'\n');
      });
    }
    const base = { v: 1, sessionId: randomUUID(), turnId: randomUUID(), workspace, sessionPath: path.join(temp,'synthetic.jsonl') };
    assert((await run({ ...base, action: 'probe' })).some(e => e.type === 'ready'));
    server = http.createServer((req,res) => {
      let body = ''; req.on('data', b => { body += b; });
      req.on('end', () => {
        if (!JSON.parse(body).stream) { res.writeHead(200, { 'Content-Type':'application/json' }); res.end(JSON.stringify({id:'synthetic',object:'chat.completion',created:1,model:'synthetic',choices:[{index:0,message:{role:'assistant',content:'PORTABLE_OK'},finish_reason:'stop'}],usage:{prompt_tokens:10,completion_tokens:3,total_tokens:13}})); return; }
        res.writeHead(200, { 'Content-Type': 'text/event-stream' });
        const chunk = { id: 'synthetic', object: 'chat.completion.chunk', created: 1, model: 'synthetic', choices: [{ index: 0, delta: { role: 'assistant', content: 'PORTABLE_OK' }, finish_reason: null }] };
        res.write('data: '+JSON.stringify(chunk)+'\n\n');
        chunk.choices = [{index:0,delta:{},finish_reason:'stop'}];
        res.end('data: '+JSON.stringify(chunk)+'\n\ndata: [DONE]\n\n');
      });
    });
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    await run({ ...base, turnId:randomUUID(), action:'turn', trusted:true, text:'Reply PORTABLE_OK only. Do not use tools.', provider:{baseUrl:`http://127.0.0.1:${server.address().port}/v1`,model:'synthetic',apiKey:'YOUR_TOKEN_HERE',contextWindow:8000} });
    const history = await run({ ...base, turnId:randomUUID(), action:'history' });
    assert(history.some(e => e.type === 'history' && e.messages.some(m => m.role === 'assistant' && m.content.includes('PORTABLE_OK'))));
    console.log('PASS: moved bundle, strict signature, empty HOME/system PATH, probe, synthetic model turn, persisted history. No real provider or credentials used.');
  } finally { if (server) await new Promise(resolve => server.close(resolve)); fs.rmSync(temp,{recursive:true,force:true}); }
}
main().catch(e => { console.error(e.message); process.exitCode=1; });
