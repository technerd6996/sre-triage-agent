(function () {
    const host = document.getElementById('agent-status-pane');
    if (!host) return;
    const COLORS = { online: 'var(--green)', busy: 'var(--accent)', offline: 'var(--border-strong)' };
    host.style.cssText = 'border:1px solid var(--border);border-radius:10px;padding:14px 16px;background:var(--panel);font-size:14px;color:var(--text)';

    const el = (tag, css, text) => {
        const n = document.createElement(tag);
        if (css) n.style.cssText = css;
        if (text !== undefined) n.textContent = text;
        return n;
    };
    const fmt = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${s % 60}s` : `${s}s`);
    const row = (k, v) => {
        const r = el('div', 'display:flex;justify-content:space-between;gap:12px;margin-top:6px;color:var(--muted)');
        r.append(el('span', '', k), el('span', 'color:var(--text);text-align:right', v));
        return r;
    };

    // The running time comes from the server once a second, but is counted from this page's own clock so a slow
    // reply cannot make it jump (2s, 4s, 7s). The anchor is only reset when the run changes or drifts by 2 s+.
    let anchor = null, anchorKey = '', busy = false;
    const elapsed = (run) => {
        const key = run.ticket + '|' + run.status;
        const now = Date.now();
        if (anchor === null || key !== anchorKey || Math.abs((now - anchor) / 1000 - run.seconds) > 2) {
            anchor = now - run.seconds * 1000; anchorKey = key;
        }
        return Math.max(0, Math.floor((now - anchor) / 1000));
    };

    async function tick() {
        if (busy) return;                       // never stack requests if one is slow
        busy = true;
        try {
            const r = await fetch('/api/agent/status');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            const d = await r.json();
            if (!d.run) anchor = null;
            const label = { online: 'ONLINE', busy: 'BUSY', offline: 'OFFLINE' }[d.state] || d.state;
            const head = el('div', 'display:flex;align-items:center;gap:8px;font-weight:600');
            head.append(el('span', `width:10px;height:10px;border-radius:50%;background:${COLORS[d.state] || 'var(--border-strong)'}`),
                        el('span', '', `Agent status: ${label}`));
            const body = [head];
            if (d.state === 'busy' && d.run) {
                const waiting = d.run.status === 'awaiting_approval';
                body.push(el('div', 'margin-top:8px;color:var(--text)',
                    `${waiting ? 'Waiting for approval on' : 'Working on'} ${d.run.ticket} (${d.run.server}), ${fmt(elapsed(d.run))}`));
            } else if (d.state === 'offline') {
                body.push(el('div', 'margin-top:8px;color:var(--muted)', 'The model server is not reachable.'));
            } else {
                body.push(el('div', 'margin-top:8px;color:var(--muted)', 'Idle and ready for the next ticket.'));
            }
            body.push(row('Auto-work the queue', d.auto ? 'on' : 'off'));
            body.push(row('Tickets from chat', d.ticket_mode));
            body.push(row('Autonomous restore', d.autonomy));
            body.push(row('Waiting in L1 queue', d.queue === null ? 'unknown' : (d.queue_capped ? '10+' : String(d.queue))));
            body.push(row('Pending approvals', String(d.pending_approvals)));
            if (d.last_run) body.push(row('Last run', `${d.last_run.ticket}: ${d.last_run.outcome || 'finished'}`));
            host.replaceChildren(...body);
        } catch (e) {
            host.textContent = 'Agent status unavailable';
        } finally {
            busy = false;
        }
    }
    tick();
    setInterval(tick, 1000);
})();