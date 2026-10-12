const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const filename = path.resolve('src/GumCodeMarkdown.tsx');
const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
const loaded = new Module(filename, module);
loaded.filename = filename;
loaded.paths = Module._nodeModulePaths(path.dirname(filename));
const streamFilename = path.resolve('src/useStreamText.ts');
const streamModule = new Module(streamFilename, module);
streamModule.filename = streamFilename;
streamModule.paths = loaded.paths;
streamModule._compile(ts.transpileModule(fs.readFileSync(streamFilename, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, streamFilename);
const originalRequire = loaded.require.bind(loaded);
loaded.require = id => id === './useStreamText' ? streamModule.exports : originalRequire(id);
loaded._compile(compiled, filename);
const render = value => renderToStaticMarkup(React.createElement(loaded.exports.GumCodeMarkdown, null, value));
test('Gum-Code renders CommonMark and GFM blocks as React elements', () => {
  const html = render('# 标题\n\n**重点** 与 `code`\n\n- 一项\n\n> 引用\n\n```js\nconst a = 1;\n```\n\n|列|值|\n|---|---|\n|A|B|\n\n- [x] 已完成\n\n~~删除~~');
  for (const expression of [/<h1>标题<\/h1>/, /<strong>重点<\/strong>/, /<blockquote>/, /<pre><code class="language-js">/, /<table>/, /type="checkbox" disabled="" checked=""/, /<del>删除<\/del>/]) assert.match(html, expression);
});
test('Gum-Code does not execute raw HTML or auto-load model images; unsafe links are inert', () => {
  const html = render('<script>alert(1)</script>\n\n[bad](javascript:alert%281%29) [local](file:///tmp/a) [secret](https://user:pass@example.com) [safe](https://example.com)\n\n![图](https://example.com/image.png)');
  assert.doesNotMatch(html, /<script|<img|href="(?:javascript|file):|user:pass/);
  assert.match(html, /href="https:\/\/example.com\/"/);
  assert.match(html, />图<\/a>/);
});

test('stream presentation catches up using received text and handles replacement and Unicode', () => {
  const { advanceStreamText } = streamModule.exports;
  const target = '你好🌍，这是实际接收到的内容。'.repeat(20);
  let shown = '';
  let updates = 0;
  while (shown !== target && updates < 50) {
    const next = advanceStreamText(shown, target, 32);
    assert.ok(target.startsWith(next));
    assert.ok(next.length > shown.length);
    assert.doesNotMatch(next, /[\uD800-\uDBFF]$/);
    shown = next;
    updates++;
  }
  assert.equal(shown, target);
  assert.ok(updates > 1 && updates < 20);
  assert.equal(advanceStreamText('obsolete', 'corrected', 32), 'corrected');
  assert.equal(advanceStreamText('long previous', '', 32), '');
  assert.equal(advanceStreamText('', 'final', 100), 'final');
});
