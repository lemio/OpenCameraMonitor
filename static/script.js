let isCapturing = false;
let streamReconnectTimer = null;
let streamConnected = false;
let previewEnabled = true;
let cameraStatus = 'Ready';
let lastFrameTimestamp = 0;
let lastFrameActivityMs = 0;
let staleFramePolls = 0;
let streamFailureCount = 0;

const preview = document.getElementById('preview');
const previewOverlay = document.getElementById('preview-overlay');
const previewOverlayIcon = document.getElementById('preview-overlay-icon');
const previewOverlayTitle = document.getElementById('preview-overlay-title');
const previewOverlaySubtitle = document.getElementById('preview-overlay-subtitle');
const previewToggle = document.getElementById('preview-toggle');
const shutterButton = document.getElementById('shutter-button');
const shareButton = document.getElementById('share-button');
const captureThumbButton = document.getElementById('capture-thumb-button');
const captureThumb = document.getElementById('capture-thumb');
const captureModal = document.getElementById('capture-modal');
const captureModalImage = document.getElementById('capture-modal-image');
const shareModal = document.getElementById('share-modal');
const shareModalClose = document.getElementById('share-modal-close');
const shareQrCode = document.getElementById('share-qr-code');
const shareUrl = document.getElementById('share-url');
const copyUrlButton = document.getElementById('copy-url-button');
const toast = document.getElementById('toast');
const batteryEl = document.getElementById('battery');
const shutterSpeedEl = document.getElementById('shutter-speed');
const isoEl = document.getElementById('iso');
const wbEl = document.getElementById('wb');

const PREVIEW_ENABLED_ICON = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"></path>
        <circle cx="12" cy="12" r="2.5"></circle>
    </svg>
`;

const PREVIEW_DISABLED_ICON = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M2.5 12s3.5-6 9.5-6c1.5 0 2.9.3 4.1.8"></path>
        <path d="M21.5 12s-3.5 6-9.5 6c-1.5 0-2.9-.3-4.1-.8"></path>
        <path d="M3 3l18 18"></path>
        <circle cx="12" cy="12" r="2.5"></circle>
    </svg>
`;

const SHARE_ICON = `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"></path>
        <polyline points="16 6 12 2 8 6"></polyline>
        <line x1="12" y1="2" x2="12" y2="15"></line>
    </svg>
`;

const PREVIEW_LOADING_ICON = `
    <div class="preview-spinner" aria-hidden="true"></div>
`;

const LANDSCAPE_QUERY = window.matchMedia('(orientation: landscape) and (max-width: 900px)');

document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    updateOrientationState();
    if (typeof LANDSCAPE_QUERY.addEventListener === 'function') {
        LANDSCAPE_QUERY.addEventListener('change', updateOrientationState);
    } else if (typeof LANDSCAPE_QUERY.addListener === 'function') {
        LANDSCAPE_QUERY.addListener(updateOrientationState);
    }
    window.addEventListener('resize', updateOrientationState, { passive: true });
    window.addEventListener('orientationchange', updateOrientationState);
    syncPreviewState()
        .catch(() => {
            previewEnabled = true;
            renderPreviewToggleIcon();
            updatePreviewOverlay();
            connectStream(true);
        });
});

function setupEventListeners() {
    if (shutterButton) shutterButton.addEventListener('click', handleShutter);
    if (previewToggle) previewToggle.addEventListener('click', handlePreviewToggle);
    if (shareButton) shareButton.addEventListener('click', openShareModal);
    if (preview) {
        preview.addEventListener('load', handlePreviewLoad);
        preview.addEventListener('error', handlePreviewError);
    }
    if (captureThumbButton) captureThumbButton.addEventListener('click', openCaptureModal);
    if (captureModal) captureModal.addEventListener('click', closeCaptureModal);
    if (shareModal) shareModal.addEventListener('click', handleShareModalClick);
    if (shareModalClose) shareModalClose.addEventListener('click', closeShareModal);
    if (copyUrlButton) copyUrlButton.addEventListener('click', copyUrlToClipboard);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeShareModal();
            closeCaptureModal();
        }

        if (e.code === 'Space' || e.code === 'Enter') {
            e.preventDefault();
            if (!isCapturing && shutterButton && !shutterButton.disabled) {
                handleShutter();
            }
        }
    });
}

function buildStreamUrl() {
    return `/stream?t=${Date.now()}`;
}

function updateOrientationState() {
    document.body.classList.toggle('landscape-phone', LANDSCAPE_QUERY.matches);
}

function setPreviewOverlay(mode, title, subtitle) {
    if (!previewOverlay) {
        return;
    }

    if (mode === 'hidden') {
        previewOverlay.hidden = true;
        return;
    }

    previewOverlay.hidden = false;
    previewOverlay.classList.toggle('is-disabled', mode === 'disabled');
    previewOverlay.classList.toggle('is-loading', mode === 'loading');
    previewOverlay.classList.toggle('is-error', mode === 'error');

    if (previewOverlayIcon) {
        if (mode === 'disabled') {
            previewOverlayIcon.innerHTML = PREVIEW_DISABLED_ICON;
        } else {
            previewOverlayIcon.innerHTML = PREVIEW_LOADING_ICON;
        }
    }

    if (previewOverlayTitle) {
        previewOverlayTitle.textContent = title;
    }

    if (previewOverlaySubtitle) {
        previewOverlaySubtitle.textContent = subtitle;
    }
}

function updatePreviewOverlay() {
    const cameraConnected = cameraStatus !== 'Camera disconnected';

    if (!previewEnabled) {
        setPreviewOverlay(
            'disabled',
            'Live view disabled',
            cameraConnected ? 'Camera connected.' : 'Camera disconnected.'
        );
        preview.alt = cameraConnected ? 'Live view disabled. Camera connected.' : 'Live view disabled. Camera disconnected.';
        return;
    }

    const waitingForCamera = cameraStatus === 'Camera disconnected' || cameraStatus === 'Initializing camera' || cameraStatus === 'Refreshing settings';
    const hasRecentFrameActivity = Date.now() - lastFrameActivityMs < 5000;
    const missingFrame = !hasRecentFrameActivity && (!streamConnected || !preview.src || preview.naturalWidth === 0 || streamFailureCount > 0);

    if (waitingForCamera || missingFrame) {
        if (!cameraConnected) {
            setPreviewOverlay(
                'loading',
                'No camera connection',
                'Check the USB connection or wake the camera to resume live view.'
            );
            preview.alt = 'No camera connection';
            return;
        }

        setPreviewOverlay(
            'loading',
            'Reconnecting to camera',
            waitingForCamera ? 'Waiting for the camera to come back online.' : 'Waiting for live preview frames.'
        );
        preview.alt = 'Reconnecting to camera';
        return;
    }

    setPreviewOverlay('hidden');
    preview.alt = 'Live camera preview';
}

function renderPreviewToggleIcon() {
        if (!previewToggle) {
            return;
        }

    previewToggle.innerHTML = previewEnabled ? PREVIEW_ENABLED_ICON : PREVIEW_DISABLED_ICON;
    previewToggle.classList.toggle('active', previewEnabled);
    previewToggle.classList.toggle('disabled', !previewEnabled);
    previewToggle.title = previewEnabled ? 'Disable Live Preview' : 'Enable Live Preview';
    previewToggle.setAttribute('aria-pressed', String(previewEnabled));
}

function applyPreviewState(nextEnabled) {
    previewEnabled = Boolean(nextEnabled);
    renderPreviewToggleIcon();

    if (previewEnabled) {
        connectStream(true);
    } else {
        disconnectStream();
    }

    updatePreviewOverlay();
}

async function syncPreviewState() {
    const response = await fetch('/api/preview');
    const data = await response.json();
    applyPreviewState(Boolean(data.enabled));
}

function connectStream(force = false) {
    if (!previewEnabled) {
        updatePreviewOverlay();
        return;
    }

    if (!force && streamReconnectTimer !== null) {
        return;
    }

    if (streamReconnectTimer !== null) {
        clearTimeout(streamReconnectTimer);
        streamReconnectTimer = null;
    }

    if (!force && preview.src && preview.src.includes('/stream')) {
        return;
    }

    streamConnected = false;
    preview.src = buildStreamUrl();
    updatePreviewOverlay();
}

function disconnectStream() {
    streamConnected = false;
    preview.removeAttribute('src');
    updatePreviewOverlay();
}

function scheduleStreamReconnect(delay = 2000) {
    if (!previewEnabled || streamReconnectTimer !== null) {
        return;
    }

    updatePreviewOverlay();
    streamReconnectTimer = setTimeout(() => {
        streamReconnectTimer = null;
        connectStream(true);
    }, delay);
}

function handlePreviewLoad() {
    streamConnected = true;
    streamFailureCount = 0;
    lastFrameActivityMs = Date.now();
    if (streamReconnectTimer !== null) {
        clearTimeout(streamReconnectTimer);
        streamReconnectTimer = null;
    }
    updatePreviewOverlay();
}

function handlePreviewError() {
    streamConnected = false;
    streamFailureCount += 1;
    updatePreviewOverlay();
    scheduleStreamReconnect(streamFailureCount > 2 ? 3000 : 1500);
}

async function handlePreviewToggle() {
        if (!previewToggle) {
            return;
        }

    const nextState = !previewEnabled;
    try {
        const response = await fetch('/api/preview', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: nextState }),
        });
        const data = await response.json();
        if (!response.ok || data.status !== 'ok') {
            throw new Error(data.message || `HTTP ${response.status}`);
        }

        applyPreviewState(Boolean(data.enabled));
        showToast(previewEnabled ? 'Live preview enabled' : 'Live preview disabled', 1200);
    } catch (err) {
        showToast('Error: ' + err.message, 2000);
    }
}

function handleShutter() {
        if (!shutterButton) {
            return;
        }

    if (isCapturing) {
        return;
    }

    isCapturing = true;
    shutterButton.style.opacity = '0.5';
    shutterButton.disabled = true;

    fetch('/api/shutter', { method: 'POST' })
        .then(async (r) => {
            const data = await r.json().catch(() => ({ status: 'error', message: `HTTP ${r.status}` }));
            if (!r.ok || data.status !== 'ok') {
                throw new Error(data.message || `HTTP ${r.status}`);
            }
            return data;
        })
        .then((data) => {
            if (data.capture_url) {
                updateCapturePreview(data.capture_url);
            }
            showToast('Photo captured', 1500);
        })
        .catch((err) => showToast('Error: ' + err.message, 2000))
        .finally(() => {
            isCapturing = false;
            shutterButton.style.opacity = '1';
            shutterButton.disabled = false;
            if (previewEnabled) {
                connectStream(true);
            }
        });
}

function updateCapturePreview(url) {
    if (!url) {
        showToast('No capture URL provided', 1500);
        return;
    }

    let captureUrl = url;
    if (!captureUrl.startsWith('/')) {
        captureUrl = '/' + captureUrl;
    }

    const base = window.location.origin;
    const parsed = new URL(captureUrl, base);
    parsed.searchParams.set('t', String(Date.now()));
    captureUrl = parsed.pathname + parsed.search;

    captureThumb.src = captureUrl;
    captureModalImage.src = captureUrl;
    captureThumbButton.hidden = false;
}

function openCaptureModal() {
    if (!captureModal || !captureModalImage || !captureModalImage.src) {
        return;
    }
    captureModal.hidden = false;
}

function closeCaptureModal() {
    if (captureModal) {
        captureModal.hidden = true;
    }
}

function handleShareModalClick(event) {
    if (event.target === shareModal || event.target.classList.contains('share-modal-backdrop')) {
        closeShareModal();
    }
}

async function openShareModal() {
        if (!shareModal || !shareUrl || !shareQrCode) {
            return;
        }

    shareModal.hidden = false;
    shareUrl.textContent = 'Loading network address...';
    shareQrCode.innerHTML = '';

    try {
        const response = await fetch('/api/share-info');
        const data = await response.json();
        const url = data.url || window.location.href;
        shareUrl.textContent = url;
        renderShareQrCode(url);
    } catch (err) {
        const fallbackUrl = window.location.href.replace(/\/$/, '');
        shareUrl.textContent = fallbackUrl;
        renderShareQrCode(fallbackUrl);
    }
}

function closeShareModal() {
    if (shareModal) {
        shareModal.hidden = true;
    }
}

function renderShareQrCode(url) {
        if (!shareQrCode) {
            return;
        }

    shareQrCode.innerHTML = '';

    if (typeof QRCode !== 'undefined') {
        new QRCode(shareQrCode, {
            text: url,
            width: 220,
            height: 220,
            colorDark: '#edf2f7',
            colorLight: '#1d2228',
            correctLevel: QRCode.CorrectLevel.H,
        });
        return;
    }

    shareQrCode.innerHTML = '<div class="share-qr-fallback">QR unavailable</div>';
}

async function copyUrlToClipboard() {
        if (!shareUrl) {
            return;
        }

    const url = shareUrl.textContent.trim();
    if (!url) {
        return;
    }

    try {
        await navigator.clipboard.writeText(url);
        showToast('URL copied to clipboard', 1500);
    } catch (_err) {
        const textarea = document.createElement('textarea');
        textarea.value = url;
        textarea.setAttribute('readonly', 'true');
        textarea.style.position = 'fixed';
        textarea.style.opacity = '0';
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand('copy');
        document.body.removeChild(textarea);
        showToast('URL copied to clipboard', 1500);
    }
}

function showToast(msg, duration = 3000) {
        if (!toast) {
            return;
        }

    toast.textContent = msg;
    toast.classList.add('show');

    if (duration > 0) {
        setTimeout(() => {
            toast.classList.remove('show');
        }, duration);
    }
}

function updateStatusPlaceholders() {
    batteryEl.textContent = '--';
    shutterSpeedEl.textContent = '--';
    isoEl.textContent = '--';
    wbEl.textContent = '--';
}

updateStatusPlaceholders();
