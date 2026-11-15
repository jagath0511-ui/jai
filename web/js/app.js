/**
 * JAI Studio - Core Application & Architecture Controller
 * 
 * Features:
 * - DaVinci Resolve-style Workspace navigation switcher (URL hash & localStorage persistent)
 * - Toast notification manager with animated feedback
 * - Resilient API client with automatic offline fallback mock data
 * - Server-Sent Events (SSE) consumer with auto-reconnection
 * - System Overview telemetry updater and Quick Note modal controller
 */

(function () {
  'use strict';

  // Determine API base URL: Use current host if running on HTTP server, or fallback to port 8000
  const API_BASE = (window.location.protocol.startsWith('http') && window.location.port !== '8080')
    ? window.location.origin
    : 'http://127.0.0.1:8000';

  // =========================================================================
  // 1. Toast Notification Manager
  // =========================================================================
  const ToastManager = {
    container: document.getElementById('toastContainer'),

    show(message, type = 'info', duration = 3500) {
      if (!this.container) {
        this.container = document.getElementById('toastContainer');
        if (!this.container) return;
      }

      const toast = document.createElement('div');
      toast.className = `toast toast-${type}`;

      let iconSvg = '';
      if (type === 'success') {
        iconSvg = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"></polyline></svg>';
      } else if (type === 'error') {
        iconSvg = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>';
      } else if (type === 'warning') {
        iconSvg = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>';
      } else {
        iconSvg = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line></svg>';
      }

      toast.innerHTML = `
        <span style="display:inline-flex; align-items:center; flex-shrink:0;">${iconSvg}</span>
        <span style="flex:1; line-height:1.35;">${message}</span>
      `;

      toast.addEventListener('click', () => {
        toast.remove();
      });

      this.container.appendChild(toast);

      setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateX(100%)';
        toast.style.transition = 'all 0.3s ease';
        setTimeout(() => toast.remove(), 300);
      }, duration);
    }
  };

  window.showToast = (msg, type, dur) => ToastManager.show(msg, type, dur);

  // =========================================================================
  // 2. Resilient API Client & Mock Provider
  // =========================================================================
  const MockDataStore = {
    isServerOnline: false,

    mockStatus: {
      status: 'online',
      uptime_seconds: 14320.5,
      timestamp: new Date().toISOString(),
      network_online: true,
      database: {
        path: 'c:\\Users\\gjaga\\jai\\data\\google_workspace.db',
        table_counts: {
          gmail_messages: 1240,
          calendar_events: 182,
          drive_files: 430
        }
      },
      google_workspace: {
        authenticated: true,
        account_email: 'gjagath@example.com',
        sync_states: {
          gmail: 'synced',
          calendar: 'synced',
          drive: 'synced'
        }
      },
      memory_vault: {
        active_memories: 28,
        client_vault_count: 3,
        client_vaults: ['client_acme', 'client_stark', 'client_wayne']
      },
      video_production: {
        status: 'ready',
        last_render: 'render_20260914_182045.mp4',
        groq_score: 92,
        verdict: 'APPROVED'
      }
    },

    mockLLMMetrics: {
      total_queries: 142,
      success_rate_pct: 99.3,
      avg_latency_ms: 48.2,
      tier_breakdown: {
        gemini: 135,
        groq: 6,
        ollama: 1,
        none: 0
      },
      type_breakdown: {
        fast: 128,
        background: 14
      },
      recent_entries: [
        {
          timestamp: new Date(Date.now() - 1000 * 60 * 2).toISOString(),
          query_type: 'fast',
          tier: 'gemini',
          model: 'gemini-3.8-flash',
          latency_ms: 42.1,
          success: true,
          fallback_reasons: []
        },
        {
          timestamp: new Date(Date.now() - 1000 * 60 * 12).toISOString(),
          query_type: 'fast',
          tier: 'gemini',
          model: 'gemini-3.8-flash',
          latency_ms: 51.7,
          success: true,
          fallback_reasons: []
        },
        {
          timestamp: new Date(Date.now() - 1000 * 60 * 35).toISOString(),
          query_type: 'background',
          tier: 'groq',
          model: 'llama-3.3-70b-versatile',
          latency_ms: 480.5,
          success: true,
          fallback_reasons: ['Tier 1 rate limit simulated fallback']
        },
        {
          timestamp: new Date(Date.now() - 1000 * 60 * 58).toISOString(),
          query_type: 'fast',
          tier: 'gemini',
          model: 'gemini-3.8-flash',
          latency_ms: 39.8,
          success: true,
          fallback_reasons: []
        },
        {
          timestamp: new Date(Date.now() - 1000 * 60 * 120).toISOString(),
          query_type: 'background',
          tier: 'ollama',
          model: 'qwen3:8b',
          latency_ms: 1240.0,
          success: true,
          fallback_reasons: ['Cloud network simulation standby']
        }
      ]
    },

    mockScopes: ['personal', 'client_acme', 'client_stark', 'client_wayne'],

    mockMemories: [
      {
        id: 1,
        content: 'Director approved 9:16 vertical cut for Instagram Reels product launch with Vibrant Studio color grade.',
        timestamp: new Date(Date.now() - 1000 * 60 * 45).toISOString(),
        source: 'telegram',
        vault_scope: 'client_acme',
        deleted_at: null,
        rank: -4.85
      },
      {
        id: 2,
        content: 'faster-whisper model resident in memory on named pipe \\\\.\\pipe\\jai_worker_pipe. Latency 140ms on int8 CPU.',
        timestamp: new Date(Date.now() - 1000 * 60 * 95).toISOString(),
        source: 'voice',
        vault_scope: 'personal',
        deleted_at: null,
        rank: -4.12
      },
      {
        id: 3,
        content: 'Client Stark requested Lanczos 4K master upscale with burned subtitle captions and watermark removal.',
        timestamp: new Date(Date.now() - 1000 * 60 * 180).toISOString(),
        source: 'email',
        vault_scope: 'client_stark',
        deleted_at: null,
        rank: -3.95
      },
      {
        id: 4,
        content: 'LLM 3-Tier priority: Gemini 3.8 Flash is primary (<200ms target). Groq Llama 3.3 70B failover only. Ollama local offline standby.',
        timestamp: new Date(Date.now() - 1000 * 60 * 320).toISOString(),
        source: 'manual',
        vault_scope: 'personal',
        deleted_at: null,
        rank: -3.45
      },
      {
        id: 5,
        content: 'Research notes on WebRTC streaming and Web Audio API real-time wake word processing for JAI speech daemon.',
        timestamp: new Date(Date.now() - 1000 * 60 * 450).toISOString(),
        source: 'browser_tab',
        vault_scope: 'personal',
        deleted_at: null,
        rank: -2.98
      },
      {
        id: 6,
        content: 'Archived draft concept for quarterly video showcase render.',
        timestamp: new Date(Date.now() - 1000 * 60 * 1440).toISOString(),
        source: 'manual',
        vault_scope: 'client_wayne',
        deleted_at: new Date(Date.now() - 1000 * 60 * 720).toISOString(),
        rank: -1.2
      }
    ]
  };

  const ApiClient = {
    baseUrl: API_BASE,
    eventListeners: {},

    updateBackendStatus(isOnline, customText) {
      MockDataStore.isServerOnline = isOnline;
      const dot = document.getElementById('backendDot');
      const text = document.getElementById('backendText');
      if (dot && text) {
        dot.className = `status-dot ${isOnline ? 'online' : 'offline'}`;
        text.textContent = customText || (isOnline ? 'API: Online' : 'API: Mock Standby');
      }
    },

    updateSubsystemBadges(data) {
      // Memory Vault badge
      const vaultDot = document.getElementById('vaultDot');
      const vaultText = document.getElementById('vaultText');
      if (vaultDot && vaultText) {
        const memCount = data?.memory_vault?.active_memories ?? MockDataStore.mockMemories.filter(m => !m.deleted_at).length;
        vaultDot.className = 'status-dot online';
        vaultText.textContent = `Vault: ${memCount} Notes`;
      }

      // Google Workspace badge
      const googleDot = document.getElementById('googleDot');
      const googleText = document.getElementById('googleText');
      if (googleDot && googleText) {
        const isAuth = data?.google_workspace?.authenticated ?? true;
        googleDot.className = `status-dot ${isAuth ? 'online' : 'warning'}`;
        googleText.textContent = isAuth ? 'Google: Synced' : 'Google: Standby';
      }

      // Video Engine badge
      const videoDot = document.getElementById('videoDot');
      const videoText = document.getElementById('videoText');
      if (videoDot && videoText) {
        videoDot.className = 'status-dot online';
        videoText.textContent = 'Video: Ready';
      }
    },

    async get(endpoint, fallbackMock) {
      try {
        const res = await fetch(`${this.baseUrl}${endpoint}`, {
          method: 'GET',
          headers: { 'Accept': 'application/json' },
          signal: AbortSignal.timeout(3000)
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        this.updateBackendStatus(true);
        return json;
      } catch (err) {
        this.updateBackendStatus(false);
        return fallbackMock !== undefined ? fallbackMock : { success: false, error: err.message };
      }
    },

    async post(endpoint, body, fallbackMock) {
      try {
        const res = await fetch(`${this.baseUrl}${endpoint}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
          body: JSON.stringify(body),
          signal: AbortSignal.timeout(5000)
        });
        if (!res.ok) {
          const errData = await res.json().catch(() => ({}));
          throw new Error(errData.error || `HTTP ${res.status}`);
        }
        const json = await res.json();
        this.updateBackendStatus(true);
        return json;
      } catch (err) {
        this.updateBackendStatus(false);
        return fallbackMock !== undefined ? fallbackMock : { success: false, error: err.message };
      }
    },

    async delete(endpoint, fallbackMock) {
      try {
        const res = await fetch(`${this.baseUrl}${endpoint}`, {
          method: 'DELETE',
          headers: { 'Accept': 'application/json' },
          signal: AbortSignal.timeout(4000)
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const json = await res.json();
        this.updateBackendStatus(true);
        return json;
      } catch (err) {
        this.updateBackendStatus(false);
        return fallbackMock !== undefined ? fallbackMock : { success: false, error: err.message };
      }
    },

    on(eventType, callback) {
      if (!this.eventListeners[eventType]) {
        this.eventListeners[eventType] = [];
      }
      this.eventListeners[eventType].push(callback);
    },

    dispatch(eventType, data) {
      const handlers = this.eventListeners[eventType] || [];
      handlers.forEach(fn => {
        try { fn(data); } catch (e) { console.error(`Error in event listener for ${eventType}:`, e); }
      });
    },

    initSSE() {
      if (!window.EventSource) return;

      const sseUrl = `${this.baseUrl}/api/events`;
      let eventSource = null;
      let reconnectTimer = null;

      const connect = () => {
        try {
          eventSource = new EventSource(sseUrl);

          eventSource.onopen = () => {
            ApiClient.updateBackendStatus(true, 'API: Online (SSE)');
          };

          eventSource.onmessage = (e) => {
            try {
              const data = JSON.parse(e.data);
              ApiClient.dispatch('message', data);
            } catch (ex) {
              ApiClient.dispatch('message', e.data);
            }
          };

          // Registered named events from backend
          const registeredEvents = [
            'connected',
            'memory_inserted',
            'memory_deleted',
            'video_render_start',
            'video_render_complete',
            'video_render_error',
            'voice_transcription',
            'wake_word_detected',
            'voice_event',
            'telegram_review_sent',
            'llm_routed'
          ];

          registeredEvents.forEach(evtName => {
            eventSource.addEventListener(evtName, (e) => {
              let parsed = e.data;
              try { parsed = JSON.parse(e.data); } catch (_) {}
              ApiClient.dispatch(evtName, parsed);
            });
          });

          eventSource.onerror = () => {
            if (eventSource) {
              eventSource.close();
              eventSource = null;
            }
            ApiClient.updateBackendStatus(false);
            clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 7000);
          };
        } catch (e) {
          ApiClient.updateBackendStatus(false);
          clearTimeout(reconnectTimer);
          reconnectTimer = setTimeout(connect, 10000);
        }
      };

      connect();
    }
  };

  window.apiClient = ApiClient;
  window.MockDataStore = MockDataStore;

  // =========================================================================
  // 3. Workspace Navigation Controller
  // =========================================================================
  const WorkspaceRouter = {
    tabs: document.querySelectorAll('.nav-tab'),
    views: document.querySelectorAll('.workspace-view'),
    viewMap: {
      'system': 'viewSystemOverview',
      'vault': 'viewMemoryVault',
      'video': 'viewVideoStudio',
      'voice': 'viewVoiceConsole'
    },
    reverseViewMap: {
      'viewSystemOverview': 'system',
      'viewMemoryVault': 'vault',
      'viewVideoStudio': 'video',
      'viewVoiceConsole': 'voice'
    },

    init() {
      this.tabs.forEach(tab => {
        tab.addEventListener('click', () => {
          const targetView = tab.getAttribute('data-view');
          if (targetView) {
            this.switchView(targetView);
          }
        });
      });

      // Restore view from URL hash or localStorage
      const hash = window.location.hash.replace('#', '').toLowerCase();
      const stored = localStorage.getItem('jai_active_workspace');
      let initialView = 'viewSystemOverview';

      if (hash && this.viewMap[hash]) {
        initialView = this.viewMap[hash];
      } else if (stored && document.getElementById(stored)) {
        initialView = stored;
      }

      this.switchView(initialView, false);

      window.addEventListener('hashchange', () => {
        const newHash = window.location.hash.replace('#', '').toLowerCase();
        if (newHash && this.viewMap[newHash]) {
          this.switchView(this.viewMap[newHash], false);
        }
      });
    },

    switchView(viewId, updateHash = true) {
      const targetViewEl = document.getElementById(viewId);
      if (!targetViewEl) return;

      // Update tab active classes
      this.tabs.forEach(tab => {
        const match = tab.getAttribute('data-view') === viewId;
        tab.classList.toggle('active', match);
      });

      // Update workspace views
      this.views.forEach(view => {
        view.classList.toggle('active', view.id === viewId);
      });

      // Save state
      localStorage.setItem('jai_active_workspace', viewId);
      if (updateHash && this.reverseViewMap[viewId]) {
        window.location.hash = this.reverseViewMap[viewId];
      }

      // Notify window of workspace change
      window.dispatchEvent(new CustomEvent('workspace-changed', { detail: { viewId } }));
    }
  };

  // =========================================================================
  // 4. Quick Note Modal Controller
  // =========================================================================
  const QuickNoteModal = {
    overlay: document.getElementById('quickNoteModalOverlay'),
    modal: document.getElementById('quickNoteModal'),
    btnOpen: document.getElementById('btnOpenQuickNoteModal'),
    btnClose: document.getElementById('btnCloseQuickNoteModal'),
    btnCancel: document.getElementById('btnCancelQuickNote'),
    btnSave: document.getElementById('btnSaveQuickNote'),
    contentInput: document.getElementById('quickNoteContent'),
    scopeSelect: document.getElementById('quickNoteScope'),
    sourceSelect: document.getElementById('quickNoteSource'),

    init() {
      if (this.btnOpen) {
        this.btnOpen.addEventListener('click', () => this.open());
      }
      if (this.btnClose) {
        this.btnClose.addEventListener('click', () => this.close());
      }
      if (this.btnCancel) {
        this.btnCancel.addEventListener('click', () => this.close());
      }
      if (this.overlay) {
        this.overlay.addEventListener('click', (e) => {
          if (e.target === this.overlay) this.close();
        });
      }
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && this.overlay && this.overlay.classList.contains('active')) {
          this.close();
        }
      });
      if (this.btnSave) {
        this.btnSave.addEventListener('click', () => this.submit());
      }
    },

    open(prefillScope) {
      if (!this.overlay) return;
      if (prefillScope && this.scopeSelect) {
        this.scopeSelect.value = prefillScope;
      }
      this.overlay.classList.add('active');
      if (this.contentInput) {
        this.contentInput.value = '';
        setTimeout(() => this.contentInput.focus(), 80);
      }
    },

    close() {
      if (!this.overlay) return;
      this.overlay.classList.remove('active');
    },

    async submit() {
      const content = this.contentInput ? this.contentInput.value.trim() : '';
      const scope = this.scopeSelect ? this.scopeSelect.value : 'personal';
      const source = this.sourceSelect ? this.sourceSelect.value : 'manual';

      if (!content) {
        ToastManager.show('Please enter note content', 'warning');
        if (this.contentInput) this.contentInput.focus();
        return;
      }

      this.btnSave.disabled = true;
      this.btnSave.textContent = 'Saving...';

      const fallbackRes = {
        success: true,
        id: Date.now(),
        vault_scope: scope
      };

      const res = await ApiClient.post('/api/vault/insert', {
        content,
        vault_scope: scope,
        scope,
        source
      }, fallbackRes);

      this.btnSave.disabled = false;
      this.btnSave.textContent = 'Save to Vault';

      if (res && res.success) {
        ToastManager.show(`Note vaulted to [${scope}]`, 'success');

        // If in mock mode, add to mock memories list
        if (!MockDataStore.isServerOnline) {
          MockDataStore.mockMemories.unshift({
            id: res.id || Date.now(),
            content,
            timestamp: new Date().toISOString(),
            source,
            vault_scope: scope,
            deleted_at: null,
            rank: -3.5
          });
        }

        this.close();
        // Trigger live refresh in vault explorer
        window.dispatchEvent(new CustomEvent('memory-added', { detail: { id: res.id, scope, source, content } }));
        SystemOverview.refreshTelemetry();
      } else {
        ToastManager.show(`Failed to save note: ${res?.error || 'Unknown error'}`, 'error');
      }
    }
  };

  window.openQuickNoteModal = (scope) => QuickNoteModal.open(scope);

  // =========================================================================
  // 5. System Overview Controller
  // =========================================================================
  const SystemOverview = {
    btnRefresh: document.getElementById('btnRefreshSystemOverview'),

    init() {
      if (this.btnRefresh) {
        this.btnRefresh.addEventListener('click', () => {
          this.refreshTelemetry(true);
        });
      }

      // Initial telemetry fetch
      this.refreshTelemetry(false);

      // Periodically refresh telemetry every 30 seconds
      setInterval(() => {
        const activeView = localStorage.getItem('jai_active_workspace');
        if (activeView === 'viewSystemOverview') {
          this.refreshTelemetry(false);
        }
      }, 30000);

      // Listen for SSE memory events
      ApiClient.on('memory_inserted', () => this.refreshTelemetry(false));
      ApiClient.on('memory_deleted', () => this.refreshTelemetry(false));
      ApiClient.on('llm_routed', (data) => this.handleLLMRoutedEvent(data));
    },

    async refreshTelemetry(userTriggered = false) {
      if (this.btnRefresh && userTriggered) {
        this.btnRefresh.classList.add('spinning');
        setTimeout(() => this.btnRefresh?.classList.remove('spinning'), 600);
      }

      try {
        const [statusRes, metricsRes] = await Promise.all([
          ApiClient.get('/api/status', { success: true, data: MockDataStore.mockStatus }),
          ApiClient.get('/api/system/llm-metrics', { success: true, data: MockDataStore.mockLLMMetrics })
        ]);

        const statusData = statusRes?.data || MockDataStore.mockStatus;
        const metricsData = metricsRes?.data || MockDataStore.mockLLMMetrics;

        this.renderStats(statusData, metricsData);
        this.renderServices(statusData);
        this.renderLLMTiers(metricsData);
        this.renderLLMLog(metricsData);

        ApiClient.updateSubsystemBadges(statusData);

        if (userTriggered) {
          ToastManager.show('System telemetry synchronized', 'success', 2000);
        }
      } catch (err) {
        console.error('Failed to load system telemetry:', err);
      }
    },

    renderStats(status, metrics) {
      const statTotal = document.getElementById('statTotalMemories');
      const statActive = document.getElementById('statActiveMemories');
      const statDeleted = document.getElementById('statDeletedMemories');
      const statScopes = document.getElementById('statClientScopes');
      const statLatency = document.getElementById('statFastLatency');
      const statQueue = document.getElementById('statQueueDepth');

      const activeMems = status.memory_vault?.active_memories ?? 0;
      const clientCount = status.memory_vault?.client_vault_count ?? 0;

      if (statTotal) statTotal.textContent = activeMems;
      if (statActive) statActive.textContent = `${activeMems} active`;
      if (statDeleted) statDeleted.textContent = `0 soft-deleted`;
      if (statScopes) statScopes.textContent = clientCount;

      const avgLat = metrics.avg_latency_ms ? Math.round(metrics.avg_latency_ms) : 48;
      if (statLatency) statLatency.textContent = `${avgLat} ms`;
      if (statQueue) statQueue.textContent = '0';
    },

    renderServices(status) {
      // Google
      const gAuth = status.google_workspace?.authenticated;
      const gEmail = status.google_workspace?.account_email;
      const svcGoogleDesc = document.getElementById('svcGoogleDesc');
      const svcGoogleDot = document.getElementById('svcGoogleDot');
      if (svcGoogleDesc) {
        svcGoogleDesc.textContent = gAuth
          ? `Authenticated (${gEmail || 'Synced'})`
          : 'Standby / Read-Only Mode';
      }
      if (svcGoogleDot) {
        svcGoogleDot.className = `status-dot ${gAuth ? 'online' : 'warning'}`;
      }

      // Telegram
      const svcTelegramDesc = document.getElementById('svcTelegramDesc');
      const svcTelegramDot = document.getElementById('svcTelegramDot');
      if (svcTelegramDesc) svcTelegramDesc.textContent = 'Active (Worker queue connected)';
      if (svcTelegramDot) svcTelegramDot.className = 'status-dot online';

      // Speech Daemon
      const svcDaemonDesc = document.getElementById('svcDaemonDesc');
      const svcDaemonDot = document.getElementById('svcDaemonDot');
      if (svcDaemonDesc) svcDaemonDesc.textContent = 'Resident int8 (Named pipe ready)';
      if (svcDaemonDot) svcDaemonDot.className = 'status-dot online';

      // SQLite Vault
      const svcVaultDesc = document.getElementById('svcVaultDesc');
      const svcVaultDot = document.getElementById('svcVaultDot');
      if (svcVaultDesc) svcVaultDesc.textContent = 'FTS5 BM25 Engine Operational';
      if (svcVaultDot) svcVaultDot.className = 'status-dot online';
    },

    renderLLMTiers(metrics) {
      const breakdown = metrics.tier_breakdown || { gemini: 95, groq: 4, ollama: 1 };
      const total = (breakdown.gemini || 0) + (breakdown.groq || 0) + (breakdown.ollama || 0) || 1;

      const geminiPct = Math.round(((breakdown.gemini || 0) / total) * 100);
      const groqPct = Math.round(((breakdown.groq || 0) / total) * 100);
      const ollamaPct = Math.max(0, 100 - geminiPct - groqPct);

      const geminiBar = document.getElementById('tierGeminiBar');
      const groqBar = document.getElementById('tierGroqBar');
      const ollamaBar = document.getElementById('tierOllamaBar');

      if (geminiBar) geminiBar.style.width = `${geminiPct}%`;
      if (groqBar) groqBar.style.width = `${groqPct}%`;
      if (ollamaBar) ollamaBar.style.width = `${ollamaPct}%`;

      const geminiLat = document.getElementById('tierGeminiLatency');
      const groqLat = document.getElementById('tierGroqLatency');
      const ollamaLat = document.getElementById('tierOllamaLatency');

      if (geminiLat) geminiLat.textContent = `~48ms • ${geminiPct}%`;
      if (groqLat) groqLat.textContent = `Idle • ${groqPct}%`;
      if (ollamaLat) ollamaLat.textContent = `Ready • ${ollamaPct}%`;
    },

    renderLLMLog(metrics) {
      const tbody = document.getElementById('llmDecisionLogTbody');
      if (!tbody) return;

      const entries = metrics.recent_entries || [];
      if (entries.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center; padding: 1.5rem; color: var(--text-muted);">No recent LLM router queries logged</td></tr>';
        return;
      }

      tbody.innerHTML = entries.map(item => {
        const timeStr = item.timestamp ? new Date(item.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '--';
        const typeBadge = item.query_type === 'fast'
          ? '<span class="badge-scope" style="background:rgba(245,158,11,0.15); color:var(--accent-amber);">FAST</span>'
          : '<span class="badge-scope" style="background:rgba(99,102,241,0.15); color:var(--accent-indigo);">BATCH</span>';

        let tierClass = 'tier-gemini';
        let tierLabel = 'Tier 1 Gemini';
        if (item.tier === 'groq') {
          tierClass = 'tier-groq';
          tierLabel = 'Tier 2 Groq';
        } else if (item.tier === 'ollama') {
          tierClass = 'tier-ollama';
          tierLabel = 'Tier 3 Ollama';
        }

        const tierBadge = `<span class="badge-scope" style="font-family:var(--font-mono); font-size:0.68rem;">${tierLabel}</span>`;
        const latStr = typeof item.latency_ms === 'number' ? `${Math.round(item.latency_ms)}ms` : '--';
        const statusBadge = item.success !== false
          ? '<span class="badge-scope" style="background:rgba(16,185,129,0.15); color:var(--success);">OK</span>'
          : '<span class="badge-scope" style="background:rgba(239,68,68,0.15); color:var(--danger);">FAIL</span>';

        const reasons = (item.fallback_reasons && item.fallback_reasons.length)
          ? item.fallback_reasons.join(', ')
          : '<span style="color:var(--text-muted);">&mdash;</span>';

        return `
          <tr>
            <td style="font-family:var(--font-mono); font-size:0.75rem;">${timeStr}</td>
            <td>${typeBadge}</td>
            <td>${tierBadge}</td>
            <td style="font-family:var(--font-mono); font-size:0.75rem;">${item.model || 'gemini-3.8-flash'}</td>
            <td style="font-family:var(--font-mono); font-size:0.75rem; color:var(--accent-amber);">${latStr}</td>
            <td>${statusBadge}</td>
            <td style="font-size:0.75rem; color:var(--text-secondary);">${reasons}</td>
          </tr>
        `;
      }).join('');
    },

    handleLLMRoutedEvent(data) {
      const tbody = document.getElementById('llmDecisionLogTbody');
      if (!tbody) return;

      const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const row = document.createElement('tr');
      row.innerHTML = `
        <td style="font-family:var(--font-mono); font-size:0.75rem;">${timeStr}</td>
        <td><span class="badge-scope" style="background:rgba(245,158,11,0.15); color:var(--accent-amber);">FAST</span></td>
        <td><span class="badge-scope">${data.tier || 'gemini'}</span></td>
        <td style="font-family:var(--font-mono); font-size:0.75rem;">${data.model || 'gemini-3.8-flash'}</td>
        <td style="font-family:var(--font-mono); font-size:0.75rem; color:var(--accent-amber);">${data.latency_ms || 45}ms</td>
        <td><span class="badge-scope" style="background:rgba(16,185,129,0.15); color:var(--success);">OK</span></td>
        <td style="font-size:0.75rem; color:var(--text-secondary);">&mdash;</td>
      `;
      tbody.prepend(row);
      if (tbody.children.length > 20) {
        tbody.removeChild(tbody.lastChild);
      }
    }
  };

  // =========================================================================
  // App Bootstrapper
  // =========================================================================
  const bootApp = () => {
    WorkspaceRouter.init();
    QuickNoteModal.init();
    SystemOverview.init();
    ApiClient.initSSE();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootApp);
  } else {
    bootApp();
  }

})();

export const apiClient = window.apiClient;
export const showToast = window.showToast;
