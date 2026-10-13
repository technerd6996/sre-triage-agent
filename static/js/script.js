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

// Every piece of markdown goes through DOMPurify before it touches innerHTML.
// If either library failed to load, fail CLOSED: show escaped plain text, never raw HTML.
function renderMarkdown(text) {
    if (typeof marked === 'undefined' || typeof DOMPurify === 'undefined') {
        console.error('Markdown libraries missing. marked:', typeof marked, '| DOMPurify:', typeof DOMPurify,
                      '. Check the <script> tags in index.html.');
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
            // a title request interrupted by the page change would stay stuck otherwise
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

// Close menu when clicking outside
document.addEventListener('click', (e) => {
    if (openMenuId && !e.target.closest('.chat-menu-container')) {
        const menu = document.getElementById(`menu-${openMenuId}`);
        if (menu) menu.classList.add('hidden');
        openMenuId = null;
    }
});

function toggleMenu(id, event) {
    event.stopPropagation();

    // Close previously open menu
    if (openMenuId && openMenuId !== id) {
        const oldMenu = document.getElementById(`menu-${openMenuId}`);
        if (oldMenu) oldMenu.classList.add('hidden');
    }

    const menu = document.getElementById(`menu-${id}`);
    menu.classList.toggle('hidden');
    openMenuId = menu.classList.contains('hidden') ? null : id;
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
    if (Object.keys(chats).length <= 1) return; // the Delete option is hidden in that case anyway
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
    chatListContainer.innerHTML = '';

    for (const [id, chat] of Object.entries(chats)) {
        const isActive = id === activeChatId;

        // Wrapper Container
        const div = document.createElement('div');
        div.className = `group relative flex items-center justify-between w-full px-2 py-1.5 rounded transition-colors ${isActive ? 'bg-amber-500/10' : 'hover:bg-white/5'} chat-menu-container mb-1`;
        
        // Inline rename
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

        // Inline delete confirmation
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

        // Main Chat Button
        const btn = document.createElement('button');
        btn.className = `flex items-center gap-3 flex-1 text-left truncate text-[14px] ${isActive ? 'text-amber-500 font-medium' : 'text-gray-400 hover:text-gray-200'}`;
        btn.title = chat.title;
        // Title can come from the LLM, so it MUST be escaped
        btn.innerHTML = `<span class="text-[10px] shrink-0">${isActive ? '●' : '○'}</span> <span class="truncate">${esc(chat.title)}</span>`;
        btn.onclick = () => switchChat(id);

        // 3-Dots Menu Button
        const dotsBtn = document.createElement('button');
        dotsBtn.className = `p-1.5 text-gray-500 hover:text-white rounded opacity-0 group-hover:opacity-100 transition-opacity focus:outline-none focus:opacity-100 ${isActive ? 'opacity-100' : ''}`;
        dotsBtn.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="1"></circle><circle cx="12" cy="5" r="1"></circle><circle cx="12" cy="19" r="1"></circle></svg>`;
        dotsBtn.onclick = (e) => toggleMenu(id, e);

        // Dropdown Menu Panel
        const menu = document.createElement('div');
        menu.id = `menu-${id}`;
        menu.className = `hidden absolute right-4 top-8 w-32 bg-[#171C24] border border-[#2E3744] rounded-md shadow-2xl z-50 flex flex-col py-1 overflow-hidden`;
        menu.innerHTML = `
            <button class="text-left px-3 py-2 text-[12.5px] text-gray-300 hover:bg-[#232B33] hover:text-white flex items-center gap-2 transition-colors" onclick="renameChat('${id}', event)">
                ✏️ Rename
            </button>
            ${Object.keys(chats).length > 1 ? `<button class="text-left px-3 py-2 text-[12.5px] text-red-400 hover:bg-[#232B33] hover:text-red-300 flex items-center gap-2 transition-colors border-t border-[#232B33]/50" onclick="deleteChat('${id}', event)">
                🗑️ Delete
            </button>` : ''}
        `;

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
    messagesWrapper.innerHTML = '';

    const hist = chats[activeChatId].history;
    if (hist.length === 0) {
        emptyState.style.display = 'flex';
        messagesWrapper.classList.add('hidden');
        messagesWrapper.classList.remove('flex');
    } else {
        emptyState.style.display = 'none';
        messagesWrapper.classList.remove('hidden');
        messagesWrapper.classList.add('flex');
        const metaStore = chats[activeChatId].meta || {};
        hist.forEach((msg, i) => appendDOM(msg.role, msg.content,
            msg.role === 'assistant' ? metaStore[i] : undefined));
    }
    turnCount.innerText = (hist.length / 2).toString();
}

// Mode 2K answers must end with exactly ONE plain "Related:" line.
// The model sometimes also writes it as a bullet, so collect every variant,
// drop them all, and re-append only the last one at the end.
const RELATED_RE = /^[ \t]*(?:[-*•][ \t]+)?(?:\*\*)?Related(?:\*\*)?:(?:\*\*)?[ \t]*(.*)$/gim;

function normalizeRelated(content) {
    const matches = [...content.matchAll(RELATED_RE)];
    if (matches.length === 0) return content;

    const value = matches[matches.length - 1][1].trim();
    const stripped = content.replace(RELATED_RE, '').replace(/\n{3,}/g, '\n\n').trim();
    return value ? `${stripped}\n\nRelated: ${value}` : stripped;
}

// Label every answer as grounded or not, based on retrieved IDs the answer actually names
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

// Renders a user message. Attached files become a collapsible card built from ESCAPED text.
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
    emptyState.style.display = 'none';
    messagesWrapper.classList.remove('hidden');
    messagesWrapper.classList.add('flex');

    const div = document.createElement('div');
    div.className = `mb-8 ${role === 'user' ? 'msg-user text-gray-300' : 'msg-ai text-white'}`;

    const isUser = role === 'user';

    const userIcon = `<div class="w-6 h-6 rounded-full bg-[#171C24] border border-[#2E3744] flex items-center justify-center shrink-0 shadow-sm">
        <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#A7B0BC" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"></path>
            <circle cx="12" cy="7" r="4"></circle>
        </svg>
    </div>`;

    const botIcon = `<div class="w-6 h-6 bg-[#0084FF] rounded-full rounded-bl-sm flex items-center justify-center shrink-0 shadow-sm">
        <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="#FFFFFF">
            <path d="M12 2a2 2 0 0 1 2 2c0 .74-.4 1.39-1 1.73V7h2a3 3 0 0 1 3 3v2h1a2 2 0 0 1 2 2v2a2 2 0 0 1-2 2h-1v1a3 3 0 0 1-3 3H8a3 3 0 0 1-3-3v-1H4a2 2 0 0 1-2-2v-2a2 2 0 0 1 2-2h1V10a3 3 0 0 1 3-3h2V5.73A2 2 0 0 1 12 2zm-3 10a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3zm6 0a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z"/>
        </svg>
    </div>`;

    div.innerHTML = `
        <div class="flex items-center gap-2.5 mb-2">
            ${isUser ? userIcon : botIcon}
            <div class="mono text-[11px] uppercase tracking-wider ${isUser ? 'text-gray-500' : 'text-[#0084FF]'} font-semibold mt-0.5">
                ${isUser ? 'You' : 'TRIAGE-1'}
            </div>
        </div>
        <div class="content text-[15px] leading-relaxed">${isUser ? renderUserContent(content) : formatSeverity(content)}</div>
        <div class="source-line"></div>
    `; // FIXED: the .source-line div was missing, so the badge had nowhere to render

    // FIXED: show the label when an old chat is re-rendered
    if (!isUser && sources) {
        div.querySelector('.source-line').innerHTML = sourceBadgeHTML(sources, content);
    }

    messagesWrapper.appendChild(div);
    chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    return div.querySelector('.content');
}

let busy = false;
checkAgentStatus();
async function streamResponse(query) {
    const chatId = activeChatId;
    const chat = chats[chatId];
    busy = true;
    document.getElementById('status-text').innerText = "Responding";
    sendBtn.disabled = true;
    sendBtn.classList.add('opacity-50');
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

    // 2. Add the animated loading indicator
    contentNode.innerHTML = `<div class="mono text-[#FFB000] text-[13px] animate-pulse mt-1 flex items-center gap-2">▮ Diagnosing...</div>`;
    chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });

    // Apply Slider Constraint to payload (only role + content, the backend rejects extra keys)
    const maxTurns = parseInt(slider.value, 10);
    const limitedHistory = activeHistory.slice(-(maxTurns * 2)).map(({ role, content }) => ({ role, content }));

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: query, history: limitedHistory })
        });

        // FIXED: include the server's reason, not just the status code
        if (!response.ok) {
            const detail = await response.text().catch(() => "");
            throw new Error(`HTTP ${response.status} ${detail.slice(0, 300)}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";        // holds a partial line between network reads
        let fullText = "";
        let isFirstChunk = true;
        let finished = false;
        let meta = { sources: [] };   // FIXED: moved next to the other stream state

        // Handles ONE complete SSE line
        const handleLine = (line) => {
            if (!line.startsWith('data: ')) return;
            const dataStr = line.slice(6).trim();
            if (dataStr === '[DONE]') { finished = true; return; }

            let parsed;
            try { parsed = JSON.parse(dataStr); } catch (e) { return; } // only ignore bad JSON

            if (parsed.meta) { meta = parsed.meta; return; }            // FIXED: receive retrieved document IDs
            if (parsed.error) throw new Error(parsed.error);            // FIXED: surface backend/Colab errors

            if (parsed.text) {
                // 3. Clear the loading indicator on the first real token
                if (isFirstChunk) {
                    contentNode.innerHTML = "";
                    isFirstChunk = false;
                }
                fullText += parsed.text;
                contentNode.innerHTML = formatSeverity(fullText);
            }
        };

        while (!finished) {
            const { value, done } = await reader.read();
            if (done) break;

            // stream:true keeps multi-byte characters intact across chunk boundaries
            buffer += decoder.decode(value, { stream: true });

            // Only process COMPLETE lines; the last piece may be half a line, so keep it
            const lines = buffer.split('\n');
            buffer = lines.pop();

            for (const line of lines) {
                handleLine(line);
                if (finished) break;
            }
            chatContainer.scrollTo({ top: chatContainer.scrollHeight });
        }

        // Flush whatever is left if the stream ended without a trailing newline
        if (!finished && buffer) handleLine(buffer);
        if (finished) reader.cancel().catch(() => {});

        if (!fullText) {
            contentNode.innerHTML = `<span class="text-red-400">The model returned an empty response. Check the Colab server.</span>`;
            return; // do not save an empty turn into history
        }

        // Save to state AFTER completion
        activeHistory.push({ role: "user", content: query });
        activeHistory.push({ role: "assistant", content: fullText });
        turnCount.innerText = (activeHistory.length / 2).toString();

        // Source label (kept outside history, which the backend validates strictly)
        const store = (chat.meta ??= {});
        store[activeHistory.length - 1] = meta.sources;
        saveState();
        const sourceLine = contentNode.parentElement.querySelector('.source-line');
        if (sourceLine) sourceLine.innerHTML = sourceBadgeHTML(meta.sources, fullText); // FIXED: null-safe

    } catch (err) {
        // FIXED: show the real cause instead of a generic message
            console.error('Chat request failed:', err);
        // REPLACE WITH
            contentNode.innerHTML = `<span class="text-red-400">Request failed: ${esc(err.message)}<br><span class="text-gray-500 text-[13px]">Check the uvicorn terminal for the full traceback.</span></span>`;
        } finally {
            busy = false;
            sendBtn.disabled = false;
            sendBtn.classList.remove('opacity-50');
    }
}

function handleSend() {
// REPLACE WITH
    const text = input.value.trim();
    if (!text || busy) return;
    appendDOM('user', text);
    input.value = '';
    input.style.height = 'auto';
    streamResponse(text);
}

sendBtn.addEventListener('click', handleSend);
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

fileInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file || busy) return;
    if (file.size > 200 * 1024) { alert("File too large (max 200 KB)."); return; }
    try {
        const text = await file.text();

        // Same payload is used for display AND backend; renderUserContent() turns it into the card
        const payload = `[Attached file: ${file.name}]\n\n${text}`;

        appendDOM('user', payload);
        streamResponse(payload);
    } catch (err) {
        alert("Failed to read file.");
    }
});

let healthFails = 0;
async function checkAgentStatus() {
    if (busy) return; // the server is busy generating, so don't poll it
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');

    try {
        const response = await fetch('/api/health', { signal: AbortSignal.timeout(8000) });
        const data = await response.json();
        healthFails = 0;

        if (data.status === 'online') {
            const side = document.getElementById('agent-status');
            dot.className = "w-3 h-3 rounded-full bg-[#FFB000] animate-pulse";
            text.innerText = "Listening";
            if (side) side.innerText = "● online";
        } else {
            dot.className = "w-3 h-3 rounded-full bg-gray-500";
            text.innerText = "Offline";
            if (side) side.innerText = "● offline";
        } if (++healthFails < 2) return;
        } catch (err) {
        dot.className = "w-3 h-3 rounded-full bg-red-500 animate-ping";
        text.innerText = "Offline";
        const side = document.getElementById('agent-status');
        if (side) side.innerText = "● offline";
        }
}

// Run on load and poll every 10 seconds
checkAgentStatus();
setInterval(checkAgentStatus, 10000);

// --- Clear all chats ---
const clearBtn = document.getElementById('clear-btn');
const clearConfirm = document.getElementById('clear-confirm');

function setClearConfirm(on) {
    clearBtn.classList.toggle('hidden', on);
    clearConfirm.classList.toggle('hidden', !on);
    clearConfirm.classList.toggle('flex', on);
}

function clearAllChats() {
    if (busy) { setClearConfirm(false); return; } // don't wipe while a reply is streaming
    try { localStorage.removeItem(STORE_KEY); } catch (e) {}
    chats = { "chat_1": { title: "Default Chat", history: [] } };
    activeChatId = "chat_1";
    renamingId = deletingId = openMenuId = null;
    setClearConfirm(false);
    switchChat(activeChatId);
}

clearBtn.addEventListener('click', () => setClearConfirm(true));
document.getElementById('clear-no').addEventListener('click', () => setClearConfirm(false));
document.getElementById('clear-yes').addEventListener('click', clearAllChats);

// Initialize UI
switchChat(activeChatId);