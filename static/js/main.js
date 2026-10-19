/**
 * static/js/main.js: Global scripts for Theme switcher, Recent Batches drawer,
 * Camera modal stream, Split PDF modal, Toast notifications, and Copy-to-clipboard.
 */

// Global helper for toast notifications
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast ${type === 'error' ? 'alert-danger' : type === 'success' ? 'alert-success' : type === 'warning' ? 'alert-warning' : ''}`;
  toast.textContent = message;
  container.appendChild(toast);

  setTimeout(() => {
    toast.style.transition = 'opacity 420ms cubic-bezier(0.32,0.72,0,1), transform 420ms cubic-bezier(0.32,0.72,0,1)';
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(12px) scale(0.97)';
    setTimeout(() => toast.remove(), 420);
  }, 3500);
}

// Global copy-to-clipboard helper
async function copyToClipboard(text, elem = null) {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(String(text).trim());
    showToast(`Copied to clipboard: "${text}"`, 'success');
    if (elem) {
      const orig = elem.innerHTML;
      elem.innerHTML = 'Copied';
      setTimeout(() => { elem.innerHTML = orig; }, 1500);
    }
  } catch (err) {
    console.error('Clipboard copy error:', err);
    showToast('Failed to copy to clipboard', 'warning');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  // ── Theme Manager ────────────────────────────────────────────────────────
  const themeToggleBtn = document.getElementById('theme-toggle-btn');
  const themeText = document.getElementById('theme-toggle-text');

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('ocr_hub_theme', theme);
    if (themeText) {
      themeText.textContent = theme === 'dark' ? 'Light' : 'Dark';
    }
  }

  const currentTheme = localStorage.getItem('ocr_hub_theme') || 'light';
  applyTheme(currentTheme);

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', () => {
      const active = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(active === 'dark' ? 'light' : 'dark');
    });
  }

  // ── Nav Dropdown Click Toggle (mobile-safe) ─────────────────────────────
  document.querySelectorAll('.navbtn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const menu = btn.parentElement.querySelector('.dropdown-menu');
      if (!menu) return;
      const isOpen = menu.classList.contains('open');
      document.querySelectorAll('.dropdown-menu.open').forEach(m => m.classList.remove('open'));
      if (!isOpen) menu.classList.add('open');
      btn.setAttribute('aria-expanded', !isOpen);
      e.stopPropagation();
    });
    btn.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        btn.click();
      }
      if (e.key === 'Escape') {
        const menu = btn.parentElement.querySelector('.dropdown-menu');
        if (menu) {
          menu.classList.remove('open');
          btn.setAttribute('aria-expanded', 'false');
        }
      }
    });
  });
  document.addEventListener('click', () => {
    document.querySelectorAll('.dropdown-menu.open').forEach(m => m.classList.remove('open'));
    document.querySelectorAll('.navbtn').forEach(b => b.setAttribute('aria-expanded', 'false'));
  });

  // ── Recent Batches Drawer ────────────────────────────────────────────────
  const openDrawerBtn = document.getElementById('open-drawer-btn');
  const closeDrawerBtn = document.getElementById('close-drawer-btn');
  const drawerOverlay = document.getElementById('drawer-overlay');
  const drawerContainer = document.getElementById('recent-drawer');
  const drawerJobsList = document.getElementById('drawer-jobs-list');

  function openDrawer() {
    drawerOverlay.classList.add('active');
    drawerContainer.classList.add('active');
    fetchRecentJobs();
  }

  function closeDrawer() {
    drawerOverlay.classList.remove('active');
    drawerContainer.classList.remove('active');
  }

  if (openDrawerBtn) openDrawerBtn.addEventListener('click', openDrawer);
  if (closeDrawerBtn) closeDrawerBtn.addEventListener('click', closeDrawer);
  if (drawerOverlay) drawerOverlay.addEventListener('click', closeDrawer);

  async function fetchRecentJobs() {
    if (!drawerJobsList) return;
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10000);
      const resp = await fetch('/api/recent_jobs', { signal: controller.signal });
      clearTimeout(timeoutId);
      if (!resp.ok) throw new Error('Failed to fetch recent jobs');
      const data = await resp.json();
      renderRecentJobs(data.jobs || []);
    } catch (err) {
      drawerJobsList.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px;">No recent job history found.</div>';
    }
  }

  function renderRecentJobs(jobs) {
    if (!jobs || jobs.length === 0) {
      drawerJobsList.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 20px;">No jobs recorded in this session.</div>';
      return;
    }

    drawerJobsList.innerHTML = jobs.map(j => {
      const isComplete = j.status === 'completed';
      const isFailed = j.status === 'failed';
      const badgeClass = isComplete ? 'badge-ok' : isFailed ? 'badge-error' : 'badge-warning';

      let downloadLink = '';
      if (isComplete) {
        if (j.type === 'policy_reader') {
          downloadLink = `<a class="btn btn-secondary" href="/policy/api/download_excel" style="padding: 4px 8px; font-size: 0.78rem;">Download Excel</a>`;
        } else if (j.type === 'lp_delete') {
          downloadLink = `<a class="btn btn-secondary" href="/lp_delete/api/download/${j.id}" style="padding: 4px 8px; font-size: 0.78rem;">Download Excel</a>`;
        } else if (j.type === 'lp_datewise') {
          downloadLink = `<a class="btn btn-secondary" href="/lp_datewise/api/download/${j.id}" style="padding: 4px 8px; font-size: 0.78rem;">Download Excel</a>`;
        }
      }

      return `
        <div style="background: var(--bg-hover); border: 1px solid var(--border-light); border-radius: var(--radius-md); padding: 12px 14px; margin-bottom: 8px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
            <strong style="font-size: 0.9rem; color: var(--text-main); text-transform: capitalize;">${j.type ? j.type.replace('_', ' ') : 'Job'}</strong>
            <span class="badge ${badgeClass}">${j.status}</span>
          </div>
          <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 8px;">
            Progress: ${j.progress}% | ${j.progress_text || ''}
          </div>
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 0.75rem; color: var(--text-muted); font-family: monospace;">#${j.id}</span>
            ${downloadLink}
          </div>
        </div>
      `;
    }).join('');
  }

  // ── Split PDF Side-by-Side Modal ─────────────────────────────────────────
  const splitModal = document.getElementById('split-modal');
  const closeSplitModalBtn = document.getElementById('close-split-modal-btn');
  const splitPdfFrame = document.getElementById('split-pdf-frame');
  const splitFieldsContainer = document.getElementById('split-fields-container');
  const splitModalTitle = document.getElementById('split-modal-title');

  window.openSplitPdfModal = function(pdfUrl, fieldsData, filename = 'Document') {
    if (!splitModal) return;
    splitModalTitle.textContent = `Side-by-Side: ${filename}`;
    splitPdfFrame.src = pdfUrl;

    if (fieldsData && typeof fieldsData === 'object') {
      const items = Object.entries(fieldsData)
        .filter(([k, v]) => v && typeof v !== 'object' && !['raw_text', '_debug_raw_model_output'].includes(k))
        .map(([k, v]) => `
          <div style="border-bottom: 1px solid var(--border-light); padding: 8px 0; display: flex; justify-content: space-between; align-items: center;">
            <span style="font-size: 0.78rem; text-transform: uppercase; color: var(--text-muted); font-weight: 600;">${k.replace(/_/g, ' ')}</span>
            <div style="display: flex; align-items: center; gap: 6px;">
              <span style="font-size: 0.88rem; font-weight: 500; color: var(--text-main); font-family: monospace;">${v}</span>
              <button class="copy-btn" onclick="copyToClipboard('${v}', this)" title="Copy field">Copy</button>
            </div>
          </div>
        `).join('');

      splitFieldsContainer.innerHTML = `
        <h4 style="margin: 0 0 12px; font-size: 1rem; color: var(--text-main);">Extracted Fields</h4>
        <div style="display: flex; flex-direction: column;">${items}</div>
      `;
    } else {
      splitFieldsContainer.innerHTML = '<div style="color: var(--text-muted); padding: 20px;">No field data available.</div>';
    }

    splitModal.classList.add('active');
  };

  function closeSplitModal() {
    if (splitModal) {
      splitModal.classList.remove('active');
      splitPdfFrame.src = '';
    }
  }

  if (closeSplitModalBtn) {
    closeSplitModalBtn.addEventListener('click', closeSplitModal);
  }

  // ── Camera Modal Stream ──────────────────────────────────────────────────
  const cameraModal = document.getElementById('camera-modal');
  const closeCameraModalBtn = document.getElementById('close-camera-modal-btn');
  const cameraVideo = document.getElementById('camera-video');
  const cameraCanvas = document.getElementById('camera-canvas');
  const shutterBtn = document.getElementById('shutter-btn');
  const switchCameraBtn = document.getElementById('switch-camera-btn');

  let currentStream = null;
  let useFacingMode = 'environment';
  let activeCaptureCallback = null;

  window.openCameraCaptureModal = async function(onCaptured) {
    activeCaptureCallback = onCaptured;
    cameraModal.classList.add('active');
    await startCameraStream();
  };

  async function startCameraStream() {
    if (currentStream) {
      currentStream.getTracks().forEach(t => t.stop());
    }
    try {
      const constraints = {
        video: {
          facingMode: useFacingMode,
          width: { ideal: 1920 },
          height: { ideal: 1080 }
        }
      };
      currentStream = await navigator.mediaDevices.getUserMedia(constraints);
      cameraVideo.srcObject = currentStream;
    } catch (err) {
      console.error('Camera access error:', err);
      showToast('Could not access camera device. Please check permissions.', 'error');
      closeCameraModal();
    }
  }

  function closeCameraModal() {
    if (currentStream) {
      currentStream.getTracks().forEach(t => t.stop());
      currentStream = null;
    }
    if (cameraModal) cameraModal.classList.remove('active');
  }

  if (closeCameraModalBtn) closeCameraModalBtn.addEventListener('click', closeCameraModal);

  if (switchCameraBtn) {
    switchCameraBtn.addEventListener('click', async () => {
      useFacingMode = useFacingMode === 'environment' ? 'user' : 'environment';
      await startCameraStream();
    });
  }

  if (shutterBtn) {
    shutterBtn.addEventListener('click', () => {
      if (!cameraVideo.videoWidth) return;
      cameraCanvas.width = cameraVideo.videoWidth;
      cameraCanvas.height = cameraVideo.videoHeight;
      const ctx = cameraCanvas.getContext('2d');
      ctx.drawImage(cameraVideo, 0, 0, cameraCanvas.width, cameraCanvas.height);

      cameraCanvas.toBlob(blob => {
        if (activeCaptureCallback && blob) {
          activeCaptureCallback(blob);
          showToast('Card photo captured successfully!', 'success');
        }
        closeCameraModal();
      }, 'image/jpeg', 0.95);
    });
  }

  // Global Escape key modal dismissal
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (drawerContainer && drawerContainer.classList.contains('active')) {
        closeDrawer();
      }
      if (splitModal && splitModal.classList.contains('active')) {
        closeSplitModal();
      }
      if (cameraModal && cameraModal.classList.contains('active')) {
        closeCameraModal();
      }
    }
  });
});

// ── Mobile island menu ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const burger = document.getElementById('nav-burger');
  const items = document.getElementById('nav-items');
  if (!burger || !items) return;

  const setOpen = (open) => {
    document.body.classList.toggle('nav-open', open);
    burger.setAttribute('aria-expanded', String(open));
    burger.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  };

  burger.addEventListener('click', () => setOpen(!document.body.classList.contains('nav-open')));
  items.addEventListener('click', (e) => {
    if (e.target.closest('a, .drawer-toggle-btn')) setOpen(false);
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setOpen(false);
  });
  window.matchMedia('(min-width: 1081px)').addEventListener('change', (e) => {
    if (e.matches) setOpen(false);
  });
});

// ── Scroll-entry reveals (IntersectionObserver, no scroll listeners) ──────
// Targets must match the html.reveal-on selectors in style.css.
const REVEAL_TARGETS = '.main-content .page-header, .main-content .hub-card, .main-content .card:not(.card .card), .fileshare-header, .breadcrumb-bar';

document.addEventListener('DOMContentLoaded', () => {
  const root = document.documentElement;
  if (!root.classList.contains('reveal-on')) return;
  window.__revealReady = true;

  const targets = Array.from(document.querySelectorAll(REVEAL_TARGETS));
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const el = entry.target;
      io.unobserve(el);
      el.classList.add('is-in');
      // Hand control back to the element's own transitions once it has arrived.
      const delay = parseInt(el.style.getPropertyValue('--reveal-delay'), 10) || 0;
      setTimeout(() => {
        el.classList.add('reveal-done');
        el.style.removeProperty('--reveal-delay');
      }, delay + 1000);
    });
  }, { rootMargin: '0px 0px -6% 0px', threshold: 0 });

  // Read all positions first, then write, to avoid a forced reflow per element.
  // Hidden (display:none) results sections report no boxes: no stagger when they appear later.
  const tops = targets.map((el) => (el.getClientRects().length ? el.getBoundingClientRect().top : Infinity));
  let i = 0;
  targets.forEach((el, k) => {
    // Stagger only what is on screen at load; later items reveal as they scroll in.
    if (tops[k] < window.innerHeight) el.style.setProperty('--reveal-delay', `${Math.min(i++, 6) * 90}ms`);
    io.observe(el);
  });
});
