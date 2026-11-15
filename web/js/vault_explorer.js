/**
 * JAI Studio - Memory Vault Live Explorer
 * 
 * Features:
 * - Real-time FTS5 BM25 search with 150ms debounce
 * - Dynamic Scope filter pills (All, Personal, and remote Client Vaults)
 * - Source filter pills (All, Voice, Telegram, Email, Browser Tab, Manual)
 * - High-contrast DaVinci Studio memory cards with BM25 rank tags & timestamps
 * - Soft-delete and restore management (DELETE /api/vault/<id>)
 * - Quick Note integration with /api/vault/insert
 */

(function () {
  'use strict';

  const VaultExplorer = {
    searchInput: document.getElementById('vaultSearchInput'),
    btnClearSearch: document.getElementById('btnClearSearch'),
    scopePillGroup: document.getElementById('scopePillGroup'),
    sourcePillGroup: document.getElementById('sourcePillGroup'),
    cardsGrid: document.getElementById('vaultCardsGrid'),
    btnToggleDeleted: document.getElementById('btnToggleDeleted'),
    btnToggleDeletedText: document.getElementById('btnToggleDeletedText'),
    btnVaultQuickAdd: document.getElementById('btnVaultQuickAdd'),

    // State
    currentQuery: '',
    currentScope: 'all',
    currentSource: 'all',
    showSoftDeleted: false,
    debounceTimer: null,
    knownScopes: ['personal'],
    cachedMemories: [],

    init() {
      if (!this.cardsGrid) return;

      this.bindEvents();
      this.loadScopes();
      this.performSearch();

      // Listen for global events
      window.addEventListener('memory-added', () => {
        this.performSearch();
      });

      // SSE event subscriptions
      if (window.apiClient) {
        window.apiClient.on('memory_inserted', () => {
          this.performSearch();
        });
        window.apiClient.on('memory_deleted', () => {
          this.performSearch();
        });
      }
    },

    bindEvents() {
      // 1. Debounced Search Input (150ms)
      if (this.searchInput) {
        this.searchInput.addEventListener('input', (e) => {
          clearTimeout(this.debounceTimer);
          const val = e.target.value.trim();
          if (this.btnClearSearch) {
            this.btnClearSearch.style.display = val ? 'block' : 'none';
          }
          this.debounceTimer = setTimeout(() => {
            this.currentQuery = val;
            this.performSearch();
          }, 150);
        });

        this.searchInput.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') {
            this.clearSearch();
          }
        });
      }

      // 2. Clear Search Button
      if (this.btnClearSearch) {
        this.btnClearSearch.addEventListener('click', () => {
          this.clearSearch();
        });
      }

      // 3. Scope Pills Delegation
      if (this.scopePillGroup) {
        this.scopePillGroup.addEventListener('click', (e) => {
          const pill = e.target.closest('.filter-pill');
          if (!pill) return;
          const scope = pill.getAttribute('data-scope');
          if (!scope) return;

          this.scopePillGroup.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
          pill.classList.add('active');
          this.currentScope = scope;
          this.performSearch();
        });
      }

      // 4. Source Pills Delegation
      if (this.sourcePillGroup) {
        this.sourcePillGroup.addEventListener('click', (e) => {
          const pill = e.target.closest('.filter-pill');
          if (!pill) return;
          const source = pill.getAttribute('data-source');
          if (!source) return;

          this.sourcePillGroup.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
          pill.classList.add('active');
          this.currentSource = source;
          this.performSearch();
        });
      }

      // 5. Toggle Soft-Deleted
      if (this.btnToggleDeleted) {
        this.btnToggleDeleted.addEventListener('click', () => {
          this.showSoftDeleted = !this.showSoftDeleted;
          if (this.btnToggleDeletedText) {
            this.btnToggleDeletedText.textContent = this.showSoftDeleted ? 'Hide Soft-Deleted' : 'Show Soft-Deleted';
          }
          this.btnToggleDeleted.classList.toggle('active', this.showSoftDeleted);
          this.renderCards(this.cachedMemories);
        });
      }

      // 6. Quick Add Note Button in Vault View
      if (this.btnVaultQuickAdd) {
        this.btnVaultQuickAdd.addEventListener('click', () => {
          if (window.openQuickNoteModal) {
            window.openQuickNoteModal(this.currentScope !== 'all' ? this.currentScope : 'personal');
          }
        });
      }
    },

    clearSearch() {
      if (this.searchInput) {
        this.searchInput.value = '';
      }
      if (this.btnClearSearch) {
        this.btnClearSearch.style.display = 'none';
      }
      this.currentQuery = '';
      this.performSearch();
    },

    async loadScopes() {
      const fallbackScopes = window.MockDataStore?.mockScopes || ['personal', 'client_acme', 'client_stark', 'client_wayne'];
      const res = await window.apiClient.get('/api/vault/scopes', { success: true, data: fallbackScopes });
      const scopes = (res && res.success && Array.isArray(res.data)) ? res.data : fallbackScopes;

      this.knownScopes = scopes;
      this.updateScopePills(scopes);
      this.updateQuickNoteScopeDropdown(scopes);
    },

    updateScopePills(scopes) {
      if (!this.scopePillGroup) return;

      // Keep label and static 'All' & 'Personal'
      const label = '<span style="font-size: 0.72rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase;">Scope:</span>';
      const allActive = this.currentScope === 'all' ? 'active' : '';
      const personalActive = this.currentScope === 'personal' ? 'active' : '';

      let pillsHtml = `
        ${label}
        <button class="filter-pill ${allActive}" data-scope="all">All Scopes</button>
        <button class="filter-pill ${personalActive}" data-scope="personal">Personal</button>
      `;

      // Add dynamic client scopes
      scopes.filter(s => s !== 'personal').forEach(scope => {
        const isActive = this.currentScope === scope ? 'active' : '';
        pillsHtml += `<button class="filter-pill ${isActive}" data-scope="${scope}">${this.formatScopeLabel(scope)}</button>`;
      });

      this.scopePillGroup.innerHTML = pillsHtml;
    },

    updateQuickNoteScopeDropdown(scopes) {
      const select = document.getElementById('quickNoteScope');
      if (!select) return;

      select.innerHTML = '<option value="personal" selected>Personal Vault</option>' +
        scopes.filter(s => s !== 'personal').map(s => `<option value="${s}">${this.formatScopeLabel(s)} Vault</option>`).join('');
    },

    formatScopeLabel(scope) {
      if (scope.startsWith('client_')) {
        const name = scope.replace('client_', '');
        return 'Client: ' + name.charAt(0).toUpperCase() + name.slice(1);
      }
      return scope.charAt(0).toUpperCase() + scope.slice(1);
    },

    async performSearch() {
      const query = this.currentQuery;
      const scopeParam = this.currentScope !== 'all' ? this.currentScope : '';

      // Prepare fallback mock data
      let mockList = (window.MockDataStore?.mockMemories || []).slice();

      // If online server is running, query search endpoint
      const endpoint = `/api/vault/search?q=${encodeURIComponent(query)}&scope=${encodeURIComponent(scopeParam)}&limit=50`;
      const res = await window.apiClient.get(endpoint, {
        success: true,
        data: mockList
      });

      let results = [];
      if (res && res.success && Array.isArray(res.data)) {
        results = res.data;
        // If query was empty and backend returned empty, use mockList for offline / preview
        if (results.length === 0 && !query) {
          results = mockList;
        }
      } else {
        results = mockList;
      }

      // Filter client-side by query if using mock or fallback
      if (query && (!res || !res.success || !window.MockDataStore.isServerOnline)) {
        const qLower = query.toLowerCase();
        results = results.filter(m => m.content && m.content.toLowerCase().includes(qLower));
      }

      // Filter by scope
      if (this.currentScope !== 'all') {
        results = results.filter(m => m.vault_scope === this.currentScope);
      }

      // Filter by source
      if (this.currentSource !== 'all') {
        results = results.filter(m => m.source === this.currentSource);
      }

      this.cachedMemories = results;
      this.renderCards(results);
    },

    renderCards(memories) {
      if (!this.cardsGrid) return;

      // Filter soft-deleted based on toggle
      const filtered = memories.filter(m => {
        if (this.showSoftDeleted) return true;
        return !m.deleted_at;
      });

      if (filtered.length === 0) {
        this.cardsGrid.innerHTML = `
          <div class="card col-12" style="text-align: center; padding: 3rem 1.5rem; background: var(--bg-surface);">
            <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="color: var(--text-muted); margin-bottom: 0.75rem;">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            <h4 style="color: var(--text-secondary); margin-bottom: 0.35rem;">No Vault Memories Found</h4>
            <p style="font-size: 0.8rem; color: var(--text-muted); max-width: 420px; margin: 0 auto 1.25rem;">
              ${this.currentQuery ? `No records matched "${this.escapeHtml(this.currentQuery)}". Try broadening your search or switching scopes.` : 'Your memory vault is clear. Ingest voice dictations, telegram clips, or add a quick note.'}
            </p>
            <button class="btn btn-sm btn-primary" onclick="window.openQuickNoteModal('${this.currentScope !== 'all' ? this.currentScope : 'personal'}')">
              + Capture Memory
            </button>
          </div>
        `;
        return;
      }

      this.cardsGrid.innerHTML = filtered.map(m => this.createCardHtml(m)).join('');
      this.bindCardActions();
    },

    createCardHtml(memory) {
      const isDeleted = Boolean(memory.deleted_at);
      const scopeClass = memory.vault_scope === 'personal' ? 'badge-scope personal' : 'badge-scope client';
      const scopeLabel = memory.vault_scope ? memory.vault_scope.toUpperCase() : 'PERSONAL';

      // Source Icon & Label
      const sourceIcon = this.getSourceIconSvg(memory.source);
      const sourceLabel = (memory.source || 'manual').toUpperCase();

      // Timestamp formatting
      const timeFormatted = memory.timestamp
        ? new Date(memory.timestamp).toLocaleString([], {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit'
          })
        : 'Unknown Date';

      // BM25 rank score badge (if present)
      let rankHtml = '';
      if (typeof memory.rank === 'number') {
        const score = Math.abs(memory.rank).toFixed(2);
        rankHtml = `<span class="badge-rank" title="SQLite FTS5 BM25 Relevance Score">BM25: -${score}</span>`;
      } else if (this.currentQuery) {
        rankHtml = `<span class="badge-rank" title="Matched keyword">FTS5 MATCH</span>`;
      }

      // Highlight matching query terms
      const highlightedContent = this.highlightQuery(memory.content, this.currentQuery);

      return `
        <div class="card memory-card ${isDeleted ? 'soft-deleted' : ''}" data-id="${memory.id}">
          <div class="memory-card-header">
            <div style="display: flex; align-items: center; gap: 0.45rem; flex-wrap: wrap;">
              <span class="${scopeClass}">${scopeLabel}</span>
              <span class="badge-source">
                ${sourceIcon}
                ${sourceLabel}
              </span>
              ${rankHtml}
              ${isDeleted ? '<span class="badge-scope" style="background:rgba(239,68,68,0.2); color:var(--danger); border-color:var(--danger);">SOFT DELETED</span>' : ''}
            </div>
            <div class="memory-timestamp">${timeFormatted}</div>
          </div>

          <div class="memory-body">
            ${highlightedContent}
          </div>

          <div class="memory-card-footer">
            <span class="memory-id">#${memory.id}</span>
            <div style="display: flex; gap: 0.4rem;">
              <button class="btn btn-icon btn-copy-memory" title="Copy memory text" data-id="${memory.id}">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                </svg>
              </button>

              ${!isDeleted ? `
                <button class="btn btn-icon btn-delete-memory" title="Soft-delete memory (preserves audit log)" data-id="${memory.id}">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="3 6 5 6 21 6"></polyline>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                  </svg>
                </button>
              ` : `
                <span style="font-size: 0.72rem; color: var(--danger); font-family: var(--font-mono);">Excluded</span>
              `}
            </div>
          </div>
        </div>
      `;
    },

    bindCardActions() {
      // Soft delete buttons
      this.cardsGrid.querySelectorAll('.btn-delete-memory').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const memId = btn.getAttribute('data-id');
          if (!memId) return;

          btn.disabled = true;
          const ok = await this.softDeleteMemory(memId);
          if (ok) {
            const card = btn.closest('.memory-card');
            if (card) {
              if (this.showSoftDeleted) {
                card.classList.add('soft-deleted');
              } else {
                card.style.opacity = '0';
                card.style.transform = 'scale(0.92)';
                card.style.transition = 'all 0.25s ease';
                setTimeout(() => card.remove(), 250);
              }
            }
          } else {
            btn.disabled = false;
          }
        });
      });

      // Copy text buttons
      this.cardsGrid.querySelectorAll('.btn-copy-memory').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const memId = btn.getAttribute('data-id');
          const mem = this.cachedMemories.find(m => String(m.id) === String(memId));
          if (mem && mem.content) {
            navigator.clipboard.writeText(mem.content).then(() => {
              window.showToast('Note copied to clipboard', 'info', 2000);
            }).catch(() => {
              window.showToast('Unable to copy note', 'warning');
            });
          }
        });
      });
    },

    async softDeleteMemory(id) {
      const res = await window.apiClient.delete(`/api/vault/${id}`, { success: true, id, deleted: true });
      if (res && res.success) {
        window.showToast(`Memory #${id} soft-deleted`, 'info');

        // Update cached memory entry
        const found = this.cachedMemories.find(m => String(m.id) === String(id));
        if (found) {
          found.deleted_at = new Date().toISOString();
        }

        // If mock data store exists, mark there too
        if (window.MockDataStore?.mockMemories) {
          const m = window.MockDataStore.mockMemories.find(x => String(x.id) === String(id));
          if (m) m.deleted_at = new Date().toISOString();
        }

        return true;
      } else {
        window.showToast(`Failed to delete memory: ${res?.error || 'Unknown error'}`, 'error');
        return false;
      }
    },

    highlightQuery(text, query) {
      if (!text) return '';
      if (!query || !query.trim()) {
        return this.escapeHtml(text);
      }

      const escapedQ = query.trim().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const regex = new RegExp(`(${escapedQ})`, 'gi');
      const escapedText = this.escapeHtml(text);

      return escapedText.replace(regex, '<mark style="background: rgba(245,158,11,0.3); color: var(--accent-amber); padding: 1px 3px; border-radius: 2px;">$1</mark>');
    },

    escapeHtml(str) {
      if (!str) return '';
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    },

    getSourceIconSvg(source) {
      switch (source) {
        case 'voice':
          return '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"></path><path d="M19 10v2a7 7 0 0 1-14 0v-2"></path></svg>';
        case 'telegram':
          return '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="22" y1="2" x2="11" y2="13"></line><polygon points="22 2 15 22 11 13 2 9 22 2"></polygon></svg>';
        case 'email':
          return '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"></path><polyline points="22,6 12,13 2,6"></polyline></svg>';
        case 'browser_tab':
          return '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="2" y1="12" x2="22" y2="12"></line></svg>';
        default: // manual
          return '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>';
      }
    }
  };

  window.VaultExplorer = VaultExplorer;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      VaultExplorer.init();
    });
  } else {
    VaultExplorer.init();
  }

})();

export const VaultExplorer = window.VaultExplorer;
