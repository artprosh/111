/* Диспетчерская карта для ПК (/dispatch), 25.10.2026.
 * Данные — те же, что у админки Mini App (через /dispatch/api/admin/...), вход — cookie
 * после одноразовой ссылки (webapp/dispatch.py). Карта — OpenLayers + тайлы OpenStreetMap. */
(() => {
  'use strict';
  const OL_VERSION = '10.5.0';
  const REFRESH_MS = 10000;
  const STALE_MIN = 10; // точка курьера старше — «нет свежей точки» (как в Mini App)
  const SOON_MIN = 10; // за 10 минут до конца входа таймер мигает
  // Цвета меток курьеров — как COURIER_COLORS / colorDot в app.js (15 цветов × кружок/квадрат).
  const COURIER_COLORS = ['#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6',
    '#bfef45', '#fabed4', '#469990', '#dcbeff', '#9a6324', '#800000', '#000075'];
  const LIGHT = ['#ffe119', '#bfef45', '#fabed4', '#dcbeff', '#42d4f4'];
  const KIND = {
    free: { label: 'Не назначен', color: '#3b82f6' },
    busy: { label: 'У курьера', color: '#f97316' },
    time: { label: 'Ко времени', color: '#c026d3' },
    ind: { label: 'Индивидуальный', color: '#e53935' },
  };
  // Деньги курьера (28.10.2026): собрано, отдано за пустые, сдать в кассу (= собрано − отдано), заработал.
  const COUNTERS = [['cash', '💵 Собрано за заказы', 1], ['returns', '♻️ Отдано за пустые', -1], ['hand', '🏦 Сдать в кассу', 1], ['earned', '💼 Заработал', 1]];

  const $ = (s, el = document) => el.querySelector(s);
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (n) => `${Math.round(+n || 0).toLocaleString('ru-RU')} ₽`;
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }) : '');
  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }) : '');
  function ago(iso) {
    if (!iso) return '';
    const min = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (min < 1) return 'только что';
    if (min < 60) return `${min} мин назад`;
    const h = Math.floor(min / 60);
    return h < 24 ? `${h} ч ${min % 60} мин назад` : fmtDate(iso);
  }
  const isStale = (iso) => !iso || Date.now() - new Date(iso).getTime() > STALE_MIN * 60000;

  const state = {
    orders: [], couriers: [], mapCouriers: [], full: [], regions: [],
    filters: { regions: new Set(), kinds: new Set(['free', 'busy', 'time', 'ind']), showCouriers: true },
    selected: new Map(), // id заказа → {status, courier_id} на момент выбора
    detail: null, // {type: 'order'|'courier', id}; заказ раскрывается прямо в списке справа
    open: new Set(), // раскрытые карточки заказов в списке
    tab: 'orders', targetCourier: null,
    session: null, serverOffset: 0, expired: false,
    map: null, orderSrc: null, courierSrc: null, fitted: false, keys: {},
  };

  // ------------------------------------------------------------ запросы
  async function api(method, path, body) {
    const opts = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
    if (method !== 'GET') {
      opts.headers['Content-Type'] = 'application/json';
      opts.headers['X-Dispatch'] = '1';
      opts.body = JSON.stringify(body || {});
    }
    const r = await fetch('/dispatch/api' + path, opts);
    let data = {};
    try { data = await r.json(); } catch (e) { /* пустой ответ */ }
    if (r.status === 401) { showOverlay('expired'); throw new Error(data.error || 'Вход истёк'); }
    if (!r.ok) throw new Error(data.error || `Ошибка ${r.status}`);
    return data;
  }
  let toastTimer = null;
  function toast(text, ms = 4000) {
    const el = $('[data-toast]');
    el.innerHTML = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, ms);
  }

  // ------------------------------------------------------------ вход и таймер
  function showOverlay(kind) {
    if (state.expired && kind !== 'confirm') return;
    state.expired = true;
    const el = $('[data-overlay]');
    const texts = {
      expired: ['⏰ Срок работы ссылки истёк', 'Вход на этом компьютере закрыт (прошли сутки, вход отозван или выполнен «Выйти везде»).'],
      out: ['👋 Вы вышли', 'Вход на этом компьютере закрыт.'],
    };
    const [title, text] = texts[kind] || texts.expired;
    el.innerHTML = `<div class="box"><h2>${title}</h2><p class="muted">${text}</p>
      <p>Новая ссылка: Mini App → Админка → «🗺 Карта» → <b>«🖥 Карта для ПК»</b>.</p></div>`;
    el.hidden = false;
  }
  const serverNow = () => Date.now() / 1000 + state.serverOffset;
  function tickBomb() {
    if (!state.session || state.expired) return;
    const left = Math.max(0, state.session.expires_ts - serverNow());
    const min = Math.floor(left / 60); // часы:минуты; цифры меняются раз в минуту
    const text = `${String(Math.floor(min / 60)).padStart(2, '0')}:${String(min % 60).padStart(2, '0')}`;
    const digits = $('[data-bomb-digits]');
    if (digits.textContent !== text) digits.textContent = text;
    $('[data-bomb]').classList.toggle('soon', min <= SOON_MIN);
    if (left <= 0) showOverlay('expired');
  }
  async function loadSession() {
    const s = await api('GET', '/session');
    state.session = s.session;
    state.serverOffset = s.server_ts - Date.now() / 1000;
    $('[data-who]').textContent = s.admin.name.replace(/\s*\(@[^)]*\)$/, '');
    $('[data-who]').title = s.admin.name;
    $('[data-bomb]').hidden = false;
    tickBomb();
  }

  // ------------------------------------------------------------ OpenLayers: сначала с нашего сервера, запасной — jsDelivr
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.onload = () => (window.ol && window.ol.Map ? resolve() : reject(new Error('нет ol')));
      s.onerror = () => { s.remove(); reject(new Error('не загрузилось: ' + src)); };
      document.head.appendChild(s);
    });
  }
  function loadCss(href) {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = href;
    document.head.appendChild(l);
  }
  async function loadOl() {
    if (window.ol && window.ol.Map) return 'есть';
    try {
      await loadScript('/dispatch/vendor/ol.js');
      loadCss('/dispatch/vendor/ol.css');
      return 'сервер';
    } catch (e) {
      await loadScript(`https://cdn.jsdelivr.net/npm/ol@${OL_VERSION}/dist/ol.js`);
      loadCss(`https://cdn.jsdelivr.net/npm/ol@${OL_VERSION}/ol.css`);
      return 'jsdelivr';
    }
  }

  // ------------------------------------------------------------ заказы: вид и фильтры
  function kindOf(o) {
    if (o.individual && o.status === 'new' && !o.courier) return 'ind';
    if (o.deliver_time && ['new', 'accepted', 'in_transit'].includes(o.status)) return 'time';
    return o.courier ? 'busy' : 'free';
  }
  function passes(o) {
    const f = state.filters;
    if (f.regions.size && !f.regions.has(o.region || 'none')) return false;
    return f.kinds.has(kindOf(o));
  }
  const hasPoint = (o) => o.customer_location && o.customer_location.lat != null && o.customer_location.lon != null;
  const firstLine = (s) => String(s || '').split('\n')[0];

  // ------------------------------------------------------------ карта
  function courierStyleKey(c) {
    return [c.color, c.name, pale(c), state.detail && state.detail.type === 'courier' && state.detail.id === c.id, c.orders.length].join('|');
  }
  function pale(c) { return !c.live.active || isStale(c.live.at) || c.status === 'break'; }
  function hexA(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  const styleCache = {};
  function courierStyle(c) {
    const ol = window.ol;
    const key = courierStyleKey(c);
    if (styleCache[key]) return styleCache[key];
    const color = COURIER_COLORS[c.color % COURIER_COLORS.length];
    const square = Math.floor(c.color / COURIER_COLORS.length) % 2 === 1;
    const alpha = pale(c) ? 0.38 : 1;
    const chosen = state.detail && state.detail.type === 'courier' && state.detail.id === c.id;
    const fill = new ol.style.Fill({ color: hexA(color, alpha) });
    const stroke = new ol.style.Stroke({ color: chosen ? '#fde047' : `rgba(0,0,0,${alpha})`, width: chosen ? 4 : 3 });
    const image = square
      ? new ol.style.RegularShape({ points: 4, radius: 17, angle: Math.PI / 4, fill, stroke })
      : new ol.style.Circle({ radius: 13, fill, stroke });
    const letter = (c.name || '?').trim()[0] || '?';
    const styles = [
      new ol.style.Style({
        image,
        text: new ol.style.Text({ text: letter.toUpperCase(), font: 'bold 13px system-ui, sans-serif', fill: new ol.style.Fill({ color: LIGHT.includes(color) ? '#111' : '#fff' }) }),
        zIndex: 20,
      }),
      new ol.style.Style({
        text: new ol.style.Text({
          text: c.name + (c.orders.length ? ` · 🚚${c.orders.length}` : ''), offsetY: 26, font: '600 12px system-ui, sans-serif',
          fill: new ol.style.Fill({ color: pale(c) ? '#cbd5e1' : '#fff' }),
          backgroundFill: new ol.style.Fill({ color: 'rgba(11,18,32,.82)' }), padding: [2, 5, 2, 5],
        }),
        zIndex: 20,
      }),
    ];
    styleCache[key] = styles;
    return styles;
  }
  function orderStyle(feature) {
    const ol = window.ol;
    const o = feature.get('order');
    const kind = kindOf(o);
    const sel = state.selected.has(o.id);
    const open = state.detail && state.detail.type === 'order' && state.detail.id === o.id;
    const zoom = state.map ? state.map.getView().getZoom() : 10;
    const key = ['o', kind, o.individual_any ? 1 : 0, sel, open, o.deliver_time || '', zoom >= 13 ? o.id : ''].join('|');
    if (styleCache[key]) return styleCache[key];
    const fillColor = kind === 'ind' ? KIND.free.color : KIND[kind].color;
    const ring = o.individual_any ? KIND.ind.color : '#ffffff';
    const out = [];
    if (sel || open) {
      out.push(new ol.style.Style({ image: new ol.style.Circle({ radius: 15, fill: new ol.style.Fill({ color: 'rgba(253,224,71,.28)' }), stroke: new ol.style.Stroke({ color: '#fde047', width: 3 }) }), zIndex: 9 }));
    }
    out.push(new ol.style.Style({
      image: new ol.style.Circle({ radius: kind === 'time' ? 10 : 8, fill: new ol.style.Fill({ color: fillColor }), stroke: new ol.style.Stroke({ color: ring, width: o.individual_any ? 3.5 : 2.5 }) }),
      zIndex: 10,
    }));
    const label = [kind === 'time' ? `к ${o.deliver_time}` : '', zoom >= 13 ? `№${o.id}` : ''].filter(Boolean).join(' · ');
    if (label) {
      out.push(new ol.style.Style({
        text: new ol.style.Text({
          text: label, offsetY: -20, font: kind === 'time' ? 'bold 15px system-ui, sans-serif' : '600 11px system-ui, sans-serif',
          fill: new ol.style.Fill({ color: '#fff' }),
          backgroundFill: new ol.style.Fill({ color: kind === 'time' ? 'rgba(162,28,175,.95)' : 'rgba(11,18,32,.8)' }), padding: [2, 5, 2, 5],
        }),
        zIndex: 11,
      }));
    }
    styleCache[key] = out;
    return out;
  }

  function initMap() {
    const ol = window.ol;
    const osm = new ol.source.OSM({ attributions: '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© участники OpenStreetMap, ODbL</a>' });
    let tileOk = 0, tileErr = 0;
    osm.on('tileloadend', () => { tileOk += 1; $('[data-map-note]').hidden = true; });
    osm.on('tileloaderror', () => {
      tileErr += 1;
      if (!tileOk && tileErr >= 6) {
        const note = $('[data-map-note]');
        note.textContent = 'Подложка карты (тайлы OpenStreetMap) не загружается из вашей сети. Метки заказов и курьеров при этом работают.';
        note.hidden = false;
      }
    });
    state.orderSrc = new ol.source.Vector();
    state.courierSrc = new ol.source.Vector();
    const orderLayer = new ol.layer.Vector({ source: state.orderSrc, style: orderStyle, zIndex: 10 });
    const courierLayer = new ol.layer.Vector({ source: state.courierSrc, style: (f) => courierStyle(f.get('courier')), zIndex: 20 });
    state.map = new ol.Map({
      target: $('[data-map]'),
      layers: [new ol.layer.Tile({ source: osm }), orderLayer, courierLayer],
      controls: [new ol.control.Zoom(), new ol.control.Attribution({ collapsible: false }), new ol.control.ScaleLine()],
      view: new ol.View({ center: ol.proj.fromLonLat([37.6176, 55.7558]), zoom: 10 }),
    });
    state.map.getView().on('change:resolution', () => orderLayer.changed());
    // Shift + рамка — выбрать несколько заказов (только видимые по фильтрам).
    const box = new ol.interaction.DragBox({ condition: ol.events.condition.shiftKeyOnly, className: 'ol-dragbox' });
    state.map.addInteraction(box);
    box.on('boxend', () => {
      const ext = box.getGeometry().getExtent();
      let added = 0;
      state.orderSrc.forEachFeatureInExtent(ext, (f) => {
        const o = f.get('order');
        if (!state.selected.has(o.id)) { select(o, false); added += 1; }
      });
      toast(added ? `Добавлено в выбор: ${added}` : 'В рамке нет новых заказов', 2000);
      afterSelection();
    });
    state.map.on('singleclick', (e) => {
      const hit = state.map.forEachFeatureAtPixel(e.pixel, (f) => f, { hitTolerance: 5 });
      if (!hit) return;
      if (hit.get('courier')) { openDetail('courier', hit.get('courier').id); return; }
      const o = hit.get('order');
      if (state.selected.has(o.id)) state.selected.delete(o.id); else select(o, false);
      openDetail('order', o.id, false);
      afterSelection();
    });
    state.map.on('pointermove', (e) => {
      const hit = state.map.hasFeatureAtPixel(e.pixel, { hitTolerance: 5 });
      state.map.getTargetElement().style.cursor = hit ? 'pointer' : '';
    });
  }

  function syncFeatures() {
    if (!state.map) return;
    const ol = window.ol;
    // Заказы: обновляем на месте, без очистки слоя — карта не мигает.
    const want = new Map(state.orders.filter((o) => hasPoint(o) && passes(o)).map((o) => [o.id, o]));
    state.orderSrc.getFeatures().forEach((f) => { if (!want.has(f.getId())) state.orderSrc.removeFeature(f); });
    want.forEach((o, id) => {
      const coord = ol.proj.fromLonLat([+o.customer_location.lon, +o.customer_location.lat]);
      let f = state.orderSrc.getFeatureById(id);
      if (!f) {
        f = new ol.Feature({ geometry: new ol.geom.Point(coord), order: o });
        f.setId(id);
        state.orderSrc.addFeature(f);
      } else {
        f.set('order', o, true);
        const g = f.getGeometry();
        const cur = g.getCoordinates();
        if (cur[0] !== coord[0] || cur[1] !== coord[1]) g.setCoordinates(coord);
        f.changed();
      }
    });
    // Курьеры на смене и на перерыве, у кого есть точка.
    const onMap = state.filters.showCouriers ? state.mapCouriers.filter((c) => c.status !== 'offline' && c.live.lat != null) : [];
    const wantC = new Map(onMap.map((c) => ['c' + c.id, c]));
    state.courierSrc.getFeatures().forEach((f) => { if (!wantC.has(f.getId())) state.courierSrc.removeFeature(f); });
    wantC.forEach((c, id) => {
      const coord = ol.proj.fromLonLat([+c.live.lon, +c.live.lat]);
      let f = state.courierSrc.getFeatureById(id);
      if (!f) {
        f = new ol.Feature({ geometry: new ol.geom.Point(coord), courier: c });
        f.setId(id);
        state.courierSrc.addFeature(f);
      } else {
        f.set('courier', c, true);
        f.getGeometry().setCoordinates(coord);
        f.changed();
      }
    });
    if (!state.fitted) {
      const pts = [...want.values()].map((o) => ol.proj.fromLonLat([+o.customer_location.lon, +o.customer_location.lat]))
        .concat(onMap.map((c) => ol.proj.fromLonLat([+c.live.lon, +c.live.lat])));
      if (pts.length) {
        state.fitted = true;
        state.map.getView().fit(ol.extent.boundingExtent(pts), { padding: [70, 60, 60, 60], maxZoom: 14, duration: 0 });
      }
    }
  }
  function focusOn(lon, lat) {
    if (!state.map) return;
    state.map.getView().animate({ center: window.ol.proj.fromLonLat([+lon, +lat]), zoom: Math.max(14, state.map.getView().getZoom()), duration: 300 });
  }

  // ------------------------------------------------------------ выбор и назначение
  function select(o, render = true) {
    state.selected.set(o.id, { status: o.status, courier_id: o.courier ? o.courier.id : null });
    if (render) afterSelection();
  }
  function afterSelection() {
    if (state.orderSrc) state.orderSrc.changed();
    renderSelbar();
    renderList();
  }
  const orderById = (id) => state.orders.find((o) => o.id === id);
  const fullCourier = (id) => state.full.find((c) => c.id === id);
  function shiftLabel(c) {
    const s = c.shift || {};
    if (s.status === 'online') return '🟢 на смене';
    if (s.status === 'break') return '🔴 перерыв';
    if (s.status === 'silent') return '🟥 не отвечает на опрос';
    if (s.status === 'ending') return '🏁 закончил смену';
    return '⚪ не на смене';
  }
  // 28.10.2026: хватает ли курьеру товаров на выбранные заказы (сервер проверяет сам и не даст назначить).
  function stockFit(c, ids) {
    const sh = c.shift || {};
    if (sh.status === 'silent') return { ok: false, text: '⛔ не отвечает на опрос' };
    if (!['online', 'break'].includes(sh.status)) return { ok: false, text: '⛔ не на смене' };
    if (!sh.stock) return { ok: false, text: '⛔ нет учёта товаров' };
    const need = {}, names = {};
    ids.map(orderById).filter((o) => o && !o.is_return && !(o.courier && o.courier.id === c.id))
      .forEach((o) => o.items.forEach((i) => { if (i.product_id != null) { need[i.product_id] = (need[i.product_id] || 0) + i.quantity; names[i.product_id] = i.name; } }));
    const have = (pid) => Math.max(0, +((sh.stock.now || {})[pid] || 0));
    const short = Object.entries(need).filter(([pid, n]) => n > have(pid)).map(([pid, n]) => `${names[pid]} — нужно ${n}, есть ${have(pid)}`);
    return short.length ? { ok: false, text: `⛔ не хватает: ${short.join(', ')}` } : { ok: true, text: '✅ хватает' };
  }
  function renderSelbar() {
    const bar = $('[data-selbar]');
    const active = state.full.filter((c) => c.is_active).sort((a, b) => rankShift(a) - rankShift(b) || a.name.localeCompare(b.name, 'ru'));
    if (state.targetCourier && !active.some((c) => c.id === state.targetCourier)) state.targetCourier = null;
    if (!state.selected.size && state.targetCourier) {
      // Сначала отметили курьера — теперь к нему выбирают заказы.
      const c = fullCourier(state.targetCourier);
      bar.hidden = false;
      bar.innerHTML = `<div class="row"><span style="display:flex;gap:8px;align-items:center;flex:1">${colorDot(c.color, (c.name || '?')[0])}<span><b>Курьер: ${esc(c.name)}</b><br><span class="small muted">Отметьте заказы ☐ во вкладке «Заказы» или на карте</span></span></span>
        <button class="btn ghost small" data-unpick>✕ Снять</button></div>`;
      $('[data-unpick]', bar).onclick = () => { state.targetCourier = null; renderSelbar(); renderList(); };
      return;
    }
    if (!state.selected.size) { bar.hidden = true; bar.innerHTML = ''; return; }
    const ids = [...state.selected.keys()];
    const busyCount = ids.map(orderById).filter((o) => o && o.courier).length;
    bar.hidden = false;
    bar.innerHTML = `<div><b>Выбрано заказов: ${ids.length}</b>${busyCount ? ` <span class="small warn-text">· у курьера уже ${busyCount}</span>` : ''}</div>
      <div class="sel-ids">${ids.map((id) => `<span class="sel-id">№${id}<button title="Убрать" data-unsel="${id}">✕</button></span>`).join('')}</div>
      <select data-target><option value="">— выберите курьера (или клик по его метке) —</option>
        ${active.map((c) => `<option value="${c.id}" ${state.targetCourier === c.id ? 'selected' : ''}>${esc(c.name)} · ${shiftLabel(c)} · ${esc(stockFit(c, ids).text)}</option>`).join('')}
      </select>
      <div class="row"><button class="btn" data-give ${state.targetCourier ? '' : 'disabled'}>🚴 ${state.targetCourier ? `Отдать: ${esc((fullCourier(state.targetCourier) || {}).name || '')} (${ids.length})` : 'Отдать курьеру'}</button><button class="btn ghost" data-unsel-all>Сбросить</button></div>`;
    bar.querySelectorAll('[data-unsel]').forEach((b) => { b.onclick = () => { state.selected.delete(+b.dataset.unsel); afterSelection(); }; });
    $('[data-unsel-all]', bar).onclick = () => { state.selected.clear(); afterSelection(); };
    $('[data-target]', bar).onchange = (e) => { state.targetCourier = e.target.value ? +e.target.value : null; renderSelbar(); renderList(); if (state.targetCourier) openDetail('courier', state.targetCourier, false); };
    $('[data-give]', bar).onclick = (e) => give(e.target);
  }
  const rankShift = (c) => ({ online: 0, break: 1 }[(c.shift || {}).status] ?? 2);

  function confirmBox(html, okText) {
    return new Promise((resolve) => {
      const el = $('[data-overlay]');
      el.innerHTML = `<div class="box">${html}<div class="actions" style="justify-content:center">
        <button class="btn" data-yes>${esc(okText)}</button><button class="btn ghost" data-no>Отмена</button></div></div>`;
      el.hidden = false;
      const done = (v) => { el.hidden = true; el.innerHTML = ''; resolve(v); };
      $('[data-yes]', el).onclick = () => done(true);
      $('[data-no]', el).onclick = () => done(false);
    });
  }
  async function give(btn) {
    const courier = fullCourier(state.targetCourier);
    if (!courier) return;
    const ids = [...state.selected.keys()];
    const others = ids.map(orderById).filter((o) => o && o.courier && o.courier.id !== courier.id);
    let reassign = false;
    if (others.length) {
      reassign = await confirmBox(`<h2>Переназначить?</h2>
        <p>Эти заказы уже у других курьеров. Их снимут с прежнего курьера (он получит «Заказ снят с вас») и отдадут <b>${esc(courier.name)}</b>:</p>
        <div class="confirm-list">${others.map((o) => `№${o.id} — у ${esc(o.courier.name)}${o.status === 'in_transit' ? ' · <b class="warn-text">уже в пути</b>' : ''}`).join('<br>')}</div>`, 'Переназначить');
      if (!reassign && others.length === ids.length) return;
    }
    if (courier.shift && courier.shift.status !== 'online') {
      const ok = await confirmBox(`<h2>Курьер не на смене</h2><p><b>${esc(courier.name)}</b> сейчас: ${shiftLabel(courier)}. Всё равно отдать?</p>`, 'Отдать');
      if (!ok) return;
    }
    btn.disabled = true;
    try {
      const r = await api('POST', '/assign', { order_ids: ids, courier_id: courier.id, reassign });
      const parts = [];
      if (r.assigned.length) parts.push(`✅ Отдано ${esc(courier.name)}: ${r.assigned.map((i) => '№' + i).join(', ')}`);
      if (r.already.length) parts.push(`ℹ️ Уже у него: ${r.already.map((i) => '№' + i).join(', ')}`);
      if (r.busy.length) parts.push(`⏭ Не тронуты (у других курьеров): ${r.busy.map((i) => '№' + i).join(', ')}`);
      if (r.failed.length) parts.push(`⛔ Не удалось (уже взяты или недоступны): ${r.failed.map((i) => '№' + i).join(', ')}`);
      toast(parts.join('<br>') || 'Готово', 8000);
      r.assigned.concat(r.already).forEach((id) => state.selected.delete(id));
      if (!r.failed.length && !r.busy.length) state.selected.clear();
      await refresh();
    } catch (e) {
      toast('⛔ ' + esc(e.message), 6000);
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------ карточки
  function openDetail(type, id, focus = true) {
    state.detail = { type, id };
    if (type === 'order') {
      // Заказ — не большой карточкой сверху, а раскрытой строкой в списке «Заказы» / «Без точки».
      const o = orderById(id);
      state.open.add(id);
      if (o) {
        const shown = (state.tab === 'orders' && passes(o)) || (state.tab === 'nopoint' && !hasPoint(o));
        if (!shown) setTab(passes(o) || hasPoint(o) ? 'orders' : 'nopoint');
      }
      renderList();
      const row = document.querySelector(`[data-list] [data-o="${id}"]`);
      if (row) row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
    if (type === 'courier' && state.selected.size) { state.targetCourier = id; renderSelbar(); }
    renderDetail();
    if (state.orderSrc) state.orderSrc.changed();
    if (state.courierSrc) state.courierSrc.changed();
    if (focus && type === 'order') {
      const o = orderById(id);
      if (o && hasPoint(o)) focusOn(o.customer_location.lon, o.customer_location.lat);
    }
    if (focus && type === 'courier') {
      const c = state.mapCouriers.find((x) => x.id === id);
      if (c && c.live.lat != null) focusOn(c.live.lon, c.live.lat);
    }
  }
  function colorDot(i, letter) {
    const c = COURIER_COLORS[i % COURIER_COLORS.length];
    const square = Math.floor(i / COURIER_COLORS.length) % 2 === 1;
    return `<span class="cdot ${square ? 'sq' : ''}" style="background:${c};color:${LIGHT.includes(c) ? '#111' : '#fff'}">${esc(letter)}</span>`;
  }
  function liveText(live) {
    if (!live || !live.active) return live && live.at ? `⚠️ Трансляция выключена · последняя точка ${ago(live.at)}` : '⚠️ Трансляция не включена';
    return isStale(live.at) ? `⚠️ Трансляция включена, но нет обновлений ${ago(live.at).replace(' назад', '')}` : `📡 Трансляция идёт · точка ${ago(live.at)}`;
  }
  // Числовой ключ — литры (пустые баллоны), иначе — название товара (26.10.2026: учёт всех товаров каталога).
  const stockLine = (obj) => (obj ? Object.entries(obj).filter(([, n]) => +n).map(([z, n]) => `${/^\d+$/.test(z) ? `${z} л` : esc(z)} · <b class="${n < 0 ? 'neg' : ''}">${n}</b>`).join('&nbsp;&nbsp; ') || 'нет' : '—');
  const stockNow = (st) => (st && st.items ? stockLine(Object.fromEntries(st.items.map((i) => [i.name, i.now]))) : '—');
  function courierHtml(id) {
    const c = fullCourier(id);
    if (!c) return '<div class="empty">Курьер не найден.</div>';
    const m = state.mapCouriers.find((x) => x.id === id) || { orders: [], live: c.live };
    const sh = c.shift || {};
    const status = sh.status === 'online' ? `🟢 На смене с ${fmtTime(sh.started_at)}`
      : sh.status === 'break' ? `🔴 Перерыв с ${fmtTime(sh.ended_at)}`
        : sh.ended_at ? `🔴 ${sh.end_reason === 'auto' ? 'Не ответил «Вы ещё на смене?» — офлайн' : 'Закончил смену'} · ${fmtDate(sh.ended_at)}` : '🔴 Не выходил на смену';
    const username = (c.username || '').replace(/^@/, '');
    const counters = c.counters || {};
    return `<h3>${colorDot(c.color, (c.name || '?')[0])} ${esc(c.name)}${c.is_active ? '' : ' <span class="small muted">(отключён)</span>'}</h3>
      <div>${status}</div>
      ${sh.status !== 'offline' ? `<div class="small ${m.live && m.live.active && !isStale(m.live.at) ? 'muted' : 'warn-text'}">${liveText(m.live)}</div>` : ''}
      ${m.live && m.live.at ? `<div class="small muted">Точка обновлена: ${fmtDate(m.live.at)} (МСК)</div>` : ''}
      <div class="kv">
        <span>📦 С собой</span><span>${sh.stock ? stockNow(sh.stock) : '—'}</span>
        <span>♻️ Пустые</span><span>${stockLine(c.empties)}</span>
        ${COUNTERS.map(([k, label, sign]) => { const v = k === 'hand' ? (counters.cash || 0) + (counters.returns || 0) : sign * (counters[k] || 0); return `<span>${label}</span><span class="${v < 0 ? 'neg' : ''}">${k === 'hand' ? `<b>${money(v)}</b>` : money(v)}${counters[k + '_since'] ? ` <span class="small muted">с ${fmtDate(counters[k + '_since'])}</span>` : ''}</span>`; }).join('')}
        <span>Рейтинг</span><span>${c.rating != null ? `⭐ ${c.rating} (${c.rating_count})` : '—'}</span>
        <span>Телефон</span><span>${c.phone ? `<a href="tel:${esc(String(c.phone).replace(/[^\d+]/g, ''))}" style="color:inherit">${esc(c.phone)}</a>` : '—'}</span>
        <span>Telegram</span><span>${username ? `<a href="https://t.me/${esc(username)}" target="_blank" rel="noopener" style="color:inherit">@${esc(username)}</a>` : '—'}</span>
      </div>
      ${c.reload_pending ? `<div class="box warn-text small">🏭 Ждёт подтверждения загрузки на складе: ${stockLine(c.reload_pending)}</div>` : ''}
      <div class="box"><b>Заказы в работе: ${m.orders.length}</b>${m.orders.map((o) => `<div class="small" style="margin-top:4px;cursor:pointer" data-goto="${o.id}">🚚 №${o.id} · ${esc(o.status_label)}${o.region ? ' · 📍 ' + esc(o.region) : ''}<div class="muted">${esc(o.address)}</div></div>`).join('')}</div>
      <div class="actions">
        ${m.live && m.live.lat != null && sh.status !== 'offline' ? `<button class="btn ghost small" data-focus-c="${c.id}">🎯 Показать на карте</button>` : ''}
        ${state.selected.size ? `<button class="btn small" data-give-to="${c.id}">🚴 Отдать ему выбранные (${state.selected.size})</button>` : ''}
      </div>`;
  }
  function orderHtml(id) {
    const o = orderById(id);
    if (!o) return '<div class="empty">Заказ уже закрыт или не найден.</div>';
    const kind = kindOf(o);
    const cust = o.customer || {};
    const sel = state.selected.has(o.id);
    return `<h3>Заказ №${o.id} <span class="badge ${kind}">${KIND[kind].label}</span></h3>
      <div class="small muted">${esc(o.number || '')} · ${esc(o.status_label)} · создан ${fmtDate(o.created_at)}</div>
      ${o.deliver_time ? `<div class="box" style="background:#a21caf33"><b>🟣 Доставка ко времени: к ${esc(o.deliver_time)}</b> (МСК)</div>` : ''}
      ${o.individual ? '<div class="box warn-text small">🧾 Индивидуальный заказ (не Москва) — ждёт администратора: условия доставки согласуйте в Mini App.</div>' : ''}
      <div class="box">${o.items.map((i) => `<div>${i.gift ? '🎁 ' : ''}${esc(i.name)} × ${i.quantity}${i.gift ? ' <span class="small muted">(подарок)</span>' : i.variant === 'exchange' ? ' <span class="small muted">(обмен)</span>' : i.variant === 'return' ? ' <span class="small muted">(возврат)</span>' : ''}</div>`).join('')}</div>
      <div class="kv">
        <span>Сумма</span><span><b>${money(o.total)}</b>${o.delivery_fee ? ` <span class="small muted">в т. ч. доставка ${money(o.delivery_fee)}${o.delivery_km ? ` · ${o.delivery_km} км` : ''}${o.delivery_toll ? ` · платная дорога ${money(o.delivery_toll)}` : ''}</span>` : ''}</span>
        <span>Оплата</span><span>${esc(o.payment_label)}${o.paid ? ' · ✅' : ' · ⏳ не подтверждена'}</span>
        ${o.is_return ? `<span>Возврат</span><span>выплатить покупателю ${money(o.return_payout)}</span>` : ''}
        <span>Адрес</span><span>${esc(o.address || '').replace(/\n/g, '<br>')}</span>
        <span>Регион</span><span>${esc(o.region_label || '—')}</span>
        <span>Покупатель</span><span>${esc(cust.name || '—')}${cust.phone ? `<br><a href="tel:${esc(String(cust.phone).replace(/[^\d+]/g, ''))}" style="color:inherit">${esc(cust.phone)}</a>` : ''}${cust.blocked ? ' <span class="neg">· в чёрном списке</span>' : ''}</span>
        <span>Курьер</span><span>${o.courier ? `${esc(o.courier.name)}${o.courier.phone ? ' · ' + esc(o.courier.phone) : ''}` : 'не назначен'}</span>
      </div>
      ${o.comment ? `<div class="box small">💬 ${esc(o.comment)}</div>` : ''}
      ${!hasPoint(o) ? '<div class="box small warn-text">📍 У заказа нет точки на карте — адрес только текстом.</div>' : ''}
      <div class="actions">
        <button class="btn small ${sel ? 'ghost' : ''}" data-toggle-sel="${o.id}">${sel ? 'Убрать из выбора' : '☑️ Выбрать'}</button>
        ${hasPoint(o) ? `<button class="btn ghost small" data-focus-o="${o.id}">🎯 Показать на карте</button>` : ''}
      </div>`;
  }
  function renderDetail() {
    const el = $('[data-detail]');
    if (!state.detail || state.detail.type === 'order') { el.hidden = true; el.innerHTML = ''; state.keys.detail = ''; return; }
    const html = state.detail.type === 'courier' ? courierHtml(state.detail.id) : orderHtml(state.detail.id);
    const key = state.detail.type + state.detail.id + html;
    if (state.keys.detail === key && !el.hidden) return;
    state.keys.detail = key;
    const scroll = el.scrollTop;
    el.hidden = false;
    el.innerHTML = `<button class="close" title="Закрыть" data-close>✕</button>${html}`;
    el.scrollTop = scroll;
    $('[data-close]', el).onclick = () => {
      state.detail = null;
      renderDetail();
      if (state.orderSrc) state.orderSrc.changed();
      if (state.courierSrc) state.courierSrc.changed();
    };
    el.querySelectorAll('[data-goto]').forEach((b) => { b.onclick = () => openDetail('order', +b.dataset.goto); });
    el.querySelectorAll('[data-focus-c]').forEach((b) => { b.onclick = () => openDetail('courier', +b.dataset.focusC); });
    el.querySelectorAll('[data-focus-o]').forEach((b) => { b.onclick = () => openDetail('order', +b.dataset.focusO); });
    el.querySelectorAll('[data-toggle-sel]').forEach((b) => {
      b.onclick = () => {
        const o = orderById(+b.dataset.toggleSel);
        if (!o) return;
        if (state.selected.has(o.id)) state.selected.delete(o.id); else select(o, false);
        afterSelection();
        renderDetail();
      };
    });
    el.querySelectorAll('[data-give-to]').forEach((b) => { b.onclick = () => { state.targetCourier = +b.dataset.giveTo; renderSelbar(); give(b); }; });
  }

  // ------------------------------------------------------------ списки
  function renderList() {
    const visible = state.orders.filter(passes);
    const noPoint = state.orders.filter((o) => !hasPoint(o));
    const active = state.full.filter((c) => c.is_active).sort((a, b) => rankShift(a) - rankShift(b) || a.name.localeCompare(b.name, 'ru'));
    $('[data-count="orders"]').textContent = visible.length;
    $('[data-count="couriers"]').textContent = active.filter((c) => (c.shift || {}).status !== 'offline').length;
    $('[data-count="nopoint"]').textContent = noPoint.length;
    let html = '';
    // Короткая строка заказа: кто, куда, контакт, что везём; ▾ — подробности тут же (26.10.2026).
    const orderItem = (o) => {
      const kind = kindOf(o);
      const cust = o.customer || {};
      const sel = state.selected.has(o.id);
      const open = state.open.has(o.id);
      const tel = cust.phone ? String(cust.phone).replace(/[^\d+]/g, '') : '';
      const what = o.items.map((i) => `${i.gift ? '🎁 ' : ''}${esc(i.name)} × ${i.quantity}${i.gift ? ' (подарок)' : i.variant === 'exchange' ? ' (обмен)' : ''}`).join(', ');
      const more = open ? `<div class="more">
          <div class="kv">
            <span>Статус</span><span>${esc(o.status_label)} · создан ${fmtDate(o.created_at)}</span>
            <span>Сумма</span><span><b>${money(o.total)}</b>${o.delivery_fee ? ` <span class="small muted">в т. ч. доставка ${money(o.delivery_fee)}${o.delivery_km ? ` · ${o.delivery_km} км` : ''}${o.delivery_toll ? ` · платная дорога ${money(o.delivery_toll)}` : ''}</span>` : ''}</span>
            <span>Оплата</span><span>${esc(o.payment_label)}${o.paid ? ' · ✅' : ' · ⏳ не подтверждена'}</span>
            ${o.is_return ? `<span>Возврат</span><span>выплатить покупателю <b>${money(o.return_payout)}</b>${o.descent ? ` <span class="small muted">(спуск ${o.descent.count} × ${money(o.descent.price)} вычтен)</span>` : ''}</span>` : ''}
            <span>Адрес</span><span>${esc(o.address || '').replace(/\n/g, '<br>')}</span>
            <span>Регион</span><span>${esc(o.region_label || '—')}</span>
            <span>Курьер</span><span>${o.courier ? `${esc(o.courier.name)}${o.courier.phone ? ' · ' + esc(o.courier.phone) : ''}` : 'не назначен'}</span>
            ${cust.username ? `<span>Telegram</span><span>@${esc(cust.username)}</span>` : ''}
          </div>
          ${o.deliver_time ? `<div class="box" style="background:#a21caf33">🟣 Доставка ко времени: <b>к ${esc(o.deliver_time)}</b> (МСК)</div>` : ''}
          ${o.individual ? '<div class="box warn-text small">🧾 Индивидуальный заказ (не Москва) — условия доставки согласуйте в Mini App.</div>' : ''}
          ${o.comment ? `<div class="box small">💬 ${esc(o.comment)}</div>` : ''}
          ${cust.blocked ? '<div class="box small neg">Покупатель в чёрном списке</div>' : ''}
          ${!hasPoint(o) ? '<div class="box small warn-text">📍 У заказа нет точки на карте — адрес только текстом.</div>' : ''}
          <div class="actions">${hasPoint(o) ? `<button class="btn ghost small" data-focus-o="${o.id}">🎯 Показать на карте</button>` : ''}</div>
        </div>` : '';
      return `<div class="item ${kind} ${sel ? 'sel' : ''} ${open ? 'open' : ''}" data-o="${o.id}">
        <div class="top"><span class="ttl"><button class="chk ${sel ? 'on' : ''}" data-sel-o="${o.id}" title="${sel ? 'Убрать из выбора' : 'Выбрать'}">${sel ? '✓' : ''}</button><b>№${o.id}</b>${o.is_return ? '<span class="tag ret">♻️ Возврат</span>' : ''}${o.deliver_time ? `<span class="tag time">к ${esc(o.deliver_time)}</span>` : ''}</span>
          <span class="small">${o.is_return ? `−${money(o.return_payout)}` : money(o.total)} <span class="chev">${open ? '▴' : '▾'}</span></span></div>
        <div class="line">📦 ${what || '—'}</div>
        <div class="line">📍 ${esc(firstLine(o.address).replace(/^📍\s*/u, ''))}${hasPoint(o) ? '' : ' <span class="warn-text">· без точки</span>'}</div>
        <div class="line">👤 ${esc(cust.name || '—')}${tel ? ` · <a href="tel:${esc(tel)}">${esc(cust.phone)}</a>` : ''}${o.courier ? ` <span class="muted">· 🚴 ${esc(o.courier.name)}</span>` : ''}</div>
        ${more}
      </div>`;
    };
    if (state.tab === 'orders') {
      html = visible.map(orderItem).join('') || '<div class="empty">Открытых заказов по выбранным фильтрам нет.</div>';
    } else if (state.tab === 'nopoint') {
      html = (noPoint.length ? '<div class="small muted">У этих заказов нет точки дома — адрес только текстом. Выбрать и отдать курьеру их можно и отсюда.</div>' : '')
        + (noPoint.map(orderItem).join('') || '<div class="empty">Все заказы стоят на карте.</div>');
    } else {
      html = active.map((c) => {
        const m = state.mapCouriers.find((x) => x.id === c.id) || { orders: [], live: c.live };
        const sh = c.shift || {};
        const picked = state.targetCourier === c.id;
        const n = state.selected.size;
        return `<div class="item ${picked ? 'sel' : ''}" data-c="${c.id}" style="${sh.status === 'offline' && !picked ? 'opacity:.6' : ''}">
          <div class="top"><span style="display:flex;gap:8px;align-items:center"><button class="chk round ${picked ? 'on' : ''}" data-pick-c="${c.id}" title="${picked ? 'Снять отметку' : 'Отметить курьера — потом выбрать ему заказы'}">${picked ? '✓' : ''}</button>${colorDot(c.color, (c.name || '?')[0])}<b>${esc(c.name)}</b></span><span class="small">${shiftLabel(c)}</span></div>
          <div class="small muted">🚚 в работе ${m.orders.length}${sh.stock ? ' · 📦 ' + stockNow(sh.stock) : ''}</div>
          ${sh.status !== 'offline' ? `<div class="small ${m.live && m.live.active && !isStale(m.live.at) ? 'muted' : 'warn-text'}">${liveText(m.live)}</div>` : ''}
          ${n ? (() => { const fit = stockFit(c, [...state.selected.keys()]); return `<div class="small ${fit.ok ? '' : 'warn-text'}" style="margin-top:6px">${esc(fit.text)}</div><button class="btn small give-row" data-give-row="${c.id}" ${fit.ok ? '' : 'disabled'}>🚴 Назначить ${n === 1 ? 'заказ' : `заказы (${n})`}: ${[...state.selected.keys()].map((i) => '№' + i).join(', ')}</button>`; })() : ''}
        </div>`;
      }).join('') || '<div class="empty">Курьеров пока нет.</div>';
    }
    const key = state.tab + html;
    const list = $('[data-list]');
    if (state.keys.list === key) return;
    state.keys.list = key;
    const scroll = list.scrollTop;
    list.innerHTML = html;
    list.scrollTop = scroll;
    list.querySelectorAll('[data-o]').forEach((el) => {
      el.onclick = (e) => {
        const o = orderById(+el.dataset.o);
        if (!o) return;
        if (e.target.closest('a')) return; // звонок по номеру
        const selBtn = e.target.closest('[data-sel-o]');
        if (selBtn || e.shiftKey || e.ctrlKey || e.metaKey) {
          if (state.selected.has(o.id)) state.selected.delete(o.id); else select(o, false);
          afterSelection();
          renderList();
          return;
        }
        if (e.target.closest('[data-focus-o]')) {
          if (hasPoint(o)) focusOn(o.customer_location.lon, o.customer_location.lat);
          return;
        }
        if (e.target.closest('.more')) return; // выделение текста в подробностях
        if (state.open.has(o.id)) {
          state.open.delete(o.id);
          if (state.detail && state.detail.type === 'order' && state.detail.id === o.id) state.detail = null;
          renderList();
          if (state.orderSrc) state.orderSrc.changed();
          return;
        }
        openDetail('order', o.id);
      };
    });
    list.querySelectorAll('[data-c]').forEach((el) => {
      el.onclick = (e) => {
        const id = +el.dataset.c;
        const gbtn = e.target.closest('[data-give-row]');
        if (gbtn) { state.targetCourier = id; renderSelbar(); give(gbtn); return; }
        if (e.target.closest('[data-pick-c]')) {
          state.targetCourier = state.targetCourier === id ? null : id;
          renderSelbar();
          if (state.targetCourier && !state.selected.size) { setTab('orders'); toast('Курьер отмечен — теперь отметьте ему заказы ☐', 3000); } else renderList();
          return;
        }
        openDetail('courier', id);
      };
    });
  }
  function renderFilters() {
    const regs = [['', 'Все регионы']].concat(state.regions.map((r) => [r.id, r.label]), [['none', 'Без региона']]);
    const f = state.filters;
    $('[data-f-regions]').innerHTML = regs.map(([id, label]) => {
      const on = id === '' ? !f.regions.size : f.regions.has(id);
      return `<button class="chip ${on ? 'on' : ''}" data-reg="${id}">${esc(label)}</button>`;
    }).join('');
    $('[data-f-status]').innerHTML = Object.entries(KIND).map(([k, v]) => `<button class="chip ${f.kinds.has(k) ? 'on' : ''}" data-kind="${k}"><span class="dot" style="background:${v.color}"></span>${v.label}</button>`).join('')
      + `<button class="chip ${f.showCouriers ? 'on' : ''}" data-show-c>🚴 Курьеры</button>`;
    document.querySelectorAll('[data-reg]').forEach((b) => {
      b.onclick = () => {
        const id = b.dataset.reg;
        if (!id) f.regions.clear(); else if (f.regions.has(id)) f.regions.delete(id); else f.regions.add(id);
        applyFilters();
      };
    });
    document.querySelectorAll('[data-kind]').forEach((b) => {
      b.onclick = () => { if (f.kinds.has(b.dataset.kind)) f.kinds.delete(b.dataset.kind); else f.kinds.add(b.dataset.kind); applyFilters(); };
    });
    $('[data-show-c]').onclick = () => { f.showCouriers = !f.showCouriers; applyFilters(); };
  }
  function applyFilters() {
    renderFilters();
    syncFeatures();
    renderList();
  }
  function renderStats() {
    const kinds = { free: 0, busy: 0, time: 0, ind: 0 };
    state.orders.forEach((o) => { kinds[kindOf(o)] += 1; });
    const online = state.full.filter((c) => c.is_active && (c.shift || {}).status === 'online').length;
    const brk = state.full.filter((c) => c.is_active && (c.shift || {}).status === 'break').length;
    $('[data-stats]').innerHTML = `<span>Открытых: <b>${state.orders.length}</b></span>
      <span style="color:${KIND.free.color}">● не назначено <b>${kinds.free}</b></span>
      <span style="color:${KIND.busy.color}">● у курьеров <b>${kinds.busy}</b></span>
      <span style="color:${KIND.time.color}">● ко времени <b>${kinds.time}</b></span>
      ${kinds.ind ? `<span style="color:${KIND.ind.color}">● индивид. <b>${kinds.ind}</b></span>` : ''}
      <span>🚴 на смене <b>${online}</b>${brk ? ` · перерыв <b>${brk}</b>` : ''}</span>`;
  }

  // ------------------------------------------------------------ обновление раз в 10 секунд
  let refreshing = false;
  async function refresh() {
    if (refreshing || state.expired) return;
    refreshing = true;
    const ind = $('[data-refresh]');
    try {
      const [orders, map, couriers] = await Promise.all([
        api('GET', '/admin/orders'), api('GET', '/admin/map'), api('GET', '/admin/couriers'),
      ]);
      state.orders = orders.orders;
      state.regions = orders.regions;
      state.mapCouriers = map.couriers;
      state.full = couriers.couriers;
      // Выбранный заказ закрыли или его отдали другому, пока он был выбран, — снять выбор и сказать.
      const notes = [];
      state.selected.forEach((was, id) => {
        const o = orderById(id);
        if (!o) { state.selected.delete(id); notes.push(`№${id} закрыт`); return; }
        const now = o.courier ? o.courier.id : null;
        if (now !== was.courier_id) { state.selected.delete(id); notes.push(`№${id} уже ${o.courier ? 'у курьера ' + esc(o.courier.name) : 'снят с курьера'}`); }
      });
      if (notes.length) toast('ℹ️ Сняты с выбора: ' + notes.join('; '), 7000);
      const fkey = JSON.stringify(state.regions);
      if (state.keys.filters !== fkey) { state.keys.filters = fkey; renderFilters(); }
      syncFeatures();
      renderStats();
      renderSelbar();
      renderList();
      renderDetail();
      ind.classList.remove('err');
      ind.textContent = `Обновлено ${new Date().toLocaleTimeString('ru-RU')}`;
    } catch (e) {
      if (!state.expired) { ind.classList.add('err'); ind.textContent = '⚠️ Нет связи с сервером — повторю через 10 с'; }
    } finally {
      refreshing = false;
    }
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll('[data-tab]').forEach((x) => x.classList.toggle('on', x.dataset.tab === tab));
    renderList();
  }

  // ------------------------------------------------------------ запуск
  async function start() {
    document.querySelectorAll('[data-tab]').forEach((b) => { b.onclick = () => setTab(b.dataset.tab); });
    $('[data-logout]').onclick = async () => {
      if (!(await confirmBox('<h2>Выйти?</h2><p class="muted">Вход на этом компьютере закроется. Снова войти — по новой ссылке из Mini App.</p>', 'Выйти'))) return;
      try { await api('POST', '/logout'); } catch (e) { /* уже вышли */ }
      showOverlay('out');
    };
    $('[data-logout-all]').onclick = async () => {
      if (!(await confirmBox('<h2>Выйти везде?</h2><p class="muted">Закроются все ваши входы в карту на всех компьютерах, а неоткрытая ссылка перестанет работать.</p>', 'Выйти везде'))) return;
      try { await api('POST', '/logout-all'); } catch (e) { /* уже вышли */ }
      showOverlay('out');
    };
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && $('[data-overlay]').hidden) { state.selected.clear(); afterSelection(); }
    });
    try {
      await loadSession();
    } catch (e) {
      if (!state.expired) showOverlay('expired');
      return;
    }
    setInterval(tickBomb, 1000);
    try {
      await loadOl();
      initMap();
    } catch (e) {
      const note = $('[data-map-note]');
      note.textContent = 'Библиотека карты не загрузилась. Списки заказов и курьеров справа работают; обновите страницу позже.';
      note.hidden = false;
    }
    await refresh();
    setInterval(refresh, REFRESH_MS);
  }
  start();
})();
