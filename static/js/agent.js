const $ = (id) => document.getElementById(id);

// All dynamic text goes through textContent: ticket text and tool output are untrusted.
function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
}

async function api(path, opts) {
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || `HTTP ${r.status}`);
    return data;
}

const post = (path, body) => api(path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

let runId = null, nextIdx = 0, timer = null, curStatus = null;

const STATUS = {
    running: ['Working…', 'text-[#FFB000] border-[#FFB000]'],
    awaiting_approval: ['Waiting for approval', 'text-[#FF8A00] border-[#FF8A00]'],
    done: ['Finished', 'text-[#3DDC84] border-[#3DDC84]'],
    error: ['Error', 'text-red-400 border-red-400'],
};

// ---------- queue and run lists ----------
async function loadQueue() {
    const box = $('queue');
    try {
        const rows = await api('/api/agent/queue');
        box.replaceChildren();
        if (!rows.length) box.append(el('p', 'text-[13px] text-gray-500', 'Queue is empty.'));
        rows.forEach((t) => {
            const row = el('div', 'flex items-center gap-3 p-3 rounded-lg bg-[#12161C] border border-[#232B33]');
            const txt = el('div', 'min-w-0 flex-1');
            txt.append(el('div', 'mono text-[13px] text-[#FFB000]', t.number),
                       el('div', 'text-[13px] text-gray-300 truncate', t.short_description));
            const btn = el('button', 'px-3 py-1.5 rounded-md bg-[#0084FF] hover:bg-[#0073E6] text-[13px] font-semibold', 'Work');
            btn.onclick = () => startRun(t.number);
            row.append(txt, btn);
            box.append(row);
        });
    } catch (e) {
        box.replaceChildren(el('p', 'text-[13px] text-red-400', e.message));
    }
}

async function loadRuns() {
    try {
        const runs = await api('/api/agent/runs');
        const box = $('runs');
        box.replaceChildren();
        if (!runs.length) box.append(el('p', 'text-[13px] text-gray-500', 'No runs yet.'));
        runs.forEach((r) => {
            const border = r.id === runId ? 'border-[#FFB000]' : 'border-[#232B33]';
            const row = el('button', `text-left p-3 rounded-lg bg-[#12161C] border ${border} hover:border-[#FFB000]`);
            row.append(el('div', 'mono text-[13px] text-[#FFB000]', r.ticket),
                       el('div', 'text-[12px] text-gray-400',
                          `${(STATUS[r.status] || [r.status])[0]}${r.server ? ' · ' + r.server : ''}`));
            row.onclick = () => attach(r.id);
            box.append(row);
        });
        // follow a run that auto mode started, once we are not busy watching another one
        const active = runs.find((r) => r.status === 'running' || r.status === 'awaiting_approval');
        const idle = !runId || curStatus === 'done' || curStatus === 'error';
        if (active && idle && active.id !== runId) attach(active.id);
    } catch (e) { /* the next refresh retries */ }
}

async function loadPending() {
    try {
        const rows = await api('/api/agent/approvals');
        $('pending-count').textContent = `(${rows.length})`;
        const box = $('pending');
        box.replaceChildren();
        if (!rows.length) box.append(el('p', 'text-[13px] text-gray-500', 'Nothing waiting.'));
        rows.forEach((p) => {
            const mins = Math.max(0, Math.round((Date.now() / 1000 - p.requested_at) / 60));
            const row = el('button', 'text-left p-3 rounded-lg bg-[#1A130A] border border-[#FF8A00] hover:bg-[#241A0D]');
            row.append(el('div', 'mono text-[13px] text-[#FF8A00]', p.ticket + (p.server ? ' \u00b7 ' + p.server : '')));
            p.actions.forEach((a) => row.append(el('div', 'text-[12px] text-gray-300', `${a.text} (${a.risk})`)));
            row.append(el('div', 'text-[11px] text-gray-500 mt-1', `waiting ${mins} min`));
            row.onclick = () => attach(p.run_id);
            box.append(row);
        });
    } catch (e) { /* the next refresh retries */ }
}

// ---------- one run ----------
async function startRun(ticket) {
    try {
        const r = await post('/api/agent/run', { ticket });
        attach(r.run_id);
    } catch (e) { alert(e.message); }
}

function attach(id) {
    clearTimeout(timer);
    runId = id; nextIdx = 0; curStatus = null;
    $('steps').replaceChildren();
    showApproval(null);
    poll();
}

async function poll() {
    if (!runId) return;
    try {
        const d = await api(`/api/agent/runs/${runId}?after=${nextIdx}`);
        nextIdx = d.next;
        render(d);
        if (d.status === 'running' || d.status === 'awaiting_approval') timer = setTimeout(poll, 1500);
        else { loadQueue(); loadRuns(); }
    } catch (e) { timer = setTimeout(poll, 3000); }
}

function chip(text) {
    return el('span', 'mono text-[12px] px-2.5 py-1 rounded-md bg-[#171C24] border border-[#2E3744] text-gray-300', text);
}

function render(d) {
    $('run-empty').classList.add('hidden');
    $('run-view').classList.remove('hidden');
    $('run-title').textContent = d.ticket;
    const chips = $('run-chips');
    chips.replaceChildren();
    if (d.server) chips.append(chip(`server: ${d.server}`));
    if (d.skill) chips.append(chip(`skill: ${d.skill}`));
    curStatus = d.status;
    const [label, cls] = STATUS[d.status] || [d.status, 'text-gray-400 border-gray-600'];
    const pill = $('run-status');
    pill.textContent = label;
    pill.className = `mono text-[12px] px-3 py-1 rounded-full border ${cls}`;
    d.events.forEach(addEvent);
    showApproval(d.status === 'awaiting_approval' ? d.pending : null);
    showP1(d.p1);
    const last = $('steps').lastElementChild;
    if (last) last.scrollIntoView({ block: 'nearest' });
}

function stepRow(icon, iconCls, title, detail) {
    const row = el('div', 'flex gap-3 p-3 rounded-lg bg-[#12161C] border border-[#232B33]');
    row.append(el('span', `mono text-[13px] mt-0.5 ${iconCls}`, icon));
    const body = el('div', 'min-w-0 flex-1');
    body.append(el('div', 'mono text-[13px] text-[#E8ECF1]', title));
    if (detail) body.append(el('pre', 'mono text-[12px] text-gray-400 whitespace-pre-wrap break-all max-h-28 overflow-y-auto mt-1', detail));
    row.append(body);
    return row;
}

function addEvent(ev) {
    let row = null;
    if (ev.type === 'tool_call') row = stepRow('▶', 'text-[#0084FF]', ev.name, JSON.stringify(ev.args));
    else if (ev.type === 'tool_result') row = stepRow('✓', 'text-[#3DDC84]', `${ev.name} result`, ev.content);
    else if (ev.type === 'decision') row = stepRow(ev.approve ? '✔' : '✖', ev.approve ? 'text-[#3DDC84]' : 'text-red-400', ev.approve ? 'Engineer approved' : 'Engineer rejected');
    else if (ev.type === 'nudge') row = stepRow('↻', 'text-[#FFB000]', 'Nudged the agent to finish', ev.text);
    else if (ev.type === 'warning') row = stepRow('!', 'text-[#FFB000]', 'Warning', ev.message);
    else if (ev.type === 'error') row = stepRow('✖', 'text-red-400', 'Error', ev.message);
    else if (ev.type === 'final') {
        row = el('div', 'p-4 rounded-lg bg-[#0F1A14] border border-[#1F9D55]');
        row.append(el('div', 'mono text-[12px] uppercase tracking-[0.14em] text-[#3DDC84] mb-1', 'Summary'),
                   el('div', 'text-[14px] text-gray-200 whitespace-pre-wrap', ev.text));
    }
    if (row) $('steps').append(row);
}

// ---------- approval ----------
function showApproval(actions) {
    const box = $('approval');
    if (!actions) { box.classList.add('hidden'); return; }
    if (!box.classList.contains('hidden')) return;  // already showing, do not rebuild on every poll
    const list = $('approval-actions');
    list.replaceChildren();
    actions.forEach((a) => {
        const row = el('div', 'p-3 rounded-lg bg-[#0A0D10] border border-[#3A2A14]');
        row.append(el('div', 'mono text-[13px] text-[#FF8A00]', `${a.name}  (${a.risk})`),
                   el('pre', 'mono text-[12px] text-gray-300 whitespace-pre-wrap break-all mt-1', JSON.stringify(a.args)));
        list.append(row);
    });
    $('approve').disabled = $('reject').disabled = false;
    box.classList.remove('hidden');
}

async function decide(approve) {
    const feedback = $('comment').value.trim();
    if (!approve && !feedback) { $('comment').focus(); alert('Add a comment explaining the rejection.'); return; }
    $('approve').disabled = $('reject').disabled = true;
    try {
        await post(`/api/agent/runs/${runId}/decision`, { approve, feedback });
        $('approval').classList.add('hidden');
        $('comment').value = '';
        clearTimeout(timer);
        poll();
        loadPending();
    } catch (e) {
        alert(e.message);
        $('approve').disabled = $('reject').disabled = false;
    }
}
$('approve').onclick = () => decide(true);
$('reject').onclick = () => decide(false);

// ---------- P1 declaration ----------
function showP1(p) {
    const box = $('p1box');
    if (!p || p.status === 'dismissed' || p.status === 'declared') { box.classList.add('hidden'); return; }
    $('p1-reason').textContent = 'Reason: ' + p.reason;
    $('p1-to').textContent = 'To: ' + (p.to.join(', ') || 'NOT CONFIGURED') + (p.cc.length ? ' | Cc: ' + p.cc.join(', ') : '') + ' | Bridge: ' + (p.bridge || 'NOT CONFIGURED');
    $('p1-err').textContent = p.notice_error ? 'Notice not sent: ' + p.notice_error : '';
    if (box.classList.contains('hidden')) $('p1-body').value = p.draft;
    $('p1-go').textContent = p.status === 'proposed' ? 'Declare P1 and send notice' : 'Retry sending notice';
    box.classList.remove('hidden');
}

async function p1Decide(approve) {
    try {
        const p = await post(`/api/agent/runs/${runId}/p1`, { approve, body: $('p1-body').value });
        showP1(p);
        if (approve && p.status === 'declared') alert('P1 declared and notice sent.');
        loadPending();
    } catch (e) { alert(e.message); }
}
$('p1-go').onclick = () => p1Decide(true);
$('p1-no').onclick = () => p1Decide(false);

// ---------- auto mode and startup ----------
$('auto').onchange = async (e) => {
    try { await post('/api/agent/auto', { enabled: e.target.checked }); }
    catch (err) { e.target.checked = !e.target.checked; alert(err.message); }
};
$('refresh').onclick = loadQueue;

api('/api/auth/me').then((m) => {
    if (m.role === 'admin') api('/api/agent/auto').then((a) => { $('auto').checked = a.enabled; }).catch(() => {});
    else $('auto-wrap').style.display = 'none';
}).catch(() => {});
loadQueue();
loadRuns();
loadPending();
setInterval(loadQueue, 10000);
setInterval(loadRuns, 5000);
setInterval(loadPending, 5000);