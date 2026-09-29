/* SQL Pulse form: structured draft, shared server parser, typed variables. */
'use strict';
const RunForm = (() => {
  const types = {rand:'随机整数', randf:'随机小数', pick:'随机枚举', pickw:'加权枚举', randstr:'随机字符串', randdate:'随机日期', randdt:'随机日期时间', uuid:'UUID'};
  const scalarTypes = {string:'字符串', integer:'整数', number:'小数', boolean:'布尔值', null:'空值'};
  const defaults = {rand:['1','10000'], randf:['0','100','2'], randstr:['16'], randdate:['2026-01-01','2026-12-31'], randdt:['2026-01-01 00:00:00','2026-12-31 23:59:59'], uuid:[], pick:[], pickw:[]};
  const labels = {rand:['最小值','最大值'], randf:['最小值','最大值','小数位'], randstr:['长度'], randdate:['开始日期','结束日期'], randdt:['开始时间','结束时间'], uuid:[]};
  const clone = x => JSON.parse(JSON.stringify(x));
  function numeric(value, integer=false) {
    const text = String(value).trim();
    const pattern = integer ? /^-?\d+$/ : /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;
    if (!pattern.test(text) || !Number.isFinite(Number(text))) throw Error(integer ? '请输入整数' : '请输入有限数字');
    return integer ? BigInt(text).toString() : text;
  }
  function literal(choice) {
    if (choice.type === 'null') return 'None';
    if (choice.type === 'boolean') {
      if (!['true','false'].includes(choice.value)) throw Error('请选择布尔值');
      return choice.value === 'true' ? 'True' : 'False';
    }
    if (choice.type === 'integer' || choice.type === 'number') return numeric(choice.value, choice.type === 'integer');
    return JSON.stringify(choice.value);
  }
  function expression(row) {
    let args;
    if (['pick','pickw'].includes(row.type)) {
      if (!row.choices.length) throw Error('至少添加一个候选值');
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
        if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(row.name)) throw Error('变量名须为 1～64 位字母、数字、下划线，不能以数字开头');
        if (Object.hasOwn(result, row.name)) throw Error('变量名重复');
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
    if(state.mode !== 'form') throw Error('请先将导入内容应用到任务组表单，再开始压测。');
    if(state.legacy) throw Error('请先修正旧变量配置。');
    if(shares(state.groups)===null) throw Error('所有非空任务组的权重必须是正整数。');
    const groups=state.groups.map(({id,name,weight,execution_mode,sql})=>({id,name,weight:Number(weight),execution_mode,sql}));
    return {groups,variables:variables(state.rows)};
  }
  function mergeImport(existing, imported, replace, makeId) {
    const additions=imported.map(g=>({...clone(g),id:makeId()}));
    return replace ? additions : [...clone(existing),...additions];
  }
  return {types, scalarTypes, defaults, labels, clone, expression, variables, shares, formPayload, mergeImport};
})();
if (typeof module !== 'undefined') module.exports = RunForm;
if (typeof document !== 'undefined') (() => {
  const {types, scalarTypes, defaults, labels, clone, expression, variables, shares, formPayload, mergeImport} = RunForm;
  const $ = s => document.querySelector(s);
  const all = s => [...document.querySelectorAll(s)];
  const key = 'sqlpulse.last_form';
  const uid = () => crypto.randomUUID();
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
    if (!response.ok) { const e=Error('请求失败'); e.details=detailError(data.detail || '请求失败'); throw e; }
    return data;
  }
  const tool = body => api('/api/runs/form-tools',body);
  function showErrors(items) {
    $('#form-errors').replaceChildren();
    all('.field-error').forEach(n=>n.textContent='');
    all('.has-error').forEach(n=>n.classList.remove('has-error'));
    for (const item of items) {
      const group=state.groups.find(g=>g.id===item.group_id);
      const location=[group?.name || item.group_name,item.statement && `第 ${item.statement} 条 SQL`,item.line && `第 ${item.line} 行第 ${item.column || 1} 列`].filter(Boolean).join(' · ');
      const text=(location ? location+'：' : '')+(item.message || '配置错误');
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
      $('#import-result').replaceChildren(element('p','内容已修改，导入结果已过期，请重新解析。','hint warning'));
    }
    if (!$('#preview-panel').hidden) { $('#preview-panel').classList.add('stale'); const status=$('#preview-status'); if(status) status.textContent='配置已修改，预览已过期。请重新校验。'; }
    clearTimeout(analysisTimer); analysisTimer=setTimeout(analyze,400);
  }
  function undo(text, snapshot, restore) {
    const bar=$('#undo-bar'); clearTimeout(undoTimer); bar.hidden=false; bar.replaceChildren(element('span',text));
    bar.append(button('撤销',()=> {if(restore)restore();else state=clone(snapshot); editor=null; render(); changed();bar.hidden=true;}));
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
      card.querySelector('.t-badge').textContent=!g.sql.trim() ? '未填写 · 提交时忽略' : invalid ? '权重须为正整数' : percentages ? `预计占比 ${percentages[i].toFixed(1)}%` : '请修正权重';
      card.querySelector('.t-weight').setAttribute('aria-invalid',Boolean(invalid));
      card.querySelector('.execution-note').textContent=g.execution_mode==='transaction' ? '组内全部成功后统一提交，失败时回滚未提交的修改。系统自动添加 BEGIN / COMMIT；DDL 等隐式提交语句不保证整体回滚。' : '组内顺序执行，每条成功后提交。中途失败则停止本组，之前已提交的修改保留。';
    });
    const empty=state.mode!=='form' || !state.groups.some(g=>g.sql.trim());
    $('#btn-submit').disabled=busy || empty || !!state.legacy;
    $('#btn-preview').disabled=busy || empty || !!state.legacy;
    all('.insert-variable').forEach((b,i)=>{
      let valid=true;try{variables([state.rows[i]]);}catch(_){valid=false;}
      b.disabled=busy || state.mode!=='form' || !valid;
      b.title=state.mode!=='form' ? '请先将导入内容应用到表单' : '插入到最近选择的 SQL 编辑位置';
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
        card.querySelector('.references').textContent=names.length ? '引用变量：'+names.map(n=>n+(state.rows.some(r=>r.name===n)?'':'（未定义）')).join('、')+'。同次任务组执行内复用。' : '未引用命名变量。直接随机占位符各位置独立采样。';
      } catch (_) { /* Full validation reports incomplete SQL on demand. */ }
    }));
  }
  async function normalizeGroup(g) {
    const original=g.sql;
    try {
      const result=await tool({action:'normalize',sql_content:original});
      if (g.sql!==original || !state.groups.includes(g) || !result.normalized)return;
      const snapshot=clone(state);g.sql=result.sql;g.execution_mode='transaction';renderGroups();changed();undo('已识别事务，边界由系统管理。',snapshot);
    }catch(e){e.details=(e.details||[{message:e.message}]).map(d=>({...d,group_id:g.id}));fail(e);}
  }
  function renderGroups() {
    $('#task-list').replaceChildren();
    state.groups.forEach((g,index)=>{
      const card=element('article',undefined,'task-card');card.dataset.id=g.id;
      const head=element('div',undefined,'group-heading');
      const title=input(g.name,'任务组名称',v=>{g.name=v;changed();});title.className='group-title';title.maxLength=100;
      const actions=element('div',undefined,'group-actions');
      actions.append(button(g.collapsed?'展开':'折叠',()=>{g.collapsed=!g.collapsed;renderGroups();save();}),button('复制',()=>{state.groups.splice(index+1,0,{...clone(g),id:uid(),name:g.name+' 副本'});renderGroups();changed();}),button('删除',()=>{const before=clone(state);state.groups.splice(index,1);renderGroups();changed();undo('已删除任务组。',before,()=>{state.groups.splice(Math.min(index,state.groups.length),0,clone(g));});},'secondary danger-link'));
      head.append(title,actions);
      const controls=element('div',undefined,'group-controls');
      const mode=select({autocommit:'逐条提交',transaction:'事务执行'},g.execution_mode,v=>{g.execution_mode=v;changed();},'执行方式');
      const weight=input(g.weight,'权重',v=>{g.weight=v;changed();},'number');weight.min='1';weight.step='1';weight.className='t-weight';
      const modeLabel=element('label','执行方式');modeLabel.append(mode);
      const weightLabel=element('label','权重');weightLabel.append(weight);
      controls.append(modeLabel,weightLabel,element('span','', 't-badge'));
      const body=element('div',undefined,'group-body');body.hidden=!!g.collapsed;
      const note=element('p','', 'hint execution-note');
      const label=element('label','SQL · 多条语句用分号分隔');
      const area=element('textarea');area.rows=6;area.value=g.sql;area.spellcheck=false;area.setAttribute('aria-label',g.name+' SQL');area.placeholder="SELECT * FROM orders WHERE id = {{var('order_id')}};";
      area.addEventListener('input',()=>{g.sql=area.value;changed();});
      area.addEventListener('paste',()=>setTimeout(()=>normalizeGroup(g),0));remember(area);label.append(area);
      body.append(note,label,button('识别已有事务边界',()=>normalizeGroup(g)),element('p','','hint references'));
      card.append(head,controls,body,element('p','','field-error'));$('#task-list').append(card);
    });
    refresh();analyze();
  }
  function newRow() {return {id:uid(),name:'',type:'rand',args:clone(defaults.rand),choices:[]};}
  function renderRows() {
    $('#variable-list').replaceChildren();
    state.rows.forEach((row,index)=>{
      const tr=element('tr',undefined,'variable-row');tr.dataset.id=row.id;tr.dataset.name=row.name;
      const nameCell=element('td');let oldName=row.name;
      const name=input(row.name,'变量名称',v=>{row.name=v;tr.dataset.name=v;changed();});name.maxLength=64;
      name.addEventListener('focus',()=>{oldName=row.name;});
      name.addEventListener('change',()=>renameVariable(row,oldName));nameCell.append(name,element('p','','field-error'));
      const typeCell=element('td');typeCell.append(select(types,row.type,v=>{row.type=v;row.args=clone(defaults[v]);row.choices=['pick','pickw'].includes(v)?[{type:'string',value:'',weight:'1'}]:[];renderRows();changed();},'生成类型'));
      const params=element('td');
      if (['pick','pickw'].includes(row.type)) {
        row.choices.forEach((choice,i)=>{
          const line=element('div',undefined,'choice-row');
          line.append(select(scalarTypes,choice.type,v=>{choice.type=v;choice.value=v==='boolean'?'true':'';renderRows();changed();},'候选值类型'));
          if(choice.type==='boolean') line.append(select({true:'true',false:'false'},choice.value,v=>{choice.value=v;changed();},'布尔值'));
          else if(choice.type==='null')line.append(element('span','NULL','null-value'));
          else if(choice.type==='string') {const value=element('textarea');value.rows=1;value.value=choice.value;value.setAttribute('aria-label','候选值');value.addEventListener('input',()=>{choice.value=value.value;changed();});line.append(value);}
          else {const value=input(choice.value,'候选值',v=>{choice.value=v;changed();});line.append(value);}
          if(row.type==='pickw') {const w=input(choice.weight??'1','候选权重',v=>{choice.weight=v;changed();},'number');w.min='0';w.step='any';w.placeholder='权重';line.append(w);}
          line.append(button('移除',()=>{row.choices.splice(i,1);renderRows();changed();},'secondary compact'));params.append(line);
        });
        params.append(button('+ 候选值',()=>{row.choices.push({type:'string',value:'',weight:'1'});renderRows();changed();},'secondary compact'));
      }else if(row.type==='uuid') params.append(element('span','无需参数','hint'));
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
      const insert=button('插入占位符',()=>insertVariable(row));insert.classList.add('insert-variable');
      ops.append(insert,button('删除',()=>{const before=clone(state);state.rows.splice(index,1);renderRows();changed();undo(`已删除变量 ${row.name || '（未命名）'}；引用位置会标记为未定义。`,before,()=>{state.rows.splice(Math.min(index,state.rows.length),0,clone(row));});},'secondary danger-link'));
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
      if(revision!==rev)throw Error('内容已变化，未同步引用。请恢复旧变量名后重试。');
      state.groups.forEach((g,i)=>g.sql=results[i].sql_content);
      render();changed();
    }catch(e){row.name=oldName;renderRows();changed();fail(e);}
  }
  async function insertVariable(row) {
    const target=editor, range=[...selection], name=row.name, rev=revision;
    try {
      if(state.mode!=='form' || !target?.isConnected || target.closest('[hidden]') || !target.closest('#tab-form'))throw Error('请先在目标 SQL 编辑框中放置光标。');
      const config=variables(state.rows);await tool({action:'variables',variables:{[name]:config[name]}});
      if(rev!==revision || !target.isConnected)throw Error('配置已变化，请重新选择插入位置。');
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
      [d.group_name,d.line && `第 ${d.line} 行第 ${d.column || 1} 列`,d.message].filter(Boolean).join(' · '),'field-error')));
  }
  async function parseImport() {
    if(state.mode==='form')return;
    const text=importText(),rev=revision;
    pendingImport=null;$('#import-result').hidden=true;$('#import-errors').replaceChildren();
    $('#btn-parse-import').disabled=true;
    try {
      if(!text.trim())throw Error('请先粘贴 SQL 或选择文件。');
      const data=await tool({action:'import',sql_content:text});
      if(rev!==revision)throw Error('导入内容或配置已变化，请重新解析。');
      pendingImport={...data,revision:rev,source:state.mode,text};
      const panel=$('#import-result');panel.hidden=false;panel.replaceChildren(element('h3',`解析成功 · ${data.groups.length} 个任务组`));
      panel.append(element('p','按 weight 段划分任务组；无权重时普通语句各成一组，完整事务块整体成组。尚未应用到表单。','hint'));
      const wrap=element('div',undefined,'variable-scroll'), table=element('table',undefined,'table');
      const head=element('tr');['任务组','执行方式','权重','业务 SQL 数量'].forEach(t=>head.append(element('th',t)));table.append(head);
      data.groups.forEach((g,i)=>{
        const info=data.summary[i],tr=element('tr');
        [g.name,g.execution_mode==='transaction'?'事务执行':'逐条提交',g.weight,info.statement_count].forEach(t=>tr.append(element('td',String(t))));table.append(tr);
        const details=element('details');details.append(element('summary',g.name+' · 查看转换后的 SQL'),element('pre',g.sql,'sql-view'));
        if(g.execution_mode==='transaction')details.append(element('p','已识别外层事务边界，应用后由表单统一管理 BEGIN / COMMIT。','hint'));
        const missing=info.references.filter(n=>!state.rows.some(r=>r.name===n));
        if(missing.length)details.append(element('p','待配置变量：'+missing.join('、')+'。可先应用到表单，补全后才能开始压测。','hint warning'));
        info.warnings.forEach(w=>details.append(element('p',w,'hint warning')));
        // Keep important conversion notices visible without expanding SQL details.
        if(missing.length)panel.append(element('p',g.name+' · 待配置变量：'+missing.join('、'),'hint warning'));
        info.warnings.forEach(w=>panel.append(element('p',g.name+' · '+w,'hint warning')));
        panel.append(details);
      });
      wrap.append(table);panel.insertBefore(wrap,panel.children[2] || null);
      const actions=element('div',undefined,'form-actions');
      if(state.groups.some(g=>g.sql.trim()))actions.append(button('追加到现有表单',()=>applyImport(false)),button('替换现有表单',()=>applyImport(true)));
      else actions.append(button('应用到表单',()=>applyImport(true),'primary'));
      actions.append(button('返回修改',()=>{pendingImport=null;panel.hidden=true;if(state.mode==='paste')$('#f-sql').focus();}));panel.append(actions);
    }catch(e){importError(e);}finally{$('#btn-parse-import').disabled=false;}
  }
  function applyImport(replace) {
    if(!pendingImport || pendingImport.revision!==revision || pendingImport.source!==state.mode || pendingImport.text!==importText()) {
      importError(Error('导入结果已过期，请重新解析。'));return;
    }
    const previous=clone(state.groups),data=pendingImport;
    state.groups=mergeImport(state.groups,data.groups,replace,uid);
    pendingImport=null;state.mode='form';editor=null;$('#import-result').hidden=true;
    render();changed();showErrors([]);
    undo(replace?'已用导入结果替换表单；变量配置保留。':'已追加任务组，预计占比已重新计算。',null,()=>{
      if(replace)state.groups=clone(previous);
      else {const oldIds=new Set(previous.map(g=>g.id));const importedIds=new Set(data.appliedIds);state.groups=state.groups.filter(g=>!importedIds.has(g.id) || oldIds.has(g.id));}
    });
    data.appliedIds=state.groups.filter(g=>!previous.some(p=>p.id===g.id)).map(g=>g.id);
    $('#tab-form').scrollIntoView({block:'start',behavior:'smooth'});
  }
  function newGroup() {return {id:uid(),name:`任务组 ${state.groups.length+1}`,execution_mode:'autocommit',weight:1,sql:''};}
  function payload() {return formPayload(state);}
  function renderPreview(result) {
    const panel=$('#preview-panel');panel.hidden=false;panel.classList.remove('stale');panel.replaceChildren();
    const status=element('h3','✓ 校验通过');status.id='preview-status';panel.append(status,element('p','仅展示示例，不连接目标数据库、不执行 SQL；正式压测时重新生成。','hint'));
    for(const group of result.groups) {
      const block=element('article',undefined,'preview-group');
      const count=group.statements.length-(group.execution_mode==='transaction'?2:0);
      block.append(element('h4',`${group.name} · ${group.execution_mode==='transaction'?'事务执行':group.execution_mode==='autocommit'?'逐条提交':'文本脚本'} · ${count} 条业务 SQL · 预计占比 ${group.share}%`));
      block.append(element('pre',Object.entries(group.variables).map(([name,v])=>`${name} = ${v.value} (${v.type})`).join('\n') || '未引用命名变量','preview-values'));
      block.append(element('p',group.execution_mode==='transaction'?`BEGIN → ${count} 条业务 SQL → COMMIT`:'按语句顺序执行','hint'));
      group.warnings.forEach(w=>block.append(element('p',w,'hint warning')));
      const details=element('details');details.append(element('summary','查看参数化 SQL 与本次参数'));
      group.statements.forEach((s,i)=>details.append(element('pre',`${i+1}. ${s.sql}\n参数：${JSON.stringify(s.parameters,null,2)}`,'sql-view')));block.append(details);panel.append(block);
    }
    panel.append(button('重新生成示例',runPreview));
  }
  async function runPreview() {
    try {const body=payload(),rev=revision;const result=await api('/api/runs/preview',body);if(rev!==revision)throw Error('配置已修改，请重新校验。');showErrors(result.errors);if(result.ok)renderPreview(result);else{$('#preview-panel').hidden=true;}}
    catch(e){fail(e);}
  }
  async function migrateVariables() {
    try {
      const config=Object.create(null);
      for(const [index,line] of state.legacy.split('\n').entries()) {
        if(!line.trim())continue;
        const m=line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$/);
        if(!m || Object.hasOwn(config,m[1]))throw Error(`旧变量配置第 ${index+1} 行格式错误或重名`);
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
        state.groups=(saved.tasks||[]).map((g,i)=>({id:uid(),name:`任务组 ${i+1}`,weight:g.weight,execution_mode:g.type==='txn'?'transaction':'autocommit',sql:g.sql||''}));state.sql=saved.sql||'';state.legacy=saved.variables||'';
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
    const target=$(saved?'#db-test-result':'#db-test-result-temp');target.textContent='测试中…';
    try {if(saved&&!$('#f-connection-id').value)throw Error('请先选择连接');const url=saved?'/api/connections/'+$('#f-connection-id').value+'/test':'/api/connections/test';const data=await api(url,saved?{}:dsn());target.textContent=data.ok?'✓ 连接成功':'✗ '+data.error;target.className='hint '+(data.ok?'ok':'err');}catch(e){target.textContent=e.message;target.className='hint err';}
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
  $('#f-sql-file').addEventListener('change',async e=>{try{const file=e.target.files[0],rev=revision;if(!file)return;const text=await file.text();if(e.target.files[0]!==file || rev!==revision)throw Error('选择或配置已变化，请重新选择文件。');state.fileText=text;state.fileName=file.name;renderMode();changed();}catch(e){importError(e);}});
  $('#btn-parse-import').addEventListener('click',parseImport);
  $('#btn-edit-file').addEventListener('click',()=>{const before=state.sql;state.sql=state.fileText;switchMode('paste');undo('文件原文已复制到文本导入区；当前表单未改动。',null,()=>{state.sql=before;});});
  $('#btn-example').addEventListener('click',async()=>{try{const rev=revision,response=await fetch('/static/examples/demo.sql');if(!response.ok)throw Error('示例加载失败');const text=await response.text();if(rev!==revision)throw Error('内容已变化，未覆盖编辑内容。');const oldText=state.sql;state.sql=text;state.mode='paste';render();changed();undo('示例已放入导入区，请检查后应用到表单。',null,()=>{state.sql=oldText;});await parseImport();}catch(e){fail(e);}});
  ['f-name','f-connection-id','f-host','f-port','f-user','f-database','f-concurrency','f-spawn-rate','f-duration'].forEach(id=>$('#'+id).addEventListener('input',changed));
  $('#run-form').addEventListener('submit',async e=>{
    e.preventDefault();if(busy)return;
    try {
      const source=payload(),rev=revision;busy=true;refresh();const checked=await api('/api/runs/preview',source);
      if(rev!==revision)throw Error('配置已变化，请检查后重新开始压测。');
      if(!checked.ok){showErrors(checked.errors);return;}
      const body={...source,name:$('#f-name').value,sql_source:'form',concurrency:+$('#f-concurrency').value,spawn_rate:+$('#f-spawn-rate').value,duration_sec:+$('#f-duration').value};
      if(connMode()==='saved'){if(!$('#f-connection-id').value)throw Error('请选择已保存连接');body.connection_id=$('#f-connection-id').value;}else body.db_dsn=dsn();
      busy=true;refresh();$('#submit-result').textContent='提交中…';save();const data=await api('/api/runs',body);window.location.href='/runs/'+data.run_id+'/monitor';
    }catch(e){fail(e);$('#submit-result').textContent='提交失败，请检查提示。';}finally{busy=false;refresh();}
  });
  load().catch(fail);
})();
