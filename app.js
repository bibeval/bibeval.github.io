// BibEval — Client-Side BibTeX Evaluator
// Parses .bib files, validates URLs, classifies references

// ═══════════════════════════════════════════════════════════════════════
// Mini BibTeX Parser
// ═══════════════════════════════════════════════════════════════════════

function parseBibTeX(content) {
  const entries = [];
  // Normalize line endings
  content = content.replace(/\r\n/g, '\n').replace(/\r/g, '\n');

  // Split into entry blocks
  const entryRegex = /@(\w+)\s*\{([^,]*),\s*([\s\S]*?)\n\s*\}/g;
  let match;

  while ((match = entryRegex.exec(content)) !== null) {
    const entryType = match[1].toLowerCase();
    const entryId = match[2].trim();
    const fieldsBlock = match[3];

    const entry = { entry_id: entryId, entry_type: entryType };
    const fields = parseFields(fieldsBlock);

    entry.title = cleanValue(fields.title || fields.TITLE || '');
    entry.author_string = fields.author || fields.AUTHOR || '';
    entry.authors = parseAuthors(entry.author_string);
    entry.url = cleanValue(fields.url || fields.URL || '');
    entry.doi = cleanValue(fields.doi || fields.DOI || '');
    entry.year = cleanValue(fields.year || fields.YEAR || '');
    entry.booktitle = cleanValue(fields.booktitle || fields.BOOKTITLE || '');
    entry.journal = cleanValue(fields.journal || fields.JOURNAL || entry.booktitle || '');

    entries.push(entry);
  }

  return entries;
}

function parseFields(block) {
  const fields = {};
  // Match field = value pairs (value can be {...} or "...")
  const fieldRegex = /(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|"(?:[^"\\]|\\.)*")/g;
  let match;

  while ((match = fieldRegex.exec(block)) !== null) {
    const key = match[1].toLowerCase();
    let value = match[2];
    // Strip surrounding braces or quotes
    if (value.startsWith('{') && value.endsWith('}')) {
      value = value.slice(1, -1);
    } else if (value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1);
    }
    fields[key] = value;
  }

  return fields;
}

function cleanValue(val) {
  if (!val) return '';
  return val.replace(/[\n\r]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function parseAuthors(raw) {
  if (!raw) return [];
  return raw.split(/\s+and\s+/).map(a => {
    a = a.trim();
    a = a.replace(/^\{|\}$/g, '');
    return a.replace(/\s+/g, ' ').trim();
  }).filter(Boolean);
}

// ═══════════════════════════════════════════════════════════════════════
// URL Validator (client-side using fetch)
// ═══════════════════════════════════════════════════════════════════════

async function checkUrl(url) {
  if (!url || !(url.startsWith('http://') || url.startsWith('https://'))) {
    return { valid: false, status: null, error: 'No URL or invalid scheme' };
  }

  const isDoi = url.includes('doi.org');
  const timeoutMs = isDoi ? 15000 : 8000;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);

  try {
    // Try with no-cors mode first for a quick check
    const resp = await fetch(url, {
      method: 'HEAD',
      mode: 'no-cors',
      signal: controller.signal,
      redirect: 'follow',
    });
    clearTimeout(timeout);

    // no-cors returns opaque response — we can't read status
    // Success means the server accepted the request
    return { valid: true, status: 'reachable', error: null };
  } catch (err) {
    clearTimeout(timeout);

    if (err.name === 'AbortError') {
      return { valid: false, status: null, error: `Timeout after ${timeoutMs / 1000}s` };
    }

    // Try GET as fallback
    try {
      const c2 = new AbortController();
      const t2 = setTimeout(() => c2.abort(), timeoutMs);
      await fetch(url, { method: 'GET', mode: 'no-cors', signal: c2.signal, redirect: 'follow' });
      clearTimeout(t2);
      return { valid: true, status: 'reachable (GET)', error: null };
    } catch (e2) {
      clearTimeout(t2);
      return { valid: false, status: null, error: e2.message || 'Connection failed' };
    }
  }
}

async function validateUrls(entries, onProgress) {
  const results = [];
  const urlsToCheck = [];

  for (const entry of entries) {
    const url = entry.url || (entry.doi ? `https://doi.org/${entry.doi}` : '');
    if (url) urlsToCheck.push({ entry, url });
  }

  let done = 0;
  const total = urlsToCheck.length;

  // Check in batches of 5 concurrently
  const batchSize = 5;
  for (let i = 0; i < urlsToCheck.length; i += batchSize) {
    const batch = urlsToCheck.slice(i, i + batchSize);
    const batchResults = await Promise.all(
      batch.map(async ({ entry, url }) => {
        const result = await checkUrl(url);
        done++;
        if (onProgress) onProgress(done, total, entry.entry_id);
        return { ...entry, url, ...result };
      })
    );
    results.push(...batchResults);
  }

  return results;
}

// ═══════════════════════════════════════════════════════════════════════
// Classification
// ═══════════════════════════════════════════════════════════════════════

function classifyEntry(entry) {
  const reasons = [];
  let status = 'correct';

  if (entry.url_valid === false) {
    reasons.push(`Invalid URL: ${entry.url_error} (${entry.url})`);
    status = 'wrong';
  }

  // No URL at all is suspicious but not necessarily wrong
  if (!entry.url && !entry.doi) {
    status = 'unverifiable';
  }

  return { status, reasons };
}

// ═══════════════════════════════════════════════════════════════════════
// UI Logic
// ═══════════════════════════════════════════════════════════════════════

const dropZone = document.getElementById('drop-zone');
const fileInput = document.getElementById('file-input');
const progressSection = document.getElementById('progress-section');
const progressFill = document.getElementById('progress-fill');
const progressText = document.getElementById('progress-text');
const resultsSection = document.getElementById('results-section');
const resultsTable = document.getElementById('results-table');
const demoBtn = document.getElementById('demo-btn');

let allResults = [];
let currentFilter = 'all';
let startTime = 0;

// ── File Upload ──

dropZone.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', handleFileSelect);

dropZone.addEventListener('dragover', (e) => {
  e.preventDefault();
  dropZone.classList.add('drag-over');
});

dropZone.addEventListener('dragleave', () => {
  dropZone.classList.remove('drag-over');
});

dropZone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file && file.name.endsWith('.bib')) {
    processFile(file);
  } else {
    alert('Please drop a .bib file');
  }
});

function handleFileSelect(e) {
  const file = e.target.files[0];
  if (file) processFile(file);
}

async function processFile(file) {
  showProgress(true);
  setProgress(0, `Reading ${file.name}...`);

  const content = await file.text();
  setProgress(10, 'Parsing BibTeX...');

  const entries = parseBibTeX(content);
  if (entries.length === 0) {
    alert('No valid BibTeX entries found in file.');
    showProgress(false);
    return;
  }

  setProgress(20, `Found ${entries.length} entries. Checking URLs...`);
  startTime = Date.now();

  allResults = await validateUrls(entries, (done, total, currentId) => {
    const pct = 20 + Math.round((done / total) * 70);
    setProgress(pct, `Checking URL ${done}/${total}: ${currentId}`);
  });

  // Classify
  allResults = allResults.map(entry => {
    const { status, reasons } = classifyEntry(entry);
    return { ...entry, status, reasons };
  });

  setProgress(100, 'Done!');
  showProgress(false);
  renderResults();
}

// ── Demo Data ──

async function loadDemo() {
  const demoBib = `@inproceedings{demo2023correct,
  author    = {Ashish Vaswani and Noam Shazeer and Niki Parmar and Jakob Uszkoreit and Llion Jones and Aidan N. Gomez and Lukasz Kaiser and Illia Polosukhin},
  title     = {Attention Is All You Need},
  booktitle = {Advances in Neural Information Processing Systems 30},
  year      = {2017},
  url       = {https://proceedings.neurips.cc/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html}
}

@inproceedings{demo2023broken,
  author    = {John Doe and Jane Smith},
  title     = {A Groundbreaking Study},
  booktitle = {Proceedings of Some Conference},
  year      = {2023},
  url       = {https://this-url-definitely-does-not-exist-99999.com/paper.pdf}
}

@article{demo2023nourl,
  author    = {Alice Researcher},
  title     = {Important Findings in Widget Science},
  journal   = {Journal of Important Studies},
  year      = {2024}
}

@inproceedings{demo2023real,
  author    = {Jacob Devlin and Ming-Wei Chang and Kenton Lee and Kristina Toutanova},
  title     = {BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding},
  booktitle = {Proceedings of NAACL-HLT 2019},
  year      = {2019},
  url       = {https://aclanthology.org/N19-1423/}
}`;

  showProgress(true);
  setProgress(10, 'Parsing demo data...');

  const entries = parseBibTeX(demoBib);
  setProgress(20, `Found ${entries.length} entries. Checking URLs...`);
  startTime = Date.now();

  allResults = await validateUrls(entries, (done, total, currentId) => {
    const pct = 20 + Math.round((done / total) * 70);
    setProgress(pct, `Checking URL ${done}/${total}: ${currentId}`);
  });

  allResults = allResults.map(entry => {
    const { status, reasons } = classifyEntry(entry);
    return { ...entry, status, reasons };
  });

  setProgress(100, 'Done!');
  showProgress(false);
  renderResults();
}

demoBtn.addEventListener('click', loadDemo);

// ── Progress ──

function showProgress(show) {
  progressSection.classList.toggle('hidden', !show);
}

function setProgress(pct, text) {
  progressFill.style.width = pct + '%';
  progressText.textContent = text;
}

// ── Results Rendering ──

function renderResults() {
  const filtered = currentFilter === 'all'
    ? allResults
    : allResults.filter(r => r.status === currentFilter);

  // Summary counts
  const wrong = allResults.filter(r => r.status === 'wrong').length;
  const unverifiable = allResults.filter(r => r.status === 'unverifiable').length;
  const correct = allResults.filter(r => r.status === 'correct').length;
  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

  document.getElementById('summary-wrong').querySelector('.count').textContent = wrong;
  document.getElementById('summary-unverifiable').querySelector('.count').textContent = unverifiable;
  document.getElementById('summary-correct').querySelector('.count').textContent = correct;
  document.getElementById('summary-total').textContent = allResults.length;
  document.getElementById('summary-time').querySelector('.count').textContent = elapsed + 's';

  // Build cards
  resultsTable.innerHTML = filtered.map(entry => {
    const statusLabels = {
      wrong: '❌ Wrong',
      unverifiable: '⚠️ Unverifiable',
      correct: '✅ Correct',
    };
    const badgeClass = entry.status === 'wrong' ? 'fail' : entry.status === 'unverifiable' ? 'warn' : 'ok';

    const urlStatus = entry.url_valid === true
      ? '✅ URL reachable'
      : entry.url_valid === false
        ? '❌ URL failed'
        : '⊘ No URL';

    return `
      <div class="entry-card ${entry.status}">
        <div class="entry-header">
          <div>
            <span class="entry-id">${escapeHtml(entry.entry_id)}</span>
            <span class="entry-type">${entry.entry_type}</span>
          </div>
          <span class="status-badge ${badgeClass}">${statusLabels[entry.status]}</span>
        </div>
        <div class="entry-title">${escapeHtml(entry.title || '(no title)')}</div>
        <div class="entry-meta">
          <span>${urlStatus}</span>
          ${entry.year ? `<span>📅 ${entry.year}</span>` : ''}
          ${entry.authors.length ? `<span>👥 ${entry.authors.length} authors</span>` : ''}
        </div>
        ${entry.reasons.length ? `
        <ul class="entry-reasons">
          ${entry.reasons.map(r => `<li>• ${escapeHtml(r)}</li>`).join('')}
        </ul>` : ''}
      </div>
    `;
  }).join('');

  if (filtered.length === 0) {
    resultsTable.innerHTML = '<p style="text-align:center;color:var(--text-secondary);padding:24px">No entries match this filter.</p>';
  }

  resultsSection.classList.remove('hidden');
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

// ── Filter Buttons ──

document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentFilter = btn.dataset.filter;
    renderResults();
  });
});

// ── Initial State ──
setProgress(0, 'Ready. Drop a .bib file or try the demo.');
