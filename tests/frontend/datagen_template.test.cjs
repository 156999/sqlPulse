const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const template = fs.readFileSync(
  path.join(__dirname, '../../app/templates/datagen.html'),
  'utf8',
);
const match = template.match(/<script>([\s\S]*?)<\/script>/);
assert.ok(match, 'datagen template must contain an inline script');

const script = match[1]
  .replace(/const scriptEnabled = \{\{.*?\}\};/, 'const scriptEnabled = true;')
  .replace(/const maxSqlChars = \{\{.*?\}\};/, 'const maxSqlChars = 5000000;')
  .replace(/const maxScriptChars = \{\{.*?\}\};/, 'const maxScriptChars = 500000;');

assert.doesNotThrow(() => new Function(script));
assert.match(script, /const placeholder=name=>'\\u007b\\u007bvar\('/);
assert.match(script, /values\.join\('\, '\)/);
assert.doesNotMatch(script, /const placeholder=name=>'\{\{var\('/);

const responseHelper = script.match(/async function readDatagenResponse\(resp\) \{[\s\S]*?\n\}/);
assert.ok(responseHelper);
const readResponse = new Function(responseHelper[0] + '; return readDatagenResponse;')();
(async () => {
  assert.deepEqual(await readResponse({ok: true, text: async () => '{"job_id":"job-1"}'}), {job_id: 'job-1'});
  assert.deepEqual(await readResponse({ok: false, text: async () => '{"detail":"invalid rules"}'}), {detail: 'invalid rules'});
  await assert.rejects(readResponse({ok: false, status: 500, text: async () => 'Internal Server Error'}), /HTTP 500/);
  await assert.rejects(readResponse({ok: true, text: async () => '<html>login</html>'}), /预期 JSON/);
})().catch(error => { console.error(error); process.exitCode = 1; });
