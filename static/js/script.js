const chatContainer = document.getElementById('chat-container');
const messagesWrapper = document.getElementById('messages-wrapper');
const emptyState = document.getElementById('empty-state');
const input = document.getElementById('prompt-input');
const sendBtn = document.getElementById('send-btn');
const fileInput = document.getElementById('chat-file');
const turnCount = document.getElementById('turn-count');
const chatListContainer = document.getElementById('chat-list');
const slider = document.getElementById('context-slider');
const WRAP_Q = 'Is there anything else we can help you with?';
const DEFAULT_PLACEHOLDER = input ? input.placeholder : '';
const watchers = {};   // runId -> poll timer
const FIXED_RE = /\b(fixed|resolved|solved|sorted)\b|\bworking\s+(now|again|fine)\b|\b(it'?s|is)\s+working\b/i;

// --- Security helpers ---
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function renderMarkdown(text) {
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
        console.error('Markdown libraries missing.');
        return `<div style="white-space: pre-wrap">${esc(text)}</div>`;
    }
    return DOMPurify.sanitize(marked.parse(text));
}

// --- State Management ---
const STORE_KEY = 'triage1_chats_v1';

function saveState() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify({ chats, activeChatId })); } catch (e) {}
}

function loadState() {
    try {
        const s = JSON.parse(localStorage.getItem(STORE_KEY));
        if (s && s.chats && Object.keys(s.chats).length) {
            for (const c of Object.values(s.chats)) {
                if (c.title === "Generating title...") c.title = "Incident Triage";
                // Flag older off-topic turns so they stay out of the model context and the ticket transcript
                (c.history || []).forEach((m, i, arr) => {
                    if (m.role === 'assistant' && /^\s*I['\u2019]?m a triage assistant/i.test(m.content)) {
                        m.skip = true;
                        if (arr[i - 1]) arr[i - 1].skip = true;
                    }
                });
            }
            return s;
        }
    } catch (e) {}
    return null;
}

const saved = loadState();
let chats = saved ? saved.chats : { "chat_1": { title: "Default Chat", history: [] } };
let activeChatId = (saved && saved.chats[saved.activeChatId]) ? saved.activeChatId : Object.keys(chats)[0];

// --- Menu State Management ---
let openMenuId = null;

document.addEventListener('click', (e) => {
    if (openMenuId && !e.target.closest('.chat-menu-container')) {
        const menu = document.getElementById(`menu-${openMenuId}`);
        if (menu) menu.classList.add('hidden');
        openMenuId = null;
    }
});

function toggleMenu(id, event) {
    event.stopPropagation();
    if (openMenuId && openMenuId !== id) {
        const oldMenu = document.getElementById(`menu-${openMenuId}`);
        if (oldMenu) oldMenu.classList.add('hidden');
    }
    const menu = document.getElementById(`menu-${id}`);
    if (menu) {
        menu.classList.toggle('hidden');
        openMenuId = menu.classList.contains('hidden') ? null : id;
    }
}

let renamingId = null;
let deletingId = null;

function renameChat(id, event) {
    event.stopPropagation();
    openMenuId = null;
    renamingId = id;
    renderChatList();
}

function deleteChat(id, event) {
    event.stopPropagation();
    openMenuId = null;
    if (Object.keys(chats).length <= 1) return;
    deletingId = id;
    renderChatList();
}

function removeChat(id) {
    delete chats[id];
    if (activeChatId === id) {
        activeChatId = Object.keys(chats)[0];
        switchChat(activeChatId);
    } else {
        renderChatList();
    }
}

function renderChatList() {
    if (!chatListContainer) return;
    chatListContainer.innerHTML = '';

    for (const [id, chat] of Object.entries(chats)) {
        const isActive = id === activeChatId;

        const div = document.createElement('div');
        div.className = `group relative flex items-center justify-between w-full px-2 py-1.5 rounded transition-colors ${isActive ? 'bg-amber-500/10' : 'hover:bg-white/5'} chat-menu-container mb-1`;

        if (renamingId === id) {
            const inp = document.createElement('input');
            inp.value = chat.title;
            inp.maxLength = 60;
            inp.className = 'w-full bg-[#0A0D10] border border-[#FFB000]/60 rounded px-2 py-1 text-[14px] text-gray-100 outline-none';
            let done = false;
            const finish = (save) => {
                if (done) return;
                done = true;
                const v = inp.value.trim();
                if (save && v) chat.title = v;
                renamingId = null;
                renderChatList();
            };
            inp.onkeydown = (e) => {
                if (e.key === 'Enter') finish(true);
                else if (e.key === 'Escape') finish(false);
            };
            inp.onblur = () => finish(true);
            div.appendChild(inp);
            chatListContainer.appendChild(div);
            setTimeout(() => { inp.focus(); inp.select(); });
            continue;
        }

        if (deletingId === id) {
            div.innerHTML = `<span class="text-[13px] text-red-400 truncate">Delete “${esc(chat.title)}”?</span>
                <span class="flex gap-1 shrink-0 ml-2">
                    <button data-yes class="px-2 py-0.5 text-[12px] rounded bg-red-500/20 text-red-300 hover:bg-red-500/30">Yes</button>
                    <button data-no class="px-2 py-0.5 text-[12px] rounded bg-[#232B33] text-gray-300 hover:bg-[#2E3744]">No</button>
                </span>`;
            div.querySelector('[data-yes]').onclick = () => { deletingId = null; removeChat(id); };
            div.querySelector('[data-no]').onclick = () => { deletingId = null; renderChatList(); };
            chatListContainer.appendChild(div);
            continue;
        }

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `flex items-center gap-3 flex-1 text-left truncate text-[14px] ${isActive ? 'text-amber-500 font-medium' : 'text-gray-400 hover:text-gray-200'}`;
        btn.title = chat.title;
        const dot = chat.closed ? '🔒' : (isActive ? '●' : '○');
        btn.innerHTML = `<span class="text-[10px] shrink-0">${dot}</span> <span class="truncate">${esc(chat.title)}</span>${chat.closed ? '<span class="text-[10px] mono text-gray-500 shrink-0 ml-1">closed</span>' : ''}`;
        btn.onclick = () => switchChat(id);

        const dotsBtn = document.createElement('button');
        dotsBtn.type = 'button';
        dotsBtn.className = `p-1.5 text-gray-500 hover:text-white rounded opacity-0 group-hover:opacity-100 transition-opacity focus:outline-none focus:opacity-100 ${isActive ? 'opacity-100' : ''}`;
        dotsBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"></circle><circle cx="12" cy="5" r="1"></circle><circle cx="12" cy="19" r="1"></circle></svg>`;
        dotsBtn.onclick = (e) => toggleMenu(id, e);

        const menu = document.createElement('div');
        menu.id = `menu-${id}`;
        menu.className = `hidden absolute right-4 top-8 w-32 bg-[#171C24] border border-[#2E3744] rounded-md shadow-2xl z-50 flex flex-col py-1 overflow-hidden`;

        const renameBtn = document.createElement('button');
        renameBtn.type = 'button';
        renameBtn.className = 'text-left px-3 py-2 text-[12.5px] text-gray-300 hover:bg-[#232B33] hover:text-white flex items-center gap-2 transition-colors';
        renameBtn.innerHTML = '✏️ Rename';
        renameBtn.onclick = (e) => renameChat(id, e);
        menu.appendChild(renameBtn);

        if (!chat.closed) {
            const closeBtn = document.createElement('button');
            closeBtn.type = 'button';
            closeBtn.className = 'text-left px-3 py-2 text-[12.5px] text-gray-300 hover:bg-[#232B33] hover:text-white flex items-center gap-2 transition-colors border-t border-[#232B33]/50';
            closeBtn.innerHTML = '🔒 Close chat';
            closeBtn.onclick = (e) => { e.stopPropagation(); openMenuId = null; closeChat(id, true); };
            menu.appendChild(closeBtn);
        }

        if (Object.keys(chats).length > 1) {
            const delBtn = document.createElement('button');
            delBtn.type = 'button';
            delBtn.className = 'text-left px-3 py-2 text-[12.5px] text-red-400 hover:bg-[#232B33] hover:text-red-300 flex items-center gap-2 transition-colors border-t border-[#232B33]/50';
            delBtn.innerHTML = '🗑️ Delete';
            delBtn.onclick = (e) => deleteChat(id, e);
            menu.appendChild(delBtn);
        }

        div.appendChild(btn);
        div.appendChild(dotsBtn);
        div.appendChild(menu);
        chatListContainer.appendChild(div);
    }
    saveState();
}

function createNewChat() {
    const newId = 'chat_' + Date.now();
    chats[newId] = { title: "New Triage", history: [] };
    switchChat(newId);
}

function switchChat(id) {
    activeChatId = id;
    renderChatList();
    if (messagesWrapper) messagesWrapper.innerHTML = '';

    const chat = chats[activeChatId];
    const hist = chat.history || [];
    const notes = chat.notes || [];

    if (!hist.length && !notes.length) {
        if (emptyState) emptyState.style.display = 'flex';
        if (messagesWrapper) {
            messagesWrapper.classList.add('hidden');
            messagesWrapper.classList.remove('flex');
        }
    } else {
        if (emptyState) emptyState.style.display = 'none';
        if (messagesWrapper) {
            messagesWrapper.classList.remove('hidden');
            messagesWrapper.classList.add('flex');
        }
        const metaStore = chat.meta || {};
        const showNotes = (at) => notes.filter((n) => n.at === at).forEach((n) => appendDOM('assistant', n.text));
        hist.forEach((msg, i) => {
            try {
                showNotes(i);
                appendDOM(msg.role, msg.content, msg.role === 'assistant' ? metaStore[i] : undefined);
            } catch (e) {
                console.error('Could not render message', i, e);   // one bad message must not blank the chat
            }
        });
        showNotes(hist.length);

        if (chat.closed) renderClosedFooter();
        else if (chat.wrap === 'asking') renderWrapUp(id);
        else if (chat.run) resumeRun(id);
    }
    if (turnCount) turnCount.innerText = (hist.length / 2).toString();
    applyComposerState();
}

const RELATED_RE = /^[ \t]*(?:[-*•][ \t]+)?(?:\*\*)?Related(?:\*\*)?:(?:\*\*)?[ \t]*(.*)$/gim;

function normalizeRelated(content) {
    const matches = [...content.matchAll(RELATED_RE)];
    if (matches.length === 0) return content;
    const value = matches[matches.length - 1][1].trim();
    const stripped = content.replace(RELATED_RE, '').replace(/\n{3,}/g, '\n\n').trim();
    return value ? `${stripped}\n\nRelated: ${value}` : stripped;
}

function sourceBadgeHTML(sources, answer) {
    if (/^\s*I['\u2019]?m a triage assistant/i.test(answer)) return '';
    const retrieved = Array.isArray(sources) ? sources : ((sources && sources.sources) || []);
    const body = answer.replace(/^\s*Related:.*$/gim, '');
    const mentioned = [...new Set(body.match(/\b(?:RB|SOP)-\d{3}\b|\bINC-\d{4}-\d{3}\b/g) || [])];
    const phantom = mentioned.filter(id => !retrieved.includes(id));
    const phantomHTML = phantom.length
        ? ` <span class="src-badge src-gen">⚠ Mentions ${phantom.map(esc).join(', ')}, which was not retrieved. May be inaccurate.</span>`
        : '';
    const fromGraph = /Source:\s*graph\b/i.test(answer);

    const cited = retrieved.filter(id =>
        new RegExp(`(?<![\\w-])${id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\w-])`).test(answer));
    if (cited.length) {
        return `<span class="src-badge src-kb">📚 From your knowledge base: ${cited.map(esc).join(', ')}</span>${phantomHTML}`;
    }
    if (fromGraph) {
        return `<span class="src-badge src-kb">🕸 From your knowledge graph</span>${phantomHTML}`;
    }
    if (retrieved.length) {
        return `<span class="src-badge src-gen">⚠ Retrieved ${retrieved.map(esc).join(', ')} but the answer doesn't cite them. Verify before acting.</span>`;
    }
    return `<span class="src-badge src-gen">⚠ Not from your knowledge base. Verify against your runbooks before acting.</span>`;
}

function formatSeverity(content) {
    content = normalizeRelated(content);
    content = content.replace(/\s*\bSource:\s*[^\n]*$/i, '').trim();  // shown as the source badge instead
    const match = content.match(/SEVERITY:\s*(INFO|WARN|CRITICAL)/i);
    let html;
    if (match) {
        const sev = match[1].toUpperCase();
        const cleanContent = content.replace(match[0], '').trim();
        const marker = `<div class="sev-card sev-${sev}"><span class="w-2 h-2 rounded-full bg-current"></span>SEVERITY — ${sev}</div>`;
        html = marker + renderMarkdown(cleanContent);
    } else {
        html = renderMarkdown(content);
    }
    return `<div class="kb-answer">${html}</div>`;
}

const ATTACHMENT_RE = /^\[Attached file: ([^\]]+)\]\n\n([\s\S]*)$/;

function renderUserContent(content) {
    const m = content.match(ATTACHMENT_RE);
    if (!m) return `<div style="white-space: pre-wrap">${esc(content)}</div>`;
    const [, name, text] = m;
    return `<strong>[Attached file: ${esc(name)}]</strong>
        <details class="mt-3 mb-2 bg-[#12161C] border border-[#2E3744] rounded-lg overflow-hidden cursor-pointer shadow-sm transition-colors hover:border-[#4B5563]">
            <summary class="p-3 text-[13px] text-gray-300 font-medium flex items-center gap-2 outline-none">
                <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-gray-500"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>
                Preview File Contents
            </summary>
            <div class="p-4 bg-[#0A0D10] text-[12.5px] text-gray-400 font-mono whitespace-pre-wrap border-t border-[#2E3744] max-h-72 overflow-y-auto cursor-text">${esc(text)}</div>
        </details>`;
}

function appendDOM(role, content, info) {
    if (emptyState) emptyState.style.display = 'none';
    if (messagesWrapper) {
        messagesWrapper.classList.remove('hidden');
        messagesWrapper.classList.add('flex');
    }

    const div = document.createElement('div');
    div.className = `mb-8 ${role === 'user' ? 'msg-user text-gray-300' : 'msg-ai text-white'}`;

    const isUser = role === 'user';
    const userIcon = `<div class="w-6 h-6 rounded-full bg-[#171C24] border border-[#2E3744] flex items-center justify-center shrink-0 shadow-sm"><svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#A7B0BC" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg></div>`;
    const botIcon = `<div class="w-6 h-6 bg-[#0084FF] rounded-full rounded-bl-sm flex items-center justify-center shrink-0 shadow-sm"><svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="#FFFFFF"><path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h2a3 3 0 0 1 3 3v2h1a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2h-1v1a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-1H4a2 2 0 0 1-2-2v-2a2 2 0 0 1 2-2h1V10a3 3 0 0 1 3-3h2V5.73A2 2 0 0 1 12 2zm-3 10a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zm6 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z"/></svg></div>`;

    div.innerHTML = `
        <div class="flex items-center gap-2.5 mb-2">
            ${isUser ? userIcon : botIcon}
            <div class="mono text-[11px] uppercase tracking-wider ${isUser ? 'text-gray-500' : 'text-[#0084FF]'} font-semibold mt-0.5">${isUser ? 'You' : 'TRIAGE-1'}</div>
        </div>
        <div class="content text-[15px] leading-relaxed">${isUser ? renderUserContent(content) : formatSeverity(content)}</div>
        <div class="source-line"></div>
    `;

    if (!isUser && info) {
        const isArr = Array.isArray(info);                       // old saves stored a bare array of sources
        const srcs = isArr ? info : (info.sources || []);
        div.querySelector('.source-line').innerHTML = (!isArr && info.kind === 'canned') ? '' : sourceBadgeHTML(srcs, content);
        const inc = isArr ? null : info.incident;
        if (inc && ['can_report', 'needs_server'].includes(inc.status) && !chats[activeChatId]?.closed) {
            renderCreateIncidentAction(div.querySelector('.content'), inc.description || '', inc.suggested_server, inc.mode);
        }
    }

    if (messagesWrapper) messagesWrapper.appendChild(div);
    if (chatContainer) chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    return div.querySelector('.content');
}

const THINKING_STEPS = ['Thinking…', 'Searching runbooks…', 'Checking the knowledge graph…', 'Putting the answer together…'];

function startThinking(node) {
    node.innerHTML = `<div class="thinking"><span class="thinking-dots"><i></i><i></i><i></i></span><span class="thinking-label shimmer">${THINKING_STEPS[0]}</span></div>`;
    const label = node.querySelector('.thinking-label');
    let i = 0;
    const timer = setInterval(() => {
        i = Math.min(i + 1, THINKING_STEPS.length - 1);   // stays on the last step
        label.textContent = THINKING_STEPS[i];
    }, 2500);
    return () => clearInterval(timer);
}

function setComposer(enabled, placeholder) {
    [input, sendBtn, fileInput].forEach((el) => { if (el) el.disabled = !enabled; });
    if (input) input.placeholder = placeholder || DEFAULT_PLACEHOLDER;
    if (sendBtn) sendBtn.classList.toggle('opacity-50', !enabled);
}

function applyComposerState() {
    const chat = chats[activeChatId];
    if (chat.closed) setComposer(false, 'This chat is closed. Start a new chat to continue.');
    else if (chat.wrap === 'asking') setComposer(false, 'Choose Yes or No above to continue…');
    else setComposer(true);
}

// Saves a UI-only assistant message with the chat and shows it if that chat is open
function pushNote(chatId, text) {
    const chat = chats[chatId];
    (chat.notes ??= []).push({ at: chat.history.length, text });
    saveState();
    return chatId === activeChatId ? appendDOM('assistant', text) : null;
}

function renderWrapUp(chatId) {
    const body = appendDOM('assistant', WRAP_Q);
    const row = mkEl('div', 'mt-3 flex gap-3');
    const yes = mkEl('button', 'px-5 py-2 rounded-lg bg-[#FFB000] text-black font-semibold text-[14px] hover:bg-[#E6A000]', 'Yes');
    const no = mkEl('button', 'px-5 py-2 rounded-lg border border-[#2E3744] text-gray-200 text-[14px] hover:border-gray-500', 'No');
    yes.type = no.type = 'button';
    const answer = (wantsMore) => {
        const chat = chats[chatId];
        const line = `${WRAP_Q} — **${wantsMore ? 'Yes' : 'No'}**`;
        body.innerHTML = formatSeverity(line);          // replaces the buttons with the answered line
        chat.wrap = null;
        (chat.notes ??= []).push({ at: chat.history.length, text: line });
        if (wantsMore) {
            pushNote(chatId, 'Sure, what else can I help you with?');
            applyComposerState();
        } else {
            closeChat(chatId);
        }
    };
    yes.onclick = () => answer(true);
    no.onclick = () => answer(false);
    row.append(yes, no);
    body.appendChild(row);
}

function renderClosedFooter() {
    const b = mkEl('button', 'self-start mt-2 mb-8 px-4 py-2 rounded-lg border border-[#2E3744] text-gray-200 text-[14px] hover:border-[#FFB000] hover:text-[#FFB000]', '＋ Start a new chat');
    b.type = 'button';
    b.onclick = createNewChat;
    if (messagesWrapper) messagesWrapper.appendChild(b);
}

function closeChat(chatId, rerender) {
    const chat = chats[chatId];
    const text = '🔒 This chat is now closed. Thank you for contacting TRIAGE-1.';
    chat.closed = true;
    chat.wrap = null;
    chat.run = null;
    (chat.notes ??= []).push({ at: chat.history.length, text });
    saveState();
    if (chatId !== activeChatId) { renderChatList(); return; }
    if (rerender) { switchChat(chatId); return; }
    appendDOM('assistant', text);
    renderClosedFooter();
    applyComposerState();
    renderChatList();
}

// Runs when the agent run for a ticket ends
async function onRunFinished(chatId, outcome, number) {
    const chat = chats[chatId];
    if (!chat || !chat.run || chat.run.number !== number) return;   // also stops double-firing
    chat.run = null;
    let text;
    if (outcome === 'resolved') {
        text = `✅ Ticket **${number}** has been resolved.`;
    } else if (outcome === 'escalated') {
        let sla = {};
        try { sla = await (await fetch(`/api/issues/${encodeURIComponent(number)}/sla`)).json(); } catch (e) {}
        text = `⬆️ Ticket **${number}** has been passed to our L2 team. An engineer will reach you in **${sla.response || 'a short while'}**, and the issue should be fixed in **${sla.resolution || 'the agreed resolution time'}**.`;
    } else if (outcome === 'queued') {
        text = `🎫 Ticket **${number}** has been created. TRIAGE-1 will pick it up shortly.`;
    } else {
        text = `Ticket **${number}** is still open. The support team will follow up with you.`;
    }
    pushNote(chatId, text);
    chat.wrap = 'asking';
    saveState();
    if (chatId === activeChatId) { renderWrapUp(chatId); applyComposerState(); }
}

// After a refresh, re-attach to a run that was still going
function resumeRun(chatId) {
    const r = chats[chatId].run;
    const body = appendDOM('assistant', `Checking on ticket **${r.number}**…`);
    watchRun(body, r.runId, r.number, r.server, chatId);
}

function clearIncidentButtons(chat) {
    document.querySelectorAll('.incident-action').forEach((n) => n.remove());
    Object.values(chat.meta || {}).forEach((m) => { if (m && !Array.isArray(m)) m.incident = null; });
}

function looksFixed(t) {
    return t.length <= 80 && !t.includes('?') && FIXED_RE.test(t) && !/\b(not|never|still)\b|n't/i.test(t);
}

function handleFixed(text) {
    const chat = chats[activeChatId];
    const reply = 'Glad to hear it is sorted! 🎉';
    chat.history.push({ role: 'user', content: text, skip: true }, { role: 'assistant', content: reply, skip: true });
    appendDOM('assistant', reply);
    chat.wrap = 'asking';
    saveState();
    renderWrapUp(activeChatId);
    applyComposerState();
    if (turnCount) turnCount.innerText = (chat.history.length / 2).toString();
}

// What goes into the ServiceNow work note: no off-topic turns, no ticket requests, last 12 messages
function ticketTranscript(chatId) {
    return (chats[chatId].history || []).filter((m) => !m.skip).slice(-12)
        .map((m) => ({ role: m.role, content: String(m.content).slice(0, 4000) }));
}

let busy = false;

async function streamResponse(query) {
    const chatId = activeChatId;
    const chat = chats[chatId];
    busy = true;
    const statusText = document.getElementById('status-text');
    if (statusText) statusText.innerText = "Responding";
    if (sendBtn) {
        sendBtn.disabled = true;
        sendBtn.classList.add('opacity-50');
    }

    if (chat.history.length === 0 && !chat.titled) {
        chat.titled = true;
        const titleChatId = chatId;
        chats[titleChatId].title = "Generating title...";
        renderChatList();

        fetch('/api/title', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: query })
        })
        .then(res => res.json())
        .then(data => {
            if (chats[titleChatId]?.title === "Generating title...") chats[titleChatId].title = data.title || "Incident Triage";
            renderChatList();
        })
        .catch(() => {
            if (chats[titleChatId]?.title === "Generating title...") chats[titleChatId].title = "Incident Triage";
            renderChatList();
        });
    }

    const activeHistory = chat.history;
    const contentNode = appendDOM('assistant', '');

    const stopThinking = contentNode ? startThinking(contentNode) : () => {};
    if (chatContainer) chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });

    const maxTurns = slider ? parseInt(slider.value, 10) : 6;
    const limitedHistory = activeHistory.filter((m) => !m.skip).slice(-(maxTurns * 2)).map(({ role, content }) => ({ role, content }));

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query, history: limitedHistory })
        });

        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(`HTTP ${response.status} ${detail.slice(0, 300)}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let fullText = "";
        let isFirstChunk = true;
        let finished = false;
        let meta = { sources: [] };

        const handleLine = (line) => {
            if (!line.startsWith('data: ')) return;
            const dataStr = line.slice(6).trim();
            if (dataStr === '[DONE]') { finished = true; return; }

            let parsed;
            try { parsed = JSON.parse(dataStr); } catch (e) { return; }

            if (parsed.meta) { meta = parsed.meta; return; }
            if (parsed.error) throw new Error(parsed.error);

            if (parsed.text) {
                if (isFirstChunk && contentNode) {
                    stopThinking();
                    contentNode.innerHTML = "";
                    isFirstChunk = false;
                }
                fullText += parsed.text;
                if (contentNode) contentNode.innerHTML = formatSeverity(fullText);
            }
        };

        while (!finished) {
            const { value, done } = await reader.read();
            if (done) break;

            buffer += decoder.decode(value, { stream: true });
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                handleLine(line);
                if (finished) break;
            }
            if (chatContainer) chatContainer.scrollTo({ top: chatContainer.scrollHeight });
        }

        if (!finished && buffer) handleLine(buffer);
        if (finished) reader.cancel().catch(() => {});

        if (!fullText) {
            if (contentNode) contentNode.innerHTML = `<span class="text-red-400">The model returned an empty response. Check the Colab server.</span>`;
            return;
        }

        // Off-topic turns and ticket requests stay out of the model context and the ticket transcript
        const offTopic = /^\s*I['\u2019]?m a triage assistant/i.test(fullText);
        const skip = offTopic || meta.incident?.mode === 'explicit';
        if (offTopic) meta.incident = null;   // no Create incident button under a declined message
        activeHistory.push({ role: "user", content: query, skip });
        activeHistory.push({ role: "assistant", content: fullText, skip });
        if (turnCount) turnCount.innerText = (activeHistory.length / 2).toString();

        const store = (chat.meta ??= {});
        store[activeHistory.length - 1] = { sources: meta.sources, incident: meta.incident || null, kind: meta.kind || null };
        saveState();

        const sourceLine = contentNode?.parentElement?.querySelector('.source-line');
        if (sourceLine) {
            let badgeHTML = meta.kind === 'canned' ? '' : sourceBadgeHTML(meta.sources, fullText);
            if (meta.incident?.status === 'created') {
                badgeHTML += ` <a href="/agent" class="src-badge src-kb" style="text-decoration:none; margin-left:6px;">🎫 Ticket ${esc(meta.incident.number)} created for ${esc(meta.incident.server)} · View Agent Run →</a>`;
            } else if (meta.incident?.status === 'busy') {
                badgeHTML += ` <span class="src-badge src-gen" style="margin-left:6px;">⏳ Agent is busy with another ticket (Server: ${esc(meta.incident.server)})</span>`;
            }
            sourceLine.innerHTML = badgeHTML;
        }

        if (['can_report', 'needs_server'].includes(meta.incident?.status) && contentNode) {
            renderCreateIncidentAction(contentNode, meta.incident.description ?? '', meta.incident.suggested_server, meta.incident.mode);
        }

        const citedSop = (meta.sources || []).some((id) => fullText.includes(id));
        if (citedSop && !meta.incident && !/SEVERITY:/i.test(fullText)) {
            chat.wrap = 'asking';
            saveState();
            if (chatId === activeChatId) renderWrapUp(chatId);
        }

    } catch (err) {
        console.error('Chat request failed:', err);
        if (contentNode) {
            contentNode.innerHTML = `<span class="text-red-400">Request failed: ${esc(err.message)}<br><span class="text-gray-500 text-[13px]">Check the uvicorn terminal for the full traceback.</span></span>`;
        }
    } finally {
        stopThinking();
        busy = false;
        applyComposerState();
    }
}

const GREETING_RE = /^(hi+|hello+|hey+|hola|good\s+(morning|afternoon|evening)|greetings)[\s!.,?]*$/i;

function showGreeting() {
    const body = appendDOM('assistant', "Hi! I'm TRIAGE-1. Ask me a question, or report an issue and I'll raise a ticket and start working on it.");
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '🛠 Report an Issue';
    btn.className = 'mt-4 px-4 py-2 rounded-lg bg-[#FFB000] text-black font-semibold text-[14px] hover:bg-[#E6A000]';
    btn.onclick = () => startIssueFlow();
    body.appendChild(btn);
}

function mkEl(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
}

function renderCreateIncidentAction(container, description, server, mode) {
    const bar = mkEl('div', 'incident-action');
    const msg = mode === 'explicit'
        ? 'Ready to raise a ticket?'
        : (server ? `This looks like a live issue on ${server}.` : 'This looks like a live issue.');
    bar.append(mkEl('span', 'incident-action-text', msg));
    const btn = mkEl('button', 'incident-action-btn', '🎫 Create incident');
    btn.type = 'button';
    btn.onclick = () => startIssueFlow({ description, server });
    bar.append(btn);
    container.appendChild(bar);
}

// Search box with results that update as the user types
function makeTypeahead(placeholder, search, initial, onPick) {
    const field = 'w-full rounded-lg bg-[#0A0D10] border border-[#2E3744] px-3 py-2.5 text-[14px] text-white outline-none focus:border-[#FFB000]';
    const input = mkEl('input', field);
    input.placeholder = placeholder;
    input.autocomplete = 'off';
    if (initial) input.value = initial;
    const list = mkEl('div', 'hidden absolute left-0 right-0 top-full mt-1 rounded-lg bg-[#171C24] border border-[#2E3744] z-10 overflow-hidden max-h-60 overflow-y-auto');
    const wrap = mkEl('div', 'relative');
    wrap.append(input, list);
    let timer = null, seq = 0;

    async function load() {
        const mine = ++seq;
        try {
            const rows = await search(input.value.trim());
            if (mine !== seq) return;  // a newer keystroke is already in flight
            list.replaceChildren();
            if (!rows.length) list.append(mkEl('div', 'px-3 py-2 text-[13px] text-gray-500', 'No matches'));
            rows.forEach((row) => {
                const b = mkEl('button', 'w-full text-left px-3 py-2 hover:bg-[#232B33] flex justify-between gap-3 text-[14px] text-gray-200');
                b.type = 'button';
                b.append(mkEl('span', '', row.primary), mkEl('span', 'mono text-[11px] text-gray-500', row.secondary || ''));
                b.onclick = () => { input.value = row.primary; list.classList.add('hidden'); onPick(row); };
                list.append(b);
            });
            list.classList.remove('hidden');
        } catch (e) { list.classList.add('hidden'); }
    }
    input.addEventListener('input', () => { onPick(null); clearTimeout(timer); timer = setTimeout(load, 150); });
    input.addEventListener('focus', load);
    return { wrap, input, hide: () => list.classList.add('hidden') };
}

function startIssueFlow(prefill) {
    const chatId = activeChatId;
    if (document.getElementById('issue-modal')) return;
    const pre = (prefill && typeof prefill.description === 'string') ? prefill : {};
    const chosen = { caller: null, ci: pre.server || null };

    const overlay = mkEl('div', 'fixed inset-0 z-[100] bg-black/60 flex items-center justify-center p-4');
    overlay.id = 'issue-modal';
    const card = mkEl('div', 'w-full max-w-lg rounded-2xl bg-[#12161C] border border-[#2E3744] p-6 shadow-2xl');
    const close = () => overlay.remove();
    const label = (t, first) => mkEl('label', `block text-[12px] mono uppercase tracking-wider text-gray-400 ${first ? '' : 'mt-4'} mb-2`, t);
    const field = 'w-full rounded-lg bg-[#0A0D10] border border-[#2E3744] px-3 py-2.5 text-[14px] text-white outline-none focus:border-[#FFB000]';

    const caller = makeTypeahead('Search by name, username or email…', async (q) => {
        if (q.length < 2) return [];
        const r = await fetch('/api/users?q=' + encodeURIComponent(q));
        return (await r.json()).map((u) => ({ primary: u.name || u.user_name, secondary: u.email || u.user_name, user_name: u.user_name }));
    }, '', (row) => { chosen.caller = row ? row.user_name : null; });

    const ci = makeTypeahead('Start typing a server name…', async (q) => {
        const r = await fetch('/api/servers?q=' + encodeURIComponent(q));
        return (await r.json()).map((s) => ({ primary: s.name, secondary: `${s.platform} · ${s.state}` }));
    }, pre.server || '', (row) => { chosen.ci = row ? row.primary : null; });

    const levels = (selected) => {
        const s = mkEl('select', field);
        [['1', '1 - High'], ['2', '2 - Medium'], ['3', '3 - Low']].forEach(([v, t]) => {
            const o = mkEl('option', '', t);
            o.value = v;
            if (v === selected) o.selected = true;
            s.append(o);
        });
        return s;
    };
    const impact = levels('2');
    const urgency = levels('2');
    const grid = mkEl('div', 'grid grid-cols-2 gap-4');
    const colI = mkEl('div'); colI.append(label('Impact', false), impact);
    const colU = mkEl('div'); colU.append(label('Urgency', false), urgency);
    grid.append(colI, colU);

    const desc = mkEl('textarea', field + ' resize-none');
    desc.rows = 3;
    desc.placeholder = 'What is going wrong? For example: disk is almost full, service is down…';
    const err = mkEl('div', 'text-[13px] text-red-400 min-h-[18px] mt-3');
    const cancel = mkEl('button', 'px-4 py-2 rounded-lg border border-[#2E3744] text-gray-300 text-[14px] hover:border-gray-500', 'Cancel');
    cancel.type = 'button';
    const submit = mkEl('button', 'px-5 py-2 rounded-lg bg-[#FFB000] text-black font-semibold text-[14px] hover:bg-[#E6A000] disabled:opacity-50', 'Create incident');
    submit.type = 'button';
    const btns = mkEl('div', 'flex justify-end gap-3 mt-2');
    btns.append(cancel, submit);

    card.append(
        mkEl('h3', 'text-[18px] font-semibold text-white mb-4', 'Create incident'),
        label('Caller', true), caller.wrap,
        label('Configuration item', false), ci.wrap,
        mkEl('div', 'mt-4'), grid,
    );
    if (pre.description) {
        card.append(mkEl('p', 'mt-4 text-[12.5px] text-gray-500', `Description (from your message): ${pre.description.slice(0, 110)}${pre.description.length > 110 ? '…' : ''}`));
    } else {
        card.append(label('Description', false), desc);
    }
    card.append(err, btns);
    overlay.append(card);
    document.body.append(overlay);
    caller.input.focus();

    card.addEventListener('click', (e) => {
        [caller, ci].forEach((t) => { if (!t.wrap.contains(e.target)) t.hide(); });
    });
    overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    cancel.onclick = close;

    submit.onclick = async () => {
        err.textContent = '';
        const description = (pre.description || desc.value).trim();
        if (!chosen.caller) { err.textContent = 'Pick a caller from the list.'; return; }
        if (!chosen.ci) { err.textContent = 'Pick a configuration item (server) from the list.'; return; }
        if (description.length < 10) { err.textContent = 'Please describe the problem in a few words.'; return; }
        submit.disabled = true;
        submit.textContent = 'Creating incident…';
        const transcript = ticketTranscript(chatId);
        try {
            const r = await fetch('/api/issues', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    server: chosen.ci, description, transcript, caller: chosen.caller,
                    impact: Number(impact.value), urgency: Number(urgency.value),
                }),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not create the incident.');

            close();
            const chat = chats[chatId];
            chat.run = { runId: data.run_id || null, number: data.number, server: chosen.ci };
            clearIncidentButtons(chat);
            saveState();
            const live = pushNote(chatId, `Incident **${data.number}** has been created for **${chosen.ci}**. Here is what TRIAGE-1 is doing:`);
            if (live) watchRun(live, data.run_id, data.number, chosen.ci, chatId);
        } catch (e) {
            err.textContent = e.message;
            submit.disabled = false;
            submit.textContent = 'Create incident';
        }
    };
}

function handleSend() {
    const text = input ? input.value.trim() : '';
    const chat = chats[activeChatId];
    if (!text || busy || chat.closed || chat.wrap === 'asking') return;
    appendDOM('user', text);
    if (input) {
        input.value = '';
        input.style.height = 'auto';
    }
    if (GREETING_RE.test(text)) { showGreeting(); return; }
    if (looksFixed(text)) { handleFixed(text); return; }
    streamResponse(text);
}

if (sendBtn) sendBtn.addEventListener('click', handleSend);
if (input) {
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
            e.preventDefault();
            handleSend();
        }
    });

    input.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = (this.scrollHeight) + 'px';
        if(this.scrollHeight > 200) this.style.overflowY = 'auto';
        else this.style.overflowY = 'hidden';
    });
}

if (fileInput) {
    fileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        e.target.value = '';
        if (!file || busy) return;
        if (file.size > 200 * 1024) { alert("File too large (max 200 KB)."); return; }
        try {
            const text = await file.text();
            const payload = `[Attached file: ${file.name}]\n\n${text}`;
            appendDOM('user', payload);
            streamResponse(payload);
        } catch (err) {
            alert("Failed to read file.");
        }
    });
}

let healthFails = 0;
async function checkAgentStatus() {
    if (busy) return;
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');

    try {
        const response = await fetch('/api/health', { signal: AbortSignal.timeout(8000) });
        const data = await response.json();
        healthFails = 0;

        if (data.status === 'online') {
            const side = document.getElementById('agent-status');
            if (dot) dot.className = "w-3 h-3 rounded-full bg-[#FFB000] animate-pulse";
            if (text) text.innerText = "Listening";
            if (side) side.innerText = "● online";
        } else {
            if (dot) dot.className = "w-3 h-3 rounded-full bg-gray-500";
            if (text) text.innerText = "Offline";
            const side = document.getElementById('agent-status');
            if (side) side.innerText = "● offline";
        }
        if (++healthFails < 2) return;
    } catch (err) {
        if (dot) dot.className = "w-3 h-3 rounded-full bg-red-500 animate-ping";
        if (text) text.innerText = "Offline";
        const side = document.getElementById('agent-status');
        if (side) side.innerText = "● offline";
    }
}

checkAgentStatus();
setInterval(checkAgentStatus, 10000);

const clearBtn = document.getElementById('clear-btn');
const clearConfirm = document.getElementById('clear-confirm');

function setClearConfirm(on) {
    if (clearBtn) clearBtn.classList.toggle('hidden', on);
    if (clearConfirm) {
        clearConfirm.classList.toggle('hidden', !on);
        clearConfirm.classList.toggle('flex', on);
    }
}

function clearAllChats() {
    if (busy) { setClearConfirm(false); return; }
    try { localStorage.removeItem(STORE_KEY); } catch (e) {}
    chats = { "chat_1": { title: "Default Chat", history: [] } };
    activeChatId = "chat_1";
    renamingId = deletingId = openMenuId = null;
    setClearConfirm(false);
    switchChat(activeChatId);
}

if (clearBtn) clearBtn.addEventListener('click', () => setClearConfirm(true));
const clearNo = document.getElementById('clear-no');
const clearYes = document.getElementById('clear-yes');
if (clearNo) clearNo.addEventListener('click', () => setClearConfirm(false));
if (clearYes) clearYes.addEventListener('click', clearAllChats);

switchChat(activeChatId);

// ---- live progress card (per ticket) and global status box (for every user) ----
const RUN_STATUS_LABEL = {
    running: '⚙ TRIAGE-1 is working…',
    awaiting_approval: '⏸ Waiting for an engineer to approve an action',
    done: '✓ Finished',
    error: '✗ Stopped with an error',
    interrupted: '✗ Interrupted by a server restart',
};

function watchRun(container, runId, number, server, chatId) {
    const card = mkEl('div', 'mt-3 p-4 rounded-xl bg-[#12161C] border border-[#2E3744]');
    const head = mkEl('div', 'mono text-[13px] font-semibold text-[#FFB000] mb-1', `🎫 ${number} · ${server}`);
    const status = mkEl('div', 'mono text-[12px] text-gray-400 mb-2', 'Starting…');
    const list = mkEl('ul', 'space-y-1 text-[13.5px] text-gray-200');
    const link = mkEl('a', 'inline-block mt-3 mono text-[12px] text-[#FFB000] hover:underline', 'Open Agent Console →');
    link.href = '/agent';
    card.append(head, status, list, link);
    container.appendChild(card);
    if (!runId) {
        status.textContent = 'Ticket created. The agent is busy and will pick it up shortly.';
        if (chatId) onRunFinished(chatId, 'queued', number);
        return;
    }
    if (watchers[runId]) clearInterval(watchers[runId]);
    let after = 0;
    const timer = setInterval(async () => {
        try {
            const r = await fetch(`/api/agent/runs/${runId}?after=${after}`);
            if (r.status === 404) {   // run history is lost when the server restarts
                clearInterval(timer); delete watchers[runId];
                status.textContent = 'Live progress is no longer available.';
                if (chatId) onRunFinished(chatId, 'unknown', number);
                return;
            }
            if (!r.ok) return;
            const d = await r.json();
            after = d.next;
            (d.steps || []).forEach((s) => list.appendChild(mkEl('li', '', s.text)));
            status.textContent = RUN_STATUS_LABEL[d.status] || d.status;
            if (['done', 'error', 'interrupted'].includes(d.status)) {
                clearInterval(timer); delete watchers[runId];
                if (chatId) onRunFinished(chatId, d.outcome, number);
            }
        } catch (e) { /* keep polling */ }
    }, 3000);
    watchers[runId] = timer;
}

(function liveBanner() {
    const bar = mkEl('div', 'fixed bottom-4 right-4 z-[90] hidden max-w-sm rounded-xl bg-[#12161C] border border-[#FFB000]/50 px-4 py-3 shadow-2xl');
    const t1 = mkEl('div', 'mono text-[11px] uppercase tracking-wider text-[#FFB000] font-semibold');
    const t2 = mkEl('div', 'text-[13px] text-gray-200 mt-1');
    bar.append(t1, t2);
    document.body.appendChild(bar);
    async function tick() {
        try {
            const r = await fetch('/api/agent/live');
            if (!r.ok) return;
            const d = await r.json();
            if (!d.active) { bar.classList.add('hidden'); return; }
            t1.textContent = `TRIAGE-1 · ${d.ticket}${d.server ? ' · ' + d.server : ''}`;
            t2.textContent = d.text || 'Working…';
            bar.classList.remove('hidden');
        } catch (e) { /* ignore */ }
    }
    tick();
    setInterval(tick, 4000);
})();