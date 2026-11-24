/* SQL Pulse form: structured draft, shared server parser, typed variables. */
'use strict';
const RunForm = (() => {
  const types = {rand:'Random integer number', randf:'Random decimal', pick:'Random enumeration', pickw:'Weighted list', randstr:'Random string', randdate:'Random date', randdt:'Random date time', uuid:'UUID'};
  const scalarTypes = {string:'String', integer:'Integer', number:'Decimal', boolean:'Boolean value', null:'Empty value'};
  const defaults = {rand:['1','10000'], randf:['0','100','2'], randstr:['16'], randdate:['2026-01-01','2026-12-31'], randdt:['2026-01-01 00:00:00','2026-12-31 23:59:59'], uuid:[], pick:[], pickw:[]};
  const labels = {rand:['Min','Maximum value'], randf:['Min','Maximum value','Decimal places'], randstr:['Length'], randdate:['Start date','End Date'], randdt:['Started At','End Time'], uuid:[]};
  const clone = x => JSON.parse(JSON.stringify(x));
  function clientId() {
    // Some embedded browsers expose crypto but not randomUUID, or throw when
    // crypto APIs are accessed from an insecure origin. IDs only need to be
    // unique in the current form, so always keep a local fallback available.
    try {
      const cryptoApi = typeof globalThis !== 'undefined' ? globalThis.crypto : null;
      if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID();
      if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
        const bytes = new Uint8Array(16);
        cryptoApi.getRandomValues(bytes);
        return `local-${Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')}`;
      }
    } catch (_) { /* Fall through to the non-cryptographic local ID. */ }
    return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
  function numeric(value, integer=false) {
    const text = String(value).trim();
    const pattern = integer ? /^-?\d+$/ : /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
    if (!pattern.test(text) || !Number.isFinite(Number(text))) throw Error(integer ? 'Enter an integer' : 'Enter a finite number');
    return integer ? BigInt(text).toString() : text;
  }
  function literal(choice) {
    if (choice.type === 'null') return 'None';
    if (choice.type === 'boolean') {
      if (!['true','false'].includes(choice.value)) throw Error('Please select a boolean value');
      return choice.value === 'true' ? 'True' : 'False';
    }
    if (choice.type === 'integer' || choice.type === 'number') return numeric(choice.value, choice.type === 'integer');
    return JSON.stringify(choice.value);
  }
  function expression(row) {
    let args;
    if (['pick','pickw'].includes(row.type)) {
      if (!row.choices.length) throw Error('Add at least one candidate');
      args = row.choices.map(c => row.type === 'pickw' ? `(${literal(c)},${numeric(c.weight)})` : literal(c));
    } else if (['randdate','randdt'].includes(row.type)) {
      args = row.args.map(v => JSON.stringify(v.replace('T',' ')));
    } else args = row.args.map((v, i) => numeric(v, row.type !== 'randf' || i === 2));
    return `${row.type}(${args.join(',')})`;
  }
  function variables(rows) {
    const result = Object.create(null);
    for (const row of rows) {
      try {
        if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(row.name)) throw Error('Use 1–64 letters, numbers, or underscores. The first character cannot be a number.');
        if (Object.hasOwn(result, row.name)) throw Error('Duplicate variable name');
        result[row.name] = expression(row);
      } catch (e) { e.variable = row.name; e.rowId = row.id; throw e; }
    }
    return result;
  }
  function shares(groups) {
    const active = groups.filter(g => g.sql.trim());
    if (active.some(g => !Number.isSafeInteger(Number(g.weight)) || Number(g.weight) <= 0)) return null;
    const total = active.reduce((sum,g) => sum + Number(g.weight),0);
    return groups.map(g => g.sql.trim() && total ? Number(g.weight)/total*100 : 0);
  }
  function formPayload(state) {
    if(state.mode !== 'form') throw Error('Apply the import to the form before starting the load test.');
    if(state.legacy) throw Error('Please amend the old variable configuration first.');
    if(shares(state.groups)===null) throw Error('All nonempty task groups must have positive integer weights.');
    const groups=state.groups.map(({id,name,weight,execution_mode,sql})=>({id,name,weight:Number(weight),execution_mode,sql}));
    return {groups,variables:variables(state.rows)};
  }
  function mergeImport(existing, imported, replace, makeId) {
    const additions=imported.map(g=>({...clone(g),id:makeId()}));
    return replace ? additions : [...clone(existing),...additions];
  }
  return {types, scalarTypes, defaults, labels, clone, clientId, expression, variables, shares, formPayload, mergeImport};
})();
if (typeof module !== 'undefined') module.exports = RunForm;
if (typeof document !== 'undefined') (() => {
  const {types, scalarTypes, defaults, labels, clone, clientId, expression, variables, shares, formPayload, mergeImport} = RunForm;
  const $ = s => document.querySelector(s);
  const all = s => [...document.querySelectorAll(s)];
  const key = 'sqlpulse.last_form';
  const uid = clientId;
  let state = {version:3, mode:'form', groups:[], rows:[], sql:'', fileText:'', fileName:'', legacy:''};
  let pendingImport = null;
  let revision = 0, editor = null, selection = [0,0], undoTimer, analysisTimer, busy = false;
  function element(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className=cls; return node; }
  function button(text, action, cls='secondary') { const node=element('button',text,cls); node.type='button'; node.addEventListener('click',action); return node; }
  function input(value, label, change, type='text') {
    const node=element('input'); node.type=type; node.value=value ?? ''; node.setAttribute('aria-label',label);
    node.addEventListener('input',()=>change(node.value)); return node;
  }
  function select(options, value, change, label) {
    const node=element('select'); node.setAttribute('aria-label',label);
    Object.entries(options).forEach(([key,text])=> { const o=element('option',text); o.value=key; node.append(o); });
    node.value=value; node.addEventListener('change',()=>change(node.value)); return node;
  }
  function detailError(detail) {
    if (Array.isArray(detail)) return detail.map(d => ({message:d.msg, field:d.loc?.join('.'), group_id:d.loc?.[1]==='groups' ? state.groups[d.loc[2]]?.id : undefined}));
    return [typeof detail === 'string' ? {message:detail} : detail];
  }
  async function api(url, body) {
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const data=await response.json();
    if (!response.ok) { const e=Error('Request failed'); e.details=detailError(data.detail || 'Request failed'); throw e; }
    return data;
  }
  const tool = body => api('/api/runs/form-tools',body);
  function showErrors(items) {
    $('#form-errors').replaceChildren();
    all('.field-error').forEach(n=>n.textContent='');
    all('.has-error').forEach(n=>n.classList.remove('has-error'));
    for (const item of items) {
      const group=state.groups.find(g=>g.id===item.group_id);
      const location=[group?.name || item.group_name,item.statement && `${item.statement} SQL statement`,item.line && `${item.line} line, column ${item.column || 1}`].filter(Boolean).join(' · ');
      const text=(location ? location+': ' : '')+(sqlPulseUiText(item.message) || 'Invalid configuration');
      const action=button(text,()=>focusError(item),'error-link');
      $('#form-errors').append(action);
      const card=all('.task-card').find(n=>n.dataset.id===item.group_id);
      const row=all('.variable-row').find(n=>n.dataset.id===item.rowId || n.dataset.name===item.variable);
      for (const target of [card,row].filter(Boolean)) {target.classList.add('has-error');target.querySelector('.field-error').textContent=text;}
    }
  }
  function fail(error) { showErrors(error.details || [{message:error.message,variable:error.variable,rowId:error.rowId}]); }
  function focusError(error) {
    const card=all('.task-card').find(n=>n.dataset.id===error.group_id);
    const row=all('.variable-row').find(n=>n.dataset.id===error.rowId || n.dataset.name===error.variable);
    if (card) { state.groups.find(g=>g.id===error.group_id).collapsed=false; card.querySelector('.group-body').hidden=false; }
    const target=card?.querySelector('textarea') || row?.querySelector('input') || (state.mode==='paste' ? $('#f-sql') : null);
    if (target) {target.focus();target.scrollIntoView({block:'center',behavior:'smooth'});if (error.offset !== undefined) target.setSelectionRange(error.offset,error.offset);}
  }
  function connMode() { return $('.seg-btn.active[data-conn-mode]').dataset.connMode; }
  function dsn() { return {host:$('#f-host').value,port:+$('#f-port').value,user:$('#f-user').value,password:$('#f-password').value,database:$('#f-database').value}; }
  function save() {
    const d=dsn(); delete d.password;
    try {localStorage.setItem(key,JSON.stringify({...state, name:$('#f-name').value,concurrency:$('#f-concurrency').value,spawn_rate:$('#f-spawn-rate').value,duration:$('#f-duration').value,connMode:connMode(),connection_id:$('#f-connection-id').value,dsn:d}));} catch (_) {}
  }
  function changed() {
    revision++; save(); refresh();
    if(pendingImport) {
      pendingImport=null;
      $('#import-result').replaceChildren(element('p','The input has changed. Parse it again.','hint warning'));
    }
    if (!$('#preview-panel').hidden) { $('#preview-panel').classList.add('stale'); const status=$('#preview-status'); if(status) status.textContent='The configuration has been modified and the preview has expired. Please check again.'; }
    clearTimeout(analysisTimer); analysisTimer=setTimeout(analyze,400);
  }
  function undo(text, snapshot, restore) {
    const bar=$('#undo-bar'); clearTimeout(undoTimer); bar.hidden=false; bar.replaceChildren(element('span',text));
    bar.append(button('Undo',()=> {if(restore)restore();else state=clone(snapshot); editor=null; render(); changed();bar.hidden=true;}));
    undoTimer=setTimeout(()=>{bar.hidden=true;},15000);
  }
  function remember(textarea) {
    ['focus','click','keyup','select','blur'].forEach(event=>textarea.addEventListener(event,()=> { editor=textarea; selection=[textarea.selectionStart,textarea.selectionEnd]; }));
  }
  function groupData() { return state.groups.map(({id,name,weight,execution_mode,sql})=>({id,name,weight:Number(weight),execution_mode,sql})); }
  function refresh() {
    const percentages=shares(state.groups);
    all('.task-card').forEach((card,i)=>{
      const g=state.groups[i];
      const invalid=g.sql.trim() && (!Number.isSafeInteger(Number(g.weight)) || Number(g.weight)<=0);
      card.querySelector('.t-badge').textContent=!g.sql.trim() ? 'Unfilled · Ignore when submitting' : invalid ? 'Weight must be a positive integer' : percentages ? `Projected share ${percentages[i].toFixed(1)}%` : 'Please amend the weights.';
      card.querySelector('.t-weight').setAttribute('aria-invalid',Boolean(invalid));
      card.querySelector('.execution-note').textContent=g.execution_mode==='transaction' ? 'SQL Pulse adds BEGIN and COMMIT. DDL may commit implicitly and prevent a full rollback.' : 'On failure, the group stops. Changes already committed remain in place.';
    });
    const empty=state.mode!=='form' || !state.groups.some(g=>g.sql.trim());
    $('#btn-submit').disabled=busy || empty || !!state.legacy;
    $('#btn-preview').disabled=busy || empty || !!state.legacy;
    all('.insert-variable').forEach((b,i)=>{
      let valid=true;try{variables([state.rows[i]]);}catch(_){valid=false;}
      b.disabled=busy || state.mode!=='form' || !valid;
      b.title=state.mode!=='form' ? 'Please apply the import to the form first' : 'Insert to the recently selected SQL editing position';
    });
  }
  async function analyze() {
    const rev=revision;
    await Promise.all(state.groups.map(async g=>{
      try {
        const result=await tool({action:'analyze',sql_content:g.sql});
        if (revision!==rev) return;
        const card=all('.task-card').find(n=>n.dataset.id===g.id);if(!card)return;
        const names=[...new Set(result.references.map(r=>r.name))];
        card.querySelector('.references').textContent=names.length ? 'Referenced variables: '+names.map(n=>n+(state.rows.some(r=>r.name===n)?'':' (Not defined)')).join(', ')+'. Reused within this task group.' : 'No named variables are referenced. Inline random placeholders are sampled independently.';
      } catch (_) { /* Full validation reports incomplete SQL on demand. */ }
    }));
  }
  async function normalizeGroup(g) {
    const original=g.sql;
    try {
      const result=await tool({action:'normalize',sql_content:original});
      if (g.sql!==original || !state.groups.includes(g) || !result.normalized)return;
      const snapshot=clone(state);g.sql=result.sql;g.execution_mode='transaction';renderGroups();changed();undo('Transaction boundaries moved to the task group settings.',snapshot);
    }catch(e){e.details=(e.details||[{message:e.message}]).map(d=>({...d,group_id:g.id}));fail(e);}
  }
  function renderGroups() {
    $('#task-list').replaceChildren();
    state.groups.forEach((g,index)=>{
      const card=element('article',undefined,'task-card');card.dataset.id=g.id;
      const head=element('div',undefined,'group-heading');
      const title=input(g.name,'Task Group Name',v=>{g.name=v;changed();});title.className='group-title';title.maxLength=100;
      const actions=element('div',undefined,'group-actions');
      actions.append(button(g.collapsed?'Expand':'Collapse',()=>{g.collapsed=!g.collapsed;renderGroups();save();}),button('Copy',()=>{state.groups.splice(index+1,0,{...clone(g),id:uid(),name:g.name+' Copy'});renderGroups();changed();}),button('Delete',()=>{const before=clone(state);state.groups.splice(index,1);renderGroups();changed();undo('Task Force deleted.',before,()=>{state.groups.splice(Math.min(index,state.groups.length),0,clone(g));});},'secondary danger-link'));
      head.append(title,actions);
      const controls=element('div',undefined,'group-controls');
      const mode=select({autocommit:'Autocommit',transaction:'Transaction'},g.execution_mode,v=>{g.execution_mode=v;changed();},'Execution Mode');
      const weight=input(g.weight,'Weight',v=>{g.weight=v;changed();},'number');weight.min='1';weight.step='1';weight.className='t-weight';
      const modeLabel=element('label','Execution Mode');modeLabel.append(mode);
      const weightLabel=element('label','Weight');weightLabel.append(weight);
      controls.append(modeLabel,weightLabel,element('span','', 't-badge'));
      const body=element('div',undefined,'group-body');body.hidden=!!g.collapsed;
      const note=element('p','', 'hint execution-note');
      const label=element('label','SQL · Multiple statements separated by semicolons');
      const area=element('textarea');area.rows=6;area.value=g.sql;area.spellcheck=false;area.setAttribute('aria-label',g.name+' SQL');area.placeholder="SELECT * FROM orders WHERE id = {{var('order_id')}};";
      area.addEventListener('input',()=>{g.sql=area.value;changed();});
      area.addEventListener('paste',()=>setTimeout(()=>normalizeGroup(g),0));remember(area);label.append(area);
      const tools=element('div','', 'form-actions');
      tools.append(button(' Suggest Placeholders',()=>smartComplete(g,area)),button('Detect Transaction Boundaries ',()=>normalizeGroup(g)));
      body.append(note,label,tools,element('p','','hint references'));
      card.append(head,controls,body,element('p','','field-error'));$('#task-list').append(card);
    });
    refresh();analyze();
  }
  async function smartComplete(g, area) {
    try {
      const body={sql:g.sql};
      if(connMode()==='saved') { if(!$('#f-connection-id').value) throw Error(' Please select the saved connection'); body.connection_id=$('#f-connection-id').value; }
      else body.db_dsn=dsn();
      const result=await api('/api/runs/smart-complete',body);
      if(!result.suggestions?.length) { alert(sqlPulseUiText(result.message) || ' No matching conditions found '); return; }
      const before=g.sql;
      const suggestions=result.suggestions;
      g.sql=before;
      for (const suggestion of [...suggestions].sort((a,b)=>b.start-a.start)) {
        g.sql=g.sql.slice(0,suggestion.start)+suggestion.replacement+g.sql.slice(suggestion.end);
      }
      renderGroups();changed();undo(`Auto-completion  ${suggestions.length} Placeholder. `,null,()=>{g.sql=before;renderGroups();changed();});
    } catch(e) { fail(e); }
  }
  function newRow() {return {id:uid(),name:'',type:'rand',args:clone(defaults.rand),choices:[]};}
  function renderRows() {
    $('#variable-list').replaceChildren();
    state.rows.forEach((row,index)=>{
      const tr=element('tr',undefined,'variable-row');tr.dataset.id=row.id;tr.dataset.name=row.name;
      const nameCell=element('td');let oldName=row.name;
      const name=input(row.name,'Variable Name',v=>{row.name=v;tr.dataset.name=v;changed();});name.maxLength=64;
      name.addEventListener('focus',()=>{oldName=row.name;});
      name.addEventListener('change',()=>renameVariable(row,oldName));nameCell.append(name,element('p','','field-error'));
      const typeCell=element('td');typeCell.append(select(types,row.type,v=>{row.type=v;row.args=clone(defaults[v]);row.choices=['pick','pickw'].includes(v)?[{type:'string',value:'',weight:'1'}]:[];renderRows();changed();},'Generation Type '));
      const params=element('td');
      if (['pick','pickw'].includes(row.type)) {
        row.choices.forEach((choice,i)=>{
          const line=element('div',undefined,'choice-row');
          line.append(select(scalarTypes,choice.type,v=>{choice.type=v;choice.value=v==='boolean'?'true':'';renderRows();changed();},' Candidate Type'));
          if(choice.type==='boolean') line.append(select({true:'true',false:'false'},choice.value,v=>{choice.value=v;changed();},' Boolean value '));
          else if(choice.type==='null')line.append(element('span','NULL','null-value'));
          else if(choice.type==='string') {const value=element('textarea');value.rows=1;value.value=choice.value;value.setAttribute('aria-label','Candidate ');value.addEventListener('input',()=>{choice.value=value.value;changed();});line.append(value);}
          else {const value=input(choice.value,'Candidate',v=>{choice.value=v;changed();});line.append(value);}
          if(row.type==='pickw') {const w=input(choice.weight??'1','Candidate weight ',v=>{choice.weight=v;changed();},'number');w.min='0';w.step='any';w.placeholder=' Weight';line.append(w);}
          line.append(button(' Remove',()=>{row.choices.splice(i,1);renderRows();changed();},'secondary compact'));params.append(line);
        });
        params.append(button('+ Candidate ',()=>{row.choices.push({type:'string',value:'',weight:'1'});renderRows();changed();},'secondary compact'));
      }else if(row.type==='uuid') params.append(element('span',' No arguments required ','hint'));
      else {
        const grid=element('div',undefined,'parameter-grid');
        labels[row.type].forEach((text,i)=>{
          const label=element('label',text);
          let kind=['randdate','randdt'].includes(row.type)?(row.type==='randdate'?'date':'datetime-local'):'number';
          const field=input((row.args[i]??'').replace(' ','T'),text,v=>{row.args[i]=v.replace('T',' ');if(row.type==='randdt' && row.args[i].length===16)row.args[i]+=':00';changed();},kind);
          field.step=row.type==='randdt'?'1':row.type==='randf'&&i<2?'any':'1';label.append(field);grid.append(label);
        });params.append(grid);
      }
      const ops=element('td');ops.className='variable-ops';
      const insert=button(' Insert Placeholder ',()=>insertVariable(row));insert.classList.add('insert-variable');
      ops.append(insert,button(' Delete ',()=>{const before=clone(state);state.rows.splice(index,1);renderRows();changed();undo(` Deleted Variables  ${row.name || ' ( Unnamed)'}; The reference location will be marked as undefined. `,before,()=>{state.rows.splice(Math.min(index,state.rows.length),0,clone(row));});},'secondary danger-link'));
      tr.append(nameCell,typeCell,params,ops);$('#variable-list').append(tr);
    });refresh();
  }
  async function renameVariable(row, oldName) {
    if(!oldName || oldName===row.name)return;
    const newName=row.name, rev=revision;
    try {
      variables(state.rows);
      const sources=state.groups.map(g=>g.sql);
      const results=await Promise.all(sources.map(sql_content=>tool({action:'rename',sql_content,old_name:oldName,new_name:newName})));
      if(revision!==rev)throw Error(' The content has changed, not synchronised. Please try again after you restore the old variable name. ');
      state.groups.forEach((g,i)=>g.sql=results[i].sql_content);
      render();changed();
    }catch(e){row.name=oldName;renderRows();changed();fail(e);}
  }
  async function insertVariable(row) {
    const target=editor, range=[...selection], name=row.name, rev=revision;
    try {
      if(state.mode!=='form' || !target?.isConnected || target.closest('[hidden]') || !target.closest('#tab-form'))throw Error(' Please place the cursor in the target SQL edit box first.');
      const config=variables(state.rows);await tool({action:'variables',variables:{[name]:config[name]}});
      if(rev!==revision || !target.isConnected)throw Error(', re-select the place of insertion. ');
      target.setRangeText(`{{var('${name}')}}`,range[0],range[1],'end');target.dispatchEvent(new Event('input',{bubbles:true}));target.focus();selection=[target.selectionStart,target.selectionEnd];showErrors([]);
    }catch(e){e.details=(e.details||[{message:e.message}]).map(d=>({...d,variable:name,rowId:row.id}));fail(e);}
  }
  function renderMode() {
    all('[data-tab]').forEach(n=>{n.classList.toggle('active',n.dataset.tab===state.mode);n.setAttribute('aria-selected',n.dataset.tab===state.mode);});
    ['form','paste','file'].forEach(mode=>{$('#tab-'+mode).style.display=mode===state.mode?'':'none';});
    $('#import-actions').hidden=state.mode==='form';
    $('#form-submit-actions').hidden=state.mode!=='form';
    if(state.mode!=='form')$('#preview-panel').hidden=true;
    $('#f-sql').value=state.sql;$('#file-preview').textContent=state.fileText;$('#file-preview').hidden=!state.fileText;
    $('#legacy-variables').hidden=!state.legacy;$('#legacy-variable-text').value=state.legacy;
  }
  function render() {renderMode();renderGroups();renderRows();}
  function switchMode(mode) {
    if(mode===state.mode || busy)return;
    state.mode=mode;editor=null;pendingImport=null;
    $('#import-result').hidden=true;$('#import-errors').replaceChildren();
    render();changed();showErrors([]);
  }
  function importText() {return state.mode==='file'?state.fileText:state.sql;}
  function importError(error) {
    const items=error.details || [{message:error.message}];
    $('#import-errors').replaceChildren(...items.map(d=>element('p',
      [d.group_name,d.line && `  ${d.line} line, column ${d.column || 1}`,sqlPulseUiText(d.message)].filter(Boolean).join(' · '),'field-error')));
  }
  async function parseImport() {
    if(state.mode==='form')return;
    const text=importText(),rev=revision;
    pendingImport=null;$('#import-result').hidden=true;$('#import-errors').replaceChildren();
    $('#btn-parse-import').disabled=true;
    try {
      if(!text.trim())throw Error('Please paste SQL or select a file first.');
      const data=await tool({action:'import',sql_content:text});
      if(rev!==revision)throw Error('Import content or configuration has changed, please resolve. ');
      pendingImport={...data,revision:rev,source:state.mode,text};
      const panel=$('#import-result');panel.hidden=false;panel.replaceChildren(element('h3',`Import Parsed · ${data.groups.length}  task groups `));
      panel.append(element('p',' Weight comments define task groups. Without weights, standalone statements are grouped separately and complete transactions stay together. ','hint'));
      const wrap=element('div',undefined,'variable-scroll'), table=element('table',undefined,'table');
      const head=element('tr');[' Task Groups ',' Execution Mode ',' Weight ',' SQL Statements '].forEach(t=>head.append(element('th',t)));table.append(head);
      data.groups.forEach((g,i)=>{
        const info=data.summary[i],tr=element('tr');
        [g.name,g.execution_mode==='transaction'?' Transaction ':'Autocommit',g.weight,info.statement_count].forEach(t=>tr.append(element('td',String(t))));table.append(tr);
        const details=element('details');details.append(element('summary',g.name+' · View Converted SQL'),element('pre',g.sql,'sql-view'));
        if(g.execution_mode==='transaction')details.append(element('p','Outer transaction boundaries were detected. The form will manage BEGIN and COMMIT after import.','hint'));
        const missing=info.references.filter(n=>!state.rows.some(r=>r.name===n));
        if(missing.length)details.append(element('p',' Configure these variables: '+missing.join(', ')+'. Apply the import and configure them before starting the test. ','hint warning'));
        info.warnings.forEach(w=>details.append(element('p',sqlPulseUiText(w),'hint warning')));
        // Keep important conversion notices visible without expanding SQL details.
        if(missing.length)panel.append(element('p',g.name+' ·  Configure these variables: '+missing.join(', '),'hint warning'));
        info.warnings.forEach(w=>panel.append(element('p',g.name+' · '+sqlPulseUiText(w),'hint warning')));
        panel.append(details);
      });
      wrap.append(table);panel.insertBefore(wrap,panel.children[2] || null);
      const actions=element('div',undefined,'form-actions');
      if(state.groups.some(g=>g.sql.trim()))actions.append(button('Add to existing form ',()=>applyImport(false)),button(' Replace existing forms ',()=>applyImport(true)));
      else actions.append(button(' Apply to Form',()=>applyImport(true),'primary'));
      actions.append(button('Back to Editor ',()=>{pendingImport=null;panel.hidden=true;if(state.mode==='paste')$('#f-sql').focus();}));panel.append(actions);
    }catch(e){importError(e);}finally{$('#btn-parse-import').disabled=false;}
  }
  function applyImport(replace) {
    if(!pendingImport || pendingImport.revision!==revision || pendingImport.source!==state.mode || pendingImport.text!==importText()) {
      importError(Error('Import is out of date. Parse the input again.'));return;
    }
    const previous=clone(state.groups),data=pendingImport;
    state.groups=mergeImport(state.groups,data.groups,replace,uid);
    pendingImport=null;state.mode='form';editor=null;$('#import-result').hidden=true;
    render();changed();showErrors([]);
    undo(replace?' The form has been replaced with the import result; the variable configuration is retained.':' The task groups were added and their estimated shares recalculated. ',null,()=>{
      if(replace)state.groups=clone(previous);
      else {const oldIds=new Set(previous.map(g=>g.id));const importedIds=new Set(data.appliedIds);state.groups=state.groups.filter(g=>!importedIds.has(g.id) || oldIds.has(g.id));}
    });
    data.appliedIds=state.groups.filter(g=>!previous.some(p=>p.id===g.id)).map(g=>g.id);
    $('#tab-form').scrollIntoView({block:'start',behavior:'smooth'});
  }
  function newGroup() {return {id:uid(),name:`Task Group ${state.groups.length+1}`,execution_mode:'autocommit',weight:1,sql:''};}
  function payload() {return formPayload(state);}
  function renderPreview(result) {
    const panel=$('#preview-panel');panel.hidden=false;panel.classList.remove('stale');panel.replaceChildren();
    const status=element('h3','✓  Validation ');status.id='preview-status';panel.append(status,element('p',' This preview uses sample values and does not connect to the target database or execute SQL. Values are regenerated during the test.','hint'));
    for(const group of result.groups) {
      const block=element('article',undefined,'preview-group');
      const count=group.statements.length-(group.execution_mode==='transaction'?2:0);
      block.append(element('h4',`${group.name} · ${group.execution_mode==='transaction'?'Transaction':group.execution_mode==='autocommit'?'Autocommit ':'Text Script '} · ${count} SQL statements ·  Projected share  ${group.share}%`));
      block.append(element('pre',Object.entries(group.variables).map(([name,v])=>`${name} = ${v.value} (${v.type})`).join('\n') || ' No named variables','preview-values'));
      block.append(element('p',group.execution_mode==='transaction'?`BEGIN → ${count} SQL statements → COMMIT`:' Execute in the order of statements ','hint'));
      group.warnings.forEach(w=>block.append(element('p',sqlPulseUiText(w),'hint warning')));
      const details=element('details');details.append(element('summary',' View SQL with Bound Parameters '));
      group.statements.forEach((s,i)=>details.append(element('pre',`${i+1}. ${s.sql}\n Parameters: ${JSON.stringify(s.parameters,null,2)}`,'sql-view')));block.append(details);panel.append(block);
    }
    panel.append(button(' Refresh Sample Values ',runPreview));
  }
  async function runPreview() {
    try {const body=payload(),rev=revision;const result=await api('/api/runs/preview',body);if(rev!==revision)throw Error(' Configuration changed. Validate it again. ');showErrors(result.errors);if(result.ok)renderPreview(result);else{$('#preview-panel').hidden=true;}}
    catch(e){fail(e);}
  }
  async function migrateVariables() {
    try {
      const config=Object.create(null);
      for(const [index,line] of state.legacy.split('\n').entries()) {
        if(!line.trim())continue;
        const m=line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$/);
        if(!m || Object.hasOwn(config,m[1]))throw Error(`Old variable configuration first  ${index+1}  Line Format Error or Rename `);
        config[m[1]]=m[2];
      }
      const data=await tool({action:'variables',variables:config});state.rows=data.rows.map(r=>({...r,id:uid()}));state.legacy='';render();changed();showErrors([]);
    }catch(e){renderMode();fail(e);}
  }
  async function load() {
    let saved;try{saved=JSON.parse(localStorage.getItem(key)||'null');}catch(_){}
    if(saved) {
      if(saved.version===2 || saved.version===3) state={...state,...saved,version:3};
      else {
        state.groups=(saved.tasks||[]).map((g,i)=>({id:uid(),name:`Task Group ${i+1}`,weight:g.weight,execution_mode:g.type==='txn'?'transaction':'autocommit',sql:g.sql||''}));state.sql=saved.sql||'';state.legacy=saved.variables||'';
        for(const g of state.groups) {try{const normalized=await tool({action:'normalize',sql_content:g.sql});if(normalized.normalized){g.sql=normalized.sql;g.execution_mode='transaction';}}catch(_){/* Preserve complex drafts verbatim. */}}
      }
      for(const [field,id] of Object.entries({name:'f-name',concurrency:'f-concurrency',spawn_rate:'f-spawn-rate',duration:'f-duration'}))if(saved[field])$('#'+id).value=saved[field];
      if(saved.dsn)for(const name of ['host','port','user','database'])if(saved.dsn[name]!==undefined)$('#f-'+name).value=saved.dsn[name];
      if(saved.connection_id && [...$('#f-connection-id').options].some(o=>o.value===saved.connection_id))$('#f-connection-id').value=saved.connection_id;
      if(saved.connMode)connectionMode(saved.connMode,false);
    }
    if(!state.groups.length)state.groups.push(newGroup());render();
    if(state.legacy)await migrateVariables();
  }
  function connectionMode(mode,persist=true) {
    all('[data-conn-mode]').forEach(n=>n.classList.toggle('active',n.dataset.connMode===mode));$('#conn-saved').style.display=mode==='saved'?'':'none';$('#conn-temp').style.display=mode==='temp'?'':'none';if(persist)save();
  }
  async function testConnection(saved) {
    const target=$(saved?'#db-test-result':'#db-test-result-temp');target.textContent=' Testing... ';
    try {if(saved&&!$('#f-connection-id').value)throw Error(' Select a connection first ');const url=saved?'/api/connections/'+$('#f-connection-id').value+'/test':'/api/connections/test';const data=await api(url,saved?{}:dsn());target.textContent=data.ok?'✓ Connection successful ':'✗ '+sqlPulseUiText(data.error);target.className='hint '+(data.ok?'ok':'err');}catch(e){target.textContent=e.message;target.className='hint err';}
  }
  all('[data-conn-mode]').forEach(n=>n.addEventListener('click',()=>connectionMode(n.dataset.connMode)));
  all('[data-tab]').forEach(n=>n.addEventListener('click',()=>switchMode(n.dataset.tab)));
  $('#btn-test-saved').addEventListener('click',()=>testConnection(true));$('#btn-test-db').addEventListener('click',()=>testConnection(false));
  $('#btn-add-task').addEventListener('click',()=>{state.groups.push(newGroup());renderGroups();changed();});
  $('#btn-add-variable').addEventListener('click',()=>{state.rows.push(newRow());renderRows();changed();});
  $('#btn-preview').addEventListener('click',runPreview);
  $('#btn-migrate-variables').addEventListener('click',migrateVariables);
  $('#legacy-variable-text').addEventListener('input',e=>{state.legacy=e.target.value;changed();});
  $('#f-sql').addEventListener('input',e=>{state.sql=e.target.value;changed();});remember($('#f-sql'));
  $('#f-sql-file').addEventListener('change',async e=>{try{const file=e.target.files[0],rev=revision;if(!file)return;const text=await file.text();if(e.target.files[0]!==file || rev!==revision)throw Error('Select or configure changed, re-select the file. ');state.fileText=text;state.fileName=file.name;renderMode();changed();}catch(e){importError(e);}});
  $('#btn-parse-import').addEventListener('click',parseImport);
  $('#btn-edit-file').addEventListener('click',()=>{const before=state.sql;state.sql=state.fileText;switchMode('paste');undo('The original document has been copied to the text import area; the current form has not been changed.',null,()=>{state.sql=before;});});
  $('#btn-example').addEventListener('click',async()=>{try{const rev=revision,response=await fetch('/static/examples/demo.sql');if(!response.ok)throw Error('Could not load the example ');const text=await response.text();if(rev!==revision)throw Error(' content has changed and the editing is not covered.');const oldText=state.sql;state.sql=text;state.mode='paste';render();changed();undo(' Example has been placed in the import area. Please check and apply to the form.',null,()=>{state.sql=oldText;});await parseImport();}catch(e){fail(e);}});
  ['f-name','f-connection-id','f-host','f-port','f-user','f-database','f-concurrency','f-spawn-rate','f-duration'].forEach(id=>$('#'+id).addEventListener('input',changed));
  $('#run-form').addEventListener('submit',async e=>{
    e.preventDefault();if(busy)return;
    try {
      const source=payload(),rev=revision;busy=true;refresh();const checked=await api('/api/runs/preview',source);
      if(rev!==revision)throw Error(' Configuration changed. Validate it before starting the test. ');
      if(!checked.ok){showErrors(checked.errors);return;}
      const body={...source,name:$('#f-name').value,sql_source:'form',concurrency:+$('#f-concurrency').value,spawn_rate:+$('#f-spawn-rate').value,duration_sec:+$('#f-duration').value};
      if(connMode()==='saved'){if(!$('#f-connection-id').value)throw Error(' Please select the saved connection ');body.connection_id=$('#f-connection-id').value;}else body.db_dsn=dsn();
      busy=true;refresh();$('#submit-result').textContent='Starting test… ';save();const data=await api('/api/runs',body);window.location.href='/runs/'+data.run_id+'/monitor';
    }catch(e){fail(e);$('#submit-result').textContent=' Could not close temporary folder: %s.';}finally{busy=false;refresh();}
  });
  load().catch(fail);
})();
