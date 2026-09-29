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
