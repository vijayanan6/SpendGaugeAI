// dashboard.js — Alpine x-data component for /usage. Polls GET /usage/data
// (Basic Auth, inherited from the page load — see docs/DESIGN.md §8) and
// derives every readout/table/chart value from that one payload. Formulas
// (burn rate, runway, cache hit rate, cache savings) are ported unchanged
// from the source project's usage.html so the numbers stay consistent with
// its proven accounting.
const POLL_INTERVAL_MS = 15000;

function usageDashboard() {
  return {
    project: '',
    data: { totals: {}, by_model: [], by_day: [], by_session: [], by_tool: [], by_project: [], credit: {} },
    saving: false,
    _pollHandle: null,

    cfgBalance: '0.00',
    cfgAlert: '1.00',
    cfgReset: false,

    async init() {
      await this.load();
      this._startPolling();
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
          clearInterval(this._pollHandle);
        } else {
          this.load();
          this._startPolling();
        }
      });
      this.$watch('project', () => this.load());
    },

    _startPolling() {
      clearInterval(this._pollHandle);
      this._pollHandle = setInterval(() => this.load(), POLL_INTERVAL_MS);
    },

    async load() {
      const qs = this.project ? ('?project=' + encodeURIComponent(this.project)) : '';
      try {
        const res = await fetch('/usage/data' + qs);
        if (!res.ok) return;
        this.data = await res.json();
        if (!this.saving) {
          this.cfgBalance = (this.data.credit.starting_balance || 0).toFixed(2);
          this.cfgAlert = (this.data.credit.alert_threshold || 1).toFixed(2);
        }
        this.$nextTick(() => {
          renderDailyChart(this.dailyChartData());
          renderModelDonut(this.donutSegments);
        });
      } catch (e) {
        // Silent — dashboard just shows stale data until the next poll tick.
      }
    },

    async saveConfig() {
      const balance = parseFloat(this.cfgBalance);
      const alertThreshold = parseFloat(this.cfgAlert) || 1.0;
      if (isNaN(balance) || balance <= 0) {
        alert('Enter a valid starting balance.');
        return;
      }
      if (this.cfgReset && !confirm(
        'Reset spend tracking? This archives the current period and starts fresh from now. ' +
        'Historical charts and tables are never deleted — only the remaining/burn-rate math resets.'
      )) return;

      this.saving = true;
      try {
        await fetch('/usage/credit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ starting_balance: balance, alert_threshold: alertThreshold, reset: this.cfgReset }),
        });
        this.cfgReset = false;
        await this.load();
      } finally {
        this.saving = false;
      }
    },

    // ── formatting ──────────────────────────────────────────────────────
    fmtCost(v) { return '$' + (v || 0).toFixed(4); },
    fmtCost2(v) { return '$' + (v || 0).toFixed(2); },
    fmtTokens(n) {
      n = n || 0;
      if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
      if (n >= 1000) return (n / 1000).toFixed(0) + 'K';
      return String(n);
    },
    fmtDate(s) { return s ? s.slice(0, 10) : '—'; },

    // ── credit / gauge ──────────────────────────────────────────────────
    get remaining() {
      const c = this.data.credit || {};
      return Math.max((c.starting_balance || 0) - (c.period_cost_usd || 0), 0);
    },
    get remainingPct() {
      const c = this.data.credit || {};
      return (c.starting_balance || 0) > 0 ? Math.min((this.remaining / c.starting_balance) * 100, 100) : 0;
    },
    get statusTier() {
      const c = this.data.credit || {};
      if (!c.starting_balance) return 'good';
      if (this.remaining <= (c.alert_threshold || 1)) return 'critical';
      if (this.remaining <= (c.warning_threshold || 5)) return 'warning';
      return 'good';
    },
    get statusColorVar() {
      return { good: 'var(--good)', warning: 'var(--warning)', critical: 'var(--critical)' }[this.statusTier];
    },
    gaugeCircumference: 2 * Math.PI * 52,
    get gaugeDashoffset() {
      return this.gaugeCircumference * (1 - this.remainingPct / 100);
    },

    // Burn rate: active days within the current tracking period. A fresh
    // period (just reset, no usage yet) has 0 active days — fall back to the
    // previous period's rate as an estimate rather than a hard $0.00/day.
    get burnRate() {
      const c = this.data.credit || {};
      if ((c.period_active_days || 0) > 0) {
        return { value: (c.period_cost_usd || 0) / c.period_active_days, estimate: false };
      }
      if ((c.prev_period_days || 0) > 0) {
        return { value: (c.prev_period_cost_usd || 0) / c.prev_period_days, estimate: true };
      }
      return { value: 0, estimate: false };
    },
    get runwayText() {
      const rate = this.burnRate.value;
      if (rate <= 0) return '∞';
      return '~' + Math.floor(this.remaining / rate) + 'd' + (this.burnRate.estimate ? '*' : '');
    },
    forecast(days) { return this.fmtCost2(this.burnRate.value * days); },

    // ── cache stats ─────────────────────────────────────────────────────
    get cacheHitRatePct() {
      const t = this.data.totals || {};
      const totalIn = (t.total_input || 0) + (t.total_cache_read || 0);
      return totalIn > 0 ? Math.round(((t.total_cache_read || 0) / totalIn) * 100) : 0;
    },
    get cacheSavings() {
      const t = this.data.totals || {};
      return ((t.total_cache_read || 0) / 1000) * 0.0018;
    },

    // ── token breakdown bars ────────────────────────────────────────────
    get tokenBars() {
      const t = this.data.totals || {};
      const items = [
        { label: 'Input', val: t.total_input || 0, color: 'var(--primary)' },
        { label: 'Cache Read', val: t.total_cache_read || 0, color: 'var(--secondary)' },
        { label: 'Output', val: t.total_output || 0, color: 'var(--good)' },
        { label: 'Cache Write', val: t.total_cache_write || 0, color: 'var(--text-muted)' },
      ];
      const max = Math.max(...items.map((i) => i.val), 1);
      return items.map((i) => ({ ...i, pct: Math.round((i.val / max) * 100) }));
    },

    // ── model helpers ───────────────────────────────────────────────────
    modelColor(i) {
      return ['var(--primary)', 'var(--secondary)', 'var(--good)', 'var(--text-muted)'][i % 4];
    },
    modelLabel(model) {
      if (model.includes('haiku')) return 'Haiku';
      if (model.includes('sonnet')) return 'Sonnet';
      if (model.includes('opus')) return 'Opus';
      return model;
    },
    get donutSegments() {
      const total = (this.data.totals || {}).total_cost_usd || 0;
      const circ = 2 * Math.PI * 42;
      let cumulative = 0;
      return (this.data.by_model || []).map((m, i) => {
        const pct = total > 0 ? m.cost_usd / total : 0;
        const segLen = circ * pct;
        const seg = {
          model: m.model,
          requests: m.requests,
          cost_usd: m.cost_usd,
          pct: Math.round(pct * 100),
          color: this.modelColor(i),
          dashArray: segLen.toFixed(2) + ' ' + (circ - segLen).toFixed(2),
          dashOffset: -cumulative,
        };
        cumulative += segLen;
        return seg;
      });
    },

    // ── shares ──────────────────────────────────────────────────────────
    sharePct(cost) {
      const total = (this.data.totals || {}).total_cost_usd || 0;
      return total > 0 ? Math.round((cost / total) * 100) : 0;
    },
    toolFreqPct(calls) {
      const max = Math.max(...(this.data.by_tool || []).map((t) => t.calls), 1);
      return Math.round((calls / max) * 100);
    },

    dailyChartData() {
      return [...(this.data.by_day || [])].reverse();
    },
  };
}
