let isCapturing = false;
let streamReconnectTimer = null;
let streamConnected = false;
let metadataPollTimer = null;
let metadataRequestInFlight = false;
let previewEnabled = true;
let lastFrameTimestamp = 0;
let staleFramePolls = 0;
let streamFailureCount = 0;

const preview = document.getElementById('preview');
const previewToggle = document.getElementById('preview-toggle');
const shutterButton = document.getElementById('shutter-button');
const captureThumbButton = document.getElementById('capture-thumb-button');
const captureThumb = document.getElementById('capture-thumb');
const captureModal = document.getElementById('capture-modal');
const captureModalImage = document.getElementById('capture-modal-image');
const toast = document.getElementById('toast');
const batteryEl = document.getElementById('battery');
const shutterSpeedEl = document.getElementById('shutter-speed');
const isoEl = document.getElementById('iso');
const wbEl = document.getElementById('wb');

document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    syncPreviewState()
        .catch(() => {
            connectStream();
        })
        .finally(() => {
            startMetadataPolling();
        });
});

function setupEventListeners() {
    shutterButton.addEventListener('click', handleShutter);
    previewToggle.addEventListener('click', handlePreviewToggle);
    preview.addEventListener('load', handlePreviewLoad);
    preview.addEventListener('error', handlePreviewError);
    captureThumbButton.addEventListener('click', openCaptureModal);
    captureModal.addEventListener('click', closeCaptureModal);
    
    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
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

function connectStream(force = false) {
    if (!previewEnabled) {
        return;
    }
    if (!force && streamReconnectTimer !== null) {
        return;
    }

    if (streamReconnectTimer !== null) {
        clearTimeout(streamReconnectTimer);
        streamReconnectTimer = null;
    }

    const nextUrl = buildStreamUrl();
    if (!force && preview.src && preview.src.includes('/stream')) {
        return;
    }

    streamConnected = false;
    preview.src = nextUrl;
}

function disconnectStream() {
    streamConnected = false;
    preview.removeAttribute('src');
}

function scheduleStreamReconnect(delay = 2000) {
    if (!previewEnabled || streamReconnectTimer !== null) {
        return;
    }

    streamReconnectTimer = setTimeout(() => {
        streamReconnectTimer = null;
        connectStream(true);
    }, delay);
}

function handlePreviewLoad() {
    streamConnected = true;
    streamFailureCount = 0;
    if (streamReconnectTimer !== null) {
        clearTimeout(streamReconnectTimer);
        streamReconnectTimer = null;
    }
    showStreamStatus(null);
}

function handlePreviewError() {
    streamConnected = false;
    streamFailureCount++;
    if (streamFailureCount > 3) {
        showStreamStatus('⚠️ Waiting for camera connection...');
    }
    scheduleStreamReconnect();
}

function showStreamStatus(message) {
    const container = document.getElementById('preview-container');
    if (!container) return;  // Exit if container doesn't exist
    
    let statusEl = document.getElementById('stream-status');
    if (!statusEl) {
        statusEl = document.createElement('div');
        statusEl.id = 'stream-status';
        statusEl.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,0.7);color:white;padding:20px;border-radius:8px;text-align:center;font-size:16px;z-index:100;';
        container.appendChild(statusEl);
    }
    if (message) {
        statusEl.textContent = message;
        statusEl.style.display = 'block';
    } else {
        statusEl.style.display = 'none';
    }
}

async function syncPreviewState() {
    const response = await fetch('/api/preview');
    const data = await response.json();
    previewEnabled = Boolean(data.enabled);
    applyPreviewButtonState();
    if (previewEnabled) {
        connectStream(true);
    } else {
        disconnectStream();
    }
}

function applyPreviewButtonState() {
    previewToggle.classList.toggle('active', previewEnabled);
    previewToggle.title = previewEnabled ? 'Disable Live Preview' : 'Enable Live Preview';
}

async function handlePreviewToggle() {
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
        previewEnabled = Boolean(data.enabled);
        applyPreviewButtonState();
        if (previewEnabled) {
            connectStream(true);
            showToast('Live preview enabled', 1200);
        } else {
            disconnectStream();
            showToast('Live preview disabled', 1200);
        }
    } catch (err) {
        showToast('Error: ' + err.message, 2000);
    }
}

function handleShutter() {
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
    
    // Ensure the URL is absolute or properly rooted
    let captureUrl = url;
    if (!captureUrl.startsWith('/')) {
        captureUrl = '/' + captureUrl;
    }
    
    // Add cache bust parameter
    const cacheBust = `?t=${Date.now()}`;
    if (!captureUrl.includes('?')) {
        captureUrl += cacheBust;
    } else {
        captureUrl += `&t=${Date.now()}`;
    }
    
    captureThumb.src = captureUrl;
    captureModalImage.src = captureUrl;
    captureThumbButton.hidden = false;
}

function openCaptureModal() {
    if (!captureModalImage.src) {
        return;
    }
    captureModal.hidden = false;
}

function closeCaptureModal() {
    captureModal.hidden = true;
}

function showToast(msg, duration = 3000) {
    toast.textContent = msg;
    toast.classList.add('show');

    if (duration > 0) {
        setTimeout(() => {
            toast.classList.remove('show');
        }, duration);
    }
}

function startMetadataPolling() {
    if (metadataPollTimer !== null) {
        return;
    }

    updateMetadata();
    metadataPollTimer = setInterval(updateMetadata, 1000);
}

function updateMetadata() {
    if (isCapturing || metadataRequestInFlight) {
        return;
    }

    metadataRequestInFlight = true;

    fetch('/api/metadata')
        .then((r) => r.json())
        .then((data) => {
            // Update battery with color indicator
            const batteryLevel = data.battery_level;
            if (batteryLevel !== null) {
                batteryEl.textContent = batteryLevel + '%';
                const batteryIcon = document.querySelector('.battery-icon');
                if (batteryIcon) {
                    if (batteryLevel <= 25) {
                        batteryIcon.classList.add('low');
                    } else {
                        batteryIcon.classList.remove('low');
                    }
                }
            } else {
                batteryEl.textContent = '--';
            }
            
            shutterSpeedEl.textContent = data.shutter_speed || '--';
            isoEl.textContent = data.iso || '--';
            wbEl.textContent = data.white_balance || '--';

            if (!streamConnected && preview.complete && preview.naturalWidth > 0) {
                streamConnected = true;
            }

            const frameTs = Number(data.preview_frame_timestamp || 0);
            if (previewEnabled && frameTs > 0) {
                if (frameTs === lastFrameTimestamp) {
                    staleFramePolls += 1;
                } else {
                    staleFramePolls = 0;
                    lastFrameTimestamp = frameTs;
                }

                if (staleFramePolls >= 4) {
                    staleFramePolls = 0;
                    connectStream(true);
                }
            }

            if (previewEnabled && data.status === 'Ready' && (!preview.src || !preview.src.includes('/stream'))) {
                connectStream(true);
            }
            if (previewEnabled && data.status === 'Camera disconnected') {
                scheduleStreamReconnect(3000);
            }
        })
        .catch(() => {
            if (previewEnabled) {
                scheduleStreamReconnect(3000);
            }
        })
        .finally(() => {
            metadataRequestInFlight = false;
        });
}
