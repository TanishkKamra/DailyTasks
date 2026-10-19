/**
 * static/js/lp_datewise.js: LP_DateWiseData frontend orchestration & async sampling execution
 */

document.addEventListener('DOMContentLoaded', () => {
  const targetTotalInput = document.getElementById('target-total-input');
  const resultColInput = document.getElementById('result-col-input');
  const seedInput = document.getElementById('seed-input');

  const dropzone = document.getElementById('dw-dropzone');
  const fileInput = document.getElementById('dw-file');
  const fileInfo = document.getElementById('dw-file-info');
  const mappingSection = document.getElementById('mapping-section');
  const sheetSelectGroup = document.getElementById('sheet-select-group');
  const dwSheetSelect = document.getElementById('dw-sheet-select');
  const datetimeColSelect = document.getElementById('datetime-col-select');
  const startBtn = document.getElementById('start-sampling-btn');
  const progressWrapper = document.getElementById('sampling-progress');
  const progressFill = document.getElementById('sampling-progress-fill');
  const progressStatus = document.getElementById('sampling-progress-status');
  const progressPct = document.getElementById('sampling-progress-pct');
  const logBox = document.getElementById('sampling-log-box');
  const resultsSection = document.getElementById('results-section');
  const samplingSummaryMsg = document.getElementById('sampling-summary-msg');
  const daySummaryTableContainer = document.getElementById('day-summary-table-container');
  const sampledPreviewTableContainer = document.getElementById('sampled-preview-table-container');
  const downloadSampledBtn = document.getElementById('download-sampled-btn');

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

    const formData = new FormData();
    formData.append('file', uploadedFile);

    try {
      showToast('Inspecting data columns...', 'info');
      const resp = await fetchWithTimeout('/lp_datewise/api/inspect', {
        method: 'POST',
        body: formData,
      }, 30000);

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || 'Failed to inspect file');
      }

      const data = await resp.json();
      if (data.sheets && data.sheets.length > 1) {
        dwSheetSelect.innerHTML = '';
        data.sheets.forEach(s => dwSheetSelect.add(new Option(s, s)));
        sheetSelectGroup.style.display = 'block';
      } else {
        sheetSelectGroup.style.display = 'none';
      }

      populateColumns(data.columns || []);
      mappingSection.style.display = 'block';
      resultsSection.style.display = 'none';
    } catch (err) {
      showToast(err.message, 'error');
    }
  }

  function populateColumns(columns) {
    datetimeColSelect.innerHTML = '';
    columns.forEach(col => {
      datetimeColSelect.add(new Option(col, col));
    });
    // Auto-select column containing date or time keywords
    const lowerCols = columns.map(c => c.toLowerCase());
    const dateKeywords = ['date', 'time', 'timestamp', 'datetime', 'created', 'call', 'txn', 'trans'];
    const bestIdx = lowerCols.findIndex(c => dateKeywords.some(kw => c.includes(kw)));
    if (bestIdx >= 0) datetimeColSelect.selectedIndex = bestIdx;
  }

  dwSheetSelect.addEventListener('change', async () => {
    if (!uploadedFile) return;
    const formData = new FormData();
    formData.append('file', uploadedFile);
    formData.append('sheet_name', dwSheetSelect.value);

    try {
      const resp = await fetchWithTimeout('/lp_datewise/api/columns', {
        method: 'POST',
        body: formData,
      }, 25000);
      if (!resp.ok) return;
      const data = await resp.json();
      populateColumns(data.columns || []);
    } catch (err) {
      console.error(err);
    }
  });

  startBtn.addEventListener('click', async () => {
    if (!uploadedFile) return;
    const datetimeCol = datetimeColSelect.value;
    if (!datetimeCol) {
      showToast('Please select the date & time column.', 'warning');
      return;
    }

    const formData = new FormData();
    formData.append('file', uploadedFile);
    formData.append('datetime_col', datetimeCol);
    formData.append('target_total', targetTotalInput.value || '10000');
    formData.append('result_column_name', resultColInput.value.trim() || 'Selected');
    formData.append('seed', seedInput.value.trim());
    if (sheetSelectGroup.style.display !== 'none') {
      formData.append('sheet_name', dwSheetSelect.value);
    }

    startBtn.disabled = true;
    progressWrapper.classList.add('active');
    logBox.style.display = 'block';
    logBox.textContent = 'Starting stratified random sampling...\n';
    resultsSection.style.display = 'none';

    try {
      const resp = await fetchWithTimeout('/lp_datewise/api/process', {
        method: 'POST',
        body: formData,
      }, 30000);

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || 'Failed to start sampling');
      }

      const data = await resp.json();
      currentJobId = data.job_id;
      pollJob(currentJobId);
    } catch (err) {
      showToast(err.message, 'error');
      startBtn.disabled = false;
      progressWrapper.classList.remove('active');
    }
  });

  function pollJob(jobId) {
    if (pollInterval) clearInterval(pollInterval);
    pollErrorCount = 0;

    pollInterval = setInterval(async () => {
      try {
        const resp = await fetchWithTimeout(`/lp_datewise/api/status/${jobId}`, {}, 8000);
        if (!resp.ok) {
          pollErrorCount++;
          if (pollErrorCount >= 5) {
            clearInterval(pollInterval);
            showToast('Lost connection to server while polling job status.', 'error');
            startBtn.disabled = false;
          }
          return;
        }
        pollErrorCount = 0;

        const job = await resp.json();
        progressFill.style.width = `${job.progress}%`;
        progressPct.textContent = `${job.progress}%`;
        progressStatus.textContent = job.progress_text || 'Sampling...';

        if (job.logs && job.logs.length > 0) {
          logBox.textContent = job.logs.join('\n');
          logBox.scrollTop = logBox.scrollHeight;
        }

        if (['completed', 'failed', 'cancelled'].includes(job.status)) {
          clearInterval(pollInterval);
          startBtn.disabled = false;

          if (job.status === 'completed' && job.has_result) {
            fetchFinalResult(jobId);
          } else {
            showToast(`Sampling failed: ${job.error || 'Unknown error'}`, 'error');
          }
        }
      } catch (err) {
        pollErrorCount++;
        console.error('Status poll error:', err);
        if (pollErrorCount >= 5) {
          clearInterval(pollInterval);
          showToast('Status polling stopped after consecutive network errors.', 'error');
          startBtn.disabled = false;
        }
      }
    }, 1000);
  }

  async function fetchFinalResult(jobId) {
    try {
      const resp = await fetchWithTimeout(`/lp_datewise/api/result/${jobId}`, {}, 20000);
      if (!resp.ok) return;
      const res = await resp.json();

      samplingSummaryMsg.innerHTML = `<strong>Sampling complete:</strong> ${res.total_selected.toLocaleString()} of ${res.total_rows.toLocaleString()} rows marked '1' in new '${resultColInput.value.trim() || 'Selected'}' column (${res.time_taken}s).`;

      renderDaySummary(res.summary_records || []);
      renderPreviewTable(res.preview_columns || [], res.preview_records || []);

      downloadSampledBtn.href = `/lp_datewise/api/download/${jobId}`;
      const ext = uploadedFile.name.toLowerCase().endsWith('.csv') ? '_sampled.xlsx' : '';
      downloadSampledBtn.setAttribute('download', ext ? uploadedFile.name.replace(/\.csv$/i, ext) : uploadedFile.name);

      resultsSection.style.display = 'block';
      showToast('Stratified sampling complete!', 'success');
    } catch (err) {
      console.error(err);
    }
  }

  function renderDaySummary(summaryRecords) {
    if (!summaryRecords || summaryRecords.length === 0) {
      daySummaryTableContainer.innerHTML = '<div style="padding: 10px; color: var(--text-muted);">No summary records.</div>';
      return;
    }
    const trs = summaryRecords.map(r => `
      <tr>
        <td style="font-weight: 600;">${r.Date || r._date || ''}</td>
        <td style="font-family: monospace; font-weight: 600;">${(r['Records Selected'] || 0).toLocaleString()}</td>
      </tr>
    `).join('');

    daySummaryTableContainer.innerHTML = `
      <table class="data-table">
        <thead><tr><th>Date</th><th>Records Selected</th></tr></thead>
        <tbody>${trs}</tbody>
      </table>
    `;
  }

  function renderPreviewTable(columns, records) {
    if (!columns || !records || records.length === 0) {
      sampledPreviewTableContainer.innerHTML = '<div style="padding: 10px; color: var(--text-muted);">No preview data.</div>';
      return;
    }

    const ths = columns.map(c => `<th>${c}</th>`).join('');
    const trs = records.map(r => {
      const tds = columns.map(c => `<td>${r[c] !== undefined ? r[c] : ''}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');

    sampledPreviewTableContainer.innerHTML = `
      <table class="data-table">
        <thead><tr>${ths}</tr></thead>
        <tbody>${trs}</tbody>
      </table>
    `;
  }

  // Universal hotkey: Ctrl+Enter to start sampling
  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      if (startBtn && !startBtn.disabled && mappingSection.style.display !== 'none') {
        startBtn.click();
      }
    }
  });
});
