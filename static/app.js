/* ============================================================
   history-browser · client app
   SOLID: Api (data), Store (state), View (DOM). Each module has
   one reason to change. View never knows fetch URLs; Api never
   touches DOM; Store is the only mutable state.
   DRY: el(), highlight(), groupByDay() reused everywhere.
   ============================================================ */

"use strict";

/* ---------- 1. tiny DOM helper (used by View only) ---------- */
function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k === 'html') throw new Error('refusing innerHTML — use children');
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (k === 'dataset') for (const [dk, dv] of Object.entries(v)) e.dataset[dk] = dv;
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const c of (Array.isArray(children) ? children : [children])) {
    if (c == null || c === false) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}
function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/* ---------- 2. text utilities (pure, no DOM, no IO) ---------- */
function highlight(text, terms) {
  // Returns DocumentFragment with matched terms wrapped in <mark>.
  const frag = document.createDocumentFragment();
  if (!text) return frag;
  if (!terms || !terms.length) { frag.appendChild(document.createTextNode(text)); return frag; }
  const escaped = terms
    .filter(t => t.length > 0)
    .sort((a, b) => b.length - a.length)
    .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!escaped.length) { frag.appendChild(document.createTextNode(text)); return frag; }
  const re = new RegExp(`(${escaped.join('|')})`, 'gi');
  let last = 0;
  for (const m of text.matchAll(re)) {
    if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
    frag.appendChild(el('mark', { text: m[0] }));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
  return frag;
}
function fmtRelative(iso) {
  // "2026-05-03 17:50" -> "Today" / "Yesterday" / "Wed Apr 30" / "2026-04-15"
  if (!iso) return '';
  const d = new Date(iso.replace(' ', 'T'));
  if (isNaN(d)) return iso;
  const now = new Date();
  const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return d.toLocaleDateString(undefined, { weekday: 'long' });
  if (diffDays < 365) return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}
function fmtTime(iso) {
  if (!iso) return '';
  // "2026-05-03 17:50" -> "17:50"
  const m = iso.match(/(\d{2}:\d{2})$/);
  return m ? m[1] : iso;
}
function groupByDay(sessions) {
  // Returns [[label, [sessions...]], ...] preserving recent-first order.
  const out = [];
  let last = null;
  for (const s of sessions) {
    const lbl = fmtRelative(s.last_iso);
    if (!last || last[0] !== lbl) { last = [lbl, []]; out.push(last); }
    last[1].push(s);
  }
  return out;
}

/* ---------- 3. Api — only place that knows fetch URLs ---------- */
const Api = {
  async sessions(query, { limit = 50, offset = 0 } = {}) {
    const p = new URLSearchParams();
    if (query.q) p.set('q', query.q);
    if (query.full) p.set('full', '1');
    if (query.since) p.set('since', query.since);
    if (query.long_only) p.set('long', '1');
    if (query.errored) p.set('errored', '1');
    if (query.cwd) p.set('cwd', query.cwd);
    p.set('limit', String(limit));
    p.set('offset', String(offset));
    const r = await fetch('/api/sessions?' + p.toString());
    if (!r.ok) throw new Error('sessions request failed');
    return r.json();
  },
  async session(sid) {
    const r = await fetch(`/api/session/${encodeURIComponent(sid)}?limit=0`);
    if (!r.ok) throw new Error('session detail failed');
    return r.json();
  },
};

/* ---------- 4. Store — single source of truth for app state ---------- */
class Store {
  constructor() {
    this.state = {
      q: '', since: '', long_only: false, errored: false, full: false,
      cwd: '', sort: 'recent', limit: 100,
      selectedSid: null,
    };
    this.listeners = new Set();
  }
  set(patch) { Object.assign(this.state, patch); this.emit(); }
  get() { return this.state; }
  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  emit() { for (const fn of this.listeners) fn(this.state); }

  /* URL state — load from query, push back when changed. */
  loadFromURL() {
    const p = new URLSearchParams(location.search);
    if (p.has('q')) this.state.q = p.get('q');
    if (p.has('since')) this.state.since = p.get('since');
    if (p.get('long') === '1') this.state.long_only = true;
    if (p.get('errored') === '1') this.state.errored = true;
    if (p.get('full') === '1') this.state.full = true;
    if (p.has('cwd')) this.state.cwd = p.get('cwd');
    if (p.has('sort')) this.state.sort = p.get('sort');
    this.emit();
  }
  syncToURL() {
    const p = new URLSearchParams();
    const s = this.state;
    if (s.q) p.set('q', s.q);
    if (s.since) p.set('since', s.since);
    if (s.long_only) p.set('long', '1');
    if (s.errored) p.set('errored', '1');
    if (s.full) p.set('full', '1');
    if (s.cwd) p.set('cwd', s.cwd);
    if (s.sort !== 'recent') p.set('sort', s.sort);
    const qs = p.toString();
    history.replaceState(null, '', qs ? `?${qs}` : location.pathname);
  }
}

/* ---------- 5. View — only place that touches DOM ---------- */
class View {
  constructor(store) {
    this.store = store;
    this.$ = (id) => document.getElementById(id);
    this.refs = {};
    this.toastTimer = null;
  }

  mount() {
    const refs = this.refs;
    refs.search    = this.$('search');
    refs.clear     = this.$('search-clear');
    refs.count     = this.$('result-count');
    refs.sort      = this.$('sort');
    refs.list      = this.$('list');
    refs.detail    = this.$('detail');
    refs.toast     = this.$('toast');
    this.bindHero();
    this.bindFilters();
  }

  bindHero() {
    let dt = null;
    this.refs.search.addEventListener('input', (e) => {
      const v = e.target.value;
      this.refs.clear.hidden = !v;
      clearTimeout(dt);
      dt = setTimeout(() => this.store.set({ q: v }), 180);
    });
    this.refs.clear.addEventListener('click', () => {
      this.refs.search.value = '';
      this.refs.clear.hidden = true;
      this.store.set({ q: '' });
      this.refs.search.focus();
    });
    this.refs.sort.addEventListener('change', () => this.store.set({ sort: this.refs.sort.value }));
  }

  bindFilters() {
    document.querySelectorAll('.filter-chip[data-filter]').forEach(chip => {
      const key = chip.dataset.filter;
      chip.addEventListener('click', () => {
        const cur = this.store.get();
        if (key === 'today') this.store.set({ since: cur.since === '1d' ? '' : '1d' });
        else if (key === 'week') this.store.set({ since: cur.since === '7d' ? '' : '7d' });
        else if (key === 'long') this.store.set({ long_only: !cur.long_only });
        else if (key === 'errored') this.store.set({ errored: !cur.errored });
        else if (key === 'full') this.store.set({ full: !cur.full });
      });
    });
  }

  reflectFilters() {
    const s = this.store.get();
    document.querySelectorAll('.filter-chip[data-filter]').forEach(chip => {
      const key = chip.dataset.filter;
      let on = false;
      if (key === 'today') on = s.since === '1d';
      else if (key === 'week') on = s.since === '7d';
      else if (key === 'long') on = s.long_only;
      else if (key === 'errored') on = s.errored;
      else if (key === 'full') on = s.full;
      chip.classList.toggle('on', on);
    });
    this.refs.sort.value = s.sort;
    if (this.refs.search.value !== s.q) this.refs.search.value = s.q;
    this.refs.clear.hidden = !s.q;
  }

  renderList(data, { append = false, loadedCount = 0 } = {}) {
    const list = this.refs.list;
    if (!append) clear(list);

    const totalShown = append ? loadedCount + data.sessions.length : data.shown;
    this.refs.count.textContent = data.matched === 0
      ? `0 of ${data.total_sessions}`
      : `${totalShown} of ${data.matched} matched · ${data.total_sessions} total`;

    if (!append && data.shown === 0) {
      list.appendChild(el('div', { class: 'placeholder',
        text: 'No sessions match. Clear a filter or try a different term.' }));
      return;
    }

    const arr = data.sessions || [];
    if (this.store.get().sort === 'longest') {
      arr.sort((a, b) => b.duration_ms - a.duration_ms);
    }

    const terms = (this.store.get().q || '').split(/\s+/).filter(Boolean);
    // For appended pages: continue an existing date group if the first session
    // of the new page falls under the same label as the last group on screen.
    const grouped = groupByDay(arr);
    if (append && grouped.length) {
      const lastGroup = list.querySelector('.group:last-of-type');
      const lastLabel = lastGroup?.querySelector('.group-head span')?.textContent;
      if (lastLabel && lastLabel === grouped[0][0]) {
        const [, sessions] = grouped.shift();
        sessions.forEach(s => lastGroup.appendChild(this.renderRow(s, terms)));
      }
    }
    for (const [label, group] of grouped) {
      list.appendChild(this.renderGroup(label, group, terms));
    }
  }

  showLoadingMore(visible) {
    const existing = this.refs.list.querySelector('.placeholder.more');
    if (visible && !existing) {
      this.refs.list.appendChild(el('div', { class: 'placeholder more', text: 'loading more…' }));
    } else if (!visible && existing) {
      existing.remove();
    }
  }

  renderGroup(label, sessions, terms) {
    const head = el('div', { class: 'group-head' }, [
      el('span', { text: label }),
      el('span', { class: 'count', text: String(sessions.length) }),
    ]);
    const wrap = el('div', { class: 'group' }, [head]);
    sessions.forEach(s => wrap.appendChild(this.renderRow(s, terms)));
    return wrap;
  }

  renderRow(s, terms) {
    const live = el('span', {
      class: 'live ' + (s.live ? 'is-live' : ''),
      text: s.live ? '●' : '·',
    });
    const time = el('span', { class: 'time', text: fmtTime(s.last_iso) });
    const proj = el('span', { class: 'proj', title: s.proj_path });
    proj.appendChild(document.createTextNode(s.proj || '~'));
    if (s.branch) proj.appendChild(el('span', { class: 'branch', text: '· ' + s.branch }));
    const head = el('span', { class: 'head' }, [time, proj]);
    const topic = el('span', { class: 'topic', title: s.topic });
    topic.appendChild(highlight(s.topic || '', terms));
    const dur = el('span', { class: 'dur', text: s.duration });

    const row = el('div', {
      class: 'row' + (this.store.get().selectedSid === s.sid ? ' selected' : ''),
      dataset: { sid: s.sid },
    }, [live, head, topic, dur]);
    row.addEventListener('click', () => {
      this.store.set({ selectedSid: s.sid });
      this.openDetail(s);
    });
    return row;
  }

  async openDetail(s) {
    const pane = this.refs.detail;
    pane.classList.remove('empty');
    pane.classList.add('open');           // mobile drawer reveal
    clear(pane);
    pane.appendChild(this.renderDetailSkeleton(s));
    document.querySelectorAll('.row.selected').forEach(r => r.classList.remove('selected'));
    document.querySelectorAll(`.row[data-sid="${CSS.escape(s.sid)}"]`).forEach(r => r.classList.add('selected'));
    let detail;
    try { detail = await Api.session(s.sid); }
    catch { this.fillDetailError(s); return; }
    this.fillDetail(s, detail);
  }

  renderDetailSkeleton(s) {
    return el('div', { class: 'detail' }, [
      el('button', { class: 'close-btn', onclick: () => this.closeDetail() }, ['← back']),
      this.renderResume(s),
      el('div', { class: 'detail-meta' }, [
        el('span', { text: s.proj_path }),
        el('span', { text: `${s.msg_count} prompts · ${s.duration}` }),
      ]),
      el('div', { class: 'anchor' }, [
        el('h2', { text: 'Initial prompt' }),
        el('div', { class: 'anchor-text empty', text: 'loading…' }),
      ]),
      el('div', { class: 'anchor' }, [
        el('h2', { text: 'Last assistant output' }),
        el('div', { class: 'anchor-text empty', text: 'loading…' }),
      ]),
    ]);
  }

  fillDetail(s, detail) {
    const pane = this.refs.detail;
    const anchors = pane.querySelectorAll('.anchor-text');
    if (anchors.length >= 2) {
      this.fillAnchor(anchors[0], detail.first_user_prompt);
      this.fillAnchor(anchors[1], detail.last_assistant_text);
    }
  }

  fillAnchor(node, text) {
    clear(node);
    if (text && text.trim()) {
      node.classList.remove('empty');
      node.appendChild(document.createTextNode(text));
    } else {
      node.classList.add('empty');
      node.appendChild(document.createTextNode('(no content captured)'));
    }
  }

  fillDetailError(s) {
    const pane = this.refs.detail;
    const anchors = pane.querySelectorAll('.anchor-text');
    anchors.forEach(a => { clear(a); a.classList.add('empty');
      a.appendChild(document.createTextNode('(failed to load)')); });
  }

  renderResume(s) {
    const code = el('code', { text: s.resume });
    const btn = el('button', { type: 'button', text: 'copy' });
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      navigator.clipboard.writeText(s.resume).then(() => {
        btn.classList.add('copied'); btn.textContent = 'copied';
        setTimeout(() => { btn.classList.remove('copied'); btn.textContent = 'copy'; }, 1200);
        this.toast('resume command copied');
      });
    });
    return el('div', { class: 'resume' }, [code, btn]);
  }

  closeDetail() {
    this.refs.detail.classList.remove('open');
    this.store.set({ selectedSid: null });
    document.querySelectorAll('.row.selected').forEach(r => r.classList.remove('selected'));
    setTimeout(() => {
      if (!this.store.get().selectedSid) {
        clear(this.refs.detail);
        this.refs.detail.classList.add('empty');
        this.refs.detail.appendChild(el('div', { text: 'select a session to see its first prompt and last reply' }));
      }
    }, 220);
  }

  emptyDetail() {
    const pane = this.refs.detail;
    pane.classList.add('empty');
    clear(pane);
    pane.appendChild(el('div', { text: 'select a session to see its first prompt and last reply' }));
  }

  toast(msg) {
    const t = this.refs.toast;
    t.textContent = msg;
    t.classList.add('show');
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => t.classList.remove('show'), 1400);
  }
}

/* ---------- 6. Controller — wires Api ↔ Store ↔ View ---------- */
class App {
  constructor() {
    this.store = new Store();
    this.view = new View(this.store);
    this.fetchToken = 0;
    this.pageSize = 50;
    this.offset = 0;
    this.loadedCount = 0;
    this.hasMore = false;
    this.loadingMore = false;
    this.observer = null;
  }

  async start() {
    this.view.mount();
    this.view.emptyDetail();
    this.store.loadFromURL();
    // Filter changes reset pagination — re-fetch from offset 0.
    this.store.subscribe(() => this.onStateChange());
    this.setupInfiniteScroll();
    await this.refresh();
    if (window.matchMedia('(min-width: 901px)').matches) this.view.refs.search.focus();
  }

  setupInfiniteScroll() {
    // IntersectionObserver on the scroll viewport. When the bottom 200px of
    // the list approaches the fold, fire loadMore(). Recreated each refresh
    // because the sentinel sits at the end of the list and gets removed/re-added.
    this.observerCallback = (entries) => {
      for (const e of entries) {
        if (e.isIntersecting && this.hasMore && !this.loadingMore) this.loadMore();
      }
    };
  }

  attachSentinel() {
    if (this.observer) this.observer.disconnect();
    if (!this.hasMore) return;
    const sentinel = el('div', { class: 'scroll-sentinel', 'aria-hidden': 'true' });
    sentinel.style.height = '1px';
    this.view.refs.list.appendChild(sentinel);
    this.observer = new IntersectionObserver(this.observerCallback, {
      root: null, rootMargin: '400px 0px', threshold: 0,
    });
    this.observer.observe(sentinel);
  }

  onStateChange() {
    this.view.reflectFilters();
    this.store.syncToURL();
    this.offset = 0;
    this.loadedCount = 0;
    this.refresh();
  }

  async refresh() {
    const token = ++this.fetchToken;
    let data;
    try { data = await Api.sessions(this.store.get(), { limit: this.pageSize, offset: 0 }); }
    catch { return; }
    if (token !== this.fetchToken) return;
    this.view.renderList(data, { append: false });
    this.offset = data.sessions.length;
    this.loadedCount = data.sessions.length;
    this.hasMore = !!data.has_more;
    this.attachSentinel();
  }

  async loadMore() {
    if (this.loadingMore || !this.hasMore) return;
    this.loadingMore = true;
    this.view.showLoadingMore(true);
    const token = this.fetchToken;
    let data;
    try { data = await Api.sessions(this.store.get(), { limit: this.pageSize, offset: this.offset }); }
    catch { this.loadingMore = false; this.view.showLoadingMore(false); return; }
    this.view.showLoadingMore(false);
    this.loadingMore = false;
    if (token !== this.fetchToken) return;   // a fresh refresh started — discard
    this.view.renderList(data, { append: true, loadedCount: this.loadedCount });
    this.offset += data.sessions.length;
    this.loadedCount += data.sessions.length;
    this.hasMore = !!data.has_more;
    this.attachSentinel();
  }
}

document.addEventListener('DOMContentLoaded', () => new App().start());
