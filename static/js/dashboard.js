/**
 * static/js/dashboard.js: Dashboard live logs auto-refreshing, multi-service filtering & UI logic
 */

document.addEventListener('DOMContentLoaded', () => {
  const logsContainer = document.getElementById('logs-container');
  const refreshBtn = document.getElementById('refresh-logs-btn');
  const autoRefreshToggle = document.getElementById('auto-refresh-toggle');
  const serviceFilter = document.getElementById('service-filter-select');

  let refreshTimer = null;
  let allEntries = [];
  let consecutiveErrors = 0;

  async function fetchLogs() {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10000);
      const resp = await fetch('/api/logs', { signal: controller.signal });
      clearTimeout(timeoutId);

      if (!resp.ok) throw new Error(`HTTP error ${resp.status}`);
      const data = await resp.json();
      allEntries = data.entries || [];
      consecutiveErrors = 0;
      renderLogs();
    } catch (err) {
      console.error('Logs fetch error:', err);
      consecutiveErrors++;
      if (allEntries.length === 0) {
        logsContainer.innerHTML = `<div style="text-align: center; color: var(--text-muted); padding: 20px;">Could not connect to server (${err.message}). Retrying...</div>`;
      }
      if (consecutiveErrors >= 5 && refreshTimer) {
        clearInterval(refreshTimer);
        refreshTimer = null;
        if (autoRefreshToggle) autoRefreshToggle.checked = false;
        const dot = document.getElementById('live-dot');
        if (dot) dot.style.display = 'none';
        if (typeof showToast === 'function') {
          showToast('Auto-refresh paused due to repeated connection errors.', 'warning');
        }
      }
    }
  }

  function renderLogs() {
    const filterVal = serviceFilter ? serviceFilter.value : 'All';
    let filtered = allEntries;

    if (filterVal !== 'All') {
      filtered = allEntries.filter((e) => {
        const svc = (e.service || '').toUpperCase();
        if (filterVal === 'PAN OCR') return svc.includes('PAN');
        if (filterVal === 'Aadhaar OCR') return svc.includes('AADHAAR');
        if (filterVal === 'Policy Reader') return svc.includes('POLICY');
        if (filterVal === 'LP_Delete') return svc.includes('DELETE') || svc.includes('KEYWORD');
        if (filterVal === 'LP_DateWiseData') return svc.includes('DATEWISE') || svc.includes('SAMPL');
        return true;
      });
    }

    if (!filtered || filtered.length === 0) {
      logsContainer.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px;">No requests logged for the selected filter.</div>';
      return;
    }

    logsContainer.innerHTML = filtered.map(entry => {
      const isOk = entry.status < 400;
      const statusBadge = `<span class="badge ${isOk ? 'badge-ok' : 'badge-error'}">${entry.status}</span>`;
      const svcRaw = (entry.service || 'API').toUpperCase();

      let svcColor = '#C4661A';
      if (svcRaw.includes('AADHAAR')) svcColor = '#8A5C00';
      else if (svcRaw.includes('POLICY')) svcColor = '#276136';
      else if (svcRaw.includes('DELETE')) svcColor = '#A8521A';
      else if (svcRaw.includes('DATEWISE')) svcColor = '#1C5A8A';

      const svcTag = `<span style="font-weight: 700; color: ${svcColor}; font-size: 0.8rem; text-transform: uppercase;">${entry.service || 'API'}</span>`;

      const details = entry.details_dict || {};
      const chips = Object.entries(details)
        .filter(([k, v]) => v && v !== '[]' && !['method', 'path', 'status'].includes(k))
        .map(([k, v]) => `
          <div class="log-chip">
            <span class="log-chip-key">${k.replace(/_/g, ' ')}:</span>
            <span class="log-chip-val">${v}</span>
          </div>
        `).join('');

      return `
        <div class="log-entry ${isOk ? 'log-entry-ok' : 'log-entry-err'}">
          <div class="log-entry-header">
            <div class="log-entry-meta">
              ${svcTag}
              <span class="log-method-tag">${entry.method || 'POST'}</span>
              <span class="log-path">${entry.path || ''}</span>
            </div>
            <span class="log-timestamp">${entry.timestamp}</span>
          </div>
          <div class="log-stats" style="margin-bottom: ${chips ? '8px' : '0'};">
            ${statusBadge}
            <span class="log-duration">${Math.round(entry.duration)} ms</span>
            <span class="log-reqid">#${entry.request_id}</span>
          </div>
          ${chips ? `<div class="log-chips">${chips}</div>` : ''}
        </div>
      `;
    }).join('');
  }

  if (serviceFilter) {
    serviceFilter.addEventListener('change', renderLogs);
  }

  fetchLogs();

  function setupAutoRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    const dot = document.getElementById('live-dot');
    if (dot) dot.style.display = autoRefreshToggle && autoRefreshToggle.checked ? 'inline-block' : 'none';

    if (autoRefreshToggle && autoRefreshToggle.checked) {
      consecutiveErrors = 0;
      refreshTimer = setInterval(() => {
        if (!document.hidden) {
          fetchLogs();
        }
      }, 3000);
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && autoRefreshToggle && autoRefreshToggle.checked) {
      fetchLogs();
    }
  });

  if (autoRefreshToggle) {
    autoRefreshToggle.addEventListener('change', () => {
      const dot = document.getElementById('live-dot');
      if (dot) dot.style.display = autoRefreshToggle.checked ? 'inline-block' : 'none';
      setupAutoRefresh();
    });
    setupAutoRefresh();
  }

  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      consecutiveErrors = 0;
      fetchLogs();
    });
  }

  window.addEventListener('beforeunload', () => {
    if (refreshTimer) {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }
  });
});
