const $ = (id) => document.getElementById(id);
let caps = [], selected = null;
let liveToken = 0;
let operatorIdentity = 'operator.demo';

async function jget(url){ const r = await fetch(url); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function jpost(url, body){
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await r.json();
  if(!r.ok) throw new Error(data.detail || 'request failed');
  return data;
}
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadHealth(){
  try{
    const h = await jget('/api/health');
    $('health').textContent = `${h.capabilities} capabilit${h.capabilities===1?'y':'ies'} · api up`;
    $('capCount').textContent = `${h.capabilities} active`;
    $('heroCapabilityCount').textContent = h.capabilities;
  }catch{ $('health').textContent = 'api unreachable'; }
}

async function loadCaps(){
  caps = await jget('/api/capabilities');
  if(!caps.length){ $('caps').innerHTML = '<div class="body hint">No capabilities found.</div>'; return; }
  const preferred = caps.findIndex(c => c.capability_id === 'meridian_core.check_member_balance');
  const initial = preferred >= 0 ? preferred : 0;
  $('caps').innerHTML = caps.map((c,i) => `
    <div class="cap${i===initial?' on':''}" data-i="${i}" role="button" tabindex="0"
         aria-selected="${i===initial?'true':'false'}"
         data-search="${esc((c.title+' '+c.capability_id).toLowerCase())}">
      <div class="cap-top">
        <span class="cap-index">${String(i+1).padStart(2,'0')}</span>
        <span class="risk risk-${esc(c.risk_level)}">${esc(c.risk_level)} risk</span>
      </div>
      <b>${esc(c.title)}</b>
      <span class="cap-meta">${esc(c.capability_id)} · ${c.step_count} steps · v${esc(c.version)}</span>
    </div>`).join('');
  document.querySelectorAll('.cap').forEach(el => {
    el.onclick = () => {
    document.querySelectorAll('.cap').forEach(x => { x.classList.remove('on'); x.setAttribute('aria-selected','false'); });
    el.classList.add('on');
    el.setAttribute('aria-selected','true');
    select(caps[+el.dataset.i]);
    };
    el.onkeydown = e => { if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); el.click(); } };
  });
  select(caps[initial]);
}

function filterCapabilities(){
  const query = $('capSearch').value.trim().toLowerCase();
  document.querySelectorAll('.cap').forEach(el => {
    el.hidden = query && !el.dataset.search.includes(query);
  });
}

function select(cap){
  selected = cap;
  $('invokeTitle').textContent = 'Invoke · ' + cap.capability_id;
  const fields = cap.inputs.map(i => {
    const req = i.required ? ' *' : '';
    if(i.enum){
      return `<div class="field"><label for="p_${i.name}">${esc(i.name)}${req}</label>
        <select id="p_${i.name}">${i.enum.map(o=>`<option${o===i.example?' selected':''}>${esc(o)}</option>`).join('')}</select></div>`;
    }
    if(['note','memo','notes','address'].includes(i.name)){
      return `<div class="field wide"><label for="p_${i.name}">${esc(i.name)}${req}</label>
        <textarea id="p_${i.name}">${esc(i.example || '')}</textarea></div>`;
    }
    const secret = i.sensitivity === 'secret';
    return `<div class="field"><label for="p_${i.name}">${esc(i.name)}${req}${i.pattern?` · <span class="mono">${esc(i.pattern)}</span>`:''}</label>
      <input id="p_${i.name}" type="${secret?'password':'text'}" value="${secret?'':esc(i.example || '')}"
             placeholder="${secret?'Required · never stored':''}"></div>`;
  }).join('');

  const tenantSel = cap.tenants.length
    ? `<div class="field wide"><label for="tenant">tenant</label><select id="tenant">
         <option value="">reference instance</option>
         ${cap.tenants.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join('')}
       </select></div>` : '';

  $('invoke').innerHTML = `
    <p class="section-lede">${esc(cap.description)}</p>
    <div class="capability-contract">
      <span class="contract-chip">${cap.step_count} deterministic steps</span>
      <span class="contract-chip">${cap.checkpoint_count} checkpoints</span>
      <span class="contract-chip">${cap.known_outcomes.length} declared outcomes</span>
      <span class="contract-chip">${cap.outputs.length} typed outputs</span>
    </div>
    <dl class="contract">
      <dt>returns</dt><dd>${cap.outputs.map(o=>esc(o.name)+':'+esc(o.type)).join(', ')||'—'}</dd>
    </dl>
    <div class="field-grid">${fields}${tenantSel}</div>
    ${cap.risk_level === 'high' ? `<label class="risk-confirm" style="text-transform:none;letter-spacing:0;font-size:12px">
      <input id="confirmRisk" type="checkbox">I confirm this live, state-changing operation
    </label>` : ''}
    <div class="form-actions"><button id="go">Run capability</button>
    <button id="curl" class="ghost">View API request</button></div>`;
  $('go').onclick = invoke;
  $('curl').onclick = showCurl;
}

function collect(){
  const params = {};
  selected.inputs.forEach(i => { const el = $('p_'+i.name); if(el && el.value !== '') params[i.name] = el.value; });
  const t = $('tenant');
  const confirm = $('confirmRisk');
  return { params, tenant: t && t.value ? t.value : null, confirm_risky: confirm ? confirm.checked : false };
}

function showCurl(){
  const body = collect();
  const safeBody = JSON.parse(JSON.stringify(body));
  selected.inputs.filter(i => i.sensitivity === 'secret').forEach(i => {
    if(safeBody.params[i.name] !== undefined) safeBody.params[i.name] = '<REQUIRED_SECRET>';
  });
  $('result').innerHTML = `<p class="hint">The same call, from anything that speaks HTTP:</p>
    <pre>curl -X POST http://127.0.0.1:8800/api/capabilities/${esc(selected.capability_id)}/invoke \\
  -H 'Content-Type: application/json' \\
  -d '${esc(JSON.stringify(safeBody))}'</pre>`;
}

async function invoke(){
  const btn = $('go'); btn.disabled = true; btn.textContent = 'Starting run…';
  $('result').innerHTML = '<p class="hint"><span class="spin"></span>Driving the browser…</p>';
  try{
    const started = await jpost(`/api/capabilities/${selected.capability_id}/start`, collect());
    await watchRun(started.run_id);
  }catch(e){
    $('result').innerHTML = `<p style="color:var(--bad)">${esc(e.message)}</p>`;
  }finally{
    btn.disabled = false; btn.textContent = 'Run capability'; loadRuns();
  }
}

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function watchRun(runId, chatMessageId=null){
  const token = ++liveToken;
  while(token === liveToken){
    const state = await jget('/api/live-runs/' + encodeURIComponent(runId));
    renderLive(state);
    if(state.status === 'complete' && state.result){
      renderResult(state.result);
      if(chatMessageId) $(chatMessageId).textContent = explainResult(state.result);
      loadRuns();
      return state.result;
    }
    if(state.status === 'failed'){
      const message = state.error || 'The run failed before producing a result.';
      $('result').innerHTML = `<p style="color:var(--bad)">${esc(message)}</p>`;
      if(chatMessageId) $(chatMessageId).innerHTML = `<span style="color:var(--bad)">${esc(message)}</span>`;
      loadRuns();
      return null;
    }
    await pause(400);
  }
  return null;
}

function renderLive(state){
  const incomingControlState = state.control && state.control.state;
  const existingOperatorPanel = $('operatorPanel');
  // Polling must not replace live controls while a human is interacting with
  // them. Redraw as soon as ownership changes so pause/resume/abort states are
  // still reflected immediately.
  if(existingOperatorPanel &&
      existingOperatorPanel.dataset.controlState === incomingControlState) return;
  const events = state.events || [];
  const starts = events.filter(e => e.event === 'step.start');
  const complete = events.filter(e => e.event === 'step.complete');
  const errors = events.filter(e => e.event === 'step.error');
  const total = state.total_steps || 0;
  const count = Math.min(state.completed_steps || 0, total || Number.MAX_SAFE_INTEGER);
  const percent = total ? Math.round(count * 100 / total) : 0;
  const terminal = state.status === 'complete' || state.status === 'failed';
  const rows = starts.map((step, index) => {
    const nextSeq = starts[index + 1] ? starts[index + 1].seq : Number.MAX_SAFE_INTEGER;
    const done = complete.find(e => e.step === step.step && e.seq > step.seq && e.seq < nextSeq);
    const failed = errors.find(e => e.step === step.step && e.seq > step.seq && e.seq < nextSeq);
    const status = failed ? 'failed' : done ? 'done' : terminal ? 'failed' : 'running';
    const detail = failed ? failed.code : done ? `${done.duration_ms}ms` : 'running';
    return `<div class="step ${status}">
      <span class="mono">${index+1}</span>
      <span><span class="intent">${esc(step.intent || step.step)}</span><br>
        <span class="meta">${esc(step.action)} · ${esc(step.step)}</span></span>
      <span class="mono">${esc(detail)}</span>
    </div>`;
  }).join('');
  const result = state.result;
  const control = state.control || null;
  const controlState = control && control.state;
  const waitingForHuman = controlState === 'PAUSED' || controlState === 'HUMAN_CONTROL';
  const activeEscalation = control && control.active_escalation;
  const escalated = waitingForHuman || (result && result.control && result.control.escalated);
  const escalationReason = activeEscalation
      ? `${activeEscalation.reason_code}: ${activeEscalation.reason}`
      : result && result.control ? result.control.escalation_reason : '';
  const displayStatus = waitingForHuman ? 'RECOVERABLE'
      : result ? result.category : state.status.toUpperCase();
  $('live').innerHTML = `
    <div class="live-head">
      ${(state.status === 'running' || state.status === 'queued') && !waitingForHuman ? '<span class="spin"></span>' : ''}
      <span class="tag t-${esc(displayStatus)}">${esc(displayStatus)}</span>
      <span class="mono">${esc(state.title || state.capability_id)} · ${esc(state.run_id)}</span>
    </div>
    <div class="progress"><span style="width:${percent}%"></span></div>
    <p class="hint">${count}/${total} steps complete${state.status==='queued'?' · waiting for the shared browser':''}</p>
    ${escalated ? `<div class="escalated"><b>ESCALATED</b> — ${esc(escalationReason || 'Automation paused for an operator.')}</div>` : ''}
    <div class="step-list">${rows || '<p class="hint">Preparing the browser…</p>'}</div>
    ${operatorPanel(state)}`;
}

function operatorPanel(state){
  const control = state.control;
  if(!control || !['PAUSED','HUMAN_CONTROL'].includes(control.state)) return '';
  const run = esc(state.run_id);
  const shot = `/api/live-runs/${encodeURIComponent(state.run_id)}/screenshot?t=${Date.now()}`;
  if(control.state === 'PAUSED'){
    return `<div class="operator-box" id="operatorPanel" data-control-state="PAUSED">
      <b>Human takeover available</b>
      <p class="hint">Automation released this exact browser session. Identify yourself and take control.</p>
      <img class="live-shot" src="${shot}" alt="Current paused browser session">
      <label for="operatorName">Operator identity</label>
      <input id="operatorName" value="${esc(operatorIdentity)}" maxlength="80">
      <button onclick="takeRunControl('${run}')">Take control</button>
      <span id="operatorStatus" class="hint"></span>
    </div>`;
  }
  const actions = ((control.active_escalation||{}).human_actions||[]);
  return `<div class="operator-box" id="operatorPanel" data-control-state="HUMAN_CONTROL">
    <b>Human owns the live session</b>
    <p class="hint">Actions below operate the same browser tab and are written to this run's evidence.</p>
    <img id="operatorShot" class="live-shot" src="${shot}" alt="Current browser session under human control">
    <div class="operator-grid">
      <div><label for="operatorAction">Action</label><select id="operatorAction">
        <option value="click">Click</option><option value="type">Type</option>
        <option value="select">Select option</option><option value="press">Press key</option>
        <option value="navigate">Navigate</option></select></div>
      <div><label for="operatorFrame">Frame</label><input id="operatorFrame" value="main"></div>
      <div class="wide"><label for="operatorTarget">Control</label>
        <input id="operatorTarget" placeholder="button:Sign On, visible text, or test id"></div>
      <div class="wide"><label for="operatorValue">Value</label>
        <input id="operatorValue" placeholder="typed text, option value, key, or allowed URL"></div>
    </div>
    <div class="operator-actions">
      <button onclick="actOnRun('${run}')">Perform action</button>
      <button class="ghost" onclick="resumeRun('${run}','RETRY_STEP')">Retry failed step</button>
      <button class="ghost" onclick="resumeRun('${run}','CONTINUE_FROM_CURRENT_STEP')">Continue after manual completion</button>
      <button class="ghost" onclick="resumeRun('${run}','ABORT')">Abort safely</button>
    </div>
    <p id="operatorStatus" class="hint">${actions.length} human action${actions.length===1?'':'s'} recorded.</p>
  </div>`;
}

async function takeRunControl(runId){
  const name = ($('operatorName').value || '').trim();
  if(!name){ $('operatorStatus').textContent = 'Enter an operator identity.'; return; }
  operatorIdentity = name;
  try{
    await jpost(`/api/live-runs/${encodeURIComponent(runId)}/take-control`, {operator:name});
    document.activeElement.blur();
  }catch(e){ $('operatorStatus').textContent = e.message; }
}

async function actOnRun(runId){
  const body = {
    action: $('operatorAction').value,
    target: $('operatorTarget').value,
    value: $('operatorValue').value,
    frame: $('operatorFrame').value || 'main'
  };
  $('operatorStatus').textContent = 'Performing action…';
  try{
    await jpost(`/api/live-runs/${encodeURIComponent(runId)}/operator-actions`, body);
    $('operatorStatus').textContent = 'Action recorded.';
    $('operatorShot').src = `/api/live-runs/${encodeURIComponent(runId)}/screenshot?t=${Date.now()}`;
  }catch(e){ $('operatorStatus').textContent = e.message; }
}

async function resumeRun(runId, decision){
  $('operatorStatus').textContent = 'Returning control to automation…';
  try{
    await jpost(`/api/live-runs/${encodeURIComponent(runId)}/resume`, {
      decision, operator:operatorIdentity
    });
    document.activeElement.blur();
  }catch(e){ $('operatorStatus').textContent = e.message; }
}

function explainResult(r){
  if(r.category === 'SUCCESS'){
    const values = Object.entries(r.outputs || {}).map(([k,v]) => `${k}=${v}`).join(', ');
    return values ? `Done. ${values}.` : 'Done. The capability completed successfully.';
  }
  if(r.category === 'BUSINESS_OUTCOME') return `Business outcome: ${(r.business_outcome||{}).description || r.code}`;
  if(r.category === 'RECOVERABLE') return `The run stopped in a recoverable state (${r.code}).`;
  return `The run failed: ${(r.error||{}).message || r.code}`;
}

function renderResult(r){
  const outputs = Object.entries(r.outputs || {});
  const drift = r.drift && r.drift.steps_resolved
      ? (r.drift.first_choice / r.drift.steps_resolved) : null;
  const degraded = (r.drift && r.drift.degraded) || [];
  const escalated = r.control && r.control.escalated;
  const steps = r.steps || [];
  $('result').innerHTML = `
    <div class="result-head">
      <span class="tag t-${esc(r.category)}">${esc(r.category)}</span>
      <span class="mono" style="color:var(--dim)">${esc(r.code)}</span>
      <span class="mono llm">llm_calls=${r.llm_calls}</span>
    </div>
    ${outputs.length ? `<div class="output-grid">${outputs.map(([k,v])=>
        `<div class="output-card"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}</div>` : ''}
    ${r.business_outcome ? `<p class="hint">${esc(r.business_outcome.description)}</p>` : ''}
    ${r.error ? `<p style="color:var(--bad)">${esc(r.error.message)}</p>` : ''}
    ${escalated ? `<div class="escalated"><b>ESCALATED</b> — ${esc(r.control.escalation_reason || 'Automation paused for an operator.')}</div>` : ''}
    ${drift !== null ? `<p class="hint" style="margin-top:10px">
        drift ${drift.toFixed(2)} — ${r.drift.first_choice}/${r.drift.steps_resolved}
        controls found by their preferred locator
        ${degraded.length ? '<br>' + degraded.map(d =>
          `<span class="mono">${esc(d.step_id)} → candidate ${d.candidate_index} (${esc(d.resolved_via)})</span>`
        ).join('<br>') : ''}</p>` : ''}
    <p class="hint" style="margin-top:10px">${steps.length} steps · ${r.duration_ms}ms · run ${esc(r.run_id)}</p>
    ${steps.length ? `<details><summary class="hint">Completed step timings</summary>
      <pre>${esc(JSON.stringify(steps, null, 2))}</pre></details>` : ''}`;
}

async function loadRuns(){
  try{
    const runs = await jget('/api/runs?limit=12');
    $('runs').innerHTML = runs.length ? runs.map(r => `
      <tr class="run" data-run="${esc(r.run_id)}" title="Open run evidence">
        <td class="mono">${esc(r.run_id.slice(0,26))}<br><span class="hint">${esc(r.kind)}</span></td>
        <td><span class="tag t-${esc(r.category)}">${esc(r.category)}</span>
            <span class="mono" style="color:var(--dim)"> ${esc(r.code)}</span></td>
        <td class="mono">${esc(Object.entries(r.outputs||{}).map(([k,v])=>k+'='+v).join(', ')) || '—'}</td>
        <td class="mono">${r.duration_ms===null||r.duration_ms===undefined?'—':r.duration_ms+'ms'}</td>
        <td class="mono">${r.drift_score===null||r.drift_score===undefined?'—':r.drift_score.toFixed(2)}</td>
        <td>${r.escalated?'<span class="tag t-RECOVERABLE">ESCALATED</span>':'—'}</td>
        <td class="mono">${r.llm_calls ?? '—'}</td>
      </tr>`).join('')
      : '<tr><td colspan="7" style="padding:14px" class="hint">No runs yet.</td></tr>';
    document.querySelectorAll('tr.run').forEach(row => row.onclick = () => loadRun(row.dataset.run));
  }catch{ /* leave as-is */ }
}

async function loadRun(runId){
  $('runDetail').innerHTML = '<p class="hint"><span class="spin"></span>Loading evidence…</p>';
  try{
    const d = await jget('/api/runs/' + encodeURIComponent(runId));
    const start = (d.log || []).find(e => e.event === 'replay.start');
    const params = start ? start.params : ((d.trace || {}).params || {});
    const result = d.result || {};
    const outputs = result.outputs || {};
    const evidence = (d.evidence || []).map(path => {
      const encoded = path.split('/').map(encodeURIComponent).join('/');
      return `<a class="evidence-file" href="/api/runs/${encodeURIComponent(runId)}/evidence/${encoded}" target="_blank" rel="noopener">${esc(path)}</a>`;
    }).join('');
    const tail = (d.log || []).slice(-16);
    $('runDetail').innerHTML = `
      <div class="output-grid">
        <div class="output-card"><span>Run</span><strong>${esc(runId)}</strong></div>
        <div class="output-card"><span>Status</span><strong>${esc(result.category || (d.trace||{}).status || '—')} / ${esc(result.code || '—')}</strong></div>
        <div class="output-card"><span>Duration</span><strong>${result.duration_ms===undefined?'—':esc(result.duration_ms)+'ms'}</strong></div>
        <div class="output-card"><span>Escalation</span><strong>${result.control && result.control.escalated ? 'YES · '+esc(result.control.escalation_reason || '') : 'No'}</strong></div>
      </div>
      <dl class="contract">
        <dt>inputs</dt><dd>${esc(JSON.stringify(params))}</dd>
        <dt>outputs</dt><dd>${esc(JSON.stringify(outputs))}</dd>
        <dt>steps</dt><dd>${esc(JSON.stringify(result.steps || []))}</dd>
      </dl>
      <label>Evidence files</label><div class="evidence-links">${evidence || '<span class="hint">No evidence files.</span>'}</div>
      <label>Event log · final ${tail.length}</label>
      <pre>${esc(JSON.stringify(tail, null, 2))}</pre>`;
  }catch(e){ $('runDetail').innerHTML = `<p style="color:var(--bad)">${esc(e.message)}</p>`; }
}

async function sendChat(){
  const input = $('chatIn'); const text = input.value.trim();
  if(!text) return;
  const button = $('chatGo'); button.disabled = true;
  input.value = '';
  const empty = $('chatLog').querySelector('.chat-empty');
  if(empty) empty.remove();
  $('chatLog').insertAdjacentHTML('beforeend', `<div class="msg you">${esc(text)}</div>`);
  const id = 'm'+Date.now();
  $('chatLog').insertAdjacentHTML('beforeend',
    `<div class="msg bot" id="${id}"><span class="spin"></span>working…</div>`);
  $('chatLog').scrollTop = $('chatLog').scrollHeight;
  try{
    const r = await jpost('/api/chat/start', {message: text});
    $(id).innerHTML = esc(r.reply);
    if(r.run_id) await watchRun(r.run_id, id);
  }catch(e){
    $(id).innerHTML = `<span style="color:var(--bad)">${esc(e.message)}</span>`;
  }finally{
    button.disabled = false;
    $('chatLog').scrollTop = $('chatLog').scrollHeight;
  }
}
$('chatGo').onclick = sendChat;
$('chatIn').addEventListener('keydown', e => { if(e.key==='Enter') sendChat(); });
$('capSearch').addEventListener('input', filterCapabilities);

function setChatOpen(open){
  document.body.classList.toggle('chat-open', open);
  $('chatLauncher').setAttribute('aria-expanded', String(open));
  $('chatDrawer').setAttribute('aria-hidden', String(!open));
  $('chatBackdrop').setAttribute('aria-hidden', String(!open));
  if(open){
    window.setTimeout(() => $('chatIn').focus(), 220);
  }else{
    $('chatLauncher').focus();
  }
}
$('chatLauncher').onclick = () => setChatOpen(true);
$('chatClose').onclick = () => setChatOpen(false);
$('chatBackdrop').onclick = () => setChatOpen(false);
document.addEventListener('keydown', e => {
  if(e.key === 'Escape' && document.body.classList.contains('chat-open')) setChatOpen(false);
});
document.querySelectorAll('[data-example]').forEach(button => {
  button.onclick = () => { $('chatIn').value = button.dataset.example; $('chatIn').focus(); };
});

loadHealth(); loadCaps(); loadRuns();
setInterval(loadRuns, 8000);
