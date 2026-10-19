/**
 * static/js/filesharing.js: Frontend logic for the File Share module.
 * Supports virtual folders, uploaded files, and live read-only Hosted Folders.
 */

let currentFolderId = null; // For virtual uploaded folders
let currentHostedFolderId = null; // For hosted folders
let currentHostedSubpath = ""; // Relative subpath inside hosted folder
let manifest = { folders: [], files: [], hosted_folders: [] };
let lastFocusedElement = null;

// Inline SVG Icons Map (Single-color currentColor vector set)
const SVG_ICONS = {
  folder: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>',
  hosted: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.55a11 11 0 0 1 14.08 0"></path><path d="M1.42 9a16 16 0 0 1 21.16 0"></path><path d="M8.53 16.11a6 6 0 0 1 6.95 0"></path><line x1="12" y1="20" x2="12.01" y2="20"></line></svg>',
  pdf: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>',
  doc: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line></svg>',
  xls: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="8" y1="13" x2="16" y2="17"></line><line x1="16" y1="13" x2="8" y2="17"></line></svg>',
  image: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>',
  zip: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path><polyline points="3.27 6.96 12 12.01 20.73 6.96"></polyline><line x1="12" y1="22.08" x2="12" y2="12"></line></svg>',
  code: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="16 18 22 12 16 6"></polyline><polyline points="8 6 2 12 8 18"></polyline></svg>',
  audio: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 18V5l12-2v13"></path><circle cx="6" cy="18" r="3"></circle><circle cx="18" cy="16" r="3"></circle></svg>',
  video: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="23 7 16 12 23 17 23 7"></polygon><rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect></svg>',
  file: '<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>',
  edit: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"></path><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path></svg>',
  delete: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>',
  download: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>',
  zipAction: '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"></path></svg>',
};

// Fetch helper with AbortController timeout
async function fetchWithTimeout(resource, options = {}, timeoutMs = 25000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(resource, {
      ...options,
      signal: controller.signal,
    });
    clearTimeout(id);
    return response;
  } catch (error) {
    clearTimeout(id);
    if (error.name === "AbortError") {
      throw new Error("Request timed out. The server took too long to respond.");
    }
    throw error;
  }
}

// ── Initialization ───────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  loadManifest();
  setupUploadZone();
  setupKeyboardNavigation();
});

// ── API Helpers ──────────────────────────────────────────────────────

async function loadManifest() {
  try {
    const res = await fetchWithTimeout("/filesharing/files", {}, 15000);
    if (!res.ok) throw new Error(`HTTP error ${res.status}`);
    manifest = await res.json();
    manifest.folders = manifest.folders || [];
    manifest.files = manifest.files || [];
    manifest.hosted_folders = manifest.hosted_folders || [];
    if (manifest.warning && typeof showToast === "function") showToast(manifest.warning, "error");
    renderView();
  } catch (err) {
    console.error("Failed to load manifest:", err);
    if (typeof showToast === "function") {
      showToast(`Failed to load files manifest: ${err.message}`, "error");
    }
  }
}

// ── Render View ──────────────────────────────────────────────────────

function renderView() {
  const grid = document.getElementById("file-grid");
  const emptyState = document.getElementById("empty-state");
  const emptyStateText = document.getElementById("empty-state-text");
  const hostedSection = document.getElementById("hosted-section");
  const hostedGrid = document.getElementById("hosted-grid");
  const uploadsTitle = document.getElementById("uploads-section-title");
  const uploadZone = document.getElementById("upload-zone");
  const btnNewFolder = document.getElementById("btn-new-folder");
  const btnUpload = document.getElementById("btn-upload");

  grid.innerHTML = "";
  if (hostedGrid) hostedGrid.innerHTML = "";

  // 1. Inside a Hosted Folder Tree View
  if (currentHostedFolderId !== null) {
    if (uploadZone) uploadZone.style.display = "none";
    if (btnNewFolder) btnNewFolder.style.display = "none";
    if (btnUpload) btnUpload.style.display = "none";
    if (hostedSection) hostedSection.style.display = "none";
    if (uploadsTitle) uploadsTitle.style.display = "none";

    const hf = manifest.hosted_folders.find(h => h.id === currentHostedFolderId);
    if (!hf || hf.unavailable || !hf.tree) {
      emptyState.style.display = "block";
      emptyStateText.textContent = "This hosted folder is unavailable on the host PC.";
      grid.style.display = "none";
      renderBreadcrumbs();
      return;
    }

    const targetNode = findTreeNode(hf.tree, currentHostedSubpath);
    if (!targetNode || !targetNode.children || targetNode.children.length === 0) {
      emptyState.style.display = "block";
      emptyStateText.textContent = "This hosted directory is empty.";
      grid.style.display = "none";
    } else {
      emptyState.style.display = "none";
      grid.style.display = "grid";

      targetNode.children.forEach(item => {
        if (item.type === "dir") {
          const card = document.createElement("div");
          card.className = "file-card";
          card.tabIndex = 0;
          card.role = "button";
          card.setAttribute("aria-label", `Open directory ${item.name}`);
          card.ondblclick = () => navigateHosted(hf.id, item.path);
          card.onclick = () => navigateHosted(hf.id, item.path);
          card.onkeydown = (e) => {
            if (e.key === "Enter") navigateHosted(hf.id, item.path);
          };
          card.innerHTML = `
            <div class="card-actions">
              <button onclick="event.stopPropagation(); downloadHostedZip('${hf.id}', '${escapeAttr(item.path)}')" title="Download as ZIP" aria-label="Download ${escapeAttr(item.name)} as ZIP">${SVG_ICONS.zipAction} <span>ZIP</span></button>
            </div>
            <div class="card-icon">${SVG_ICONS.folder}</div>
            <div class="card-name">${escapeHtml(item.name)}</div>
            <div class="card-meta">Hosted Folder (Live)</div>
          `;
          grid.appendChild(card);
        } else {
          const sizeStr = formatSize(item.size);
          const card = document.createElement("div");
          card.className = "file-card";
          card.tabIndex = 0;
          card.role = "button";
          card.setAttribute("aria-label", `Download file ${item.name}`);
          card.ondblclick = () => downloadHostedFile(hf.id, item.path);
          card.onclick = () => downloadHostedFile(hf.id, item.path);
          card.onkeydown = (e) => {
            if (e.key === "Enter") downloadHostedFile(hf.id, item.path);
          };
          card.innerHTML = `
            <div class="card-actions">
              <button onclick="event.stopPropagation(); downloadHostedFile('${hf.id}', '${escapeAttr(item.path)}')" title="Download" aria-label="Download ${escapeAttr(item.name)}">${SVG_ICONS.download} <span>Download</span></button>
            </div>
            <div class="card-icon">${fileIcon(item.name)}</div>
            <div class="card-name">${escapeHtml(item.name)}</div>
            <div class="card-meta">${sizeStr} (Live)</div>
          `;
          grid.appendChild(card);
        }
      });
    }

    renderBreadcrumbs();
    return;
  }

  // 2. Normal View (Root or Virtual Folder)
  if (uploadZone) uploadZone.style.display = "block";
  if (btnNewFolder) btnNewFolder.style.display = "inline-block";
  if (btnUpload) btnUpload.style.display = "inline-block";

  const isRoot = currentFolderId === null;

  // Render Hosted Folders (only at root)
  if (isRoot && manifest.hosted_folders && manifest.hosted_folders.length > 0) {
    if (hostedSection) hostedSection.style.display = "block";
    if (uploadsTitle) uploadsTitle.style.display = "flex";

    manifest.hosted_folders.forEach(hf => {
      const card = document.createElement("div");
      card.className = `file-card ${hf.unavailable ? "unavailable" : ""}`;
      card.tabIndex = 0;
      card.role = "button";
      card.setAttribute("aria-label", `Hosted folder ${hf.name}`);

      if (hf.unavailable) {
        card.innerHTML = `
          <div class="card-actions">
            <button class="danger" onclick="event.stopPropagation(); unregisterHostedFolder('${hf.id}')" title="Unregister" aria-label="Unregister ${escapeAttr(hf.name)}">${SVG_ICONS.delete} <span>Unregister</span></button>
          </div>
          <div class="card-icon">${SVG_ICONS.hosted}</div>
          <div class="card-name">${escapeHtml(hf.name)} <span class="badge-unavailable">Unavailable</span></div>
          <div class="card-meta" title="${escapeAttr(hf.path)}">Path missing on host</div>
        `;
      } else {
        card.ondblclick = () => navigateHosted(hf.id, "");
        card.onclick = () => navigateHosted(hf.id, "");
        card.onkeydown = (e) => {
          if (e.key === "Enter") navigateHosted(hf.id, "");
        };
        card.innerHTML = `
          <div class="card-actions">
            <button onclick="event.stopPropagation(); downloadHostedZip('${hf.id}', '')" title="Download all as ZIP" aria-label="Download all as ZIP">${SVG_ICONS.zipAction} <span>ZIP</span></button>
            <button class="danger" onclick="event.stopPropagation(); unregisterHostedFolder('${hf.id}')" title="Unregister" aria-label="Unregister ${escapeAttr(hf.name)}">${SVG_ICONS.delete} <span>Unregister</span></button>
          </div>
          <div class="card-icon">${SVG_ICONS.hosted}</div>
          <div class="card-name">${escapeHtml(hf.name)} <span class="badge-live">LIVE</span></div>
          <div class="card-meta" title="${escapeAttr(hf.path)}">Live Host Folder</div>
        `;
      }
      hostedGrid.appendChild(card);
    });
  } else {
    if (hostedSection) hostedSection.style.display = "none";
    if (uploadsTitle) uploadsTitle.style.display = "none";
  }

  // Render Virtual Folders & Uploaded Files
  const folders = manifest.folders.filter(f => (f.parent_id || null) === currentFolderId);
  const files = manifest.files.filter(f => (f.parent_id || null) === currentFolderId);

  const hasHosted = isRoot && manifest.hosted_folders && manifest.hosted_folders.length > 0;
  if (folders.length === 0 && files.length === 0) {
    if (!hasHosted) {
      emptyState.style.display = "block";
      emptyStateText.textContent = "This folder is empty";
      grid.style.display = "none";
    } else {
      emptyState.style.display = "none";
      grid.style.display = "grid";
    }
  } else {
    emptyState.style.display = "none";
    grid.style.display = "grid";

    // Render virtual folders
    folders.forEach(folder => {
      const card = document.createElement("div");
      card.className = "file-card";
      card.tabIndex = 0;
      card.role = "button";
      card.setAttribute("aria-label", `Open virtual folder ${folder.name}`);
      card.ondblclick = () => navigateTo(folder.id);
      card.onkeydown = (e) => {
        if (e.key === "Enter") navigateTo(folder.id);
      };
      card.innerHTML = `
        <div class="card-actions">
          <button onclick="event.stopPropagation(); showRenameModal('${folder.id}', '${escapeAttr(folder.name)}')" title="Rename" aria-label="Rename folder">${SVG_ICONS.edit}</button>
          <button class="danger" onclick="event.stopPropagation(); deleteItem('${folder.id}')" title="Delete" aria-label="Delete folder">${SVG_ICONS.delete}</button>
        </div>
        <div class="card-icon">${SVG_ICONS.folder}</div>
        <div class="card-name">${escapeHtml(folder.name)}</div>
        <div class="card-meta">Folder</div>
      `;
      grid.appendChild(card);
    });

    // Render uploaded files
    files.forEach(file => {
      const sizeStr = formatSize(file.size);
      const card = document.createElement("div");
      card.className = "file-card";
      card.tabIndex = 0;
      card.role = "button";
      card.setAttribute("aria-label", `Download file ${file.name}`);
      card.ondblclick = () => downloadFile(file.id);
      card.onkeydown = (e) => {
        if (e.key === "Enter") downloadFile(file.id);
      };
      card.innerHTML = `
        <div class="card-actions">
          <button onclick="event.stopPropagation(); downloadFile('${file.id}')" title="Download" aria-label="Download file">${SVG_ICONS.download}</button>
          <button onclick="event.stopPropagation(); showRenameModal('${file.id}', '${escapeAttr(file.name)}')" title="Rename" aria-label="Rename file">${SVG_ICONS.edit}</button>
          <button class="danger" onclick="event.stopPropagation(); deleteItem('${file.id}')" title="Delete" aria-label="Delete file">${SVG_ICONS.delete}</button>
        </div>
        <div class="card-icon">${fileIcon(file.name)}</div>
        <div class="card-name">${escapeHtml(file.name)}</div>
        <div class="card-meta">${sizeStr} · ${file.uploader || "Unknown"}</div>
      `;
      grid.appendChild(card);
    });
  }

  renderBreadcrumbs();
}

// ── Tree Traversal Helper ────────────────────────────────────────────

function findTreeNode(tree, targetPath) {
  if (!targetPath || targetPath === "") return tree;
  const parts = targetPath.replace(/\\/g, "/").split("/").filter(Boolean);
  let current = tree;
  for (const part of parts) {
    if (!current || !current.children) return null;
    current = current.children.find(c => c.type === "dir" && c.name === part);
    if (!current) return null;
  }
  return current;
}

// ── Navigation ───────────────────────────────────────────────────────

function navigateTo(folderId) {
  currentHostedFolderId = null;
  currentHostedSubpath = "";
  currentFolderId = folderId || null;
  renderView();
}

function navigateHosted(hfId, subpath = "") {
  currentFolderId = null;
  currentHostedFolderId = hfId;
  currentHostedSubpath = subpath || "";
  renderView();
}

function renderBreadcrumbs() {
  const bar = document.getElementById("breadcrumbs");

  if (currentHostedFolderId !== null) {
    const hf = manifest.hosted_folders.find(h => h.id === currentHostedFolderId);
    const hfName = hf ? hf.name : "Hosted Folder";

    let html = '<a onclick="navigateTo(null)">Root</a>';
    html += `<span class="sep">/</span><a onclick="navigateHosted('${currentHostedFolderId}', '')">Host: ${escapeHtml(hfName)}</a>`;

    if (currentHostedSubpath) {
      const parts = currentHostedSubpath.split("/").filter(Boolean);
      let cumulative = "";
      parts.forEach(part => {
        cumulative = cumulative ? `${cumulative}/${part}` : part;
        html += `<span class="sep">/</span><a onclick="navigateHosted('${currentHostedFolderId}', '${escapeAttr(cumulative)}')">${escapeHtml(part)}</a>`;
      });
    }

    html += ` <button class="btn btn-secondary" style="margin-left:auto; font-size:0.75rem; padding:3px 10px; display:inline-flex; align-items:center; gap:4px;" onclick="downloadHostedZip('${currentHostedFolderId}', '${escapeAttr(currentHostedSubpath)}')">${SVG_ICONS.zipAction} <span>Download Folder as ZIP</span></button>`;

    bar.innerHTML = html;
    return;
  }

  const trail = [];
  let id = currentFolderId;

  while (id) {
    const folder = manifest.folders.find(f => f.id === id);
    if (!folder) break;
    trail.unshift(folder);
    id = folder.parent_id || null;
  }

  let html = '<a onclick="navigateTo(null)">Root</a>';
  trail.forEach(f => {
    html += `<span class="sep">/</span><a onclick="navigateTo('${f.id}')">${escapeHtml(f.name)}</a>`;
  });
  bar.innerHTML = html;
}

// ── Hosted Folder Operations ─────────────────────────────────────────

function showHostFolderModal() {
  lastFocusedElement = document.activeElement;
  const modal = document.getElementById("host-folder-modal");
  document.getElementById("host-folder-path").value = "";
  document.getElementById("host-folder-name").value = "";
  const errDiv = document.getElementById("host-folder-error");
  if (errDiv) {
    errDiv.style.display = "none";
    errDiv.textContent = "";
  }
  modal.classList.add("active");
  setTimeout(() => document.getElementById("host-folder-path").focus(), 100);
}

async function submitHostFolder() {
  const path = document.getElementById("host-folder-path").value.trim();
  const name = document.getElementById("host-folder-name").value.trim();
  const errDiv = document.getElementById("host-folder-error");

  if (!path) {
    if (errDiv) {
      errDiv.textContent = "Folder path is required.";
      errDiv.style.display = "block";
    }
    return;
  }

  try {
    const res = await fetchWithTimeout("/filesharing/host-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, name: name || undefined }),
    }, 15000);

    const data = await res.json();
    if (!res.ok) {
      const errMsg = data.error || "Failed to host folder.";
      if (errDiv) {
        errDiv.textContent = errMsg;
        errDiv.style.display = "block";
      }
      if (typeof showToast === "function") showToast(errMsg, "error");
      return;
    }

    closeModal("host-folder-modal");
    if (typeof showToast === "function") showToast(`Folder hosted: "${data.name}"`, "success");
    loadManifest();
  } catch (err) {
    console.error("Host folder error:", err);
    if (errDiv) {
      errDiv.textContent = err.message || "Network error while hosting folder.";
      errDiv.style.display = "block";
    }
  }
}

function downloadHostedFile(hfId, relPath) {
  window.location.href = `/filesharing/host-download/${hfId}?path=${encodeURIComponent(relPath)}`;
}

function downloadHostedZip(hfId, relPath = "") {
  const q = relPath ? `?path=${encodeURIComponent(relPath)}` : "";
  window.location.href = `/filesharing/host-download-zip/${hfId}${q}`;
}

async function unregisterHostedFolder(hfId) {
  if (!confirm("Unregister this hosted folder? (This only removes live access; no files on disk will be deleted.)")) {
    return;
  }

  try {
    const res = await fetchWithTimeout(`/filesharing/host-folder/${hfId}`, { method: "DELETE" }, 10000);
    const data = await res.json();
    if (!res.ok) {
      if (typeof showToast === "function") showToast(data.error || "Failed to unregister", "error");
      return;
    }

    if (currentHostedFolderId === hfId) {
      currentHostedFolderId = null;
      currentHostedSubpath = "";
    }
    if (typeof showToast === "function") showToast("Hosted folder unregistered", "info");
    loadManifest();
  } catch (err) {
    console.error("Unregister failed:", err);
    if (typeof showToast === "function") showToast(`Failed to unregister: ${err.message}`, "error");
  }
}

// ── Virtual Folder Operations ────────────────────────────────────────

function showNewFolderModal() {
  lastFocusedElement = document.activeElement;
  document.getElementById("new-folder-name").value = "";
  document.getElementById("new-folder-modal").classList.add("active");
  setTimeout(() => document.getElementById("new-folder-name").focus(), 100);
}

async function createFolder() {
  const name = document.getElementById("new-folder-name").value.trim();
  if (!name) return;

  try {
    const res = await fetchWithTimeout("/filesharing/folder/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, parent_id: currentFolderId }),
    }, 10000);
    if (!res.ok) {
      const data = await res.json();
      if (typeof showToast === "function") showToast(data.error || "Failed to create folder", "error");
      return;
    }
    closeModal("new-folder-modal");
    loadManifest();
  } catch (err) {
    console.error("Failed to create folder:", err);
    if (typeof showToast === "function") showToast(`Create folder failed: ${err.message}`, "error");
  }
}

// ── Upload ───────────────────────────────────────────────────────────

function setupUploadZone() {
  const zone = document.getElementById("upload-zone");
  const fileInput = document.getElementById("file-input");
  if (!zone || !fileInput) return;

  zone.addEventListener("click", () => fileInput.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });

  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("drag-over");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("drag-over");
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });

  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) uploadFiles(fileInput.files);
    fileInput.value = "";
  });
}

async function uploadFiles(fileList) {
  for (const file of fileList) {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("uploader", "WebUI");
    if (currentFolderId) fd.append("folder_id", currentFolderId);

    try {
      const res = await fetchWithTimeout("/filesharing/upload", { method: "POST", body: fd }, 60000);
      if (!res.ok) {
        const data = await res.json();
        if (typeof showToast === "function") showToast(data.error || "Upload failed", "error");
      }
    } catch (err) {
      console.error("Upload failed:", err);
      if (typeof showToast === "function") showToast(`Upload error: ${err.message}`, "error");
    }
  }
  loadManifest();
}

function downloadFile(fileId) {
  window.location.href = `/filesharing/download/${fileId}`;
}

// ── Rename ───────────────────────────────────────────────────────────

function showRenameModal(itemId, currentName) {
  lastFocusedElement = document.activeElement;
  document.getElementById("rename-item-id").value = itemId;
  document.getElementById("rename-input").value = currentName;
  document.getElementById("rename-modal").classList.add("active");
  setTimeout(() => document.getElementById("rename-input").focus(), 100);
}

async function renameItem() {
  const itemId = document.getElementById("rename-item-id").value;
  const newName = document.getElementById("rename-input").value.trim();
  if (!newName) return;

  try {
    const res = await fetchWithTimeout(`/filesharing/rename/${itemId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName }),
    }, 10000);
    if (!res.ok) {
      const data = await res.json();
      if (typeof showToast === "function") showToast(data.error || "Rename failed", "error");
      return;
    }
    closeModal("rename-modal");
    loadManifest();
  } catch (err) {
    console.error("Rename failed:", err);
    if (typeof showToast === "function") showToast(`Rename error: ${err.message}`, "error");
  }
}

// ── Delete ───────────────────────────────────────────────────────────

async function deleteItem(itemId) {
  if (!confirm("Delete this item? (Folders are deleted recursively.)")) return;

  try {
    const res = await fetchWithTimeout(`/filesharing/delete/${itemId}`, { method: "DELETE" }, 10000);
    if (!res.ok) {
      const data = await res.json();
      if (typeof showToast === "function") showToast(data.error || "Delete failed", "error");
      return;
    }
    loadManifest();
  } catch (err) {
    console.error("Delete failed:", err);
    if (typeof showToast === "function") showToast(`Delete error: ${err.message}`, "error");
  }
}

// ── Modal & Keyboard Accessibility ───────────────────────────────────

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove("active");
  if (lastFocusedElement && typeof lastFocusedElement.focus === "function") {
    lastFocusedElement.focus();
    lastFocusedElement = null;
  }
}

function setupKeyboardNavigation() {
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const activeModal = document.querySelector(".fs-modal-overlay.active");
      if (activeModal) {
        closeModal(activeModal.id);
      }
    }
  });

  document.addEventListener("click", (e) => {
    if (e.target.classList.contains("fs-modal-overlay")) {
      closeModal(e.target.id);
    }
  });
}

// ── Utility ──────────────────────────────────────────────────────────

function escapeHtml(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttr(str) {
  return String(str || "")
    .replace(/&/g, "&amp;")
    .replace(/'/g, "\\'")
    .replace(/"/g, "&quot;");
}

function formatSize(bytes) {
  if (!bytes || bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i++;
  }
  return `${size.toFixed(i > 0 ? 1 : 0)} ${units[i]}`;
}

function fileIcon(filename) {
  const ext = (filename || "").split(".").pop().toLowerCase();
  switch (ext) {
    case "pdf":
      return SVG_ICONS.pdf;
    case "doc":
    case "docx":
      return SVG_ICONS.doc;
    case "xls":
    case "xlsx":
    case "csv":
      return SVG_ICONS.xls;
    case "jpg":
    case "jpeg":
    case "png":
    case "gif":
    case "svg":
    case "webp":
      return SVG_ICONS.image;
    case "zip":
    case "rar":
    case "7z":
    case "tar":
    case "gz":
      return SVG_ICONS.zip;
    case "py":
    case "js":
    case "html":
    case "css":
    case "json":
    case "xml":
    case "ts":
      return SVG_ICONS.code;
    case "mp3":
    case "wav":
    case "ogg":
    case "m4a":
      return SVG_ICONS.audio;
    case "mp4":
    case "mkv":
    case "avi":
    case "mov":
      return SVG_ICONS.video;
    default:
      return SVG_ICONS.file;
  }
}
