const chatContainer = document.getElementById('chat-container');
const messagesWrapper = document.getElementById('messages-wrapper');
const emptyState = document.getElementById('empty-state');
const input = document.getElementById('prompt-input');
const sendBtn = document.getElementById('send-btn');
const fileInput = document.getElementById('chat-file');
const turnCount = document.getElementById('turn-count');
const chatListContainer = document.getElementById('chat-list');
const slider = document.getElementById('context-slider');

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
        btn.innerHTML = `<span class="text-[10px] shrink-0">${isActive ? '●' : '○'}</span> <span class="truncate">${esc(chat.title)}</span>`;
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

    const hist = chats[activeChatId].history;
    if (!hist || hist.length === 0) {
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
        const metaStore = chats[activeChatId].meta || {};
        hist.forEach((msg, i) => appendDOM(msg.role, msg.content,
            msg.role === 'assistant' ? metaStore[i] : undefined));
    }
    if (turnCount) turnCount.innerText = ((hist ? hist.length : 0) / 2).toString();
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
    const retrieved = sources || [];
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
    if (sources && sources.length) {
        return `<span class="src-badge src-gen">⚠ Retrieved ${sources.map(esc).join(', ')} but the answer doesn't cite them. Verify before acting.</span>`;
    }
    return `<span class="src-badge src-gen">⚠ Not from your knowledge base. Verify against your runbooks before acting.</span>`;
}

function formatSeverity(content) {
    content = normalizeRelated(content);
    const match = content.match(/SEVERITY:\s*(INFO|WARN|CRITICAL)/i);
    if (match) {
        const sev = match[1].toUpperCase();
        const cleanContent = content.replace(match[0], '').trim();
        const marker = `<div class="sev-card sev-${sev}"><span class="w-2 h-2 rounded-full bg-current"></span>SEVERITY — ${sev}</div>`;
        return marker + renderMarkdown(cleanContent);
    }
    return renderMarkdown(content);
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

function appendDOM(role, content, sources) {
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

    if (!isUser && sources) {
        div.querySelector('.source-line').innerHTML = sourceBadgeHTML(sources, content);
    }

    if (messagesWrapper) messagesWrapper.appendChild(div);
    if (chatContainer) chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    return div.querySelector('.content');
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

    if (contentNode) {
        contentNode.innerHTML = `<div class="mono text-[#FFB000] text-[13px] animate-pulse mt-1 flex items-center gap-2">▮ Diagnosing...</div>`;
    }
    if (chatContainer) chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });

    const maxTurns = slider ? parseInt(slider.value, 10) : 6;
    const limitedHistory = activeHistory.slice(-(maxTurns * 2)).map(({ role, content }) => ({ role, content }));

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

        activeHistory.push({ role: "user", content: query });
        activeHistory.push({ role: "assistant", content: fullText });
        if (turnCount) turnCount.innerText = (activeHistory.length / 2).toString();

        const store = (chat.meta ??= {});
        store[activeHistory.length - 1] = meta.sources;
        saveState();

        const sourceLine = contentNode?.parentElement?.querySelector('.source-line');
        if (sourceLine) {
            let badgeHTML = sourceBadgeHTML(meta.sources, fullText);
            if (meta.incident?.status === 'created') {
                badgeHTML += ` <a href="/agent" class="src-badge src-kb" style="text-decoration:none; margin-left:6px;">🎫 Ticket ${esc(meta.incident.number)} created for ${esc(meta.incident.server)} · View Agent Run →</a>`;
            } else if (meta.incident?.status === 'busy') {
                badgeHTML += ` <span class="src-badge src-gen" style="margin-left:6px;">⏳ Agent is busy with another ticket (Server: ${esc(meta.incident.server)})</span>`;
            }
            sourceLine.innerHTML = badgeHTML;
        }

        if (meta.incident?.status === 'needs_server' && contentNode) {
            renderServerPrompt(contentNode, meta.incident.description || query);
        }

    } catch (err) {
        console.error('Chat request failed:', err);
        if (contentNode) {
            contentNode.innerHTML = `<span class="text-red-400">Request failed: ${esc(err.message)}<br><span class="text-gray-500 text-[13px]">Check the uvicorn terminal for the full traceback.</span></span>`;
        }
    } finally {
        busy = false;
        if (sendBtn) {
            sendBtn.disabled = false;
            sendBtn.classList.remove('opacity-50');
        }
    }
}

const GREETING_RE = /^(hi+|hello+|hey+|hola|good\s+(morning|afternoon|evening)|greetings)[\s!.,?]*$/i;

function showGreeting() {
    const body = appendDOM('assistant', "Hi! I'm TRIAGE-1. Ask me a question, or report an issue and I'll raise a ticket and start working on it.");
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = '🛠 Report an Issue';
    btn.className = 'mt-4 px-4 py-2 rounded-lg bg-[#FFB000] text-black font-semibold text-[14px] hover:bg-[#E6A000]';
    btn.onclick = startIssueFlow;
    body.appendChild(btn);
}

function mkEl(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
}

async function renderServerPrompt(container, issueDescription) {
    const box = mkEl('div', 'mt-4 p-4 rounded-xl bg-[#12161C] border border-[#FFB000]/40');
    const title = mkEl('div', 'mono text-[12px] uppercase tracking-wider text-[#FFB000] font-semibold mb-1', '⚡ Action Required: Select Affected Server');
    const sub = mkEl('p', 'text-[13.5px] text-gray-300 mb-3', 'Which server is experiencing this issue? Pick a server below to raise a ServiceNow ticket and start TRIAGE-1:');
    const listWrap = mkEl('div', 'flex flex-wrap gap-2');
    const statusMsg = mkEl('div', 'text-[13px] text-gray-400 mt-2 mono', 'Loading servers…');

    box.append(title, sub, listWrap, statusMsg);
    container.appendChild(box);

    try {
        const r = await fetch('/api/servers');
        const rows = await r.json();
        statusMsg.textContent = '';
        if (!rows.length) {
            statusMsg.textContent = 'No running servers found in AWS.';
            return;
        }
        rows.forEach((s) => {
            const btn = mkEl('button', 'px-3.5 py-2 rounded-lg bg-[#171C24] border border-[#2E3744] hover:border-[#FFB000] hover:text-[#FFB000] text-[13.5px] text-gray-200 flex items-center gap-2 transition-colors');
            btn.type = 'button';
            btn.append(mkEl('span', 'font-semibold', s.name), mkEl('span', 'mono text-[11px] text-gray-500', `(${s.platform} · ${s.state})`));
            btn.onclick = async () => {
                listWrap.querySelectorAll('button').forEach((b) => { b.disabled = true; b.classList.add('opacity-50'); });
                statusMsg.className = 'text-[13px] text-[#FFB000] mt-2 mono';
                statusMsg.textContent = `Creating ServiceNow ticket for ${s.name}…`;

                const paddedDesc = issueDescription.trim().length >= 10 ? issueDescription.trim() : `Issue on ${s.name}: ${issueDescription.trim()}`;
                const transcript = (chats[activeChatId].history || []).slice(-40).map((m) => ({ role: m.role, content: String(m.content).slice(0, 4000) }));

                try {
                    const res = await fetch('/api/issues', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ server: s.name, description: paddedDesc, transcript }),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not create ticket.');

                    box.replaceChildren(
                        mkEl('div', 'mono text-[13px] text-[#34D399] font-semibold', `✓ Ticket ${data.number} created for ${s.name} — TRIAGE-1 is now investigating.`),
                        (() => {
                            const link = mkEl('a', 'inline-block mt-2 mono text-[12px] text-[#FFB000] hover:underline', 'Open Agent Console to view live progress →');
                            link.href = '/agent';
                            return link;
                        })()
                    );
                } catch (err) {
                    statusMsg.className = 'text-[13px] text-red-400 mt-2 mono';
                    statusMsg.textContent = err.message;
                    listWrap.querySelectorAll('button').forEach((b) => { b.disabled = false; b.classList.remove('opacity-50'); });
                }
            };
            listWrap.appendChild(btn);
        });
    } catch (e) {
        statusMsg.className = 'text-[13px] text-red-400 mt-2 mono';
        statusMsg.textContent = 'Could not load server list.';
    }
}

function startIssueFlow() {
    if (document.getElementById('issue-modal')) return;
    let selected = null, timer = null;

    const overlay = mkEl('div', 'fixed inset-0 z-[100] bg-black/60 flex items-center justify-center p-4');
    overlay.id = 'issue-modal';
    const card = mkEl('div', 'w-full max-w-lg rounded-2xl bg-[#12161C] border border-[#2E3744] p-6 shadow-2xl');
    const close = () => overlay.remove();

    const field = 'w-full rounded-lg bg-[#0A0D10] border border-[#2E3744] px-3 py-2.5 text-[14px] text-white outline-none focus:border-[#FFB000]';
    const search = mkEl('input', field);
    search.placeholder = 'Start typing a server name…';
    search.autocomplete = 'off';
    const list = mkEl('div', 'hidden absolute left-0 right-0 top-full mt-1 rounded-lg bg-[#171C24] border border-[#2E3744] z-10 overflow-hidden');
    const wrap = mkEl('div', 'relative');
    wrap.append(search, list);

    const desc = mkEl('textarea', field + ' resize-none');
    desc.rows = 4;
    desc.placeholder = 'What is going wrong? For example: disk is almost full, cannot connect, service is down…';
    const err = mkEl('div', 'text-[13px] text-red-400 min-h-[18px] mt-3');
    const cancel = mkEl('button', 'px-4 py-2 rounded-lg border border-[#2E3744] text-gray-300 text-[14px] hover:border-gray-500', 'Cancel');
    cancel.type = 'button';
    const submit = mkEl('button', 'px-5 py-2 rounded-lg bg-[#FFB000] text-black font-semibold text-[14px] hover:bg-[#E6A000] disabled:opacity-50', 'Submit');
    submit.type = 'button';
    const btns = mkEl('div', 'flex justify-end gap-3 mt-2');
    btns.append(cancel, submit);

    card.append(
        mkEl('h3', 'text-[18px] font-semibold text-white mb-4', 'Report an Issue'),
        mkEl('label', 'block text-[12px] mono uppercase tracking-wider text-gray-400 mb-2', 'Server'), wrap,
        mkEl('label', 'block text-[12px] mono uppercase tracking-wider text-gray-400 mt-4 mb-2', 'Description'), desc,
        err, btns,
    );
    overlay.append(card);
    document.body.append(overlay);
    search.focus();

    async function load() {
        try {
            const r = await fetch('/api/servers?q=' + encodeURIComponent(search.value.trim()));
            const rows = await r.json();
            list.replaceChildren();
            if (!rows.length) list.append(mkEl('div', 'px-3 py-2 text-[13px] text-gray-500', 'No matching servers'));
            rows.forEach((s) => {
                const b = mkEl('button', 'w-full text-left px-3 py-2 hover:bg-[#232B33] flex justify-between gap-3 text-[14px] text-gray-200');
                b.type = 'button';
                b.append(mkEl('span', '', s.name), mkEl('span', 'mono text-[11px] text-gray-500', `${s.platform} · ${s.state}`));
                b.onclick = () => { selected = s.name; search.value = s.name; list.classList.add('hidden'); };
                list.append(b);
            });
            list.classList.remove('hidden');
        } catch (e) { list.classList.add('hidden'); }
    }
    search.addEventListener('input', () => { selected = null; clearTimeout(timer); timer = setTimeout(load, 200); });
    search.addEventListener('focus', load);
    card.addEventListener('click', (e) => { if (!wrap.contains(e.target)) list.classList.add('hidden'); });
    overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
    cancel.onclick = close;

    submit.onclick = async () => {
        err.textContent = '';
        const description = desc.value.trim();
        if (!selected) { err.textContent = 'Pick a server from the list.'; return; }
        if (description.length < 10) { err.textContent = 'Please describe the problem in a few words.'; return; }
        submit.disabled = true;
        submit.textContent = 'Creating ticket…';
        const transcript = (chats[activeChatId].history || []).slice(-40)
            .map((m) => ({ role: m.role, content: String(m.content).slice(0, 4000) }));
        try {
            const r = await fetch('/api/issues', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ server: selected, description, transcript }),
            });
            const data = await r.json().catch(() => ({}));
            if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not create the ticket.');
            close();
            appendDOM('assistant', `Ticket **${data.number}** has been created for **${selected}** and the agent is investigating. If a fix needs approval, an IT engineer will review it.`);
        } catch (e) {
            err.textContent = e.message;
            submit.disabled = false;
            submit.textContent = 'Submit';
        }
    };
}

function handleSend() {
    const text = input ? input.value.trim() : '';
    if (!text || busy) return;
    appendDOM('user', text);
    if (input) {
        input.value = '';
        input.style.height = 'auto';
    }
    if (GREETING_RE.test(text)) { showGreeting(); return; }
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