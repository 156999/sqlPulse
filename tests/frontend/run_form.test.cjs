const {test} = require('node:test');
const assert = require('node:assert/strict');
const {variables,expression,shares} = require('../../app/static/run_form.js');
test('typed candidates retain punctuation, Unicode, newline and scalar types',()=>{
  const row={name:'value',type:'pick',choices:[{type:'string',value:"a,b'\\\n中文"},{type:'integer',value:'9007199254740993'},{type:'number',value:'2.5'},{type:'boolean',value:'true'},{type:'null',value:''}]};
  assert.equal(expression(row),`pick(${JSON.stringify("a,b'\\\n中文")},9007199254740993,2.5,True,None)`);
  assert.equal(variables([row]).value,expression(row));
});
test('weights reflect groups and ignore blank cards',()=>{
  assert.deepEqual(shares([{sql:'SELECT 1',weight:70},{sql:'SELECT 2; SELECT 3',weight:30},{sql:' ',weight:999}]),[70,30,0]);
  assert.equal(shares([{sql:'SELECT 1',weight:0}]),null);
  assert.equal(shares([{sql:'SELECT 1',weight:1.5}]),null);
});
test('empty candidates, invalid names, duplicate names and numeric expressions fail locally',()=>{
  const row={name:'x',type:'rand',args:['1','10']};
  assert.throws(()=>variables([row,row]),/重复/);
  assert.throws(()=>variables([{...row,name:'1x'}]));
  assert.throws(()=>expression({...row,args:['1+2','10']}));
  assert.throws(()=>expression({name:'x',type:'pick',choices:[]}));
  assert.throws(()=>expression({name:'x',type:'randf',args:['NaN','10','2']}));
});
test('all eight generator families serialize into existing API rules',()=>{
  const rows=[
    [{type:'rand',args:['1','2']},'rand(1,2)'],
    [{type:'randf',args:['1.1','2.2','2']},'randf(1.1,2.2,2)'],
    [{type:'randstr',args:['16']},'randstr(16)'],
    [{type:'randdate',args:['2026-01-01','2026-12-31']},'randdate("2026-01-01","2026-12-31")'],
    [{type:'randdt',args:['2026-01-01T00:00:00','2026-12-31T23:59:59']},'randdt("2026-01-01 00:00:00","2026-12-31 23:59:59")'],
    [{type:'uuid',args:[]},'uuid()'],
    [{type:'pick',choices:[{type:'string',value:'1'}]},'pick("1")'],
    [{type:'pickw',choices:[{type:'string',value:'a',weight:'3'}]},'pickw(("a",3))']
  ];
  for(const [row,expected] of rows)assert.equal(expression(row),expected);
});
const {formPayload,mergeImport} = require('../../app/static/run_form.js');
test('text and file inputs cannot be submitted even when old groups exist',()=>{
  const groups=[{id:'old',name:'existing',weight:1,execution_mode:'autocommit',sql:'SELECT 1'}];
  for(const mode of ['paste','file']) assert.throws(()=>formPayload({mode,groups,rows:[],sql:'SELECT 2',fileText:'SELECT 3'}),/应用到任务组表单/);
  const payload=formPayload({mode:'form',groups,rows:[],sql:'SELECT 2',fileText:'SELECT 3'});
  assert.deepEqual(payload.groups,groups);
  assert.equal(Object.hasOwn(payload,'sql_content'),false);
});
test('import append and replacement preserve sources and assign fresh identifiers',()=>{
  const old=[{id:'same',name:'old',weight:1,sql:'SELECT 1'}];
  const incoming=[{id:'same',name:'imported',weight:2,sql:'SELECT 2'}];
  const appended=mergeImport(old,incoming,false,()=> 'fresh');
  assert.deepEqual(appended.map(g=>g.id),['same','fresh']);
  assert.equal(old.length,1);assert.equal(incoming[0].id,'same');
  assert.deepEqual(mergeImport(old,incoming,true,()=> 'replacement').map(g=>g.id),['replacement']);
  appended[0].sql='changed';assert.equal(old[0].sql,'SELECT 1');
});
