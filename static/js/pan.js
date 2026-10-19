/**
 * static/js/pan.js: PAN Card OCR frontend dropzone, camera snap & batch file management
 */

document.addEventListener('DOMContentLoaded', () => {
  const dropzone = document.getElementById('pan-dropzone');
  const fileInput = document.getElementById('cards');
  const fileListPreview = document.getElementById('file-list-preview');
  const cameraBtn = document.getElementById('pan-camera-btn');

  let selectedFiles = [];

  function escapeHtml(str) {
    return (str || '').replace(/[&<>"']/g, m => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
    }[m]));
  }

  function syncInputFiles() {
    if (!fileInput) return;
    const dt = new DataTransfer();
    selectedFiles.forEach(f => dt.items.add(f));
    fileInput.files = dt.files;
  }

  function renderFileList() {
    if (!fileListPreview) return;
    fileListPreview.innerHTML = '';
    if (selectedFiles.length === 0) return;

    const countLabel = document.createElement('div');
    countLabel.style.cssText = 'font-size:0.85rem; font-weight:600; color:var(--text-main); margin-top:12px; margin-bottom:6px;';
    countLabel.textContent = `Selected Files (${selectedFiles.length}):`;
    fileListPreview.appendChild(countLabel);

    const container = document.createElement('div');
    container.style.cssText = 'display:flex; flex-wrap:wrap; gap:8px;';

    selectedFiles.forEach((file, index) => {
      const badge = document.createElement('div');
      badge.style.cssText = 'display:inline-flex; align-items:center; gap:8px; padding:6px 12px; border-radius:var(--radius-sm); background:var(--bg-card-inner); border:1px solid var(--border-light); font-size:0.85rem; color:var(--text-main);';

      const sizeKb = (file.size / 1024).toFixed(0);
      badge.innerHTML = `<span>${escapeHtml(file.name)} <span style="color:var(--text-muted); font-size:0.75rem;">(${sizeKb} KB)</span></span>`;

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.innerHTML = '&times;';
      removeBtn.style.cssText = 'background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:1.15rem; line-height:1; padding:0 2px; transition:color 0.15s;';
      removeBtn.title = 'Remove file';
      removeBtn.addEventListener('mouseenter', () => removeBtn.style.color = 'var(--accent-red)');
      removeBtn.addEventListener('mouseleave', () => removeBtn.style.color = 'var(--text-muted)');
      removeBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        selectedFiles.splice(index, 1);
        syncInputFiles();
        renderFileList();
      });

      badge.appendChild(removeBtn);
      container.appendChild(badge);
    });

    fileListPreview.appendChild(container);
  }

  function addFiles(fileList) {
    if (!fileList || fileList.length === 0) return;
    for (let i = 0; i < fileList.length; i++) {
      const f = fileList[i];
      // Avoid duplicate exact file additions
      if (!selectedFiles.some(existing => existing.name === f.name && existing.size === f.size)) {
        selectedFiles.push(f);
      }
    }
    syncInputFiles();
    renderFileList();
  }

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
        addFiles(e.dataTransfer.files);
      }
    });

    fileInput.addEventListener('change', () => {
      if (fileInput.files && fileInput.files.length > 0) {
        addFiles(fileInput.files);
      }
    });
  }

  if (cameraBtn) {
    cameraBtn.addEventListener('click', () => {
      if (window.openCameraCaptureModal) {
        window.openCameraCaptureModal((capturedBlob) => {
          const file = new File([capturedBlob], `pancard_camera_${Date.now()}.jpg`, { type: 'image/jpeg' });
          addFiles([file]);
          if (window.showToast) showToast('Photo attached. Click Extract to process!', 'success');
        });
      }
    });
  }
});
