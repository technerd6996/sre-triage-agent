const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// --- HYBRID RAG LOGIC ---
async function fetchDocuments() {
    const res = await fetch('/api/rag/documents');
    const docs = await res.json();
    const tbody = document.getElementById('doc-list');
    tbody.innerHTML = docs.map(d => `
        <tr class="border-b border-[#232B33]">
            <td class="py-3 font-medium text-gray-300">📄 ${esc(d.filename)}</td>
            <td class="py-3 mono text-xs text-gray-400">${d.chunks} parts</td>
            <td class="py-3 mono text-xs text-gray-400">${d.edges} edges</td>
            <td class="py-3 text-right">
                <button data-action="delete-doc" data-name="${encodeURIComponent(d.filename)}" class="text-red-400 hover:text-red-300 text-xs font-semibold">Delete</button>
            </td>
        </tr>
    `).join('');
}

async function uploadDocument() {
    const fileInput = document.getElementById('file-upload');
    const files = Array.from(fileInput.files);
    if (!files.length) return alert("Select at least one file first.");

    const status = document.getElementById('upload-status');
    status.textContent = '';
    document.body.style.cursor = 'wait';

    for (const file of files) {
        status.textContent += `⏳ ${file.name} ...\n`;
        const formData = new FormData();
        formData.append("file", file);
        try {
            const res = await fetch('/api/rag/upload', { method: 'POST', body: formData });
            const data = await res.json();
            status.textContent = status.textContent.replace(`⏳ ${file.name} ...`, `✅ ${file.name}: ${data.message}`);
        } catch (e) {
            status.textContent = status.textContent.replace(`⏳ ${file.name} ...`, `❌ ${file.name}: upload failed`);
        }
    }

    document.body.style.cursor = 'default';
    fileInput.value = "";
    fetchDocuments();
    fetchGraph();
}

async function deleteDocument(filename) {
    if (!confirm(`Delete "${filename}"?\n\nThis removes its vectors and any graph edges that belong only to this document. Edges shared with other documents, seed edges and manual edges are kept.`)) return;
    const res = await fetch(`/api/rag/documents/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    const data = await res.json();
    const g = data.graph || {};
    document.getElementById('upload-status').textContent =
        `🗑️ ${filename}: ${g.edges_removed || 0} edges removed, ${g.edges_kept_shared || 0} shared edges kept, ${g.nodes_removed || 0} orphan nodes removed.`;
    fetchDocuments();
    fetchGraph();
}

// --- GRAPH RAG LOGIC ---
async function fetchGraph() {
    const res = await fetch('/api/rag/graph');
    const edges = await res.json();
    const tbody = document.getElementById('graph-list');
    tbody.innerHTML = edges.map(e => `
        <tr class="border-b border-[#232B33]">
            <td class="py-3 font-medium text-blue-300">${esc(e.src)}</td>
            <td class="py-3 mono text-xs text-gray-400">${esc(e.rel)}</td>
            <td class="py-3 font-medium text-emerald-300">${esc(e.dst)}</td>
            <td class="py-3 mono text-[10px] text-gray-500 max-w-[140px] truncate" title="${esc(e.source)}">${esc(e.source)}</td>
            <td class="py-3 text-right">
                <button data-action="delete-edge" data-s="${encodeURIComponent(e.src)}" data-r="${encodeURIComponent(e.rel)}" data-d="${encodeURIComponent(e.dst)}"
                    class="text-red-400 hover:text-red-300 text-xs font-semibold">Delete</button>
            </td>
        </tr>
    `).join('');
}

async function addGraphEdge() {
    const src = document.getElementById('g-src').value;
    const rel = document.getElementById('g-rel').value;
    const dst = document.getElementById('g-dst').value;

    if (!src || !rel || !dst) return alert("Fill all fields.");
    await fetch('/api/rag/graph', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ src, rel, dst })
    });

    document.getElementById('g-src').value = '';
    document.getElementById('g-rel').value = '';
    document.getElementById('g-dst').value = '';
    fetchGraph();
}

async function uploadGraphCsv() {
    const input = document.getElementById('graph-csv');
    if (!input.files[0]) return alert("Select a CSV file first.");
    const status = document.getElementById('graph-status');
    status.textContent = '⏳ Importing...';
    const formData = new FormData();
    formData.append("file", input.files[0]);
    try {
        const res = await fetch('/api/rag/graph/upload', { method: 'POST', body: formData });
        const data = await res.json();
        status.textContent = (res.ok ? '✅ ' : '❌ ') + data.message;
    } catch (e) {
        status.textContent = '❌ Import failed.';
    }
    input.value = '';
    fetchGraph();
}

async function deleteGraphEdge(src, rel, dst) {
    const params = new URLSearchParams({ src, rel, dst });
    await fetch(`/api/rag/graph?${params.toString()}`, { method: 'DELETE' });
    fetchGraph();
}

// --- Event wiring (no inline handlers in the HTML) ---
document.getElementById('btn-ingest').addEventListener('click', uploadDocument);
document.getElementById('btn-add-edge').addEventListener('click', addGraphEdge);
document.getElementById('btn-import-csv').addEventListener('click', uploadGraphCsv);

document.getElementById('doc-list').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-action="delete-doc"]');
    if (btn) deleteDocument(decodeURIComponent(btn.dataset.name));
});

document.getElementById('graph-list').addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-action="delete-edge"]');
    if (btn) deleteGraphEdge(
        decodeURIComponent(btn.dataset.s),
        decodeURIComponent(btn.dataset.r),
        decodeURIComponent(btn.dataset.d)
    );
});

// Initialize Data
fetchDocuments();
fetchGraph();