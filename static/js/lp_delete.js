/**
 * static/js/lp_delete.js: LP_Delete frontend wizard & async execution
 */

document.addEventListener('DOMContentLoaded', () => {
  const dropzone = document.getElementById('lp-dropzone');
  const fileInput = document.getElementById('lp-file');
  const fileInfo = document.getElementById('lp-file-info');
  const configSection = document.getElementById('config-section');
  const sheetEditSelect = document.getElementById('sheet-edit-select');
  const sheetKeywordsSelect = document.getElementById('sheet-keywords-select');
  const sheetMetaInfo = document.getElementById('sheet-meta-info');
  const targetColSelect = document.getElementById('target-col-select');
  const resultColNameInput = document.getElementById('result-col-name');
  const wholeWordToggle = document.getElementById('whole-word-toggle');
  const startBtn = document.getElementById('start-process-btn');
  const stopBtn = document.getElementById('stop-process-btn');
  const progressWrapper = document.getElementById('process-progress');
  const progressFill = document.getElementById('process-progress-fill');
  const progressStatus = document.getElementById('process-progress-status');
  const progressPct = document.getElementById('process-progress-pct');
  const logBox = document.getElementById('process-log-box');
  const resultsSection = document.getElementById('results-section');
  const resultSummaryMsg = document.getElementById('result-summary-msg');
  const previewTableContainer = document.getElementById('preview-table-container');
  const downloadFileBtn = document.getElementById('download-file-btn');

  let uploadedFile = null;
  let currentJobId = null;
  let pollInterval = null;
  let pollErrorCount = 0;

  async function fetchWithTimeout(resource, options = {}, timeoutMs = 25000) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(resource, {
        ...options,
        signal: controller.signal
      });
      clearTimeout(id);
      return response;
    } catch (err) {
      clearTimeout(id);
      if (err.name === 'AbortError') {
        throw new Error('Request timed out while contacting the server.');
      }
      throw err;
    }
  }

  // Cleanup polling interval on unload/navigate
  window.addEventListener('beforeunload', () => {
    if (pollInterval) clearInterval(pollInterval);
  });
  window.addEventListener('pagehide', () => {
    if (pollInterval) clearInterval(pollInterval);
  });

  if (dropzone && fileInput) {
    dropzone.addEventListener('click', () => fileInput.click());

    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });

    ['dragleave', 'dragend'].forEach(type => {
      dropzone.addEventListener(type, () => dropzone.classList.remove('dragover'));
    });

    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        fileInput.files = e.dataTransfer.files;
        handleFileSelected();
      }
    });

    fileInput.addEventListener('change', handleFileSelected);
  }

  async function handleFileSelected() {
    if (!fileInput.files || fileInput.files.length === 0) return;
    uploadedFile = fileInput.files[0];
    fileInfo.innerHTML = `<strong>Selected file:</strong> ${uploadedFile.name} (${(uploadedFile.size / 1024 / 1024).toFixed(2)} MB)`;

    // Inspect file sheets and columns
    const formData = new FormData();
    formData.append('file', uploadedFile);

    try {
      showToast('Inspecting workbook sheets...', 'info');
      const resp = await fetchWithTimeout('/lp_delete/api/inspect', {
        method: 'POST',
        body: formData,
      }, 30000);

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || 'Failed to inspect workbook');
      }

      const data = await resp.json();
      populateSheets(data.sheets || []);
      configSection.style.display = 'block';
      resultsSection.style.display = 'none';
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  function populateSheets(sheets) {
    sheetEditSelect.innerHTML = '';
    sheetKeywordsSelect.innerHTML = '';

    sheets.forEach((s) => {
      const opt1 = new Option(s, s);
      const opt2 = new Option(s, s);
      sheetEditSelect.add(opt1);
      sheetKeywordsSelect.add(opt2);
    });

    // Smart sheet selection
    const lowerSheets = sheets.map(s => s.toLowerCase());
    const kwIdx = lowerSheets.findIndex(s => s.includes('keyword') || s.includes('key') || s.includes('filter') || s.includes('search') || s.includes('del'));
    if (kwIdx >= 0) {
      sheetKeywordsSelect.selectedIndex = kwIdx;
      // Select the other sheet for edit
      const editIdx = sheets.findIndex((_, i) => i !== kwIdx);
      if (editIdx >= 0) sheetEditSelect.selectedIndex = editIdx;
    } else if (sheets.length > 1) {
      sheetKeywordsSelect.selectedIndex = 1;
    }

    loadSheetColumns();
  }

  sheetEditSelect.addEventListener('change', loadSheetColumns);

  async function loadSheetColumns() {
    if (!uploadedFile) return;
    const sheetName = sheetEditSelect.value;
    const formData = new FormData();
    formData.append('file', uploadedFile);
    formData.append('sheet_name', sheetName);

    try {
      const resp = await fetchWithTimeout('/lp_delete/api/columns', {
        method: 'POST',
        body: formData,
      }, 25000);
      if (!resp.ok) return;
      const data = await resp.json();

      targetColSelect.innerHTML = '';
      const cols = data.columns || [];
      cols.forEach(col => {
        targetColSelect.add(new Option(col, col));
      });

      // Auto-select text/description/narration column
      const lowerCols = cols.map(c => c.toLowerCase());
      const colKeywords = ['text', 'narration', 'desc', 'description', 'detail', 'name', 'remark', 'comment', 'title'];
      const bestColIdx = lowerCols.findIndex(c => colKeywords.some(kw => c.includes(kw)));
      if (bestColIdx >= 0) targetColSelect.selectedIndex = bestColIdx;

      sheetMetaInfo.textContent = `'${sheetName}' has ${data.row_count.toLocaleString()} rows and ${cols.length} columns.`;
    } catch (err) {
      console.error(err);
    }
  }

  startBtn.addEventListener('click', async () => {
    if (!uploadedFile) return;
    const newCol = resultColNameInput.value.trim();
    if (!newCol) {
      showToast('Please provide a name for the new result column.', 'warning');
      return;
    }

    const formData = new FormData();
    formData.append('file', uploadedFile);
    formData.append('sheet_edit', sheetEditSelect.value);
    formData.append('sheet_keywords', sheetKeywordsSelect.value);
    formData.append('target_col', targetColSelect.value);
    formData.append('new_col_name', newCol);
    formData.append('whole_word', wholeWordToggle.checked ? 'true' : 'false');

    startBtn.disabled = true;
    stopBtn.style.display = 'inline-flex';
    progressWrapper.classList.add('active');
    logBox.style.display = 'block';
    logBox.textContent = 'Starting processing...\n';
    resultsSection.style.display = 'none';

    try {
      const resp = await fetchWithTimeout('/lp_delete/api/process', {
        method: 'POST',
        body: formData,
      }, 30000);

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || 'Failed to start processing');
      }

      const data = await resp.json();
      currentJobId = data.job_id;
      pollJob(currentJobId);
    } catch (err) {
      showToast(err.message, 'error');
      startBtn.disabled = false;
      stopBtn.style.display = 'none';
      progressWrapper.classList.remove('active');
    }
  });

  stopBtn.addEventListener('click', async () => {
    if (!currentJobId) return;
    try {
      await fetchWithTimeout(`/lp_delete/api/cancel/${currentJobId}`, { method: 'POST' }, 10000);
      showToast('Stop requested.', 'info');
    } catch (err) {
      console.error(err);
    }
  });

  function pollJob(jobId) {
    if (pollInterval) clearInterval(pollInterval);
    pollErrorCount = 0;

    pollInterval = setInterval(async () => {
      try {
        const resp = await fetchWithTimeout(`/lp_delete/api/status/${jobId}`, {}, 8000);
        if (!resp.ok) {
          pollErrorCount++;
          if (pollErrorCount >= 5) {
            clearInterval(pollInterval);
            showToast('Lost connection to server while polling job status.', 'error');
            startBtn.disabled = false;
            stopBtn.style.display = 'none';
          }
          return;
        }
        pollErrorCount = 0;

        const job = await resp.json();
        progressFill.style.width = `${job.progress}%`;
        progressPct.textContent = `${job.progress}%`;
        progressStatus.textContent = job.progress_text || 'Processing...';

        if (job.logs && job.logs.length > 0) {
          logBox.textContent = job.logs.join('\n');
          logBox.scrollTop = logBox.scrollHeight;
        }

        if (['completed', 'failed', 'cancelled'].includes(job.status)) {
          clearInterval(pollInterval);
          startBtn.disabled = false;
          stopBtn.style.display = 'none';

          if (job.status === 'completed' && job.has_result) {
            fetchFinalResult(jobId);
          } else if (job.status === 'cancelled') {
            showToast('Processing stopped by user.', 'warning');
          } else {
            showToast(`Processing failed: ${job.error}`, 'error');
          }
        }
      } catch (err) {
        pollErrorCount++;
        console.error('Status poll error:', err);
        if (pollErrorCount >= 5) {
          clearInterval(pollInterval);
          showToast('Status polling stopped after consecutive network errors.', 'error');
          startBtn.disabled = false;
          stopBtn.style.display = 'none';
        }
      }
    }, 1000);
  }

  async function fetchFinalResult(jobId) {
    try {
      const resp = await fetchWithTimeout(`/lp_delete/api/result/${jobId}`, {}, 20000);
      if (!resp.ok) return;
      const res = await resp.json();

      resultSummaryMsg.innerHTML = `<strong>${res.match_count.toLocaleString()}</strong> of ${res.total_rows.toLocaleString()} rows matched keywords (${res.keyword_count} keywords searched in ${res.time_taken}s).`;
      renderPreviewTable(res.preview_columns, res.preview_records);

      downloadFileBtn.href = `/lp_delete/api/download/${jobId}`;
      downloadFileBtn.setAttribute('download', `updated_${uploadedFile.name}`);

      resultsSection.style.display = 'block';
      showToast('LP_Delete completed successfully!', 'success');
    } catch (err) {
      console.error(err);
    }
  }

  function renderPreviewTable(columns, records) {
    if (!columns || !records || records.length === 0) {
      previewTableContainer.innerHTML = '<div style="padding: 10px; color: var(--text-muted);">No preview data available.</div>';
      return;
    }

    const ths = columns.map(c => `<th>${c}</th>`).join('');
    const trs = records.map(r => {
      const tds = columns.map(c => `<td>${r[c] !== undefined ? r[c] : ''}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');

    previewTableContainer.innerHTML = `
      <table class="data-table">
        <thead><tr>${ths}</tr></thead>
        <tbody>${trs}</tbody>
      </table>
    `;
  }

  // Universal hotkey: Ctrl+Enter to start processing
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      if (startBtn && !startBtn.disabled && configSection && configSection.style.display !== 'none') {
        startBtn.click();
      }
    }
  });
});
