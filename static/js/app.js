/* ================================================================
   YT Downloader — Frontend Application
   State machine: idle → processing → done | error
   No auto-download on paste. File served only after user tap.
   ================================================================ */

(function () {
    'use strict';

    // ── DOM refs ────────────────────────────────────────────────────
    const urlInput      = document.getElementById('url-input');
    const pasteBtn      = document.getElementById('paste-btn');
    const urlError      = document.getElementById('url-error');

    const formatBtns    = document.querySelectorAll('.format-btn');
    const qualitySection= document.getElementById('quality-section');
    const qualityBtns   = document.querySelectorAll('.quality-btn');

    const downloadBtn   = document.getElementById('download-btn');
    const btnLabel      = downloadBtn.querySelector('.btn-label');
    const btnIcon       = downloadBtn.querySelector('.btn-icon');
    const btnSpinner    = downloadBtn.querySelector('.btn-spinner');

    const statusCard    = document.getElementById('status-card');
    const statusText    = document.getElementById('status-text');

    const errorCard     = document.getElementById('error-card');
    const errorText     = document.getElementById('error-text');

    const saveBtn       = document.getElementById('save-btn');
    const resetBtn      = document.getElementById('reset-btn');

    // ── App state ───────────────────────────────────────────────────
    let selectedFormat  = 'mp3';
    let selectedQuality = 'auto';
    let currentJobId    = null;
    let pollTimer       = null;
    let isProcessing    = false;

    // ── Utility ─────────────────────────────────────────────────────
    const show = el => el.classList.remove('hidden');
    const hide = el => el.classList.add('hidden');

    function isValidUrl(str) {
        if (!str) return false;
        try {
            const u = new URL(str);
            return u.protocol === 'http:' || u.protocol === 'https:';
        } catch (_) {
            return false;
        }
    }

    // ── State machine ────────────────────────────────────────────────
    /**
     * setState — drives ALL UI changes.
     *
     * States:
     *   'idle'       — initial, reset
     *   'url_error'  — bad URL supplied
     *   'processing' — job running, show spinner + status card
     *   'done'       — file ready, show save button
     *   'error'      — download failed, show error card
     */
    function setState(state, opts = {}) {
        // Reset everything first
        urlError.textContent = '';
        urlInput.classList.remove('invalid');
        hide(statusCard);
        hide(errorCard);
        hide(saveBtn);
        hide(resetBtn);
        show(downloadBtn);
        downloadBtn.disabled = false;
        show(btnIcon);
        hide(btnSpinner);
        btnLabel.textContent = 'Download';

        switch (state) {
            case 'idle':
                isProcessing = false;
                stopPolling();
                break;

            case 'url_error':
                urlInput.classList.add('invalid');
                urlError.textContent = opts.msg || 'Invalid URL.';
                break;

            case 'processing':
                isProcessing = true;
                downloadBtn.disabled = true;
                hide(btnIcon);
                show(btnSpinner);
                btnLabel.textContent = 'Processing…';
                show(statusCard);
                statusText.textContent = opts.msg || 'Starting…';
                break;

            case 'done':
                isProcessing = false;
                stopPolling();
                hide(downloadBtn);
                show(saveBtn);
                show(resetBtn);
                saveBtn.href = `/api/file/${currentJobId}`;
                saveBtn.setAttribute('download', opts.filename || 'download');
                break;

            case 'error':
                isProcessing = false;
                stopPolling();
                hide(downloadBtn);
                show(errorCard);
                show(resetBtn);
                errorText.textContent = opts.msg || 'Something went wrong. Please try again.';
                break;
        }
    }

    // ── Polling ──────────────────────────────────────────────────────
    function startPolling(jobId) {
        stopPolling();
        pollTimer = setInterval(() => poll(jobId), 1500);
    }

    function stopPolling() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    async function poll(jobId) {
        try {
            const res = await fetch(`/api/status/${jobId}`);

            if (!res.ok) {
                setState('error', { msg: 'Lost connection to the server. Please try again.' });
                return;
            }

            const data = await res.json();

            if (data.status === 'queued' || data.status === 'processing') {
                // Update the status message without changing the overall state
                statusText.textContent = data.progress_msg || 'Processing…';

            } else if (data.status === 'done') {
                setState('done', { filename: data.filename });

            } else if (data.status === 'error') {
                setState('error', { msg: data.error || 'Download failed.' });
            }

        } catch (_) {
            setState('error', { msg: 'Connection lost. Check your internet and try again.' });
        }
    }

    // ── Start download ───────────────────────────────────────────────
    async function startDownload() {
        if (isProcessing) return;

        const url = urlInput.value.trim();

        // Client-side validation (server also validates — this is UX only)
        if (!url) {
            setState('url_error', { msg: 'Please paste a YouTube URL first.' });
            urlInput.focus();
            return;
        }
        if (!isValidUrl(url)) {
            setState('url_error', { msg: "That doesn't look like a valid URL." });
            return;
        }

        setState('processing', { msg: 'Connecting…' });

        try {
            const res = await fetch('/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    url,
                    format: selectedFormat,
                    quality: selectedQuality,
                }),
            });

            const data = await res.json();

            if (!res.ok) {
                setState('error', { msg: data.error || 'Request rejected by server.' });
                return;
            }

            currentJobId = data.job_id;
            startPolling(currentJobId);

        } catch (_) {
            setState('error', { msg: 'Could not reach the server. Please try again.' });
        }
    }

    // ── Event listeners ──────────────────────────────────────────────

    // Paste button
    pasteBtn.addEventListener('click', async () => {
        try {
            const text = (await navigator.clipboard.readText()).trim();
            if (text) {
                urlInput.value = text;
                urlError.textContent = '';
                urlInput.classList.remove('invalid');
                // Visual feedback
                pasteBtn.textContent = '✓ Pasted';
                setTimeout(() => {
                    // Restore icon + text
                    pasteBtn.innerHTML = `
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                            <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/>
                            <rect x="8" y="2" width="8" height="4" rx="1"/>
                        </svg>
                        Paste`;
                }, 1500);
            }
        } catch (_) {
            // Clipboard API not available (HTTP or permission denied) — focus input
            urlInput.focus();
            urlInput.select();
        }
    });

    // Clear URL error on typing
    urlInput.addEventListener('input', () => {
        if (urlError.textContent) {
            urlError.textContent = '';
            urlInput.classList.remove('invalid');
        }
    });

    // Enter key triggers download
    urlInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') startDownload();
    });

    // Format selector
    formatBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            if (isProcessing) return;
            formatBtns.forEach(b => {
                b.classList.remove('active');
                b.setAttribute('aria-pressed', 'false');
            });
            btn.classList.add('active');
            btn.setAttribute('aria-pressed', 'true');
            selectedFormat = btn.dataset.format;

            if (selectedFormat === 'mp4') {
                show(qualitySection);
                qualitySection.removeAttribute('aria-hidden');
            } else {
                hide(qualitySection);
                qualitySection.setAttribute('aria-hidden', 'true');
            }
        });
    });

    // Quality selector
    qualityBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            if (isProcessing) return;
            qualityBtns.forEach(b => {
                b.classList.remove('active');
                b.setAttribute('aria-pressed', 'false');
            });
            btn.classList.add('active');
            btn.setAttribute('aria-pressed', 'true');
            selectedQuality = btn.dataset.quality;
        });
    });

    // Primary download button
    downloadBtn.addEventListener('click', startDownload);

    // Reset / Start over
    resetBtn.addEventListener('click', () => {
        currentJobId = null;
        setState('idle');
        // Keep URL in the input — user may want to re-download in a different format
    });

    // Keyboard activation for save-btn (it's an <a>, but just in case)
    saveBtn.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') saveBtn.click();
    });

    // ── Init ─────────────────────────────────────────────────────────
    setState('idle');

})();
