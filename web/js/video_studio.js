/**
 * JAI Studio - Video Production Studio & Director Bridge
 * 
 * Features:
 * - Native Render Parameter Controls (Aspect, Color, Upscale, Transition, Subtitles, Delogo, Visuals)
 * - Groq AI Quality Score Gauge with animated counter, dynamic verdict badges, and audio-visual audit
 * - Telegram Review Cut Dispatcher (/api/video/review)
 * - ClipMerge Studio Embedded Bridge with port 8080 health verification
 * - Real-time SSE integration for video_render_start, video_render_complete, video_render_error
 */

export const VideoStudio = {
  // DOM Elements
  form: document.getElementById('renderTriggerForm'),
  aspectSelect: document.getElementById('renderAspect'),
  colorSelect: document.getElementById('renderColor'),
  upscaleSelect: document.getElementById('renderUpscale'),
  transitionSelect: document.getElementById('renderTransition'),
  subtitlesCheck: document.getElementById('renderSubtitles'),
  delogoCheck: document.getElementById('renderDelogo'),
  visualsOnlyCheck: document.getElementById('renderVisualsOnly'),
  btnStartRender: document.getElementById('btnStartRender'),

  // Groq Gauge Elements
  groqScoreGauge: document.getElementById('groqScoreGauge'),
  groqVerdictTag: document.getElementById('groqVerdictTag'),
  groqScoreDesc: document.getElementById('groqScoreDesc'),
  btnSendTelegramReview: document.getElementById('btnSendTelegramReview'),

  // ClipMerge Bridge Elements
  clipmergeIframe: document.getElementById('clipmergeIframe'),
  btnOpenClipmergeTab: document.getElementById('btnOpenClipmergeTab'),

  // State
  isRendering: false,
  currentScore: 92,
  lastRenderData: null,
  clipmergePort: 8080,
  clipmergeOnline: false,

  init() {
    this.bindEvents();
    this.loadInitialStatus();
    this.checkClipmergeBridge();
    this.setupSSEListeners();
  },

  bindEvents() {
    // 1. Render Form Submit
    if (this.form) {
      this.form.addEventListener('submit', (e) => {
        e.preventDefault();
        this.handleDispatchRender();
      });
    }

    // 2. Telegram Review Dispatch
    if (this.btnSendTelegramReview) {
      this.btnSendTelegramReview.addEventListener('click', () => {
        this.handleDispatchTelegramReview();
      });
    }

    // 3. ClipMerge Direct Tab Link
    if (this.btnOpenClipmergeTab) {
      this.btnOpenClipmergeTab.addEventListener('click', () => {
        if (window.showToast) {
          window.showToast('Opening ClipMerge Studio (Port 8080)...', 'info', 2000);
        }
      });
    }
  },

  setupSSEListeners() {
    if (!window.apiClient) return;

    // Video Render Start
    window.apiClient.on('video_render_start', (data) => {
      this.setRenderingState(true);
      if (window.showToast) {
        window.showToast(`🎬 Studio Render Started: ${data?.aspect || '16:9'} / ${data?.color || 'vibrant'}`, 'info', 4000);
      }
      this.updateHeaderBadge('Rendering...', 'warning');
    });

    // Video Render Complete
    window.apiClient.on('video_render_complete', (data) => {
      this.setRenderingState(false);
      this.lastRenderData = data;
      const score = data?.groq_score ?? 90;
      const verdict = data?.groq_verdict ?? 'APPROVED';
      this.animateScoreGauge(score, verdict);

      if (window.showToast) {
        window.showToast(`✨ Render Complete! Groq Quality Score: ${score}/100 (${verdict})`, 'success', 5000);
      }
      this.updateHeaderBadge('Video: Ready', 'online');
    });

    // Video Render Error
    window.apiClient.on('video_render_error', (data) => {
      this.setRenderingState(false);
      const errMsg = data?.error || 'Unknown video render failure';
      if (window.showToast) {
        window.showToast(`❌ Video Render Failed: ${errMsg}`, 'error', 6000);
      }
      this.updateHeaderBadge('Video: Error', 'offline');
    });

    // Telegram Review Event
    window.apiClient.on('telegram_review_sent', (data) => {
      if (window.showToast) {
        window.showToast(`📬 Review card sent to Telegram channel: ${data?.chat_id || 'Team'}`, 'success', 4000);
      }
    });
  },

  async loadInitialStatus() {
    if (!window.apiClient) return;
    try {
      const res = await window.apiClient.get('/api/video/status', {
        success: true,
        data: {
          groq_score: 92,
          groq_verdict: 'APPROVED',
          title: 'JAI Autonomous Cut',
          aspect: '16:9',
          color: 'vibrant'
        }
      });

      if (res && res.data) {
        this.lastRenderData = res.data;
        const score = res.data.groq_score || 92;
        const verdict = res.data.groq_verdict || 'APPROVED';
        this.updateScoreGaugeUI(score, verdict);
      }
    } catch (err) {
      console.warn('Could not load initial video status:', err);
    }
  },

  async handleDispatchRender() {
    if (this.isRendering) return;

    const payload = {
      aspect: this.aspectSelect ? this.aspectSelect.value : '16:9',
      color: this.colorSelect ? this.colorSelect.value : 'vibrant',
      upscale: this.upscaleSelect ? this.upscaleSelect.value : '1080p',
      transition: this.transitionSelect ? this.transitionSelect.value : 'fade',
      subtitles: this.subtitlesCheck ? this.subtitlesCheck.checked : true,
      remove_watermark: this.delogoCheck ? this.delogoCheck.checked : true,
      visuals_only: this.visualsOnlyCheck ? this.visualsOnlyCheck.checked : true
    };

    this.setRenderingState(true);
    this.updateHeaderBadge('Rendering...', 'warning');

    if (window.showToast) {
      window.showToast(`Dispatching Autonomous Director cut (${payload.aspect}, ${payload.color})...`, 'info', 3000);
    }

    try {
      const res = await window.apiClient.post('/api/video/render', payload, {
        success: true,
        message: 'Render pipeline triggered in background.'
      });

      if (res && res.success) {
        if (window.showToast) {
          window.showToast('🚀 Pipeline accepted. Hardware FFmpeg + Lanczos rendering active.', 'success', 3500);
        }
      } else {
        throw new Error(res?.error || 'Render trigger returned unsuccessful status.');
      }
    } catch (err) {
      this.setRenderingState(false);
      this.updateHeaderBadge('Video: Ready', 'online');
      if (window.showToast) {
        window.showToast(`Failed to trigger render: ${err.message}`, 'error', 4500);
      }
    }
  },

  async handleDispatchTelegramReview() {
    if (!this.btnSendTelegramReview) return;
    const origHtml = this.btnSendTelegramReview.innerHTML;

    this.btnSendTelegramReview.disabled = true;
    this.btnSendTelegramReview.innerHTML = `
      <span class="studio-spinner" style="width: 12px; height: 12px; margin-right: 6px;"></span>
      <span>Dispatching to Telegram...</span>
    `;

    try {
      const payload = {
        notes: 'Requested interactive Telegram review cut from JAI Web Studio',
        render_info: this.lastRenderData || {}
      };

      const res = await window.apiClient.post('/api/video/review', payload, {
        success: true,
        message: 'Telegram review card dispatched.'
      });

      if (res && res.success) {
        this.btnSendTelegramReview.innerHTML = `
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>
          <span>Dispatched to Telegram!</span>
        `;
        if (window.showToast) {
          window.showToast('✅ Interactive review card dispatched to Telegram channel.', 'success', 4000);
        }
        setTimeout(() => {
          this.btnSendTelegramReview.disabled = false;
          this.btnSendTelegramReview.innerHTML = origHtml;
        }, 3000);
      } else {
        throw new Error(res?.error || 'Failed to dispatch Telegram review');
      }
    } catch (err) {
      this.btnSendTelegramReview.disabled = false;
      this.btnSendTelegramReview.innerHTML = origHtml;
      if (window.showToast) {
        window.showToast(`Telegram dispatch notice: ${err.message}`, 'warning', 4000);
      }
    }
  },

  setRenderingState(rendering) {
    this.isRendering = rendering;
    if (!this.btnStartRender) return;

    if (rendering) {
      this.btnStartRender.disabled = true;
      this.btnStartRender.classList.add('btn-loading');
      this.btnStartRender.innerHTML = `
        <span class="studio-spinner" style="width: 14px; height: 14px; margin-right: 8px;"></span>
        <span>Director Rendering Master Cut...</span>
      `;
    } else {
      this.btnStartRender.disabled = false;
      this.btnStartRender.classList.remove('btn-loading');
      this.btnStartRender.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <polygon points="5 3 19 12 5 21 5 3"></polygon>
        </svg>
        <span>Dispatch Studio Render</span>
      `;
    }
  },

  animateScoreGauge(targetScore, verdict) {
    const startScore = this.currentScore;
    const duration = 1200;
    const startTime = performance.now();

    const updateFrame = (now) => {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1.0);
      // Ease out cubic
      const ease = 1 - Math.pow(1 - progress, 3);
      const val = Math.round(startScore + (targetScore - startScore) * ease);

      this.updateScoreGaugeUI(val, verdict);

      if (progress < 1.0) {
        requestAnimationFrame(updateFrame);
      } else {
        this.currentScore = targetScore;
        this.updateScoreGaugeUI(targetScore, verdict);
      }
    };

    requestAnimationFrame(updateFrame);
  },

  updateScoreGaugeUI(score, verdict) {
    if (this.groqScoreGauge) {
      this.groqScoreGauge.textContent = score;
      this.groqScoreGauge.classList.remove('score-high', 'score-medium', 'score-low');
      if (score >= 85) {
        this.groqScoreGauge.classList.add('score-high');
      } else if (score >= 70) {
        this.groqScoreGauge.classList.add('score-medium');
      } else {
        this.groqScoreGauge.classList.add('score-low');
      }
    }

    if (this.groqVerdictTag) {
      this.groqVerdictTag.textContent = (verdict || 'APPROVED').toUpperCase();
      this.groqVerdictTag.classList.remove('verdict-approved', 'verdict-warning', 'verdict-rejected');
      if (score >= 85) {
        this.groqVerdictTag.classList.add('verdict-approved');
      } else if (score >= 70) {
        this.groqVerdictTag.classList.add('verdict-warning');
      } else {
        this.groqVerdictTag.classList.add('verdict-rejected');
      }
    }

    if (this.groqScoreDesc) {
      if (score >= 90) {
        this.groqScoreDesc.textContent = 'Flawless broadcast standard. Visual cuts align with audio rhythm with dynamic color LUTs.';
      } else if (score >= 80) {
        this.groqScoreDesc.textContent = 'High quality editorial cut. Minor pacing adjustments recommended for high-retention feeds.';
      } else {
        this.groqScoreDesc.textContent = 'Pacing or audio ducking below optimal retention threshold. Re-engineering directives applied.';
      }
    }
  },

  updateHeaderBadge(text, stateClass) {
    const videoBadge = document.getElementById('videoBadge');
    const videoDot = document.getElementById('videoDot');
    const videoText = document.getElementById('videoText');
    if (videoText) videoText.textContent = text;
    if (videoDot) {
      videoDot.className = `status-dot ${stateClass}`;
    }
  },

  async checkClipmergeBridge() {
    try {
      // Test fetch with short timeout to check if ClipMerge is running on port 8080
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 1200);

      const resp = await fetch(`http://${window.location.hostname || '127.0.0.1'}:8080/api/settings`, {
        method: 'GET',
        mode: 'no-cors',
        signal: controller.signal
      }).catch(() => null);

      clearTimeout(timeoutId);

      this.clipmergeOnline = resp !== null;
      this.updateClipmergeBridgeUI(this.clipmergeOnline);
    } catch (_) {
      this.clipmergeOnline = false;
      this.updateClipmergeBridgeUI(false);
    }
  },

  updateClipmergeBridgeUI(isOnline) {
    const titleElem = document.querySelector('#viewVideoStudio .view-title-group p');
    if (titleElem) {
      const indicator = isOnline
        ? '<span style="color: var(--success); font-weight: 600;">● ClipMerge Engine Active (Port 8080)</span>'
        : '<span style="color: var(--warning); font-weight: 600;">○ ClipMerge Standby (Port 8080)</span>';
      titleElem.innerHTML = `Native autonomous cloud director renders, Groq AI quality scoring, and Telegram review triggers. &bull; ${indicator}`;
    }
  }
};

// Expose globally and self-init on DOMContentLoaded
window.VideoStudio = VideoStudio;
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => VideoStudio.init());
} else {
  VideoStudio.init();
}
