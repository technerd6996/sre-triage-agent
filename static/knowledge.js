'use strict';

const $ = (id) => document.getElementById(id);
const state = { documents: [], edges: [], selectedFiles: [], csvFile: null, busy: false };

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));

async function responseData(response) {
    let data = {};
    try { data = await response.json(); }
    catch { data = {}; }
    if (!response.ok) {
        const message = data.detail || data.message || `Request failed (HTTP ${response.status}).`;
        throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
    }
    return data;
}

function setStatus(element, message, kind = 'info', visible = true) {
    element.textContent = message;
    element.dataset.kind = kind;
    element.hidden = !visible;
}

function showNotice(message, kind = 'info') {
    setStatus($('page-notice'), message, kind, true);
}

function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatNumber(value) {
    return Number(value || 0).toLocaleString();
}

function setBusy(isBusy) {
    state.busy = isBusy;
    $('btn-ingest').disabled = isBusy || state.selectedFiles.length === 0;
    $('btn-add-edge').disabled = isBusy;
    $('btn-import-csv').disabled = isBusy || !state.csvFile;
    $('btn-choose-docs').disabled = isBusy;
    $('btn-choose-csv').disabled = isBusy;
    $('btn-refresh').disabled = isBusy;
    $('btn-clear-files').disabled = isBusy || state.selectedFiles.length === 0;
}

function renderStats() {
    const chunks = state.documents.reduce((sum, document) => sum + Number(document.chunks || 0), 0);
    $('stat-documents').textContent = formatNumber(state.documents.length);
    $('stat-chunks').textContent = formatNumber(chunks);
    $('stat-edges').textContent = formatNumber(state.edges.length);
    $('document-count').textContent = formatNumber(state.documents.length);
    $('document-count').setAttribute('aria-label', `${state.documents.length} indexed documents`);
    $('graph-count').textContent = formatNumber(state.edges.length);
    $('graph-count').setAttribute('aria-label', `${state.edges.length} graph relationships`);
}

function searchTerms(value) {
    return String(value || '').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
}

function matchesSearch(values, query) {
    const terms = searchTerms(query);
    if (!terms.length) return true;
    const searchableText = values.map((value) => String(value ?? '')).join(' ').toLocaleLowerCase();
    // All entered terms must be present, but they may match different fields.
    return terms.every((term) => searchableText.includes(term));
}

function syncSearchClear(inputId, buttonId) {
    const input = $(inputId);
    const button = $(buttonId);
    button.hidden = input.value.length === 0;
}

function clearSearch(inputId, buttonId, renderFunction) {
    const input = $(inputId);
    input.value = '';
    syncSearchClear(inputId, buttonId);
    renderFunction();
    input.focus();
}

function renderDocuments() {
    const query = $('document-search').value;
    const filtered = state.documents.filter((document) => matchesSearch([document.filename], query));
    const body = $('doc-list');
    syncSearchClear('document-search', 'clear-document-search');

    if (!filtered.length) {
        body.innerHTML = `<tr><td colspan="4" class="table-state">${searchTerms(query).length ? 'No documents match your search. Try fewer keywords or clear the search.' : 'No documents indexed yet. Choose files above to start building your runbook library.'}</td></tr>`;
    } else {
        body.innerHTML = filtered.map((document) => `
            <tr>
                <td><div class="document-name"><span class="document-name-icon" aria-hidden="true">▤</span><span>${esc(document.filename)}</span></div></td>
                <td class="number-cell">${formatNumber(document.chunks)}</td>
                <td class="number-cell">${formatNumber(document.edges)}</td>
                <td class="action-cell"><button type="button" class="action-button" data-action="delete-doc" data-name="${esc(encodeURIComponent(document.filename))}" aria-label="Delete ${esc(document.filename)}">Delete</button></td>
            </tr>`).join('');
    }
    $('document-list-footer').textContent = searchTerms(query).length
        ? `Showing ${filtered.length} of ${state.documents.length} documents · Search matches filename keywords`
        : `${formatNumber(state.documents.length)} indexed ${state.documents.length === 1 ? 'document' : 'documents'} · ${formatNumber(state.documents.reduce((sum, d) => sum + Number(d.chunks || 0), 0))} vector chunks`;
}

function renderGraph() {
    const query = $('graph-search').value;
    const filtered = state.edges.filter((edge) => matchesSearch([
        edge.src, edge.rel, edge.dst, edge.source || 'seed / manual'
    ], query));
    const body = $('graph-list');
    syncSearchClear('graph-search', 'clear-graph-search');
    if (!filtered.length) {
        body.innerHTML = `<tr><td colspan="5" class="table-state">${searchTerms(query).length ? 'No relationships match your search. Try fewer keywords or clear the search.' : 'No graph relationships found. Add a manual relationship or import a CSV to populate the graph.'}</td></tr>`;
    } else {
        body.innerHTML = filtered.map((edge) => {
            const owner = String(edge.source || 'seed / manual');
            return `
                <tr>
                    <td class="entity-name">${esc(edge.src)}</td>
                    <td class="relation-text">${esc(edge.rel)}</td>
                    <td class="entity-name destination-name">${esc(edge.dst)}</td>
                    <td><span class="owner-text" title="${esc(owner)}">${esc(owner)}</span></td>
                    <td class="action-cell"><button type="button" class="action-button" data-action="delete-edge" data-s="${esc(encodeURIComponent(edge.src))}" data-r="${esc(encodeURIComponent(edge.rel))}" data-d="${esc(encodeURIComponent(edge.dst))}" aria-label="Delete relationship ${esc(edge.src)} ${esc(edge.rel)} ${esc(edge.dst)}">Delete</button></td>
                </tr>`;
        }).join('');
    }
    $('graph-list-footer').textContent = searchTerms(query).length
        ? `Showing ${filtered.length} of ${state.edges.length} relationships · Search covers source, relation, destination and owner`
        : `${formatNumber(state.edges.length)} relationships in the current graph`;
}

async function fetchDocuments() {
    const response = await fetch('/api/rag/documents');
    const documents = await responseData(response);
    if (!Array.isArray(documents)) throw new Error('Unexpected response while loading documents.');
    state.documents = documents;
    renderDocuments();
    renderStats();
}

async function fetchGraph() {
    const response = await fetch('/api/rag/graph');
    const edges = await responseData(response);
    if (!Array.isArray(edges)) throw new Error('Unexpected response while loading graph relationships.');
    state.edges = edges;
    renderGraph();
    renderStats();
}

async function refreshAll() {
    $('btn-refresh').disabled = true;
    try {
        const results = await Promise.allSettled([fetchDocuments(), fetchGraph()]);
        const errors = results.filter((result) => result.status === 'rejected').map((result) => result.reason.message || 'Unable to refresh data.');
        if (errors.length) showNotice(errors.join(' '), 'error');
        else showNotice('Knowledge base data refreshed.', 'success');
    } finally {
        $('btn-refresh').disabled = state.busy;
    }
}

function renderSelectedFiles() {
    const container = $('selected-files');
    const files = state.selectedFiles;
    container.hidden = files.length === 0;
    container.innerHTML = files.map((file, index) => {
        const extension = (file.name.split('.').pop() || 'FILE').slice(0, 4).toUpperCase();
        return `<div class="selected-file"><span class="file-type-icon">${esc(extension)}</span><span class="selected-file-name">${esc(file.name)}</span><span class="selected-file-size">${formatBytes(file.size)}</span><button type="button" class="remove-file" data-action="remove-selected-file" data-index="${index}" aria-label="Remove ${esc(file.name)} from selection" title="Remove file" ${state.busy ? 'disabled' : ''}>×</button></div>`;
    }).join('');
    $('selected-summary').textContent = files.length
        ? `${files.length} ${files.length === 1 ? 'file' : 'files'} selected · ${formatBytes(files.reduce((sum, file) => sum + file.size, 0))}`
        : 'No files selected';
    $('btn-ingest').disabled = state.busy || files.length === 0;
    $('btn-clear-files').disabled = state.busy || files.length === 0;
}

function selectFiles(files) {
    const existing = new Set(state.selectedFiles.map((file) => `${file.name}:${file.size}:${file.lastModified}`));
    const OK_EXT = /\.(md|markdown|txt|text|log|json|ya?ml|csv)$/i;
    const all = Array.from(files || []);
    const rejected = all.filter((file) => !OK_EXT.test(file.name));
    if (rejected.length) setStatus($('upload-status'), `Skipped unsupported: ${rejected.map((f) => f.name).join(', ')}`, 'error');
    const additions = all.filter((file) => OK_EXT.test(file.name) && !existing.has(`${file.name}:${file.size}:${file.lastModified}`));
    state.selectedFiles = [...state.selectedFiles, ...additions];
    renderSelectedFiles();
    if (additions.length) setStatus($('upload-status'), `${state.selectedFiles.length} file(s) ready to ingest. Files with an existing name replace that indexed version.`, 'info');
    $('file-upload').value = '';
}

async function uploadDocument() {
    if (state.busy) return;
    if (!state.selectedFiles.length) {
        setStatus($('upload-status'), 'Choose one or more files before ingesting.', 'error');
        return;
    }
    const files = [...state.selectedFiles];
    const results = [];
    setBusy(true);
    setStatus($('upload-status'), `Preparing to ingest ${files.length} file(s)…`, 'info');
    try {
        for (let index = 0; index < files.length; index += 1) {
            const file = files[index];
            setStatus($('upload-status'), `Processing ${index + 1} of ${files.length}: ${file.name}\nThis may take a moment while text chunks and graph relationships are created.`, 'info');
            const form = new FormData();
            form.append('file', file);
            try {
                const response = await fetch('/api/rag/upload', { method: 'POST', body: form });
                const data = await responseData(response);
                results.push(`✓ ${file.name}: ${data.message || 'Ingestion completed.'}`);
            } catch (error) {
                results.push(`✕ ${file.name}: ${error.message || 'Upload failed.'}`);
            }
            setStatus($('upload-status'), results.join('\n'), results.some((line) => line.startsWith('✕')) ? 'error' : 'success');
        }
        state.selectedFiles = [];
        renderSelectedFiles();
        await Promise.allSettled([fetchDocuments(), fetchGraph()]);
        showNotice(results.some((line) => line.startsWith('✕')) ? 'Ingestion finished with one or more errors. Review the file results in the runbook panel.' : 'All selected files were ingested successfully.', results.some((line) => line.startsWith('✕')) ? 'error' : 'success');
    } finally {
        setBusy(false);
        renderSelectedFiles();
    }
}

async function deleteDocument(filename) {
    if (state.busy) return;
    const confirmed = window.confirm(`Delete “${filename}”?\n\nThis removes its vector chunks and graph edges that belong only to this document. Shared edges, seed edges and manual edges are kept.`);
    if (!confirmed) return;
    setBusy(true);
    setStatus($('upload-status'), `Deleting ${filename}…`, 'info');
    try {
        const response = await fetch(`/api/rag/documents/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        const data = await responseData(response);
        const graph = data.graph || {};
        setStatus($('upload-status'), `Deleted ${filename}.\n${formatNumber(graph.edges_removed)} edges removed · ${formatNumber(graph.edges_kept_shared)} shared edges kept · ${formatNumber(graph.nodes_removed)} orphan nodes removed.`, 'success');
        await Promise.allSettled([fetchDocuments(), fetchGraph()]);
        showNotice(`Deleted “${filename}” and refreshed the index.`, 'success');
    } catch (error) {
        setStatus($('upload-status'), `Could not delete ${filename}: ${error.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

async function addGraphEdge(event) {
    event.preventDefault();
    if (state.busy) return;
    const src = $('g-src').value.trim();
    const rel = $('g-rel').value.trim();
    const dst = $('g-dst').value.trim();
    if (!src || !rel || !dst) {
        setStatus($('graph-status'), 'Enter a source entity, relationship, and destination.', 'error');
        return;
    }
    setBusy(true);
    setStatus($('graph-status'), 'Adding relationship…', 'info');
    try {
        const response = await fetch('/api/rag/graph', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ src, rel, dst })
        });
        const data = await responseData(response);
        $('g-src').value = '';
        $('g-rel').value = '';
        $('g-dst').value = '';
        setStatus($('graph-status'), data.message || 'Relationship added.', 'success');
        await fetchGraph().catch(() => {});
        showNotice('Infrastructure relationship added.', 'success');
    } catch (error) {
        setStatus($('graph-status'), `Could not add relationship: ${error.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

function selectCsv(file) {
    if (file && !/\.csv$/i.test(file.name)) {
        setStatus($('graph-status'), 'Please choose a .csv file.', 'error');
        file = null;
    }
    state.csvFile = file || null;
    $('csv-selection').hidden = !state.csvFile;
    $('csv-selection').textContent = state.csvFile ? `Selected: ${state.csvFile.name} (${formatBytes(state.csvFile.size)})` : '';
    $('btn-import-csv').disabled = state.busy || !state.csvFile;
    if (state.csvFile) setStatus($('graph-status'), 'CSV selected. Import to add the relationships to the graph.', 'info');
}

async function uploadGraphCsv() {
    if (state.busy) return;
    if (!state.csvFile) {
        setStatus($('graph-status'), 'Choose a CSV file before importing.', 'error');
        return;
    }
    const file = state.csvFile;
    setBusy(true);
    setStatus($('graph-status'), `Importing ${file.name}…`, 'info');
    try {
        const form = new FormData();
        form.append('file', file);
        const response = await fetch('/api/rag/graph/upload', { method: 'POST', body: form });
        const data = await responseData(response);
        setStatus($('graph-status'), data.message || 'CSV import completed.', 'success');
        selectCsv(null);
        $('graph-csv').value = '';
        await fetchGraph();
        showNotice('CSV relationship import completed.', 'success');
    } catch (error) {
        setStatus($('graph-status'), `CSV import failed: ${error.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

async function deleteGraphEdge(src, rel, dst) {
    if (state.busy) return;
    if (!window.confirm(`Delete this relationship?\n\n${src} → ${rel} → ${dst}`)) return;
    setBusy(true);
    setStatus($('graph-status'), 'Deleting relationship…', 'info');
    try {
        const params = new URLSearchParams({ src, rel, dst });
        const response = await fetch(`/api/rag/graph?${params.toString()}`, { method: 'DELETE' });
        const data = await responseData(response);
        setStatus($('graph-status'), data.message || 'Relationship deleted.', 'success');
        await fetchGraph();
        showNotice('Relationship deleted.', 'success');
    } catch (error) {
        setStatus($('graph-status'), `Could not delete relationship: ${error.message}`, 'error');
    } finally {
        setBusy(false);
    }
}

function activateWorkspace(name, moveFocus = false) {
    const isDocuments = name === 'documents';
    const tab = $(isDocuments ? 'tab-documents' : 'tab-graph');
    const otherTab = $(isDocuments ? 'tab-graph' : 'tab-documents');
    const panel = $(isDocuments ? 'documents-panel' : 'graph-panel');
    const otherPanel = $(isDocuments ? 'graph-panel' : 'documents-panel');

    tab.classList.add('is-active');
    tab.setAttribute('aria-selected', 'true');
    tab.tabIndex = 0;
    otherTab.classList.remove('is-active');
    otherTab.setAttribute('aria-selected', 'false');
    otherTab.tabIndex = -1;
    panel.hidden = false;
    otherPanel.hidden = true;
    if (moveFocus) tab.focus();
}

$('tab-documents').addEventListener('click', () => activateWorkspace('documents'));
$('tab-graph').addEventListener('click', () => activateWorkspace('graph'));
$('workspace-tabs')?.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    if (event.key === 'Home') return activateWorkspace('documents', true);
    if (event.key === 'End') return activateWorkspace('graph', true);
    activateWorkspace(event.key === 'ArrowRight' ? 'graph' : 'documents', true);
});
$('selected-files').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="remove-selected-file"]');
    if (!button || state.busy) return;
    const index = Number(button.dataset.index);
    if (!Number.isInteger(index) || index < 0 || index >= state.selectedFiles.length) return;
    state.selectedFiles.splice(index, 1);
    renderSelectedFiles();
    if (!state.selectedFiles.length) setStatus($('upload-status'), '', 'info', false);
});

$('btn-choose-docs').addEventListener('click', () => $('file-upload').click());
$('file-upload').addEventListener('change', (event) => selectFiles(event.target.files));
$('btn-clear-files').addEventListener('click', () => {
    state.selectedFiles = [];
    renderSelectedFiles();
    setStatus($('upload-status'), '', 'info', false);
});
$('btn-ingest').addEventListener('click', uploadDocument);
$('btn-refresh').addEventListener('click', refreshAll);
$('document-search').addEventListener('input', renderDocuments);
$('graph-search').addEventListener('input', renderGraph);
$('clear-document-search').addEventListener('click', () => clearSearch('document-search', 'clear-document-search', renderDocuments));
$('clear-graph-search').addEventListener('click', () => clearSearch('graph-search', 'clear-graph-search', renderGraph));
$('document-search').addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && event.currentTarget.value) clearSearch('document-search', 'clear-document-search', renderDocuments);
});
$('graph-search').addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && event.currentTarget.value) clearSearch('graph-search', 'clear-graph-search', renderGraph);
});
$('edge-form').addEventListener('submit', addGraphEdge);
$('btn-choose-csv').addEventListener('click', () => $('graph-csv').click());
$('graph-csv').addEventListener('change', (event) => selectCsv(event.target.files[0]));
$('btn-import-csv').addEventListener('click', uploadGraphCsv);

['dragover', 'drop'].forEach((n) => window.addEventListener(n, (e) => e.preventDefault()));
const dropZone = $('document-drop-zone');
['dragenter', 'dragover'].forEach((eventName) => dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    if (!state.busy) dropZone.classList.add('is-dragover');
}));
['dragleave', 'dragend'].forEach((eventName) => dropZone.addEventListener(eventName, () => dropZone.classList.remove('is-dragover')));
dropZone.addEventListener('drop', (event) => {
    event.preventDefault();
    dropZone.classList.remove('is-dragover');
    if (!state.busy) selectFiles(event.dataTransfer.files);
});

$('doc-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="delete-doc"]');
    if (button) deleteDocument(decodeURIComponent(button.dataset.name));
});
$('graph-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action="delete-edge"]');
    if (button) deleteGraphEdge(decodeURIComponent(button.dataset.s), decodeURIComponent(button.dataset.r), decodeURIComponent(button.dataset.d));
});

renderSelectedFiles();
Promise.allSettled([fetchDocuments(), fetchGraph()]).then((results) => {
    const errors = results.filter((result) => result.status === 'rejected').map((result) => result.reason.message || 'Could not load knowledge base data.');
    if (errors.length) showNotice(errors.join(' '), 'error');
});
