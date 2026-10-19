/**
 * static/js/policy.js: Insurance Policy Reader UI logic
 * Parallel extraction, auto-detect insurer mode, clean workbook option,
 * side-by-side PDF preview modal, one-click copy buttons, and export handlers.
 */

document.addEventListener("DOMContentLoaded", () => {
  const providerSelect = document.getElementById("provider-select");
  const apiKeyGroup = document.getElementById("api-key-group");
  const apiKeyInput = document.getElementById("api-key");
  const azureConfig = document.getElementById("azure-config");
  const azureEndpoint = document.getElementById("azure-endpoint");
  const azureDeployment = document.getElementById("azure-deployment");
  const azureVersion = document.getElementById("azure-version");
  const customConfig = document.getElementById("custom-config");
  const customBaseUrl = document.getElementById("custom-base-url");
  const customModelName = document.getElementById("custom-model-name");

  const companyModeSelect = document.getElementById("company-mode-select");
  const companyModeNote = document.getElementById("company-mode-note");
  const autoDetectToggle = document.getElementById("auto-detect-toggle");
  const freshWorkbookToggle = document.getElementById("fresh-workbook-toggle");

  const dropzone = document.getElementById("policy-dropzone");
  const fileInput = document.getElementById("policy-files");
  const fileListPreview = document.getElementById("policy-file-list");

  const startBtn = document.getElementById("start-analysis-btn");
  const stopBtn = document.getElementById("stop-analysis-btn");
  const maxPagesInput = document.getElementById("max-pages");
  const showRawToggle = document.getElementById("show-raw-toggle");

  const progressWrapper = document.getElementById("policy-progress");
  const progressFill = document.getElementById("policy-progress-fill");
  const progressStatus = document.getElementById("policy-progress-status");
  const progressPct = document.getElementById("policy-progress-pct");
  const logBox = document.getElementById("policy-log-box");

  const resultsSection = document.getElementById("results-section");
  const resultsContainer = document.getElementById("policy-results-container");
  const summaryTableBody = document.getElementById("summary-table-body");
  const downloadExcelBtn = document.getElementById("download-excel-btn");
  const downloadJsonBtn = document.getElementById("download-json-btn");
  const downloadCsvBtn = document.getElementById("download-csv-btn");

  let selectedFiles = [];
  const fileBlobMap = new Map();
  let currentJobId = null;
  let pollInterval = null;
  let pollErrorCount = 0;
  let lastCompletedResult = null;

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
      if (err.name === "AbortError") {
        throw new Error("Request timed out while contacting the server.");
      }
      throw err;
    }
  }

  // Cleanup timers on unload/navigate
  window.addEventListener("beforeunload", () => {
    if (pollInterval) clearInterval(pollInterval);
  });
  window.addEventListener("pagehide", () => {
    if (pollInterval) clearInterval(pollInterval);
  });

  // Preset Configurations
  const PRESETS = {
    AzureAI: {
      baseUrl: (window.POLICY_PROVIDERS?.AzureAI?.base_url) || "",
      model: (window.POLICY_PROVIDERS?.AzureAI?.model) || "gpt-5-mini",
      apiVersion: "2025-04-01-preview",
      keyPlaceholder: "Leave blank to use the server key",
    },
    "Google Gemini (Free)": {
      model: "gemini-1.5-flash",
      keyPlaceholder: "Enter your Google AI Studio API key",
    },
    "Groq (Free, very fast)": {
      model: "llama3-8b-8192",
      keyPlaceholder: "Enter your Groq API key (gsk_...)",
    },
    OpenAI: {
      model: "gpt-4o-mini",
      keyPlaceholder: "Enter your OpenAI API key (sk-...)",
    },
    "Azure OpenAI": {
      baseUrl: "https://<your-resource>.openai.azure.com/",
      model: "gpt-4o-mini",
      apiVersion: "2025-04-01-preview",
      keyPlaceholder: "Enter Azure OpenAI API Key",
    },
    "Custom / Other": {
      baseUrl: "http://localhost:11434/v1",
      model: "llama3",
      keyPlaceholder: "API key (if required)",
    },
  };

  function updateProviderUI() {
    const p = providerSelect.value;
    const preset = PRESETS[p] || {};

    apiKeyInput.placeholder = preset.keyPlaceholder || "Enter API Key";

    if (p === "AzureAI") {
      azureConfig.style.display = "block";
      customConfig.style.display = "none";
      azureEndpoint.value = preset.baseUrl || "";
      azureDeployment.value = preset.model || "";
      azureVersion.value = preset.apiVersion || "2025-04-01-preview";
    } else if (p === "Azure OpenAI") {
      azureConfig.style.display = "block";
      customConfig.style.display = "none";
      azureEndpoint.value = preset.baseUrl || "";
      azureDeployment.value = preset.model || "";
      azureVersion.value = preset.apiVersion || "2025-04-01-preview";
    } else if (p === "Custom / Other") {
      azureConfig.style.display = "none";
      customConfig.style.display = "block";
      customBaseUrl.value = preset.baseUrl || "";
      customModelName.value = preset.model || "";
    } else {
      azureConfig.style.display = "none";
      customConfig.style.display = "none";
    }
  }

  function updateCompanyModeNote() {
    const mode = companyModeSelect.value;
    if (mode === "ZUNO") {
      companyModeNote.textContent = "ZUNO Mode: Normalizes full vehicle number and customer name; extracts policy type and proposer section details.";
    } else if (mode === "HDFC ERGO") {
      companyModeNote.textContent = "HDFC ERGO Mode: Normalizes spaced policy numbers (e.g. '2312 2012...') and detects business type.";
    } else if (mode === "TATA AIG") {
      companyModeNote.textContent = "TATA AIG Mode: Computes OD Net Premium (Own Damage + Add-on + Others) and TP Net Premium (Liability + E row) from Schedule of Premium.";
    } else if (mode === "GO DIGIT") {
      companyModeNote.textContent = "GO DIGIT Mode: Extracts policy schedule details and routes to GoDigitPolicies worksheet.";
    } else if (mode === "ICICI Lombard") {
      companyModeNote.textContent = "ICICI Lombard Mode: Normalizes policy numbers, extracts vehicle & premium details, and routes to ICICIPolicies worksheet.";
    } else {
      companyModeNote.textContent = "";
    }
  }

  providerSelect.addEventListener("change", updateProviderUI);
  companyModeSelect.addEventListener("change", updateCompanyModeNote);
  updateProviderUI();
  updateCompanyModeNote();

  // Dropzone handling
  if (dropzone && fileInput) {
    dropzone.addEventListener("click", () => fileInput.click());

    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });

    ["dragleave", "dragend"].forEach((type) => {
      dropzone.addEventListener(type, () => dropzone.classList.remove("dragover"));
    });

    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        handleFiles(Array.from(e.dataTransfer.files));
      }
    });

    fileInput.addEventListener("change", () => {
      if (fileInput.files && fileInput.files.length > 0) {
        handleFiles(Array.from(fileInput.files));
      }
    });
  }

  function renderPolicyFileList() {
    fileListPreview.innerHTML = "";
    if (selectedFiles.length === 0) {
      startBtn.innerHTML = "<span>Analyse Policy Documents</span>";
      return;
    }

    fileBlobMap.clear();
    selectedFiles.forEach((f) => fileBlobMap.set(f.name, f));

    const countLabel = document.createElement("div");
    countLabel.style.cssText = "font-size:0.85rem; font-weight:600; color:var(--text-main); margin-top:12px; margin-bottom:6px;";
    countLabel.textContent = `Selected Documents (${selectedFiles.length}):`;
    fileListPreview.appendChild(countLabel);

    const container = document.createElement("div");
    container.style.cssText = "display:flex; flex-wrap:wrap; gap:8px;";

    selectedFiles.forEach((file, index) => {
      const badge = document.createElement("div");
      badge.style.cssText = "display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:var(--radius-sm); background:var(--bg-card-inner); border:1px solid var(--border-light); font-size:0.85rem; color:var(--text-main);";

      const sizeMb = (file.size / (1024 * 1024)).toFixed(1);
      const nameText = file.name.length > 35 ? file.name.substring(0, 32) + "..." : file.name;
      badge.innerHTML = `<span>${nameText} <span style="color:var(--text-muted); font-size:0.75rem;">(${sizeMb} MB)</span></span>`;

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.innerHTML = "&times;";
      removeBtn.style.cssText = "background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:1.15rem; line-height:1; padding:0 2px;";
      removeBtn.title = "Remove document";
      removeBtn.addEventListener("mouseenter", () => removeBtn.style.color = "var(--accent-red)");
      removeBtn.addEventListener("mouseleave", () => removeBtn.style.color = "var(--text-muted)");
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        selectedFiles.splice(index, 1);
        renderPolicyFileList();
      });

      badge.appendChild(removeBtn);
      container.appendChild(badge);
    });

    fileListPreview.appendChild(container);
    startBtn.innerHTML = `<span>Analyse ${selectedFiles.length} Document${selectedFiles.length > 1 ? "s" : ""}</span>`;
  }

  function handleFiles(files) {
    const valid = files.filter((f) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf");
    if (valid.length === 0) {
      showToast("Please select valid PDF policy documents.", "warning");
      return;
    }

    valid.forEach(f => {
      if (!selectedFiles.some(existing => existing.name === f.name && existing.size === f.size)) {
        selectedFiles.push(f);
      }
    });

    renderPolicyFileList();
  }

  // Universal hotkey: Ctrl+Enter to trigger policy analysis
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if (startBtn && !startBtn.disabled && selectedFiles.length > 0) {
        startBtn.click();
      }
    }
  });

  // Start analysis
  startBtn.addEventListener("click", async () => {
    if (selectedFiles.length === 0) {
      showToast("Please upload at least one PDF file first.", "warning");
      return;
    }

    const formData = new FormData();
    selectedFiles.forEach((f) => formData.append("files", f));

    formData.append("provider", providerSelect.value);
    formData.append("api_key", apiKeyInput.value.trim());
    formData.append("company_mode", companyModeSelect.value);
    formData.append("auto_detect", autoDetectToggle && autoDetectToggle.checked ? "true" : "false");
    formData.append("fresh_workbook", freshWorkbookToggle && freshWorkbookToggle.checked ? "true" : "false");
    formData.append("max_pages", maxPagesInput.value || "0");

    const p = providerSelect.value;
    if (p === "AzureAI" || p === "Azure OpenAI") {
      formData.append("azure_endpoint", azureEndpoint.value.trim());
      formData.append("azure_deployment", azureDeployment.value.trim());
      formData.append("azure_version", azureVersion.value.trim());
    } else if (p === "Custom / Other") {
      formData.append("custom_base_url", customBaseUrl.value.trim());
      formData.append("custom_model_name", customModelName.value.trim());
    }

    startBtn.disabled = true;
    stopBtn.style.display = "inline-flex";
    progressWrapper.classList.add("active");
    logBox.style.display = "block";
    logBox.textContent = "Starting parallel policy extraction engine...\n";
    resultsSection.style.display = "none";

    try {
      const resp = await fetchWithTimeout("/policy/api/process", {
        method: "POST",
        body: formData,
      }, 30000);

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.error || "Failed to start extraction");
      }

      const data = await resp.json();
      currentJobId = data.job_id;
      startPolling(currentJobId);
    } catch (err) {
      showToast(err.message, "error");
      startBtn.disabled = false;
      stopBtn.style.display = "none";
      progressWrapper.classList.remove("active");
    }
  });

  // Stop analysis
  stopBtn.addEventListener("click", async () => {
    if (!currentJobId) return;
    try {
      await fetchWithTimeout(`/policy/api/cancel/${currentJobId}`, { method: "POST" }, 10000);
      showToast("Cancellation requested.", "info");
    } catch (err) {
      console.error(err);
    }
  });

  function startPolling(jobId) {
    if (pollInterval) clearInterval(pollInterval);
    pollErrorCount = 0;

    pollInterval = setInterval(async () => {
      try {
        const resp = await fetchWithTimeout(`/policy/api/status/${jobId}`, {}, 8000);
        if (!resp.ok) {
          pollErrorCount++;
          if (pollErrorCount >= 5) {
            clearInterval(pollInterval);
            showToast("Lost connection to server while polling status.", "error");
            startBtn.disabled = false;
            stopBtn.style.display = "none";
          }
          return;
        }
        pollErrorCount = 0;

        const job = await resp.json();
        progressFill.style.width = `${job.progress}%`;
        progressPct.textContent = `${job.progress}%`;
        progressStatus.textContent = job.progress_text || "Processing...";

        if (job.logs && job.logs.length > 0) {
          logBox.textContent = job.logs.join("\n");
          logBox.scrollTop = logBox.scrollHeight;
        }

        if (["completed", "failed", "cancelled"].includes(job.status)) {
          clearInterval(pollInterval);
          startBtn.disabled = false;
          stopBtn.style.display = "none";

          if (job.status === "completed" && job.result) {
            lastCompletedResult = job.result;
            renderAllResults(job.result);
            showToast("Parallel policy extraction completed!", "success");
          } else if (job.status === "cancelled") {
            showToast("Extraction was stopped by user.", "warning");
          } else {
            showToast(`Extraction failed: ${job.error || "Unknown error"}`, "error");
          }
        }
      } catch (err) {
        pollErrorCount++;
        console.error("Status poll error:", err);
        if (pollErrorCount >= 5) {
          clearInterval(pollInterval);
          showToast("Status polling stopped after consecutive network errors.", "error");
          startBtn.disabled = false;
          stopBtn.style.display = "none";
        }
      }
    }, 1000);
  }

  function renderAllResults(resultData) {
    const policies = resultData.policies || [];
    if (policies.length === 0) {
      resultsContainer.innerHTML = '<div style="padding: 20px; color: var(--text-muted);">No policies were extracted.</div>';
      return;
    }

    renderSummaryTable(policies);

    resultsContainer.innerHTML = "";
    policies.forEach((p, idx) => {
      const card = createPolicyCard(p, idx + 1);
      resultsContainer.appendChild(card);
    });

    resultsSection.style.display = "block";
    if (resultData.download_url) {
      downloadExcelBtn.href = resultData.download_url;
      downloadExcelBtn.innerHTML = `<span>Download ${escapeHtml(resultData.saved_workbook || "Excel")}</span>`;
    } else {
      downloadExcelBtn.href = "/policy/api/download_excel";
    }
  }

  function renderSummaryTable(policies) {
    summaryTableBody.innerHTML = policies.map((p) => {
      const isErr = !!p.error;
      const needsRev = p.needs_review || false;
      const statusBadge = isErr
        ? '<span class="status-badge status-error">Error</span>'
        : needsRev
        ? '<span class="status-badge status-warning">Review</span>'
        : '<span class="status-badge status-ok">OK</span>';

      const fn = p.file_name || "";
      const viewPdfBtn = fileBlobMap.has(fn)
        ? `<button class="btn btn-secondary" style="padding: 3px 8px; font-size: 0.75rem;" onclick="viewSideBySide('${escapeHtml(fn)}')">Split View</button>`
        : "-";

      return `
        <tr>
          <td style="font-weight: 600;">${escapeHtml(fn)}</td>
          <td>${statusBadge}</td>
          <td style="font-family: monospace;">
            ${escapeHtml(p.policy_number || "-")}
            ${p.policy_number ? `<button class="copy-btn" onclick="copyToClipboard('${escapeHtml(p.policy_number)}', this)">Copy</button>` : ""}
          </td>
          <td>${escapeHtml(p.customer_name || "-")}</td>
          <td style="font-family: monospace;">
            ${escapeHtml(p.vehicle_number || "-")}
            ${p.vehicle_number ? `<button class="copy-btn" onclick="copyToClipboard('${escapeHtml(p.vehicle_number)}', this)">Copy</button>` : ""}
          </td>
          <td>${escapeHtml(p.detected_insurer || p.insurer || "-")}</td>
          <td style="font-weight: 600; color: var(--primary);">${escapeHtml(p.gross_premium || "-")}</td>
          <td>${viewPdfBtn}</td>
        </tr>
      `;
    }).join("");
  }

  window.viewSideBySide = function(filename) {
    const file = fileBlobMap.get(filename);
    if (!file) {
      showToast("PDF file object not found in memory.", "warning");
      return;
    }
    const blobUrl = URL.createObjectURL(file);
    const policyData = (lastCompletedResult && lastCompletedResult.policies)
      ? lastCompletedResult.policies.find((p) => p.file_name === filename)
      : null;

    if (window.openSplitPdfModal) {
      window.openSplitPdfModal(blobUrl, policyData, filename);
    }
  };

  const FULL_DISPLAY_FIELDS = [
    ["customer_name", "Customer Name"],
    ["policy_number", "Policy Number"],
    ["insurer", "Insurer"],
    ["business_type", "Business Type"],
    ["policy_type", "Policy Type"],
    ["vehicle_number", "Vehicle Number"],
    ["make", "Make"],
    ["model_variant", "Model & Variant"],
    ["engine_number", "Engine Number"],
    ["chassis_number", "Chassis Number"],
    ["year_of_manufacture", "Year of Mfg"],
    ["fuel_type", "Fuel Type"],
    ["cc_kwh", "Cubic Capacity / kWh"],
    ["rto_name", "RTO Location"],
    ["passenger", "Passenger Capacity"],
    ["gvw", "GVW"],
    ["idv", "IDV"],
    ["od_net_premium", "OD Net Premium"],
    ["tp_net_premium", "TP Net Premium"],
    ["gst", "GST"],
    ["net_premium", "Net Premium"],
    ["gross_premium", "Gross Premium"],
    ["policy_issue_date", "Policy Issue Date"],
    ["policy_start", "Policy Start Date"],
    ["policy_end", "Policy End Date"],
    ["txn_date", "Transaction Date"],
    ["current_ncb", "Current NCB"],
    ["previous_ncb", "Previous NCB"],
    ["address", "Address"],
    ["city", "City"],
    ["state", "State"],
    ["pin", "PIN Code"],
    ["phone_number", "Phone Number"],
    ["mobile_number", "Mobile Number"],
    ["email", "Email Address"],
    ["dob", "Date of Birth"],
  ];

  function createPolicyCard(data, index) {
    const card = document.createElement("div");
    card.className = "card";
    card.style.marginBottom = "20px";

    const fn = data.file_name || `Document #${index}`;
    const needsReview = data.needs_review || false;
    const hasError = !!data.error;

    let badgeHtml = "";
    if (hasError) {
      badgeHtml = '<span class="status-badge status-error">Error</span>';
    } else if (needsReview) {
      badgeHtml = '<span class="status-badge status-warning">Needs Review</span>';
    } else {
      badgeHtml = '<span class="status-badge status-ok">Looks Good</span>';
    }

    const splitBtnHtml = fileBlobMap.has(fn)
      ? `<button class="btn btn-secondary" style="padding: 4px 10px; font-size: 0.8rem;" onclick="viewSideBySide('${escapeHtml(fn)}')">Split View</button>`
      : "";

    let inner = `
      <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border-light); padding-bottom: 12px; margin-bottom: 14px; flex-wrap: wrap; gap: 10px;">
        <h3 style="margin: 0; font-size: 1.1rem; font-weight: 600; color: var(--text-main);">${escapeHtml(fn)}</h3>
        <div style="display: flex; align-items: center; gap: 10px;">
          ${splitBtnHtml}
          ${badgeHtml}
        </div>
      </div>
    `;

    if (hasError) {
      inner += `<div class="alert alert-danger">${escapeHtml(data.error)}</div>`;
      card.innerHTML = inner;
      return card;
    }

    if (data.exclusions && String(data.exclusions).trim().toLowerCase() !== "none") {
      inner += `<div class="alert alert-info" style="margin-bottom: 12px;"><strong>Exclusions:</strong> ${escapeHtml(data.exclusions)}</div>`;
    }

    if (needsReview) {
      const reason = data.review_reason || "One or more critical fields could not be extracted.";
      inner += `<div class="alert alert-warning" style="margin-bottom: 14px;"><strong>Review reason:</strong> ${escapeHtml(reason)}</div>`;
    }

    // Grid of all fields with one-click copy buttons
    inner += `<div class="field-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 12px; margin-bottom: 14px;">`;
    FULL_DISPLAY_FIELDS.forEach(([key, label]) => {
      const val = data[key];
      const hasVal = val !== undefined && val !== null && String(val).trim() !== "" && !["none", "null", "n/a"].includes(String(val).toLowerCase());
      const copyBtn = hasVal
        ? `<button class="copy-btn" onclick="copyToClipboard('${escapeHtml(String(val))}', this)" title="Copy ${label}">Copy</button>`
        : "";

      inner += `
        <div class="field-item">
          <div class="field-label">${label}</div>
          <div class="field-value ${hasVal ? "" : "missing"}" style="${!hasVal ? "color: var(--text-muted); font-style: italic;" : ""}">
            <span>${hasVal ? escapeHtml(String(val)) : "Not found"}</span>
            ${copyBtn}
          </div>
        </div>
      `;
    });
    inner += `</div>`;

    // Expanders for Raw Extracted Text & Raw Model Output
    if (data.raw_text && showRawToggle && showRawToggle.checked) {
      inner += `
        <details class="expander" style="margin-top: 10px;">
          <summary>Extracted PDF text (sent to model)</summary>
          <div class="log-box" style="margin-top: 8px;"><pre style="margin: 0; white-space: pre-wrap; font-size: 0.82rem;">${escapeHtml(data.raw_text.slice(0, 3000))}${data.raw_text.length > 3000 ? "…" : ""}</pre></div>
        </details>
      `;
    }

    if (data._debug_raw_model_output) {
      inner += `
        <details class="expander" style="margin-top: 8px;">
          <summary>Raw AI response (before merging)</summary>
          <div class="log-box" style="margin-top: 8px;"><pre style="margin: 0; white-space: pre-wrap; font-size: 0.82rem;">${escapeHtml(data._debug_raw_model_output)}</pre></div>
        </details>
      `;
    }

    card.innerHTML = inner;
    return card;
  }

  // JSON and CSV download buttons
  if (downloadJsonBtn) {
    downloadJsonBtn.addEventListener("click", () => {
      if (!lastCompletedResult) return;
      const blob = new Blob([JSON.stringify(lastCompletedResult, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "extracted_policies.json";
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  if (downloadCsvBtn) {
    downloadCsvBtn.addEventListener("click", () => {
      if (!lastCompletedResult || !lastCompletedResult.policies) return;
      const policies = lastCompletedResult.policies;
      const headers = FULL_DISPLAY_FIELDS.map(([k]) => k);
      const csvRows = [headers.join(",")];
      policies.forEach((p) => {
        const row = headers.map((h) => `"${String(p[h] || "").replace(/"/g, '""')}"`);
        csvRows.push(row.join(","));
      });
      const blob = new Blob([csvRows.join("\n")], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "extracted_policies.csv";
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }
});
