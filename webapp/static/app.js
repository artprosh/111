/* Relax Gas — Telegram Mini App.
   Чистый JS без сборки. Все данные — через /api (webapp/core.py), авторизация —
   подписанная Telegram initData в заголовке Authorization: tma <initData>. */
(() => {
  'use strict';

  const tg = window.Telegram && window.Telegram.WebApp;
  const view = document.getElementById('view');
  const titleEl = document.getElementById('title');
  const backBtn = document.getElementById('back-btn');
  const homeBtn = document.getElementById('home-btn');
  const cartPill = document.getElementById('cart-pill');
  const cartCountEl = document.getElementById('cart-count');

  const DEFAULT_THEME = {
    bg_color: '#0f172a', bg_gradient_to: '#1e293b', accent_color: '#f97316',
    button_style: 'rounded', button_fill: 'lux', button_animation: 'premium',
    menu_order: ['catalog', 'cart', 'orders_active', 'orders_history', 'call_admin'],
    bg_image_data_url: null, bg_image_pos_x: 50, bg_image_pos_y: 50,
  };
  const MENU = {
    catalog: { ico: '📦', img: 'catalog', lbl: 'Каталог', route: 'catalog' },
    cart: { ico: '🛒', img: 'cart', lbl: 'Корзина', route: 'cart' },
    orders_active: { ico: '🚚', img: 'orders-active', lbl: 'Активные заказы', sub: 'Статус и курьер на карте', route: 'orders' },
    orders_history: { ico: '📋', img: 'history', lbl: 'История', sub: 'Прошлые заказы', route: 'history' },
    call_admin: { ico: '💬', img: 'call-admin', lbl: 'Чат с RelaxGas', sub: 'Ответим здесь же', route: 'chat' },
  };
  // Иконка плитки: фото из /static/img/menu/<img>.webp, если оно есть, иначе эмодзи.
  function tileIco(emoji, img) {
    return img
      ? `<span class="ico has-img"><img src="/static/img/menu/${img}.webp" alt="" loading="lazy" decoding="async"></span>`
      : `<span class="ico">${emoji}</span>`;
  }
  // Фон плитки — та же картинка, что в превью, растянутая и размытая (см. .tile.has-bg в style.css).
  function decorateTiles(root) {
    root.querySelectorAll('.tile .ico.has-img img').forEach((img) => {
      const tile = img.closest('.tile');
      tile.style.setProperty('--tile-img', `url("${img.getAttribute('src')}")`);
      tile.classList.add('has-bg');
    });
  }
  // «📲 Добавить на рабочий стол» — ярлык Mini App (Telegram Bot API 8.0+, метод addToHomeScreen).
  // Показываем, только если Telegram это умеет и ярлыка ещё нет (статус missed/unknown).
  // Имя и иконку ярлыка подставляет сам Telegram; имя можно поправить в его окне.
  function homeScreenButton(root) {
    const slot = root.querySelector('[data-homescreen]');
    if (!slot || !tg || typeof tg.addToHomeScreen !== 'function' || !(tg.isVersionAtLeast && tg.isVersionAtLeast('8.0'))) return;
    const show = () => {
      slot.innerHTML = '<button class="btn ghost homescreen-btn">📲 Добавить на рабочий стол</button>';
      slot.querySelector('button').onclick = () => { haptic('light'); try { tg.addToHomeScreen(); } catch (e) { /* старый клиент */ } };
    };
    try {
      tg.checkHomeScreenStatus((status) => { if (status === 'missed' || status === 'unknown') show(); });
    } catch (e) { show(); }
  }
  if (tg && typeof tg.onEvent === 'function') {
    try {
      tg.onEvent('homeScreenAdded', () => {
        document.querySelectorAll('[data-homescreen]').forEach((el) => { el.innerHTML = ''; });
        toast('Ярлык добавлен на рабочий стол');
      });
    } catch (e) { /* старый клиент */ }
  }
  // Маленькое фото внутри обычной кнопки (нижняя панель ролей и т.п.).
  function btnIco(img) { return `<img class="btn-ico" src="/static/img/menu/${img}.webp" alt="" decoding="async">`; }
  const STATUS_STEPS = ['new', 'accepted', 'in_transit', 'delivered'];

  const state = {
    me: null,
    dev: null,
    devUser: null,
    timers: [],
    maps: [],
    checkout: { phone: '', address: '', mode: 'address', lat: null, lon: null, method: 'cash_on_delivery', comment: '', lift: false,
      name: '', parts: null, point: null, savedIdx: null },
    quote: null,
    proof: null,
    courierTab: 'current',  // курьер по умолчанию видит заказы «В работе»
    adminTab: 'orders',
    adminOrdersScope: 'open',
    historyFilter: { status: 'delivered', courier: 'all', q: '' },
    historyEdit: false,
    lastJson: {},
    badges: null, // последние счётчики с /api/badges
  };

  // ------------------------------------------------------------ helpers
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // 🎁 Бонус товара (28.10.2026): один на товар, применяется к «Новому» и/или к «Замене».
  const bonusScope = (p) => (p.price_exchange == null || (p.bonus_new && p.bonus_exchange) ? ''
    : p.bonus_new ? 'только к новому' : 'только к замене');
  const bonusTag = (text, desc, scope) => `<button type="button" class="bonus bonus-tap" data-bonus-info data-bt="${esc(text)}" data-bd="${esc(desc || '')}" data-bs="${esc(scope || '')}">🎁 ${esc(text)}${scope ? ` <span class="bonus-scope">· ${esc(scope)}</span>` : ''}${desc ? ' <span class="bonus-more">ⓘ</span>' : ''}</button>`;
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-bonus-info]');
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    openSheet(`<h3>🎁 ${esc(b.dataset.bt)}</h3>${b.dataset.bs ? `<div class="small muted mt">Бонус ${esc(b.dataset.bs)}.</div>` : ''}
      <div class="mt" style="white-space:pre-wrap">${b.dataset.bd ? esc(b.dataset.bd) : '<span class="muted">Подробного описания нет.</span>'}</div>
      <button class="btn mt" data-act="close">Понятно</button>`, (sheet) => { sheet.querySelector('[data-act=close]').onclick = () => closeSheet(); });
  }, true);
  const money = (v) => `${Math.round(Number(v) || 0).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ')} ₽`;
  const plural = (n, one, few, many) => {
    const a = Math.abs(n) % 100; const b = a % 10;
    if (a > 10 && a < 20) return many; if (b > 1 && b < 5) return few; if (b === 1) return one; return many;
  };
  const haptic = (type) => { try { if (type === 'select') tg.HapticFeedback.selectionChanged(); else if (type === 'error' || type === 'success' || type === 'warning') tg.HapticFeedback.notificationOccurred(type); else tg.HapticFeedback.impactOccurred(type || 'light'); } catch (e) { /* вне Telegram */ } };
  const initials = (name) => esc((String(name || '').match(/\p{L}/u) || ['•'])[0].toUpperCase());
  const photoBox = (p) => (p.photo
    ? `<img src="${esc(p.photo)}" alt="" loading="lazy" onerror="this.outerHTML='<div class=&quot;initials&quot;>${initials(p.name)}</div>'">`
    : `<div class="initials">${initials(p.name)}</div>`);
  const html = (strings, ...vals) => strings.reduce((out, s, i) => out + s + (i < vals.length ? vals[i] : ''), '');

  function toast(text, isError, ms) {
    const el = document.getElementById('toast');
    el.textContent = text;
    el.className = 'toast' + (isError ? ' err' : '');
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, ms || (isError ? 4200 : 2400));
    if (isError) haptic('error');
  }

  function confirmBox(text, inPage = false) {
    return new Promise((resolve) => {
      try {  // inPage — всегда своё окно с кнопками «Да» / «Нет» (а не системное «OK / Отмена»)
        if (!inPage && tg && tg.showConfirm && tg.initData) { tg.showConfirm(text, (ok) => resolve(!!ok)); return; }
      } catch (e) { /* старый клиент */ }
      // Вне Telegram — подтверждение внутри страницы (window.confirm
      // в некоторых встроенных окнах заблокирован и сразу отвечает «нет»).
      let done = false;
      const finish = (ok) => { if (done) return; done = true; closeSheet(); resolve(ok); };
      openSheet(`<h3>${esc(text)}</h3><div class="stack mt"><button class="btn" data-act="yes">Да</button><button class="btn ghost" data-act="no">Нет</button></div>`, (sheet) => {
        sheet.querySelector('[data-act=yes]').onclick = () => finish(true);
        sheet.querySelector('[data-act=no]').onclick = () => finish(false);
        document.getElementById('sheet-backdrop').onclick = () => finish(false);
      });
    });
  }

  async function api(method, path, body) {
    const headers = { 'Content-Type': 'application/json' };
    if (tg && tg.initData) headers.Authorization = `tma ${tg.initData}`;
    else if (state.devUser) headers['X-Dev-User'] = String(state.devUser);
    const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    let data = {};
    try { data = await res.json(); } catch (e) { /* пустой ответ */ }
    if (!res.ok) { const err = new Error(data.error || `Ошибка ${res.status}`); err.data = data; err.status = res.status; throw err; }
    return data;
  }

  async function withBusy(btn, fn) {
    if (btn) { btn.disabled = true; btn.classList.add('loading'); }
    try { return await fn(); } catch (e) { toast(e.message, true); return undefined; } finally {
      if (btn && btn.isConnected) { btn.disabled = false; btn.classList.remove('loading'); }
    }
  }

  function setCartCount(n) {
    if (state.me) state.me.cart_count = n;
    cartCountEl.textContent = n;
    cartPill.hidden = !n || currentRoute().name === 'cart' || currentRoute().name === 'pay' || getMode() !== 'customer';
  }

  // Автообновление экрана — только пока приложение на экране (свёрнутое не нагружает сервер).
  function every(ms, fn) { const id = setInterval(() => { if (!document.hidden) fn(); }, ms); state.timers.push(id); }

  function compressImage(file, maxSide, quality) {
    return new Promise((resolve, reject) => {
      if (!file || !/^image\//.test(file.type)) { reject(new Error('Выберите картинку.')); return; }
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('Не удалось прочитать файл.'));
      reader.onload = () => {
        const img = new Image();
        img.onerror = () => reject(new Error('Не удалось открыть картинку.'));
        img.onload = () => {
          const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
          const canvas = document.createElement('canvas');
          canvas.width = Math.round(img.width * scale);
          canvas.height = Math.round(img.height * scale);
          canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
          resolve(canvas.toDataURL('image/jpeg', quality));
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  function getPosition() {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) { reject(new Error('Геолокация недоступна на этом устройстве.')); return; }
      navigator.geolocation.getCurrentPosition(
        (p) => resolve({ lat: p.coords.latitude, lon: p.coords.longitude, acc: p.coords.accuracy }),
        (err) => reject(new Error(err.code === 1
          ? 'Нет доступа к геолокации. Разрешите его Telegram в настройках телефона или введите адрес вручную.'
          : 'Не удалось определить местоположение. Попробуйте ещё раз или введите адрес.')),
        { enableHighAccuracy: true, timeout: 15000, maximumAge: 10000 },
      );
    });
  }

  // ------------------------------------------------------------ theme
  function hexToRgb(hex) { const n = parseInt(hex.slice(1), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
  function luminance(hex) {
    const [r, g, b] = hexToRgb(hex).map((v) => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  function applyTheme(theme) {
    const t = Object.assign({}, DEFAULT_THEME, theme || {});
    const root = document.documentElement.style;
    root.setProperty('--bg', t.bg_color);
    root.setProperty('--bg2', t.bg_gradient_to);
    root.setProperty('--accent', t.accent_color);
    root.setProperty('--accent-ink', luminance(t.accent_color) > 0.42 ? '#111827' : '#ffffff');
    root.setProperty('--bg-image', t.bg_image_data_url ? `url("${t.bg_image_data_url}")` : 'none');
    root.setProperty('--bg-pos', `${t.bg_image_pos_x}% ${t.bg_image_pos_y}%`);
    const body = document.body;
    body.classList.remove('shape-rounded', 'shape-pill', 'shape-square', 'fill-lux', 'fill-solid', 'fill-outline', 'fill-glass', 'anim-premium', 'anim-bounce', 'anim-none');
    body.classList.add(`shape-${t.button_style}`, `fill-${t.button_fill}`, `anim-${t.button_animation}`);
    body.classList.toggle('has-bg-image', !!t.bg_image_data_url);
    // Светлый фон без картинки — переключаем текст и карточки на тёмные,
    // иначе белый текст сливается с фоном и экран выглядит пустым.
    // (Картинка всегда затемняется слоем поверх, там остаётся светлый текст.)
    const light = !t.bg_image_data_url && (luminance(t.bg_color) + luminance(t.bg_gradient_to)) / 2 > 0.35;
    body.classList.toggle('light-bg', light);
    try { tg.setHeaderColor(t.bg_color); tg.setBackgroundColor(t.bg_color); } catch (e) { /* Bot API < 6.1 */ }
  }

  // ------------------------------------------------------------ maps (OpenLayers + OpenStreetMap)
  // OpenLayers — открытая библиотека карт (BSD-2), проект фонда OSGeo; ключ не
  // нужен. Подложка — OpenStreetMap; подпись «© OpenStreetMap» обязательна
  // по лицензии ODbL, ссылка из неё открывается во внешнем браузере.
  const OL_VERSION = '10.5.0';
  let olPromise = null;
  function loadMaps() {
    if (window.ol && window.ol.Map) return Promise.resolve();
    if (!olPromise) {
      olPromise = new Promise((resolve, reject) => {
        const css = document.createElement('link');
        css.rel = 'stylesheet';
        css.href = `https://cdn.jsdelivr.net/npm/ol@${OL_VERSION}/ol.css`;
        document.head.appendChild(css);
        const js = document.createElement('script');
        js.src = `https://cdn.jsdelivr.net/npm/ol@${OL_VERSION}/dist/ol.js`;
        js.onload = () => (window.ol && window.ol.Map ? resolve() : reject(new Error('Карта не загрузилась')));
        js.onerror = () => { olPromise = null; reject(new Error('Карта не загрузилась')); };
        document.head.appendChild(js);
      });
    }
    return olPromise;
  }
  // Маркер-эмодзи. setLatLng([lat, lon]) — чтобы двигать курьера без перерисовки.
  function addPin(map, emoji, pt) {
    const ol = window.ol;
    const el = document.createElement('div');
    el.className = 'map-pin';
    el.textContent = emoji;
    const overlay = new ol.Overlay({ element: el, positioning: 'bottom-center', stopEvent: false });
    map.addOverlay(overlay);
    const pin = {
      lonlat: [pt.lon, pt.lat],
      setLatLng([lat, lon]) { pin.lonlat = [lon, lat]; overlay.setPosition(ol.proj.fromLonLat(pin.lonlat)); },
    };
    pin.setLatLng([pt.lat, pt.lon]);
    return pin;
  }
  function mapFallback(el, courier, target) {
    const pt = courier || target;
    const link = pt ? `<a class="btn ghost small mt" style="text-decoration:none" href="https://yandex.ru/maps/?pt=${pt.lon},${pt.lat}&z=16&l=map">🗺 ${courier ? 'Где курьер' : 'Точка'} — открыть в Яндекс.Картах</a>` : '';
    el.innerHTML = `<div class="empty small">Карта не загрузилась. Проверьте интернет.${link ? `<div>${link}</div>` : ''}</div>`;
  }
  async function drawMap(el, courier, target) {
    try { await loadMaps(); } catch (e) { mapFallback(el, courier, target); return null; }
    const ol = window.ol;
    let map;
    try {
      map = new ol.Map({
        target: el,
        layers: [new ol.layer.Tile({
          source: new ol.source.OSM({ attributions: '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap</a>' }),
        })],
        controls: [new ol.control.Attribution({ collapsible: false })],
        view: new ol.View({ center: ol.proj.fromLonLat([37.6176, 55.7558]), zoom: 12 }),
      });
    } catch (e) { mapFallback(el, courier, target); return null; }
    map.remove = () => map.setTarget(null);  // как у прежней карты: m.map.remove()
    const entry = { map, el, courier: null, target: null };
    if (target) entry.target = addPin(map, '🏠', target);
    if (courier) entry.courier = addPin(map, '🚚', courier);
    fitMap(entry);
    state.maps.push(entry);
    return entry;
  }
  // Коротко для кнопки сохранённого адреса: «ул Лесная, д 7, кв. 12».
  function shortAddress(a) {
    const line = String(a.line || '').replace(/^(г|город)\s+[^,]+,\s*/i, '');
    return line + (a.house ? `, д. ${a.house}` : '') + (a.apartment ? `, кв. ${a.apartment}` : '');
  }
  // «📍 Выбрать на карте»: карта OpenStreetMap, метка в центре; адрес точки — из своей базы адресов.
  function pickOnMap(start, done) {
    openSheet(`<h3>📍 Точка доставки</h3><p class="small muted">Двигайте карту — метка в центре. Затем нажмите «Выбрать эту точку».</p>
      <div class="map-pick"><div class="map" data-pickmap style="height:100%"></div><div class="map-center-pin">📍</div></div>
      <div class="btn-pair mt"><button class="btn ghost" data-act="pick-me">🎯 Где я</button><button class="btn" data-act="pick-ok">✅ Выбрать эту точку</button></div>
      <button class="btn ghost mt" style="width:100%" data-act="pick-cancel">Отмена</button>`, async (sheet) => {
      sheet.querySelector('[data-act=pick-cancel]').onclick = closeSheet;
      const el = sheet.querySelector('[data-pickmap]');
      try { await loadMaps(); } catch (e) {
        el.innerHTML = '<div class="empty small">Карта не загрузилась. Проверьте интернет и введите адрес вручную.</div>';
        sheet.querySelector('.map-center-pin').hidden = true;
        sheet.querySelector('[data-act=pick-ok]').hidden = true;
        sheet.querySelector('[data-act=pick-me]').hidden = true;
        return;
      }
      const ol = window.ol;
      const center = start ? [start.lon, start.lat] : [37.6176, 55.7558];
      const map = new ol.Map({
        target: el,
        layers: [new ol.layer.Tile({ source: new ol.source.OSM({ attributions: '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap</a>' }) })],
        controls: [new ol.control.Attribution({ collapsible: false }), new ol.control.Zoom()],
        view: new ol.View({ center: ol.proj.fromLonLat(center), zoom: start ? 17 : 11 }),
      });
      setTimeout(() => map.updateSize(), 250);
      sheet.querySelector('[data-act=pick-me]').onclick = (e) => withBusy(e.target, async () => {
        const pos = await getPosition();
        map.getView().animate({ center: ol.proj.fromLonLat([pos.lon, pos.lat]), zoom: 17, duration: 300 });
      });
      sheet.querySelector('[data-act=pick-ok]').onclick = (e) => withBusy(e.target, async () => {
        const [lon, lat] = ol.proj.toLonLat(map.getView().getCenter());
        const pt = { lat: Math.round(lat * 1e6) / 1e6, lon: Math.round(lon * 1e6) / 1e6 };
        let address = null, moscow = null, reason = null, found = null;
        try {
          const r = await api('GET', `/api/address/geolocate?lat=${pt.lat}&lon=${pt.lon}`);
          address = r.address || null;
          moscow = r.moscow;
          found = r;
          reason = r.reason || (address ? null : 'empty');
        } catch (err) { reason = 'error'; }
        map.setTarget(null);
        closeSheet();
        if (address) toast('📍 Адрес подставлен — проверьте дом и укажите квартиру.');
        else if (reason === 'not_configured') toast('📍 Точка отмечена — курьер её увидит. Впишите, пожалуйста, улицу и дом.');
        else if (reason === 'empty') toast('📍 Точка отмечена, но рядом нет адреса — впишите улицу и дом.', true);
        else toast('📍 Точка отмечена. Сервис адресов сейчас не ответил — впишите улицу и дом.', true);
        done(pt, address, moscow, found);
      });
    });
  }
  function fitMap(entry) {
    const ol = window.ol;
    const pts = [entry.courier, entry.target].filter(Boolean).map((m) => ol.proj.fromLonLat(m.lonlat));
    const view = entry.map.getView();
    if (pts.length === 2) {
      const size = [entry.el.clientWidth || 320, entry.el.clientHeight || 220];
      view.fit(ol.extent.boundingExtent(pts), { size, padding: [36, 36, 36, 36], maxZoom: 16 });
    } else if (pts.length === 1) { view.setCenter(pts[0]); view.setZoom(15); }
  }

  // ------------------------------------------------------------ режим входа
  // Курьер/админ выбирает, как войти: «администратор», «курьер» или
  // «покупатель». Выбор запоминается на сервере (users.app_mode) — при
  // следующих входах вопрос не задаётся, приложение открывается в той же роли. В режиме курьера видны только экраны курьера,
  // в режиме админа — только экраны админа; внизу всегда «Войти как покупатель».
  // Обычный покупатель вопроса не видит — сразу меню магазина.
  const MODE_KEY = 'appMode';
  const MODE_ROUTES = {
    courier: ['courier', 'order-chat', 'chat'],
    admin: ['admin-home', 'admin', 'admin-history', 'disputes', 'dev', 'referral', 'admin-chats', 'admin-chat', 'order-chat', 'courier-stats'],
  };
  const MODE_HOME = { customer: 'home', courier: 'courier', admin: 'admin-home' };
  const STAFF_ONLY = ['courier', 'admin-home', 'admin', 'admin-history', 'disputes', 'dev', 'admin-chats', 'admin-chat', 'courier-stats'];
  function isStaff() { return !!(state.me && (state.me.roles.admin || state.me.roles.courier)); }
  function getMode() {
    if (!state.me) return null;
    if (!isStaff()) return 'customer';
    const m = state.mode;
    if (m === 'admin' && state.me.roles.admin) return 'admin';
    if (m === 'courier' && state.me.roles.courier) return 'courier';
    if (m === 'customer') return 'customer';
    return null; // ещё не выбрал — покажем «Как войти?»
  }
  function setMode(m) {
    state.mode = m;
    try { if (m) sessionStorage.setItem(MODE_KEY, m); else sessionStorage.removeItem(MODE_KEY); } catch (e) { /* приватный режим */ }
    if (m !== 'customer') showRatingGate(null);
    else showRatingGate((state.badges && state.badges.pending_rating) || (state.me && state.me.pending_rating) || null);
    renderModeBar();
  }
  function switchMode(m) {
    haptic('light'); setMode(m);
    if (m === 'courier') state.courierTab = 'current';  // вход курьером — сразу «В работе»
    go(MODE_HOME[m]);
    // Запоминаем на сервере — при следующем входе приложение сразу откроется в этой роли.
    if (isStaff()) { state.me.mode = m; api('PUT', '/api/me/mode', { mode: m }).catch(() => {}); }
  }
  // Нижняя панель в режимах курьера и админа: «Войти как покупатель» (+ другая роль).
  function renderModeBar() {
    const bar = document.getElementById('mode-bar');
    if (!bar) return;
    const mode = getMode();
    const show = mode === 'courier' || mode === 'admin';
    bar.hidden = !show;
    document.body.classList.toggle('with-mode-bar', show);
    if (!show) { bar.innerHTML = ''; return; }
    const r = state.me.roles;
    const other = mode === 'courier' && r.admin ? `<button class="btn ghost" data-mode="admin">${btnIco('role-admin')}Как администратор</button>`
      : mode === 'admin' && r.courier ? `<button class="btn ghost" data-mode="courier">${btnIco('role-courier')}Как курьер</button>` : '';
    // Кнопки ролей — одна под другой (просьба владельца 23.10).
    bar.innerHTML = `<div class="inner mode-stack">${other ? other.replace('Как администратор', 'Войти как администратор').replace('Как курьер', 'Войти как курьер') : ''}<button class="btn" data-mode="customer">${btnIco('role-customer')}Войти как покупатель</button></div>`;
    document.body.classList.toggle('mode-bar-two', !!other);
    bar.querySelectorAll('[data-mode]').forEach((b) => { b.onclick = () => switchMode(b.dataset.mode); });
  }

  // ------------------------------------------------------------ router
  const routes = {};
  function currentRoute() {
    const raw = (location.hash || '#home').slice(1);
    const [name, query] = raw.split('?');
    return { name: name || 'home', params: new URLSearchParams(query || '') };
  }
  function go(name, params) {
    const q = params ? `?${new URLSearchParams(params)}` : '';
    const target = `#${name}${q}`;
    if (location.hash === target) render(); else location.hash = target;
  }
  function cleanup() {
    document.body.classList.remove('in-chat');
    state.timers.forEach(clearInterval);
    state.timers = [];
    state.maps.forEach((m) => { try { m.map.remove(); } catch (e) { /* уже удалена */ } });
    state.maps = [];
    closeSheet();
  }
  async function render() {
    cleanup();
    try { tg.enableVerticalSwipes(); } catch (_) { /* старый клиент */ }
    const route = currentRoute();
    const mode = getMode();
    let name = routes[route.name] ? route.name : 'home';
    if (!mode) name = 'role';                                   // курьер/админ ещё не выбрал вход
    else if (mode !== 'customer') {
      if (name === 'home' || !MODE_ROUTES[mode].includes(name)) name = MODE_HOME[mode];
    } else if (STAFF_ONLY.includes(name)) name = 'home';        // в режиме покупателя — только магазин
    const fn = routes[name];
    const isHome = name === 'role' || (mode && name === MODE_HOME[mode]);
    renderModeBar();
    backBtn.hidden = isHome || !!(tg && tg.initData);
    homeBtn.hidden = isHome;
    try {
      if (tg && tg.BackButton) { if (isHome) tg.BackButton.hide(); else tg.BackButton.show(); }
    } catch (e) { /* старый клиент */ }
    setCartCount(state.me ? state.me.cart_count : 0);
    view.innerHTML = '<div class="stack"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>';
    window.scrollTo(0, 0);
    try {
      await fn(route.params);
    } catch (e) {
      view.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}<div class="mt"><button class="btn ghost small" data-act="retry">Повторить</button></div></div>`;
      view.querySelector('[data-act=retry]').onclick = render;
    }
  }
  function goBack() {
    const name = currentRoute().name;
    const parents = { pay: 'checkout', checkout: 'cart', 'order-done': 'orders' };
    go(parents[name] || 'home');
  }
  function setTitle(t) { titleEl.textContent = t; }

  // ------------------------------------------------------------ sheet
  function openSheet(content, onMount) {
    const sheet = document.getElementById('sheet');
    const bd = document.getElementById('sheet-backdrop');
    sheet.innerHTML = `<div class="inner"><div class="grabber"></div>${content}</div>`;
    sheet.hidden = false; bd.hidden = false;
    bd.onclick = closeSheet;
    if (onMount) onMount(sheet);
  }
  let camStream = null; // камера внутри приложения (подтверждение оплаты)
  function stopCamera() {
    if (camStream) { camStream.getTracks().forEach((t) => t.stop()); camStream = null; }
  }
  function closeSheet() {
    stopCamera();
    document.getElementById('sheet').hidden = true;
    document.getElementById('sheet-backdrop').hidden = true;
  }

  // Обязательная оценка 1–5: кнопка подтверждения неактивна, пока не выбраны звёзды.
  function ratingSheet({ title, hint, extra = '', confirm, onSubmit }) {
    let rating = 0;
    openSheet(`<h3>${title}</h3><p class="small muted">${hint}</p>
      <div class="stars big-stars">${[1, 2, 3, 4, 5].map((r) => `<button data-star="${r}" aria-label="${r} из 5">${r}★</button>`).join('')}</div>
      ${extra}
      <div class="stack mt"><button class="btn ok" data-act="rate-ok" disabled>${confirm}</button><button class="btn ghost" data-act="rate-cancel">Отмена</button></div>`, (sheet) => {
      const ok = sheet.querySelector('[data-act=rate-ok]');
      sheet.querySelectorAll('[data-star]').forEach((b) => {
        b.onclick = () => {
          rating = +b.dataset.star;
          sheet.querySelectorAll('[data-star]').forEach((x) => x.classList.toggle('on', +x.dataset.star <= rating));
          ok.disabled = false; haptic('light');
        };
      });
      sheet.querySelector('[data-act=rate-cancel]').onclick = closeSheet;
      ok.onclick = () => withBusy(ok, async () => {
        if (!rating) throw new Error('Выберите оценку от 1 до 5 звёзд.');
        await onSubmit(rating, sheet);
      });
    });
  }

  function timeText(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  }

  // Телефон покупателя: «8 916 123 45 67» — ровно 11 цифр, первая 8 (+7 → 8).
  function formatPhone(value) {
    let d = String(value || '').replace(/\D/g, '');
    if (d[0] === '7') d = '8' + d.slice(1);
    else if (d && d[0] !== '8') d = '8' + d;
    d = d.slice(0, 11);
    const parts = [d.slice(0, 1), d.slice(1, 4), d.slice(4, 7), d.slice(7, 9), d.slice(9, 11)].filter(Boolean);
    return parts.join(' ');
  }
  // 🔍 Поиск по списку (клиенты, курьеры): имя, @логин, телефон (цифры без пробелов и скобок).
  function searchText(name, username, phones, id) {
    const ph = (phones || []).filter(Boolean);
    const digits = ph.map((p) => String(p).replace(/\D/g, ''));
    const alt = digits.filter((d) => d.length === 11 && /^[78]/.test(d)).map((d) => (d[0] === '8' ? '7' : '8') + d.slice(1));  // +7 ↔ 8
    return [name || '', username ? '@' + username : '', ...ph, ...digits, ...alt, id || ''].join(' ').toLowerCase();
  }
  // Таблица «баллон → баллы партнёру» (одинаково за новый и за замену).
  function rewardTable(rows) {
    if (!rows || !rows.length) return '<div class="small muted mt">Баллонов с баллами пока нет.</div>';
    return `<dl class="kv mt" style="text-align:left">${rows.map((r) => `<dt>${esc(r.name)}</dt><dd><b>+${Number(r.points).toLocaleString('ru-RU')} ⭐</b></dd>`).join('')}</dl>`;
  }
  // Баллон узнаём по названию (как на сервере): отдельное слово «баллон».
  const LITERS_RE = /(\d+(?:[.,]\d+)?)\s*(?:литр|л(?![а-яё])|l(?![a-z]))/i;
  // Только отдельное слово «баллон» (не «баллона», не «баллоны») — как на сервере.
  function isBalloonName(name) { return /(^|[^а-яё])баллон(?![а-яё])/i.test(name || ''); }
  function defaultPartnerReward(name) {
    if (!isBalloonName(name)) return 0;
    const m = LITERS_RE.exec(name || '');
    const l = m ? Number(m[1].replace(',', '.')) : NaN;
    return ({ 2: 100, 5: 150, 10: 200 })[l] || 0;
  }

  // Компактные карточки админки: «ℹ️ Подробнее» раскрывает остальное без перерисовки.
  // Что раскрыто — помним (state.adminExpand), чтобы после сохранения карточка не схлопывалась.
  function xOpen(key) { return !!(state.adminExpand && state.adminExpand.has(key)); }
  function xMore(key, body) {
    const open = xOpen(key);
    return `<button class="btn ghost small mt" style="width:100%" data-xmore="${key}">${open ? '▲ Скрыть' : 'ℹ️ Подробнее'}</button>
      <div class="xmore" data-xbody="${key}" ${open ? '' : 'hidden'}>${body}</div>`;
  }
  function bindExpand(container) {
    container.querySelectorAll('[data-xmore]').forEach((b) => {
      b.onclick = () => {
        if (!state.adminExpand) state.adminExpand = new Set();
        const key = b.dataset.xmore;
        const body = container.querySelector(`[data-xbody="${key}"]`);
        const open = body.hidden;
        body.hidden = !open;
        if (open) state.adminExpand.add(key); else state.adminExpand.delete(key);
        b.textContent = open ? '▲ Скрыть' : 'ℹ️ Подробнее';
        haptic('light');
      };
    });
  }
  function bindListSearch(container, stateKey) {
    const input = container.querySelector('[data-search]');
    if (!input) return;
    const apply = () => {
      const raw = input.value.trim().toLowerCase();
      state[stateKey] = input.value;
      const digits = raw.replace(/\D/g, '');
      const words = raw.replace(/^@/, '').split(/\s+/).filter(Boolean);
      let shown = 0;
      container.querySelectorAll('[data-search-text]').forEach((card) => {
        const t = card.dataset.searchText;
        const ok = !raw || words.every((w) => t.includes(w)) || (digits.length >= 3 && t.includes(digits));
        card.hidden = !ok;
        if (ok) shown += 1;
      });
      container.querySelector('[data-search-empty]').hidden = shown > 0;
    };
    input.oninput = apply;
    apply();
  }

  // 📢 Объявление админа на главном экране магазина (нет текста — нет плашки).
  function announceBox(a) {
    if (!a || !a.text) return '';
    return `<div class="announce"><div class="announce-title">📢 Объявление</div><div class="announce-text">${esc(a.text)}</div></div>`;
  }

  // ------------------------------------------------------------ shared order rendering
  function itemsList(items) {
    return `<ul class="items">${items.map((i) => i.gift
      ? `<li class="gift-li"><span>🎁 ${esc(i.name)} <span class="b">подарок</span></span><span class="muted">${i.quantity} шт. · 0 ₽</span></li>`
      : `<li><span>${esc(i.name)}${i.variant === 'exchange' ? ' · замена' : i.variant === 'return' ? ' · возврат' : ''}<span class="b">${i.bonus_text ? '🎁 ' + esc(i.bonus_text) : ''}</span></span><span class="muted">${i.quantity} × ${money(i.unit_price)}</span></li>`).join('')}</ul>`;
  }
  // Деньги курьера (25.10.2026; 4 строки — 28.10.2026): копятся до обнуления.
  // «Сдать в кассу» = собрано за заказы − отдано за пустые баллоны; «Сдал» обнуляет обе строки.
  const COUNTERS = [['cash', '💵 Собрано за заказы', 'наличные от покупателей'], ['returns', '♻️ Отдано за пустые баллоны', 'покупателям за возвраты'],
    ['earned', '💼 Заработал', 'баллоны, подъёмы, спуски, км за МКАД, возвраты']];
  // req (28.10.2026) — у курьера: запрос «обнулить» ждёт администратора; сам курьер деньги не обнуляет.
  function moneyCard(c, req) {
    if (!c) return '';
    const row = (k, label, hint, value, btn) => `<div class="money-row">
        <span><span>${label}</span><span class="small muted">${hint}${c[k + '_since'] ? ` · с ${dateText(c[k + '_since'])}` : ''}</span></span>
        <b class="${value < 0 ? 'neg' : ''}">${money(value)}</b>${btn}</div>`;
    const reset = (k, text) => `<button class="btn ghost small" data-reset="${k}" ${!req && (k === 'hand' ? c.cash || c.returns || c.keep : c[k]) ? '' : 'disabled'}>${text}</button>`;
    // 28.10.2026 (владелец): курьер сдаёт в кассу после вычета своего заработка по наличным заказам (keep).
    const hand = (c.cash || 0) + (c.returns || 0) - (c.keep || 0);
    // 01.11.2026: если «Оставил себе» совпадает с «Заработал» — одна строка вместо двух.
    const merged = !!c.keep && c.keep === (c.earned || 0);
    return `<div class="card money-card">
      ${row('cash', COUNTERS[0][1], COUNTERS[0][2], c.cash || 0, reset('cash', 'Обнулить'))}
      ${row('returns', COUNTERS[1][1], COUNTERS[1][2], -(c.returns || 0), reset('returns', 'Обнулить'))}
      ${merged ? row('earned', '💼 Заработал и оставил себе', 'вычитается из наличных при сдаче: баллоны, подъёмы, спуски, км за МКАД, возвраты', -(c.earned || 0), reset('earned', 'Обнулить')) : ''}
      ${c.keep && !merged ? `<div class="money-row"><span><span>✋ Оставил себе</span><span class="small muted">заработок и платная дорога по наличным заказам</span></span><b>${money(-c.keep)}</b><span></span></div>` : ''}
      <div class="money-row money-hand"><span><span>🏦 Сдать в кассу</span><span class="small muted">собрано − отдано за пустые − оставил себе</span></span>
        <b class="${hand < 0 ? 'neg' : ''}">${money(hand)}</b>${reset('hand', 'Сдал')}</div>
      ${merged ? '' : row('earned', COUNTERS[2][1], COUNTERS[2][2], c.earned || 0, reset('earned', 'Обнулить'))}
      ${req ? `<div class="note mt">⏳ Запрос отправлен администратору: ${req.names.includes('cash') && req.names.includes('returns') ? '«Сдал в кассу»' : req.names.map((n) => `«${(COUNTERS.find((x) => x[0] === n) || [n, n])[1].replace(/^\S+\s/, '')}»`).join(', ')}. Счётчик обнулится после подтверждения.
        <div class="mt"><button class="btn ghost small" data-reset-cancel>Отменить запрос</button></div></div>` : ''}</div>`;
  }
  // Обнулить счётчик (курьер — свой, админ — у любого курьера).
  function bindCounterResets(root, counters, path, after, courierMode) {
    const cancel = root.querySelector('[data-reset-cancel]');
    if (cancel) cancel.onclick = () => withBusy(cancel, async () => { after(await api('POST', path, { cancel: true })); toast('Запрос отменён.'); });
    root.querySelectorAll('[data-reset]').forEach((b) => {
      b.onclick = async () => {
        const k = b.dataset.reset;
        if (courierMode) {
          // Курьер — только запрос администратору (28.10.2026).
          const names = k === 'hand' ? ['cash', 'returns', 'keep'] : [k];
          const what = k === 'hand' ? `сдачу в кассу ${money((counters.cash || 0) + (counters.returns || 0) - (counters.keep || 0))}`
            : `обнуление «${COUNTERS.find((x) => x[0] === k)[1].replace(/^\S+\s/, '')}» (${money(Math.abs(counters[k]))})`;
          if (!(await confirmBox(`Отправить администратору ${what}? Счётчик обнулится после его подтверждения.`))) return;
          withBusy(b, async () => { const r = await api('POST', path, { names }); haptic('success'); toast('Запрос отправлен администратору.'); after(r); });
          return;
        }
        if (k === 'hand') {
          const hand = (counters.cash || 0) + (counters.returns || 0) - (counters.keep || 0);
          if (!(await confirmBox(`Курьер сдал в кассу ${money(hand)}? «Собрано за заказы», «Отдано за пустые баллоны» и «Оставил себе» обнулятся, история смен и статистика останутся.`))) return;
          withBusy(b, async () => {
            let r = null;
            for (const name of ['cash', 'returns', 'keep']) if (counters[name]) r = await api('POST', path, { name });
            haptic('success'); toast('Касса принята — счётчики обнулены.'); after(r);
          });
          return;
        }
        const row = COUNTERS.find((x) => x[0] === k);
        if (!(await confirmBox(`Обнулить «${row[1].replace(/^\S+\s/, '')}» (${money(Math.abs(counters[k]))})? Дальше сумма начнёт копиться с нуля, история смен останется.`))) return;
        withBusy(b, async () => { const r = await api('POST', path, { name: k }); haptic('success'); toast('Обнулено.'); after(r); });
      };
    });
  }
  // «Возврат баллона»: курьер забирает пустые баллоны и отдаёт покупателю их цену.
  function returnHead(o) {
    return o.is_return ? '<div class="ret-head">♻️ Возврат баллона · курьер заберёт пустые</div>' : '';
  }
  // «Заберите пустые»: замена и/или возврат (01.11.2026) — с названиями баллонов.
  function emptyHint(o) {
    const ex = (o.items || []).filter((i) => i.variant === 'exchange').map((i) => `${esc(i.name)} × ${i.quantity}`);
    const rt = (o.returns || []).map((r) => `${esc(r.name)} × ${r.quantity}`);
    if (!ex.length && !rt.length) return o.has_exchange ? '<div class="note mt empty-blink">⚠️ Заберите у клиента пустой баллон (замена)!</div>' : '';
    const parts = [ex.length ? `замена: ${ex.join(', ')}` : '', rt.length ? `возврат: ${rt.join(', ')}` : ''].filter(Boolean);
    return `<div class="note mt empty-blink">⚠️ Заберите у клиента ${ex.length + rt.length > 1 || (ex.length && rt.length) ? 'пустые баллоны' : 'пустой баллон'} — ${parts.join('; ')}!</div>`;
  }
  // Возврат пустых баллонов в обычном заказе (01.11.2026).
  function returnsLine(o, who) {
    if (!o.returns || !o.returns.length) return '';
    const list = o.returns.map((r) => `${esc(r.name)} × ${r.quantity}`).join(', ');
    const pts = o.return_mode === 'points';
    const eff = pts ? `+${o.return_points} баллов` : `−${money(o.return_payout)}`;
    const note = who !== 'customer' ? (pts ? 'наличные не отдавать — клиенту начислят баллы' : 'уже зачтено в сумму заказа') : (pts ? 'баллы придут после доставки' : 'зачтено в оплату');
    return `<div class="row spread small"><span>♻️ Возврат пустого: ${list}<div class="muted">${note}</div></span><b>${eff}</b></div>`;
  }
  function totalLine(o, who) {
    if (o.is_return) return `${o.descent ? `<div class="row spread small"><span>⬇️ Спуск пустого баллона: ${o.descent.count} × ${o.return_mode === 'points' ? o.descent.price + ' баллов' : money(o.descent.price)}</span><b>−${o.return_mode === 'points' ? o.descent.total + ' баллов' : money(o.descent.total)}</b></div>` : ''}<div class="total"><span>${o.return_mode === 'points' ? (who === 'customer' ? 'Вам начислят' : 'Начислить баллами') : (who === 'customer' ? 'Вам отдадут' : 'Отдать покупателю')}</span><span>${o.return_mode === 'points' ? `${o.return_points} баллов` : money(o.return_payout)}</span></div>`;
    return `${returnsLine(o, who)}<div class="total"><span>Сумма</span><span>${money(o.total)}</span></div>`;
  }
  // Услуга «Подъём на этаж» — её стоимость уже входит в o.total.
  function liftLine(o) {
    const l = o && o.floor_lift;
    // 28.10.2026: подъёмов (по баллону) × цена; спуск пустых к «Замене» — тоже входит в o.total.
    const lift = l && l.floors ? `<div class="row spread small"><span>🏢 Подъём на этаж: ${l.floors} × ${money(l.price)}</span><b>${money(l.total)}</b></div>` : '';
    const d = o && !o.is_return && o.descent;
    const desc = d && d.count ? `<div class="row spread small"><span>⬇️ Спуск пустого баллона: ${d.count} × ${money(d.price)}</span><b>${money(d.total)}</b></div>` : '';
    return lift + desc + deliveryLine(o) + bonusLine(o);
  }
  // Доставка за пределы Москвы — стоимость назначает админ, она уже входит в o.total.
  function deliveryLine(o) {
    return o && o.delivery_fee ? `<div class="row spread small"><span>🚚 Доставка за пределы Москвы</span><b>${money(o.delivery_fee - (o.delivery_toll || 0))}</b></div>`
      + (o.delivery_toll ? `<div class="row spread small"><span>🛣 Платная дорога</span><b>${money(o.delivery_toll)}</b></div>` : '') : '';
  }
  // Красная «шапка» индивидуального заказа (не Москва) — у курьера и у админа.
  function outsideHead(o) {
    return `<div class="out-head">🚚 Доставка за пределы Москвы${o.delivery_fee ? ` — <b class="nowrap">${money(o.delivery_fee)}</b>${o.delivery_km ? ` · ${o.delivery_km} км` : ''}${o.delivery_toll ? ` · <span class="nowrap">🛣 в т. ч. платная дорога ${money(o.delivery_toll)}</span>` : ''}` : ' · <span class="small">стоимость не назначена</span>'}</div>`;
  }
  // Заказ оплачен онлайн до того, как админ назначил доставку: доставка в предоплату не входила.
  function prepaidFeeNote(o) {
    return o.delivery_fee && o.paid && !o.pay_on_delivery && o.payment_method !== 'cash_on_delivery'
      ? `<div class="note warn mt">⚠️ Предоплата могла быть внесена до назначения доставки (${money(o.delivery_fee)}) — как покупатель оплачивает доставку, уточните у администратора.</div>` : '';
  }
  // Списанные бонусы (1 бонус = 1 ₽) — уже вычтены из o.total.
  function bonusLine(o) {
    return (o && o.bonus_spent ? `<div class="row spread small"><span>💰 Оплачено бонусами</span><b>−${money(o.bonus_spent)}</b></div>` : '')
      + (o && o.points_spent ? `<div class="row spread small"><span>⭐ Оплачено баллами</span><b>−${money(o.points_spent)}</b></div>` : '');
  }
  function timeline(status) {
    if (status === 'cancelled' || status === 'not_delivered' || status === 'disputed' || status === 'disputed_closed') return '';
    const idx = STATUS_STEPS.indexOf(status);
    return `<div class="timeline">${STATUS_STEPS.map((s, i) => `<i class="${i <= idx ? 'on' : ''}"></i>`).join('')}</div>`;
  }
  function payLine(o) {
    let s = esc(o.payment_label);
    if (o.payment_qr) {
      // Валюта и сеть, которые покупатель выбрал при оформлении.
      if (o.payment_qr.kind === 'crypto') s += ` · ${esc(o.payment_qr.label)}`;
      if (o.payment_qr.amount) s += ` · <b>${esc(o.payment_qr.amount)} ${esc(o.payment_qr.currency)}</b>`;
    } else if (o.usdt_amount != null) s += ` · ${Number(o.usdt_amount).toFixed(2)} USDT`;
    return s;
  }
  // Рейтинг с одним знаком после запятой: 5.0, 4.8, 3.2 (среднее всех оценок).
  function fmtRating(x) { const n = Number(x); return isNaN(n) ? '' : n.toFixed(1); }
  function dateText(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  // ------------------------------------------------------------ HOME
  routes.home = async () => {
    setTitle('Relax Gas');
    const me = state.me;
    const theme = Object.assign({}, DEFAULT_THEME, me.theme);
    const tiles = theme.menu_order.filter((k) => MENU[k]).map((k, i, arr) => {
      const m = MENU[k];
      const sub = k === 'cart' ? (me.cart_count ? `${me.cart_count} ${plural(me.cart_count, 'товар', 'товара', 'товаров')}` : 'Пока пусто') : m.sub;
      // Первый пункт — во всю ширину, остальные парами; одиночный последний — тоже во всю ширину.
      const wide = i === 0 || (i === arr.length - 1 && (arr.length - 1) % 2 === 1);
      return `<button class="tile ${wide ? 'wide' : ''} ${i === 0 ? 'main-tile' : ''}" data-go="${m.route}">${tileIco(m.ico, m.img)}<span><div class="lbl">${m.lbl}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</span>${BADGE_KEYS[k] ? `<span class="nbadge" data-badge="${BADGE_KEYS[k]}" hidden></span>` : ''}</button>`;
    }).join('');
    // Курьеру/админу в режиме покупателя — кнопки вернуться в свою роль.
    let roleTiles = '';
    if (me.roles.admin) roleTiles += `<button class="btn ghost" data-mode="admin">${btnIco('role-admin')}Войти как администратор</button>`;
    if (me.roles.courier) roleTiles += `<button class="btn ghost" data-mode="courier">${btnIco('role-courier')}Войти как курьер</button>`;
    view.innerHTML = html`
      <div class="stack">
        <div data-announce>${announceBox(me.announcement)}</div>
        <div class="hero">
          <div class="eyebrow">Relax Gas · доставка</div>
          <h1>Привет, ${esc(me.user.first_name || 'друг')}!</h1>
          <p style="white-space:pre-line">${esc(me.greeting || 'Закажите газ для начала или продолжения хорошего вечера.')}</p>
        </div>
        <div class="tiles">${tiles}</div>
        <button class="bonus-row" data-go="bonus"><img src="/static/img/menu/bonus.webp" alt="" decoding="async"><span class="br-lbl">Мои бонусы<small>1 бонус = 1 ₽</small></span><b class="br-pill" data-bonus-balance>${Number((me.bonus || {}).balance || 0).toLocaleString('ru-RU')}</b></button>
        ${me.returns_available ? '<button class="return-btn" data-go="return">♻️ Возврат баллона</button>' : ''}
        ${roleTiles ? `<div class="section-title">Ваши роли</div><div class="stack">${roleTiles}</div>` : ''}
        ${me.partner
          ? `<button class="tile wide" data-go="referral">${tileIco('⭐', 'bonus')}<span><div class="lbl">Моя реферальная программа</div><div class="sub">🤝 моя ссылка и баллы</div></span><b class="tile-val" data-partner-points>${Number(me.partner.points).toLocaleString('ru-RU')}</b></button>`
          : `<button class="tile wide" data-go="referral">${tileIco('🤝', 'partner')}<span><div class="lbl">Партнёрская программа</div></span></button>`}
        <div data-homescreen></div>
        ${APP_VERSION ? `<div class="small muted center app-version">версия ${esc(APP_VERSION)}</div>` : ''}
      </div>`;
    view.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => { haptic('light'); go(b.dataset.go); }; });
    view.querySelectorAll('[data-mode]').forEach((b) => { b.onclick = () => switchMode(b.dataset.mode); });
    decorateTiles(view);
    homeScreenButton(view);
    applyBadges();
    pollBadges();
  };

  // «Как войти?» — курьер или админ выбирает режим при каждом открытии приложения.
  routes.role = async () => {
    setTitle('Relax Gas');
    const r = state.me.roles;
    view.innerHTML = `<div class="stack">
      <div class="hero"><div class="eyebrow">Relax Gas</div><h1>Как войти?</h1>
        <p>Привет, ${esc(state.me.user.first_name || 'друг')}! Выберите, в каком качестве открыть приложение.</p></div>
      ${r.admin ? '<button class="tile wide role-admin" data-mode="admin">' + tileIco('🛠', 'role-admin') + '<span><div class="lbl">Войти как администратор</div><div class="sub">Заказы, курьеры, клиенты, спорные заказы, заявки</div></span></button>' : ''}
      ${r.courier ? '<button class="tile wide role-courier" data-mode="courier">' + tileIco('🚴', 'role-courier') + '<span><div class="lbl">Войти как курьер</div><div class="sub">Свободные заказы, заказы в работе, история</div></span></button>' : ''}
      <button class="tile wide" data-mode="customer">${tileIco('🛍', 'role-customer')}<span><div class="lbl">Войти как покупатель</div><div class="sub">Каталог, корзина, мои заказы</div></span></button>
      <div data-homescreen></div>
    </div>`;
    homeScreenButton(view);
    decorateTiles(view);
    view.querySelectorAll('[data-mode]').forEach((b) => { b.onclick = () => switchMode(b.dataset.mode); });
  };

  // Главная администратора: только то, что нужно для работы админа.
  routes['admin-home'] = async () => {
    setTitle('Администратор');
    view.innerHTML = `<div class="stack">
      <div class="stack" data-alerts></div>
      <div class="tiles">
        <button class="tile wide role-admin" data-go="admin">${tileIco('🛠', 'admin')}<span><div class="lbl">Админка</div><div class="sub" data-badge-sub="admin">Заказы, товары, курьеры</div></span><span class="nbadge" data-badge="admin" hidden></span></button>
        <button class="tile wide role-admin" data-go="admin-history">${tileIco('📜', 'admin-history')}<span><div class="lbl">История заказов</div><div class="sub">Кто отвёз, куда и что</div></span></button>
        <button class="tile role-admin" data-go="dev">${tileIco('🎨', 'dev')}<span><div class="lbl">Разработчик</div><div class="sub">Дизайн приложения</div></span></button>
        <button class="tile role-danger" data-go="disputes">${tileIco('⚠️', 'disputes')}<span><div class="lbl">Спорные заказы</div><div class="sub" data-badge-sub="disputes">Покупатель не вышел на связь</div></span><span class="nbadge" data-badge="disputes" hidden></span></button>
      </div>
      <button class="tile wide role-admin" data-go="admin-chats">${tileIco('💬', 'chats')}<span><div class="lbl">Чаты с покупателями и курьерами</div><div class="sub">Отвечаете от имени RelaxGas</div></span><span class="nbadge" data-badge="chats" hidden></span></button>
      <button class="tile wide referral-admin" data-go="referral">${tileIco('🤝', 'partner-requests')}<span><div class="lbl">Партнёры и заявки</div><div class="sub">Рефералки, личные программы, баллы</div></span>
        <span class="ref-count" title="Заявки на партнёрскую программу">💼 <b data-refcount="partner">·</b></span>
      </button>
      <div data-homescreen></div>
        ${APP_VERSION ? `<div class="small muted center app-version">версия ${esc(APP_VERSION)}</div>` : ''}
    </div>`;
    view.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => { haptic('light'); go(b.dataset.go); }; });
    decorateTiles(view);
    homeScreenButton(view);
    applyBadges();
    await renderAdminAlerts();
    pollBadges();
  };

  // Уведомления — в самом верху главного экрана админа (26.10.2026): висят, пока вопрос не решён.
  // От курьеров — жёлтая рамка, от покупателей (вызов админа, индивидуальная доставка, заявки) — бирюзовая.
  async function renderAdminAlerts(list) {
    const box = view.querySelector('[data-alerts]');
    if (!box) return;
    if (!list) list = (await api('GET', '/api/admin/alerts')).alerts;
    state.alertsKey = list.map((a) => a.key).join('|');
    const actions = (a) => {
      if (a.kind === 'reload') return `<button class="btn ok small" data-al="reload" data-id="${a.id}" data-yes="1">✅ Подтвердить</button><button class="btn ghost small" data-al="reload" data-id="${a.id}" data-yes="0">✖️ Отклонить</button>`;
      if (a.kind === 'shift') return `<button class="btn ok small" data-al="shift" data-id="${a.id}" data-yes="1">✅ Подтвердить</button><button class="btn ghost small" data-al-edit="${a.id}">✏️ Исправить</button><button class="btn ghost small" data-al="shift" data-id="${a.id}" data-yes="0">✖️ Отклонить</button>`;
      if (a.kind === 'shift_end') return `<button class="btn ok small" data-al="shift_end" data-id="${a.id}">✅ Подтвердить конец смены</button>`;
      if (a.kind === 'silent') return `<button class="btn btn-flag small" data-al="shift_end" data-id="${a.id}">🏁 Закрыть смену</button>`;
      if (a.kind === 'break') return `<button class="btn ghost small" data-al="break" data-id="${a.id}">👌 ОК</button>`;
      if (a.kind === 'transfer') return `<button class="btn ok small" data-al="transfer" data-id="${a.id}" data-yes="1">✅ Подтвердить</button><button class="btn ghost small" data-al="transfer" data-id="${a.id}" data-yes="0">✖️ Отклонить</button>`;
      if (a.kind === 'counter_reset') return `<button class="btn ok small" data-al="counter_reset" data-id="${a.id}" data-yes="1">✅ Подтвердить</button><button class="btn ghost small" data-al="counter_reset" data-id="${a.id}" data-yes="0">✖️ Отклонить</button>`;
      if (a.kind === 'call') {
        const chat = customerChatUrl({ username: a.username, phone: a.phone });
        return `${chat ? `<button class="btn ghost small" data-chat="${esc(chat)}">💬 Написать</button>` : ''}<button class="btn ok small" data-al="call" data-id="${a.id}">✅ Решено</button>`;
      }
      const to = { disputed: 'disputes', partner: 'referral', not_delivered: 'admin', individual: 'admin' }[a.kind];
      return to ? `<button class="btn ghost small" data-open-route="${to}">Открыть</button>` : '';
    };
    // 28.10.2026: конец смены / «не отвечает» — остатки полных, пустые с замен и с возвратов, деньги;
    // склад — «Понял, не забуду» (сколько пустых было на руках).
    const litres = (o) => Object.entries(o || {}).filter(([, n]) => +n).map(([z, n]) => `${z} л — ${n}`).join(', ');
    const endBox = (e) => {
      if (!e) return '';
      const c = e.counters || {};
      return `<div class="alert-end small mt">
        <div><b>Полные:</b> ${e.stock.filter((i) => i.qty).map((i) => `${esc(i.name)} — ${i.qty}`).join(', ') || 'ничего'}</div>
        <div><b>♻️ Пустые с замен:</b> ${litres(e.empties.exchange) || '0'}</div>
        <div><b>♻️ Пустые с возвратов:</b> ${litres(e.empties.return) || '0'}</div>
        ${litres(e.empties.manual) ? `<div><b>♻️ Пустые (были до этого):</b> ${litres(e.empties.manual)}</div>` : ''}
        ${litres(e.empties.transfer) ? `<div><b>🔄 Пустые по обмену с курьерами:</b> ${litres(e.empties.transfer)}</div>` : ''}
        <div><b>🏦 Сдать в кассу:</b> ${money((c.cash || 0) + (c.returns || 0) - (c.keep || 0))} · <b>💼 Заработал:</b> ${money(c.earned || 0)}</div></div>`;
    };
    const handed = (a) => (a.empties_handed ? `<div class="small mt alert-ack">✅ Курьер нажал «Понял, не забуду» сдать пустые${litres(a.empties_handed) ? `: ${litres(a.empties_handed)}` : ' (пустых не было)'}</div>` : '');
    box.innerHTML = list.length ? `<div class="section-title">🔔 Уведомления · ${list.length}</div>` + list.map((a) => `<div class="card alert-card ${a.source} al-${a.kind}">
        <div class="alert-title">${esc(a.title)}</div>
        ${a.text && !a.end ? `<div class="small muted">${esc(a.text)}</div>` : ''}${endBox(a.end)}${handed(a)}${a.busy ? `<div class="small warn-text mt">Заказов в работе: ${a.busy} — закрыть смену можно после доставки или снятия</div>` : ''}${a.at ? `<div class="small muted">${dateText(a.at)}</div>` : ''}
        <div class="row mt alert-actions">${actions(a)}</div></div>`).join('') : '';
    box.querySelectorAll('[data-chat]').forEach((b) => { b.onclick = () => openTgUrl(b.dataset.chat); });
    box.querySelectorAll('[data-open-route]').forEach((b) => {
      b.onclick = () => { if (b.dataset.openRoute === 'admin') state.adminTab = 'orders'; go(b.dataset.openRoute); };
    });
    // «✏️ Исправить» заявку на смену: админ меняет количества и сразу подтверждает.
    box.querySelectorAll('[data-al-edit]').forEach((b) => {
      b.onclick = () => {
        const a = list.find((x) => x.kind === 'shift' && String(x.id) === b.dataset.alEdit);
        if (!a) return;
        openSheet(`<h3>✏️ ${esc(a.title)}</h3><div class="small muted">Исправьте количества и подтвердите</div>
          ${(a.items || []).map((i) => `<div class="stock-row"><span class="stock-lbl">${esc(i.name)}</span>
            <input type="text" inputmode="numeric" maxlength="3" data-fix="${i.id}" value="${i.qty}"></div>`).join('')}
          <div class="stack mt"><button class="btn ok" data-act="fix-ok">✅ Подтвердить с исправлениями</button><button class="btn ghost" data-act="fix-cancel">Отмена</button></div>`, (sheet) => {
          sheet.querySelectorAll('[data-fix]').forEach((i) => { i.oninput = () => { i.value = i.value.replace(/\D/g, '').slice(0, 3); }; });
          sheet.querySelector('[data-act=fix-cancel]').onclick = closeSheet;
          sheet.querySelector('[data-act=fix-ok]').onclick = (e) => withBusy(e.target, async () => {
            const stock = {};
            sheet.querySelectorAll('[data-fix]').forEach((i) => { stock[i.dataset.fix] = +(i.value || 0); });
            await api('POST', `/api/admin/couriers/${a.id}/shift`, { approve: true, stock });
            closeSheet(); haptic('success'); toast('Выход на смену подтверждён.');
            await renderAdminAlerts(); pollBadges();
          });
        });
      };
    });
    box.querySelectorAll('[data-al]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => {
        const kind = b.dataset.al, id = b.dataset.id, yes = b.dataset.yes === '1';
        if (kind === 'reload') await api('POST', `/api/admin/couriers/${id}/reload`, { approve: yes });
        if (kind === 'shift') await api('POST', `/api/admin/couriers/${id}/shift`, { approve: yes });
        if (kind === 'call') await api('POST', `/api/admin/calls/${id}/resolve`);
        if (kind === 'shift_end') await api('POST', `/api/admin/couriers/${id}/shift-end`);
        if (kind === 'break') await api('POST', `/api/admin/couriers/${id}/break-seen`);
        if (kind === 'counter_reset') await api('POST', `/api/admin/couriers/${id}/counter-reset`, { approve: yes });
        if (kind === 'transfer') await api('POST', `/api/admin/transfers/${id}`, { approve: yes });
        haptic('success'); toast('Готово.');
        await renderAdminAlerts();
        pollBadges();
      });
    });
  }

  // Курьер отметил заказ доставленным — покупатель ОБЯЗАН оценить его работу.
  // Окно поверх всего приложения, без кнопки «Закрыть»: пока оценки нет,
  // пользоваться магазином нельзя. Сервер отдаёт такой заказ в /api/me и
  // /api/badges (pending_rating), пока оценка не сохранена.
  // «Вам назначили заказ» — у курьера; закрыть можно только кнопкой «Понятно».
  function showAssignedGate(list) {
    let gate = document.getElementById('assign-gate');
    if (!list || !list.length || getMode() !== 'courier') { if (gate) gate.remove(); return; }
    const key = list.map((o) => o.id).join(',');
    if (gate && gate.dataset.key === key) return;
    if (!gate) { gate = document.createElement('div'); gate.id = 'assign-gate'; gate.className = 'gate'; document.body.appendChild(gate); }
    gate.dataset.key = key;
    closeSheet();
    haptic('warning');
    gate.innerHTML = `<div class="card stack center">
        <div style="font-size:44px">📦</div>
        <h3>${list.length > 1 ? `Вам назначили ${list.length} ${plural(list.length, 'заказ', 'заказа', 'заказов')}` : 'Вам назначили заказ'}</h3>
        <div class="stack" style="text-align:left">${list.map((o) => `<div class="small"><b>№${o.id}</b>${o.region ? ` · 📍 ${esc(o.region)}` : ''}<div class="muted">${esc((o.address || '').split('\n')[0])}</div></div>`).join('')}</div>
        <button class="btn ok" data-act="assign-ok">Понятно</button>
      </div>`;
    gate.querySelector('[data-act=assign-ok]').onclick = (e) => withBusy(e.target, async () => {
      await api('POST', '/api/courier/assigned/ack', { ids: list.map((o) => o.id) });
      gate.remove();
      state.courierTab = 'current';
      if (currentRoute().name === 'courier') { state.lastJson.courier = null; render(); } else go('courier');
    });
  }

  // «Вы ещё на смене?» — у курьера раз в час; закрыть нельзя, только ответить.
  // «Нет, закончил работу» недоступна, пока есть заказы в работе.
  function showShiftAsk(c) {
    let gate = document.getElementById('shift-gate');
    if (!c || !c.shift_ask || getMode() !== 'courier') { if (gate) gate.remove(); return; }
    const busy = c.shift_busy || 0;
    if (gate && gate.dataset.busy === String(busy)) return;
    const fresh = !gate;
    if (!gate) { gate = document.createElement('div'); gate.id = 'shift-gate'; gate.className = 'gate'; document.body.appendChild(gate); }
    gate.dataset.busy = String(busy);
    if (fresh) { closeSheet(); haptic('warning'); }
    gate.innerHTML = `<div class="card stack center">
        <div style="font-size:44px">🚴</div>
        <h3>Вы ещё на смене?</h3>
        <button class="btn ok" data-act="shift-yes">Да, работаем!</button>
        <button class="btn danger" data-act="shift-no" ${busy ? 'disabled' : ''}>Нет, закончил работу</button>
        ${busy ? `<div class="small muted">У вас ${busy} ${plural(busy, 'заказ', 'заказа', 'заказов')} в работе — закончить смену можно после доставки.</div>` : ''}
      </div>`;
    const after = () => {
      gate.remove();
      if (state.badges && state.badges.courier) state.badges.courier.shift_ask = false;
      state.lastJson.courier = null;
      if (currentRoute().name === 'courier') render();
    };
    gate.querySelector('[data-act=shift-yes]').onclick = (e) => withBusy(e.target, async () => {
      await api('POST', '/api/courier/shift/confirm'); haptic('success'); after();
    });
    gate.querySelector('[data-act=shift-no]').onclick = (e) => withBusy(e.target, async () => {
      await api('POST', '/api/courier/shift/end'); haptic('success'); toast('🔴 Смена закончена. Хорошего отдыха!'); after();
    });
  }

  function showRatingGate(p) {
    const gate = document.getElementById('gate');
    if (p && getMode() !== 'customer') p = null; // окно оценки — только в режиме покупателя
    if (!p) { gate.hidden = true; gate.innerHTML = ''; state.gateOrder = null; return; }
    if (state.gateOrder === p.order_id && !gate.hidden) return;
    state.gateOrder = p.order_id;
    closeSheet();
    let rating = 0;
    gate.innerHTML = `<div class="card stack center">
        <div style="font-size:44px">⭐</div>
        <h3>Оцените курьера</h3>
        <div class="small muted">Заказ №${p.order_id} доставлен${p.courier_name ? ` курьером ${esc(p.courier_name)}` : ''}. Поставьте оценку, чтобы продолжить пользоваться магазином.</div>
        <div class="stars big-stars">${[1, 2, 3, 4, 5].map((r) => `<button data-gstar="${r}" aria-label="${r} из 5">${r}★</button>`).join('')}</div>
        <button class="btn ok" data-act="gate-ok" disabled>Отправить оценку</button>
      </div>`;
    gate.hidden = false;
    const ok = gate.querySelector('[data-act=gate-ok]');
    gate.querySelectorAll('[data-gstar]').forEach((b) => {
      b.onclick = () => {
        rating = +b.dataset.gstar;
        gate.querySelectorAll('[data-gstar]').forEach((x) => x.classList.toggle('on', +x.dataset.gstar <= rating));
        ok.disabled = false; haptic('light');
      };
    });
    ok.onclick = () => withBusy(ok, async () => {
      if (!rating) throw new Error('Выберите оценку от 1 до 5 звёзд.');
      try {
        await api('POST', `/api/orders/${p.order_id}/rate`, { rating });
      } catch (e) {
        // Уже оценён (например, с другого устройства) — просто идём дальше.
        if (!/уже сохранена/.test(e.message)) throw e;
      }
      haptic('success'); toast(`Спасибо! Вы поставили курьеру оценку ${rating} ★.`);
      const next = await api('GET', '/api/badges');
      state.badges = next;
      if (state.me) state.me.pending_rating = next.pending_rating || null;
      showRatingGate(next.pending_rating || null);
      if (!next.pending_rating && ['orders', 'history'].includes(currentRoute().name)) render();
    });
  }

  // ------------------------------------------------------------ BADGES
  // Счётчики на плитках. Пока приложение открыто, раз в несколько секунд
  // спрашиваем сервер; если появилось что-то новое — вибрация и сообщение
  // внутри приложения (уведомление бота в чате при этом тоже приходит).
  // Метка версии из index.html (коммит на сервере) — видно, обновилось ли приложение.
  const APP_VERSION = (document.querySelector('meta[name=app-version]') || {}).content || '';
  const BADGE_KEYS = { cart: 'cart', orders_active: 'orders', call_admin: 'chat' };
  const BADGE_POLL_MS = 7000;

  function badgeValues(b) {
    if (!b) return {};
    return {
      cart: b.cart || 0,
      orders: b.orders_active || 0,
      courier: b.courier ? b.courier.available : 0,
      admin: b.admin ? (b.admin.attention || 0) + (b.admin.reload_requests || 0) : 0,  // всё, что ждёт админа в «Админке»
      disputes: b.admin ? (b.admin.disputed || 0) : 0,
      regreq: b.admin ? (b.admin.reload_requests || 0) : 0,  // запросы курьеров: склад
      chat: b.chat_unread || 0,             // ответы RelaxGas покупателю
      chats: b.admin ? (b.admin.chats || 0) : 0,  // покупатели ждут ответа
    };
  }

  function applyBadges() {
    const b = state.badges;
    if (!b) return;
    const vals = badgeValues(b);
    document.querySelectorAll('[data-badge]').forEach((el) => {
      const n = vals[el.dataset.badge] || 0;
      el.textContent = n > 99 ? '99+' : String(n);
      el.hidden = !n;
    });
    const subs = {
      courier: b.courier ? `Свободных: ${b.courier.available} · В работе: ${b.courier.current}` : null,
      admin: b.admin ? ((b.admin.attention || b.admin.reload_requests) ? [b.admin.new ? `Новых: ${b.admin.new}` : '', b.admin.not_delivered ? `Не доставлено: ${b.admin.not_delivered}` : '', b.admin.reload_requests ? `Склад: ${b.admin.reload_requests}` : ''].filter(Boolean).join(' · ') : 'Заказы, товары, курьеры') : null,
      disputes: b.admin ? (b.admin.disputed ? `Ждут решения: ${b.admin.disputed}` : 'Покупатель не вышел на связь') : null,
    };
    if ('announcement' in b) {
      if (state.me) state.me.announcement = b.announcement;
      document.querySelectorAll('[data-announce]').forEach((el) => { el.innerHTML = announceBox(b.announcement); });
    }
    if (b.bonus) {
      if (state.me) state.me.bonus = b.bonus;
      document.querySelectorAll('[data-bonus-balance]').forEach((el) => { el.textContent = Number(b.bonus.balance).toLocaleString('ru-RU'); });
    }
    if (b.partner) {
      if (state.me) state.me.partner = b.partner;
      document.querySelectorAll('[data-partner-points]').forEach((el) => { el.textContent = Number(b.partner.points).toLocaleString('ru-RU'); });
      showLinkGate();
    }
    const ref = b.admin && b.admin.referral;
    document.querySelectorAll('[data-refcount]').forEach((el) => {
      const n = ref ? (ref[el.dataset.refcount] || 0) : 0;
      el.textContent = n > 99 ? '99+' : String(n);
      el.closest('.ref-count').classList.toggle('has', n > 0);
    });
    document.querySelectorAll('[data-badge-sub]').forEach((el) => {
      const t = subs[el.dataset.badgeSub];
      if (t) el.textContent = t;
    });
    // Непрочитанные в чатах заказов: у покупателя — от курьера, у курьера — от покупателей.
    const oc = getMode() === 'courier' ? ((b.courier && b.courier.chat) || {}) : (b.order_chat || {});
    document.querySelectorAll('[data-ochat-n]').forEach((el) => {
      const n = oc[el.dataset.ochatN] || 0;
      el.textContent = String(n); el.hidden = !n;
    });
  }
  // «💬 Чат с покупателем» у админа (индивидуальный заказ и заказ с предоплатой) — общий чат с покупателем.
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-adminchat]');
    if (!b) return;
    haptic('light');
    go('admin-chat', { u: b.dataset.adminchat });
  });
  // Кнопки «💬 Чат с курьером / покупателем / Переписка» — на любом экране.
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-ochat]');
    if (!b) return;
    haptic('light');
    go('order-chat', { id: b.dataset.ochat, as: getMode() === 'courier' ? 'courier' : getMode() === 'admin' ? 'admin' : 'customer' });
  });

  function badgeEvents(prev, next) {
    const msgs = [];
    if (!prev) return msgs;
    const mode = getMode();
    if (mode === 'courier' && next.courier && prev.courier && next.courier.available > prev.courier.available) {
      const d = next.courier.available - prev.courier.available;
      msgs.push(d === 1 ? '🆕 Появился свободный заказ' : `🆕 Появилось свободных заказов: ${d}`);
    }
    if (mode === 'customer' && (next.chat_unread || 0) > (prev.chat_unread || 0)) msgs.push('💬 Новое сообщение от RelaxGas');
    const sumv = (o) => Object.values(o || {}).reduce((a, x) => a + x, 0);
    if (mode === 'customer' && sumv(next.order_chat) > sumv(prev.order_chat)) msgs.push('💬 Курьер написал вам');
    if (mode === 'courier' && (next.chat_unread || 0) > (prev.chat_unread || 0)) msgs.push('💬 Администрация написала вам');
    if (mode === 'courier' && next.courier && prev.courier && sumv(next.courier.chat) > sumv(prev.courier.chat)) msgs.push('💬 Покупатель написал вам');
    if (mode === 'admin' && next.admin && prev.admin && (next.admin.chats || 0) > (prev.admin.chats || 0)) msgs.push('💬 Покупатель написал в чат');
    if (mode === 'admin' && next.admin && prev.admin) {
      if (next.admin.new > prev.admin.new) msgs.push(`🛠 Новый заказ (ждут: ${next.admin.new})`);
      if (next.admin.not_delivered > prev.admin.not_delivered) msgs.push('⚠️ Заказ не доставлен — нужна ваша помощь');
      if ((next.admin.disputed || 0) > (prev.admin.disputed || 0)) msgs.push('📵 Новый спорный заказ: покупатель не выходит на связь');
      const pr = prev.admin.referral || {}, nr = next.admin.referral || {};
      if ((nr.partner || 0) > (pr.partner || 0)) msgs.push('💼 Новая заявка на партнёрскую программу');
      if ((next.admin.reload_requests || 0) > (prev.admin.reload_requests || 0)) msgs.push('🏭 Курьер загрузился на складе — подтвердите');
      if ((next.admin.shift_requests || 0) > (prev.admin.shift_requests || 0)) msgs.push('🚴 Курьер хочет выйти на смену — подтвердите');
    }
    if (mode !== 'customer') return msgs; // новости о своих покупках — только в режиме покупателя
    const po = prev.orders || {};
    Object.entries(next.orders || {}).forEach(([id, o]) => {
      if (po[id] && po[id].status !== o.status) msgs.push(`🚚 Заказ №${id}: ${o.label}`);
      if (po[id] && o.arrived_at && o.arrived_at !== po[id].arrived_at) msgs.push(`🚪 Заказ №${id}: курьер на месте!`);
    });
    // Заказ пропал из активных: доставлен (окно оценки покажется само) или отменён.
    // Покупателю в бот больше ничего не приходит — сообщаем здесь.
    const rated = next.pending_rating ? String(next.pending_rating.order_id || next.pending_rating.id || '') : '';
    Object.keys(po).forEach((id) => {
      if (!(next.orders || {})[id] && id !== rated) msgs.push(`📦 Заказ №${id} закрыт — подробности в «Истории».`);
    });
    return msgs;
  }

  async function pollBadges() {
    if (!state.me || document.hidden || pollBadges.busy) return;
    pollBadges.busy = true;
    try {
      const next = await api('GET', '/api/badges');
      const msgs = badgeEvents(state.badges, next);
      const prevAlerts = state.badges && state.badges.admin ? state.badges.admin.alerts : null;
      state.badges = next;
      if (next.pending_rating) showRatingGate(next.pending_rating);
      showAssignedGate(next.courier && next.courier.assigned);
      showShiftAsk(next.courier);
      // Главная админа: список уведомлений обновляем, когда их число поменялось.
      if (currentRoute().name === 'admin-home' && next.admin && prevAlerts !== next.admin.alerts) {
        renderAdminAlerts().catch(() => {});
      }
      if (state.me) state.me.cart_count = next.cart;
      applyBadges();
      if (msgs.length) {
        haptic('warning');
        toast(msgs.join('\n'), false, 5000);
        // Экран, к которому относится новость, обновляем сразу, не дожидаясь его таймера.
        const name = currentRoute().name;
        if (['orders', 'courier', 'admin'].includes(name) && document.getElementById('sheet').hidden) render();
      }
    } catch (e) { /* сеть пропала — попробуем в следующий раз */ } finally { pollBadges.busy = false; }
  }

  function startBadgePolling() {
    setInterval(pollBadges, BADGE_POLL_MS);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) pollBadges(); });
    try { if (tg && tg.onEvent) tg.onEvent('activated', pollBadges); } catch (e) { /* старый клиент */ }
  }

  // ------------------------------------------------------------ CATALOG
  routes.catalog = async () => {
    setTitle('Каталог');
    const [cat, cart] = await Promise.all([api('GET', '/api/catalog'), api('GET', '/api/cart')]);
    const draw = (cartData) => {
      setCartCount(cartData.count);
      const qty = {};
      cartData.items.forEach((i) => { qty[`${i.product_id}:${i.variant}`] = i.quantity; });
      if (!cat.products.length) {
        view.innerHTML = '<div class="empty"><div class="big">📦</div>Каталог пока пуст.</div>';
        return;
      }
      view.innerHTML = `<div class="stack">
        ${cat.banner ? `<img class="banner" src="${esc(cat.banner)}" alt="" onerror="this.remove()">` : ''}
        ${cat.products.map((p) => {
          const variants = p.price_exchange != null
            ? [['new', 'Новый', p.price], ['exchange', 'Замена', p.price_exchange]]
            : [['new', 'Цена', p.price]];
          return `<div class="card product">
            <div class="ph">${photoBox(p)}</div>
            <div>
              <h3>${esc(p.name)}</h3>
              <div class="desc">${esc(p.description)}</div>
              ${p.bonus_text ? bonusTag(p.bonus_text, p.bonus_desc, bonusScope(p)) : ''}
            </div>
            <div class="variants">${variants.map(([v, label, price]) => {
              const q = qty[`${p.id}:${v}`] || 0;
              const ctl = q
                ? `<div class="stepper"><button data-q="${p.id}:${v}:${q - 1}" aria-label="Меньше">−</button><span>${q}</span><button data-q="${p.id}:${v}:${q + 1}" aria-label="Больше">+</button></div>`
                : `<button class="btn small" data-q="${p.id}:${v}:1">В корзину</button>`;
              return `<div class="variant"><span class="vname">${label}</span><span class="price">${money(price)}</span>${ctl}</div>`;
            }).join('')}</div>
          </div>`;
        }).join('')}
      </div>
      ${cartData.count ? `<div class="bottom-bar"><div class="inner"><button class="btn" data-go="cart">🛒 Корзина · ${money(cartData.total)}</button></div></div>` : ''}`;
      view.querySelectorAll('[data-q]').forEach((b) => {
        b.onclick = () => withBusy(b, async () => {
          const [pid, variant, q] = b.dataset.q.split(':');
          const res = await api('POST', '/api/cart', { product_id: +pid, variant, quantity: +q });
          haptic('light');
          draw(res);
        });
      });
      const goCart = view.querySelector('[data-go=cart]');
      if (goCart) goCart.onclick = () => go('cart');
    };
    draw(cart);
  };

  // ------------------------------------------------------------ CART
  routes.cart = async () => {
    setTitle('Корзина');
    const draw = (cart) => {
      setCartCount(cart.count);
      if (!cart.items.length) {
        view.innerHTML = '<div class="empty"><div class="big">🛒</div>Корзина пуста.<div class="mt"><button class="btn small" data-go="catalog">Перейти в каталог</button></div></div>';
        view.querySelector('[data-go]').onclick = () => go('catalog');
        return;
      }
      view.innerHTML = `<div class="stack">
        ${cart.items.map((i) => `<div class="card tight">
          <div class="row spread"><div class="grow"><b>${esc(i.name)}</b><div class="small muted">${i.variant === 'exchange' ? 'Замена' : (i.variant === 'new' ? 'Новый' : '')} · ${money(i.price)}</div>${i.bonus_text ? bonusTag(i.bonus_text, i.bonus_desc, '') : ''}</div>
          <div class="stepper"><button data-q="${i.product_id}:${i.variant}:${i.quantity - 1}">−</button><span>${i.quantity}</span><button data-q="${i.product_id}:${i.variant}:${i.quantity + 1}">+</button></div></div>
          <div class="row spread small mt"><span class="muted">Сумма</span><b>${money(i.line_total)}</b></div>
        </div>`).join('')}
        ${(cart.gifts || []).length ? `<div class="card tight gift-card"><b>🎁 Подарки к заказу</b>${cart.gifts.map((g) => `<div class="row spread small mt"><span>${esc(g.name)}</span><b>${g.quantity} шт. · 0 ₽</b></div>`).join('')}</div>` : ''}
        ${cart.has_exchange ? '<div class="note">♻️ Для позиций «Замена» подготовьте пустой баллон к приезду курьера.</div>' : ''}
        <div class="card"><div class="total"><span>Итого</span><span>${money(cart.total)}</span></div></div>
        <button class="btn secondary" data-act="clear">🧹 Очистить корзину</button>
      </div>
      <div class="bottom-bar"><div class="inner"><button class="btn" data-act="checkout">Оформить заказ · ${money(cart.total)}</button></div></div>`;
      view.querySelectorAll('[data-q]').forEach((b) => {
        b.onclick = () => withBusy(b, async () => {
          const [pid, variant, q] = b.dataset.q.split(':');
          draw(await api('POST', '/api/cart', { product_id: +pid, variant, quantity: +q }));
          haptic('light');
        });
      });
      view.querySelector('[data-act=clear]').onclick = async (e) => {
        if (!(await confirmBox('Очистить корзину?'))) return;
        withBusy(e.target, async () => draw(await api('DELETE', '/api/cart')));
      };
      view.querySelector('[data-act=checkout]').onclick = () => go('checkout');
    };
    draw(await api('GET', '/api/cart'));
  };

  // ------------------------------------------------------------ БАРАБАН ВРЕМЕНИ (как будильник на iPhone)
  // Часы 00–23 и минуты 00/15/30/45; недоступное (раньше чем через 2 часа по Москве) — серое,
  // барабан на нём не останавливается. На каждом щелчке — вибро-отклик Telegram.
  function timeWheel(slots, current, done) {
    const ok = new Set(slots);
    const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'));
    const MINS = ['00', '15', '30', '45'];
    const hourOk = (h) => slots.some((t) => t.startsWith(h + ':'));
    const minOk = (h, m) => ok.has(`${h}:${m}`);
    let [h, m] = (current && ok.has(current) ? current : slots[0]).split(':');
    const ITEM = 44;
    const col = (key, items) => `<div class="wheel" data-wheel="${key}"><div class="wheel-pad"></div>${items.map((v, i) => `<div class="wheel-item" data-i="${i}">${v}</div>`).join('')}<div class="wheel-pad"></div></div>`;
    openSheet(`<h3>🟣 Доставить к</h3>
      <div class="small muted">Сегодня, время московское. Самое раннее ${esc(slots[0])}</div>
      <div class="wheels"><div class="wheel-band"></div>${col('h', HOURS)}<div class="wheel-colon">:</div>${col('m', MINS)}</div>
      <div class="wheel-value" data-wv></div>
      <div class="stack mt"><button class="btn" data-act="wheel-ok">Готово</button><button class="btn ghost" data-act="wheel-cancel">Отмена</button></div>`, (sheet) => {
      const wh = sheet.querySelector('[data-wheel=h]'), wm = sheet.querySelector('[data-wheel=m]');
      const paint = () => {
        wh.querySelectorAll('.wheel-item').forEach((el, i) => el.classList.toggle('off', !hourOk(HOURS[i])));
        wm.querySelectorAll('.wheel-item').forEach((el, i) => el.classList.toggle('off', !minOk(h, MINS[i])));
        sheet.querySelector('[data-wv]').textContent = `${h}:${m}`;
      };
      const shade = (wheel) => {  // объём: дальше от центра — меньше и бледнее
        const mid = wheel.scrollTop / ITEM;
        wheel.querySelectorAll('.wheel-item').forEach((el, i) => {
          const d = Math.min(Math.abs(i - mid), 3);
          el.style.transform = `perspective(300px) rotateX(${(i - mid) * -18}deg) scale(${1 - d * 0.08})`;
          el.style.opacity = String(1 - d * 0.25);
          el.classList.toggle('on', Math.round(mid) === i);
        });
      };
      const nearestOk = (items, i, good) => {
        for (let d = 0; d < items.length; d += 1) {
          if (i + d < items.length && good(items[i + d])) return i + d;
          if (i - d >= 0 && good(items[i - d])) return i - d;
        }
        return i;
      };
      const setup = (wheel, items, get, set, good) => {
        let last = items.indexOf(get()), timer = null;
        wheel.scrollTop = last * ITEM;
        shade(wheel);
        wheel.addEventListener('scroll', () => {
          shade(wheel);
          const i = Math.max(0, Math.min(items.length - 1, Math.round(wheel.scrollTop / ITEM)));
          if (i !== last) { last = i; haptic('select'); }
          clearTimeout(timer);
          timer = setTimeout(() => {  // остановился — на ближайшее доступное значение
            const j = nearestOk(items, i, good);
            if (j !== i) wheel.scrollTo({ top: j * ITEM, behavior: 'smooth' });
            set(items[j]); paint();
          }, 140);
        }, { passive: true });
        wheel.querySelectorAll('.wheel-item').forEach((el) => {
          el.onclick = () => { const j = +el.dataset.i; if (good(items[j])) wheel.scrollTo({ top: j * ITEM, behavior: 'smooth' }); };
        });
      };
      setup(wh, HOURS, () => h, (v) => {
        h = v;
        if (!minOk(h, m)) {  // в этом часе такой минуты нет — ближайшая доступная
          m = MINS[nearestOk(MINS, MINS.indexOf(m), (x) => minOk(h, x))];
          wm.scrollTo({ top: MINS.indexOf(m) * ITEM, behavior: 'smooth' });
        }
      }, hourOk);
      setup(wm, MINS, () => m, (v) => { m = v; }, (x) => minOk(h, x));
      paint();
      sheet.querySelector('[data-act=wheel-cancel]').onclick = () => { closeSheet(); done(null); };
      sheet.querySelector('[data-act=wheel-ok]').onclick = () => {
        const t = `${h}:${m}`;
        if (!ok.has(t)) { toast('Это время недоступно, выберите другое.', true); return; }
        haptic('success'); closeSheet(); done(t);
      };
    });
  }

  // ------------------------------------------------------------ CHECKOUT
  // «♻️ Возврат баллона»: выбрать баллоны (как в корзине) → то же оформление, но без оплаты.
  routes['return'] = async () => {
    setTitle('Возврат баллона');
    const r = await api('GET', '/api/returns');
    if (!state.returnPick) state.returnPick = {};
    const pick = state.returnPick;
    if (state.returnMode !== 'points') state.returnMode = 'cash';
    const hasPoints = r.items.some((x) => x.return_points > 0);
    const draw = () => {
      const pts = state.returnMode === 'points' && hasPoints;
      const list = pts ? r.items.filter((x) => x.return_points > 0) : r.items;
      const unitOf = (x) => (pts ? x.return_points : x.return_price);
      const total = list.reduce((sum, x) => sum + (pick[x.id] || 0) * unitOf(x), 0);
      const count = list.reduce((sum, x) => sum + (pick[x.id] || 0), 0);
      view.innerHTML = `<div class="stack">
        <div class="hero"><h1>♻️ Возврат баллона</h1>
          <p>Выберите, какие пустые баллоны вы отдаёте и как хотите получить оплату: ${hasPoints ? 'наличными от курьера или баллами (за баллы выгоднее)' : 'курьер заберёт баллоны и отдаст деньги наличными'}.</p></div>
        ${hasPoints ? `<div class="card"><div class="small muted">Получить за возврат</div><div class="row mt" style="gap:8px">
            <button class="chip ${pts ? '' : 'on'}" data-ret-mode="cash">💵 Наличными</button>
            <button class="chip ${pts ? 'on' : ''}" data-ret-mode="points">⭐ Баллами</button></div></div>` : ''}
        ${list.length ? `<div class="card">${list.map((x) => `<div class="return-row">
            <span><b>${esc(x.name)}</b><div class="small muted">${pts ? `вам начислят ${x.return_points} баллов за баллон` : `вам отдадут ${money(x.return_price)} за баллон`}</div></span>
            <span class="stepper"><button data-ret-minus="${x.id}" ${pick[x.id] ? '' : 'disabled'}>−</button><span>${pick[x.id] || 0}</span><button data-ret-plus="${x.id}" ${(pick[x.id] || 0) >= 20 ? 'disabled' : ''}>+</button></span></div>`).join('')}</div>
          <div class="card"><div class="total"><span>${pts ? 'Вам начислят' : 'Вам отдадут'}</span><span>${pts ? `${total} баллов` : money(total)}</span></div></div>`
          : '<div class="empty"><div class="big">♻️</div>Сейчас баллоны на возврат не принимаются.</div>'}
      </div>
      ${r.items.length ? `<div class="bottom-bar"><div class="inner"><button class="btn" data-ret-next ${count ? '' : 'disabled'}>Далее — адрес и время</button></div></div>` : ''}`;
      view.querySelectorAll('[data-ret-mode]').forEach((b) => { b.onclick = () => { state.returnMode = b.dataset.retMode; haptic('light'); draw(); }; });
      view.querySelectorAll('[data-ret-plus]').forEach((b) => { b.onclick = () => { pick[b.dataset.retPlus] = (pick[b.dataset.retPlus] || 0) + 1; haptic('light'); draw(); }; });
      view.querySelectorAll('[data-ret-minus]').forEach((b) => { b.onclick = () => { pick[b.dataset.retMinus] = Math.max(0, (pick[b.dataset.retMinus] || 0) - 1); haptic('light'); draw(); }; });
      const next = view.querySelector('[data-ret-next]');
      if (next) next.onclick = () => {
        state.returnItems = list.filter((x) => pick[x.id]).map((x) => ({ product_id: x.id, name: x.name, quantity: pick[x.id], price: unitOf(x) }));
        state.returnPoints = pts;
        go('checkout', { ret: 1 });
      };
    };
    draw();
  };

  routes.checkout = async (params) => {
    const isReturn = !!(params && params.get && params.get('ret') === '1' && state.returnItems && state.returnItems.length);
    setTitle(isReturn ? 'Возврат баллона' : 'Оформление');
    const info = await api('GET', '/api/checkout');
    if (isReturn) {
      // Возврат: «корзина» — выбранные баллоны по цене возврата; без оплаты, подъёма, бонусов и баллов.
      const items = state.returnItems;
      info.cart = { items: items.map((x) => ({ name: x.name, variant: 'return', quantity: x.quantity, price: x.price })),
        total: items.reduce((sum, x) => sum + x.quantity * x.price, 0), has_exchange: false };
      info.floor_lift_price = 0; info.bonus = { balance: 0, percent: 0, min_items_total: Infinity }; info.partner_points = null;
      info.payment_methods = [{ id: 'cash_on_delivery', label: '' }];
    }
    if (!info.cart.items.length) { go('cart'); return; }
    const c = state.checkout;
    c.isReturn = isReturn;
    // Каждый раз поля пустые; подставляется только имя из последнего заказа.
    // Сохранённые адреса — кнопками сверху, по нажатию (сами не подставляются).
    c.phone = formatPhone(c.phone || '');
    if (!c.name) c.name = info.customer_name || '';
    const saved = info.saved_addresses || [];
    // Поля адреса: последний сохранённый адрес, иначе — старый адрес одной строкой.
    if (!c.parts) { c.parts = { line: '' }; c.point = null; c.savedIdx = null; c.outside = false; }
    if (!info.payment_methods.some((m) => m.id === c.method)) c.method = 'cash_on_delivery';
    const liftPrice = info.floor_lift_price || 0;
    // ⬇️ Спуск пустого баллона — только в «Возврате баллона» (не больше баллонов, вычитается из выплаты).
    // В обычном заказе спуска нет (владелец 28.10.2026, 17:07): при «Замене» за спуск не платят.
    const descentPrice = isReturn ? (info.descent_price || 0) : 0;
    const descentMax = isReturn ? state.returnItems.reduce((sum, x) => sum + x.quantity, 0) : 0;
    c.descent = descentPrice ? Math.max(0, Math.min(Number(c.descent) || 0, descentMax)) : 0;
    // Подъём (28.10.2026): цена за подъём одного баллона, количество — не больше баллонов в заказе.
    const liftMax = info.cart.balloons || 0;
    c.lift = liftPrice ? Math.max(0, Math.min(Number(c.lift) || 0, liftMax)) : 0;
    const bonus = info.bonus || { balance: 0, percent: 2, min_items_total: 6000 };
    // Бонусами — только товары и не вся сумма (минимум 1 ₽ — курьеру).
    const bonusMax = Math.max(0, Math.min(bonus.balance, info.cart.total - 1));
    c.bonus = Math.max(0, Math.min(Math.floor(Number(c.bonus) || 0), bonusMax));
    // Партнёрские баллы (1 балл = 1 ₽): тоже только товары, вместе с бонусами минимум 1 ₽ — деньгами.
    const pbal = info.partner_points;
    const pointsMaxFor = () => (pbal == null ? 0 : Math.max(0, Math.min(pbal, info.cart.total - 1 - c.bonus)));
    c.points = Math.max(0, Math.min(Math.floor(Number(c.points) || 0), pointsMaxFor()));
    // Бонусы за заказ: % от (товары − списанные бонусы), но не меньше чем от порога.
    const willEarnFor = (spent) => (info.cart.total >= bonus.min_items_total
      ? Math.floor(Math.max(info.cart.total - spent, bonus.min_items_total) * bonus.percent / 100 + 1e-9) : 0);

    const options = info.crypto_options || [];
    if (!options.some((x) => x.id === c.qrId)) c.qrId = options.length === 1 ? options[0].id : null;
    // Примерная сумма в монете по текущему курсу (округление вверх, как на сервере).
    const coinAmount = (opt, rub) => {
      if (!opt || !opt.rate) return null;
      const k = 10 ** opt.decimals;
      return (Math.ceil((rub / opt.rate) * k - 1e-9) / k).toFixed(opt.decimals);
    };
    const cryptoChoice = (rub) => `<div class="section-title">💎 Выберите валюту</div>
        ${options.map((x) => {
          const amt = coinAmount(x, rub);
          return `<label class="choice ${c.qrId === x.id ? 'on' : ''}"><input type="radio" name="coin" value="${x.id}" ${c.qrId === x.id ? 'checked' : ''}><span>${esc(x.label)}${amt ? `<br><span class="small muted">≈ ${esc(amt)} ${esc(x.currency)}</span>` : ''}</span></label>`;
        }).join('')}
        <div class="small muted">Точная сумма в монете фиксируется по курсу в момент оформления заказа. Платите строго в выбранной сети.</div>`;
    const draw = () => {
      const cart = info.cart;
      const liftTotal = c.lift * liftPrice;
      c.points = Math.min(c.points || 0, pointsMaxFor());
      const pointsMax = pointsMaxFor();
      const descentTotal = c.descent * descentPrice;
      // 01.11.2026: возврат пустых баллонов в этом же заказе — наличные зачитываются в оплату, баллы придут после доставки.
      const retItems = isReturn ? [] : (info.return_items || []);
      if (!c.retPick) c.retPick = {};
      const retHasPts = retItems.some((x) => x.return_points > 0);
      if (c.retMode !== 'points' || !retHasPts) c.retMode = 'cash';
      const retPts = c.retMode === 'points';
      const retAvail = retItems.filter((x) => (retPts ? x.return_points > 0 : true));
      const retUnit = (x) => (retPts ? x.return_points : x.return_price);
      const retSum = retAvail.reduce((s, x) => s + (c.retPick[x.id] || 0) * retUnit(x), 0);
      const retCount = retAvail.reduce((s, x) => s + (c.retPick[x.id] || 0), 0);
      const retOffset = retPts ? 0 : retSum;
      const grand = cart.total + liftTotal - c.bonus - c.points - retOffset;
      state.checkoutBase = grand;  // без доставки за пределы Москвы — её добавляет paintOutside()
      const willEarn = willEarnFor(c.bonus + c.points);
      const retBlock = retItems.length ? `
        <div class="card">
          <div class="row spread"><span><b>♻️ Верну пустой баллон в этом заказе</b><div class="small muted">Курьер заберёт пустой баллон. ${retPts ? 'Баллы придут на ваш счёт после доставки.' : 'Сумма зачтётся — вы заплатите меньше.'}</div></span></div>
          ${retHasPts ? `<div class="row mt" style="gap:8px"><button type="button" class="chip ${retPts ? '' : 'on'}" data-rmode="cash">💵 Зачесть деньгами</button><button type="button" class="chip ${retPts ? 'on' : ''}" data-rmode="points">⭐ Баллами</button></div>` : ''}
          ${retAvail.map((x) => `<div class="return-row mt"><span><b>${esc(x.name)}</b><div class="small muted">${retPts ? `+${x.return_points} баллов за баллон` : `−${money(x.return_price)} за баллон`}</div></span>
            <span class="stepper"><button type="button" data-rstep="${x.id}:-1" ${c.retPick[x.id] ? '' : 'disabled'}>−</button><span>${c.retPick[x.id] || 0}</span><button type="button" data-rstep="${x.id}:1" ${(c.retPick[x.id] || 0) >= 20 ? 'disabled' : ''}>+</button></span></div>`).join('')}
          ${retCount ? `<div class="small mt">${retPts ? `Вам начислят: <b>${retSum} баллов</b>` : `Зачтём в оплату: <b>−${money(retSum)}</b>`}</div>` : ''}
          ${!retPts && retSum && grand < 1 ? '<div class="note warn mt">Возврат больше суммы покупки — выберите возврат баллами или уменьшите количество.</div>' : ''}
        </div>` : '';
      const pointsBlock = pbal == null ? '' : `
        <div class="section-title">⭐ Баллы реферальной программы</div>
        <div class="card">
          ${pbal > 0 ? `<div class="row spread"><span>У вас баллов</span><b>${Number(pbal).toLocaleString('ru-RU')}</b></div>
            <label class="field mt">Списать баллами, ₽ <span class="muted small">(не больше ${money(pointsMax)})</span>
              <input type="number" id="points-spend" inputmode="numeric" min="0" max="${pointsMax}" step="1" value="${c.points || ''}" placeholder="0"></label>
            <div class="btn-pair mt"><button class="btn ghost" data-act="points-max" ${pointsMax ? '' : 'disabled'}>Списать максимум</button><button class="btn ghost" data-act="points-none" ${c.points ? '' : 'disabled'}>Не списывать</button></div>
            <div class="small muted mt">1 балл = 1 ₽, только за товары. Отмените заказ — баллы вернутся.</div>`
            : '<div class="small muted">Баллов пока нет.</div>'}
        </div>`;
      const bonusBlock = `
        <div class="section-title">💰 Бонусы</div>
        <div class="card">
          ${bonus.balance > 0 ? `<div class="row spread"><span>У вас бонусов</span><b>${Number(bonus.balance).toLocaleString('ru-RU')}</b></div>
            <label class="field mt">Списать бонусами, ₽ <span class="muted small">(не больше ${money(bonusMax)})</span>
              <input type="number" id="bonus-spend" inputmode="numeric" min="0" max="${bonusMax}" step="1" value="${c.bonus || ''}" placeholder="0"></label>
            <div class="btn-pair mt"><button class="btn ghost" data-act="bonus-max" ${bonusMax ? '' : 'disabled'}>Списать максимум</button><button class="btn ghost" data-act="bonus-none" ${c.bonus ? '' : 'disabled'}>Не списывать</button></div>`
            : '<div class="small muted">Бонусов пока нет.</div>'}
          <div class="small muted mt">${willEarn
            ? `За этот заказ после доставки начислим <b>+${willEarn}</b> 💰 (${String(bonus.percent).replace('.', ',')} % от суммы товаров).`
            : `Бонусы начисляются за заказ от ${money(bonus.min_items_total)} — ${String(bonus.percent).replace('.', ',')} % от суммы товаров (без подъёма).`} 1 бонус = 1 ₽.</div>
        </div>`;
      const liftBlock = liftPrice && liftMax ? `
        <div class="card lift-card lift-red ${c.lift ? 'lift-on' : ''}">
          <div class="row spread"><span><b>🏢 Подъём на этаж</b><div class="small muted">${money(liftPrice)} за 1 подъём (1 баллон)</div></span>
            <div class="stepper"><button type="button" data-liftstep="-1" ${c.lift ? '' : 'disabled'} aria-label="Меньше">−</button><span>${c.lift}</span><button type="button" data-liftstep="1" ${c.lift < liftMax ? '' : 'disabled'} aria-label="Больше">+</button></div></div>
          <div class="small mt">${c.lift ? `Подъём: ${c.lift} × ${money(liftPrice)} = <b>${money(liftTotal)}</b>` : '🚶 Заберу сам'}</div>
        </div>` : '';
      // Когда доставить: как можно скорее или ко времени (сегодня, шаг 15 минут, не раньше чем через час).
      const slots = info.time_slots || [];
      if (c.slot && !slots.includes(c.slot)) c.slot = '';
      const whenBlock = `
        <div class="section-title">🕒 ${c.isReturn ? 'Когда забрать' : 'Когда доставить'}</div>
        <div class="lift-pair">
          <button type="button" class="lift-opt ${c.when !== 'time' ? 'on' : ''}" data-when="asap">⚡ Как можно скорее</button>
          <button type="button" class="lift-opt ${c.when === 'time' ? 'on' : ''}" data-when="time" ${slots.length ? '' : 'disabled'}>🟣 Ко времени</button>
        </div>
        ${!slots.length ? '<div class="small muted">Сегодня заказать ко времени уже нельзя</div>' : ''}
        ${c.when === 'time' && slots.length ? `<button type="button" class="card time-pick" data-act="pick-time">
          <span>Доставить к</span><b>${c.slot ? esc(c.slot) : '--:--'}</b><span class="small muted">${c.slot ? 'Изменить время' : 'Выберите время'}</span></button>` : ''}`;
      // Комментарий к заказу (вариант А, 25.10.2026): поле сразу открыто, оранжевая светящаяся рамка;
      // при оплате наличными — подсказка про сдачу.
      // Возврат баллона (28.10.2026): просто «Комментарий» — без примера и без сдачи (деньги отдаёт курьер).
      const noteBlock = c.isReturn ? `
        <div class="card note-glow">
          <div class="note-glow-title">💬 Комментарий</div>
          <textarea id="order-note" rows="3" maxlength="${info.comment_max || 300}">${esc(c.note || '')}</textarea>
        </div>` : `
        <div class="card note-glow">
          <div class="note-glow-title">💬 Комментарий к заказу</div>
          ${c.method === 'cash_on_delivery' ? '<div class="note-glow-change">💵 Нужна сдача? Напишите, с какой суммы</div>' : ''}
          <textarea id="order-note" rows="3" maxlength="${info.comment_max || 300}" placeholder="Например: вход со двора, потребуется сдача с 13 000 ₽">${esc(c.note || '')}</textarea>
          <div class="small muted mt">Комментарий увидят курьер и администратор</div>
        </div>`;
      // «⬇️ Спуск пустого баллона» (только возврат): цена за 1 баллон, не больше баллонов, вычитается из выплаты.
      const descentBlock = c.isReturn && descentPrice && descentMax ? `
        <div class="card note-glow lift-card">
          <div class="row spread"><span><b>⬇️ Спуск пустого баллона</b><div class="small muted">${state.returnPoints ? `${descentPrice} баллов` : money(descentPrice)} за 1 баллон — вычтем из ${state.returnPoints ? 'начисляемых баллов' : 'суммы, которую вам отдадут'}</div></span>
            <div class="stepper"><button type="button" data-descstep="-1" ${c.descent ? '' : 'disabled'} aria-label="Меньше">−</button><span>${c.descent}</span><button type="button" data-descstep="1" ${c.descent < descentMax ? '' : 'disabled'} aria-label="Больше">+</button></div></div>
          <div class="small mt">${c.descent ? `Спуск: ${c.descent} × ${state.returnPoints ? descentPrice + ' баллов' : money(descentPrice)} = <b>${state.returnPoints ? descentTotal + ' баллов' : money(descentTotal)}</b>` : '🚶 Вынесу сам'}</div>
        </div>` : '';
      const p = c.parts || { line: '' };
      // Дом, подъезд, квартира, этаж, домофон — только цифры (цифровая клавиатура); корпус — обычная.
      const extra = (key, label, digits) => `<label class="field">${label}<input type="text" data-part="${key}" ${digits ? 'inputmode="numeric" pattern="[0-9]*" data-digits' : ''} maxlength="${digits ? 6 : 20}" autocomplete="off" value="${esc(p[key] || '')}"></label>`;
      const savedChips = saved.length ? `<div class="addr-saved">${saved.map((a, i) => `<span class="chip-wrap"><button class="chip ${c.savedIdx === i ? 'on' : ''}" data-saved="${i}">🏠 ${esc(shortAddress(a))}</button><button class="chip-del" data-saved-del="${i}" aria-label="Удалить адрес">✕</button></span>`).join('')}<button class="chip ${c.savedIdx == null ? 'on' : ''}" data-saved="new">➕ Новый адрес</button></div>` : '';
      c.mode = 'address';  // «Я не дома (геопозиция)» убрано: точку выбирают на встроенной карте («📍 Выбрать на карте»)
      const addressBlock = `<div class="card flat-form">
          ${savedChips}
          <div class="flat-title">${c.savedIdx == null ? 'Новый адрес' : 'Сохранённый адрес'}</div>
          <div class="addr-wrap"><label class="field">Город, улица<input type="text" id="addr-line" autocomplete="off" placeholder="Например: Москва, Лесная" value="${esc(p.line || '')}"></label>
            <div class="suggest" data-suggest hidden></div></div>
          <div class="grid2 mt">${extra('house', 'Дом', true)}${extra('building', 'Корпус / строение')}${extra('entrance', 'Подъезд', true)}${extra('apartment', 'Квартира', true)}${extra('floor', 'Этаж', true)}${extra('intercom', 'Домофон', true)}</div>
          <div data-outside>${outsideNote()}</div>
          <div class="center mt"><button class="map-pill" data-act="map">📍 ${c.point ? 'Точка отмечена — изменить' : 'Выбрать на карте'}</button></div>
          ${info.address_attribution ? `<div class="small muted mt">Подсказки адресов: ${esc(info.address_attribution)}</div>` : ''}
        </div>`;
      view.innerHTML = `<div class="stack">
        ${info.blocked_text ? `<div class="note">🚫 ${esc(info.blocked_text)}</div>` : ''}
        <div class="section-title">👤 Контактные данные</div>
        <div class="card flat-form">
          <label class="field">Как к вам обращаться<input type="text" id="cname" maxlength="64" autocomplete="name" value="${esc(c.name)}"></label>
          <label class="field mt">Номер телефона<input type="tel" id="phone" inputmode="numeric" pattern="[0-9 ]*" autocomplete="tel" maxlength="15" placeholder="8 916 123 45 67" value="${esc(c.phone)}"></label>
        </div>
        <div class="section-title">🏠 ${c.isReturn ? 'Откуда забрать баллоны' : 'Адрес доставки'}</div>
        ${addressBlock}
        ${whenBlock}
        ${noteBlock}
        ${c.isReturn ? `${descentBlock}<div class="card">${state.returnPoints
            ? cart.items.map((i) => `<div class="row spread small"><span>${esc(i.name)} × ${i.quantity}</span><b>${i.price * i.quantity} баллов</b></div>`).join('')
            : itemsList(cart.items.map((i) => ({ name: i.name, variant: 'return', quantity: i.quantity, unit_price: i.price })))}
          ${c.descent ? `<div class="row spread small"><span>⬇️ Спуск пустого баллона: ${c.descent} × ${state.returnPoints ? descentPrice + ' баллов' : money(descentPrice)}</span><b>−${state.returnPoints ? descentTotal + ' баллов' : money(descentTotal)}</b></div>` : ''}
          <div class="divider"></div><div class="total"><span>${state.returnPoints ? 'Вам начислят' : 'Вам отдадут'}</span><span>${state.returnPoints ? Math.max(0, cart.total - descentTotal) + ' баллов' : money(Math.max(0, cart.total - descentTotal))}</span></div>
          <div class="small muted mt">${state.returnPoints ? '⭐ Курьер заберёт пустые баллоны, баллы зачислятся на ваш счёт бонусов.' : '💵 Курьер заберёт пустые баллоны и отдаст деньги наличными.'}</div></div>` : `
        <div class="section-title">Оплата</div>
        ${info.payment_methods.map((m) => `<label class="choice ${c.method === m.id ? 'on' : ''}"><input type="radio" name="pm" value="${m.id}" ${c.method === m.id ? 'checked' : ''}><span>${esc(m.label)}</span></label>`).join('')}
        ${c.method === 'crypto' ? cryptoChoice(grand) : ''}
        ${c.method === 'cash_on_delivery' ? '<div class="small muted">💵 Оплата курьеру наличными при получении.</div>' : `<div class="note mt">${esc(info.pay_on_delivery_warning || '')}</div>`}
        ${liftBlock}
        ${retBlock}
        ${bonusBlock}
        ${pointsBlock}
        <div class="card">${itemsList(cart.items.map((i) => ({ name: i.name, variant: i.variant, quantity: i.quantity, unit_price: i.price, bonus_text: i.bonus_text }))
          .concat((cart.gifts || []).map((g) => ({ name: g.name, quantity: g.quantity, unit_price: 0, gift: true }))))}${liftLine({ floor_lift: c.lift ? { floors: c.lift, price: liftPrice, total: liftTotal } : null, bonus_spent: c.bonus, points_spent: c.points })}${retCount ? `<div class="row spread small"><span>♻️ Возврат пустых баллонов × ${retCount}</span><b>${retPts ? `+${retSum} баллов` : `−${money(retSum)}`}</b></div>` : ''}
          <div data-feeline>${c.outside && c.quote ? `<div class="row spread small"><span>🚚 Предоплата за доставку (отдаёте курьеру)</span><b>${money(c.quote.fee)}</b></div>` : ''}</div>
          <div class="divider"></div><div class="total"><span>Итого к оплате</span><span data-grand>${money(grand)}</span></div></div>
        ${cart.has_exchange ? '<div class="note">♻️ Для позиций «Замена» подготовьте пустой баллон к приезду курьера.</div>' : ''}`}
      </div>
      <div class="bottom-bar"><div class="inner"><button class="btn" data-act="submit" ${info.blocked_text ? 'disabled' : ''}>${c.isReturn ? '♻️ Оформить возврат' : '✅ Оформить заказ'}</button></div></div>`;

      const phone = view.querySelector('#phone');
      phone.oninput = () => { phone.value = formatPhone(phone.value); c.phone = phone.value; markRequired(); };
      const cname = view.querySelector('#cname');
      cname.oninput = () => { c.name = cname.value; markRequired(); };
      if (c.mode === 'address') bindAddressFields();
      view.querySelector('.stack').addEventListener('input', markRequired);  // блок пересоздаётся при каждом draw()
      markRequired();
      const comment = view.querySelector('#comment');
      if (comment) comment.oninput = () => { c.comment = comment.value; };
      view.querySelectorAll('input[name=pm]').forEach((r) => { r.onchange = () => { c.method = r.value; draw(); }; });
      const pickTime = () => timeWheel(slots, c.slot, (t) => {
        if (t) { c.slot = t; c.when = 'time'; } else if (!c.slot) c.when = 'asap';
        draw();
      });
      view.querySelectorAll('[data-when]').forEach((b) => {
        b.onclick = () => {
          c.when = b.dataset.when; haptic('light'); draw();
          if (c.when === 'time' && !c.slot) pickTime();
        };
      });
      const pt = view.querySelector('[data-act=pick-time]');
      if (pt) pt.onclick = pickTime;
      const noteEl = view.querySelector('#order-note');
      if (noteEl) noteEl.oninput = () => { c.note = noteEl.value; };
      view.querySelectorAll('input[name=coin]').forEach((r) => { r.onchange = () => { c.qrId = +r.value; haptic('light'); draw(); }; });
      view.querySelectorAll('[data-descstep]').forEach((b) => {
        b.onclick = () => { c.descent = Math.max(0, Math.min(descentMax, c.descent + +b.dataset.descstep)); haptic('select'); draw(); };
      });
      view.querySelectorAll('[data-rmode]').forEach((b) => { b.onclick = () => { c.retMode = b.dataset.rmode; haptic('light'); draw(); }; });
      view.querySelectorAll('[data-rstep]').forEach((b) => {
        b.onclick = () => { const [id, dl] = b.dataset.rstep.split(':'); c.retPick[id] = Math.max(0, Math.min(20, (c.retPick[id] || 0) + +dl)); haptic('select'); draw(); };
      });
      view.querySelectorAll('[data-liftstep]').forEach((b) => {
        b.onclick = () => { c.lift = Math.max(0, Math.min(liftMax, c.lift + +b.dataset.liftstep)); haptic('select'); draw(); };
      });
      const bonusInput = view.querySelector('#bonus-spend');
      if (bonusInput) {
        bonusInput.onchange = () => {
          const v = Math.floor(Number(bonusInput.value) || 0);
          if (v > bonusMax) toast(`Можно списать не больше ${money(bonusMax)}.`, true);
          c.bonus = Math.max(0, Math.min(v, bonusMax));
          draw();
        };
        bonusInput.onkeydown = (e) => { if (e.key === 'Enter') bonusInput.blur(); };
      }
      const bmax = view.querySelector('[data-act=bonus-max]');
      if (bmax) bmax.onclick = () => { c.bonus = bonusMax; haptic('light'); draw(); };
      const bnone = view.querySelector('[data-act=bonus-none]');
      if (bnone) bnone.onclick = () => { c.bonus = 0; draw(); };
      const pInput = view.querySelector('#points-spend');
      if (pInput) {
        pInput.onchange = () => {
          const v = Math.floor(Number(pInput.value) || 0), mx = pointsMaxFor();
          if (v > mx) toast(`Можно списать не больше ${money(mx)}.`, true);
          c.points = Math.max(0, Math.min(v, mx));
          draw();
        };
        pInput.onkeydown = (e) => { if (e.key === 'Enter') pInput.blur(); };
      }
      const pmax = view.querySelector('[data-act=points-max]');
      if (pmax) pmax.onclick = () => { c.points = pointsMaxFor(); haptic('light'); draw(); };
      const pnone = view.querySelector('[data-act=points-none]');
      if (pnone) pnone.onclick = () => { c.points = 0; draw(); };
      view.querySelector('[data-act=submit]').onclick = (e) => submit(e.target);
    };

    // Обязательные поля, пока не заполнены правильно, подсвечены красным.
    function markRequired() {
      const need = (el, bad) => { if (el) el.closest('label.field').classList.toggle('need', bad); };
      need(view.querySelector('#cname'), (c.name || '').trim().length < 2);
      need(view.querySelector('#phone'), (c.phone || '').replace(/\D/g, '').length !== 11);
      if (c.mode === 'address' && c.parts) {
        need(view.querySelector('#addr-line'), (c.parts.line || '').trim().length < 3);
        need(view.querySelector('[data-part=house]'), !(c.parts.house || '').trim() && !/\d/.test(c.parts.line || ''));
      }
    }
    // Адрес не в Москве (по подсказке или точке на карте) — индивидуальный заказ.
    function outsideNote() {
      if (!c.outside) return '';
      if (c.isReturn) return '<div class="note warn mt">🧾 Адрес не в Москве — администратор свяжется с вами, чтобы договориться о возврате.</div>';
      if (c.quote) {
        return `<div class="note mt">🚚 <b>Доставка за пределы Москвы</b><br>${c.quote.km} км по дорогам за Москвой<br>${money(c.quote.base)} + ${money(c.quote.per_km)} × ${c.quote.km} км = <b>${money(c.quote.fee)}</b><div class="small mt">Эту сумму вы вносите курьеру предоплатой — в «Итого к оплате» она не входит.</div><div class="small muted mt">Маршрут: openrouteservice · © участники OpenStreetMap, ODbL</div></div>`;
      }
      if (c.quoteBusy) return '<div class="note mt">🚚 Считаем стоимость доставки…</div>';
      return '<div class="note warn mt">🧾 <b>Это индивидуальный заказ.</b> Доставка за пределы Москвы обсуждается отдельно — после оформления с вами свяжется администратор.</div>';
    }
    // Стоимость доставки за пределы Москвы — по точке дома (с карты или из базы адресов).
    let quoteTimer = null, quoteSeq = 0;
    function refreshQuote() {
      clearTimeout(quoteTimer);
      c.quote = null;
      const p = c.parts || {};
      const q = c.point ? `lat=${c.point.lat}&lon=${c.point.lon}`
        : (p.street_id && (p.house || '').trim() ? `street_id=${p.street_id}&house=${encodeURIComponent(p.house.trim())}${p.building ? `&building=${encodeURIComponent(p.building)}` : ''}` : '');
      c.quoteBusy = !!(c.outside && q && !c.isReturn);
      paintOutside();
      if (!c.quoteBusy) return;
      quoteTimer = setTimeout(async () => {
        const seq = ++quoteSeq;
        let r = null;
        try { r = await api('GET', `/api/delivery/quote?${q}`); } catch (e) { r = null; }
        if (seq !== quoteSeq) return;
        c.quoteBusy = false;
        c.quote = r && r.available ? r : null;
        paintOutside();
      }, 500);
    }
    function paintOutside() {
      const box = view.querySelector('[data-outside]');
      if (box) box.innerHTML = outsideNote();
      const fee = c.outside && c.quote ? c.quote.fee : 0;
      const g = view.querySelector('[data-grand]');
      if (g) g.textContent = money(state.checkoutBase);  // доставка — предоплатой курьеру, в «Итого» не входит
      const fl = view.querySelector('[data-feeline]');
      if (fl) fl.innerHTML = fee ? `<div class="row spread small"><span>🚚 Предоплата за доставку (отдаёте курьеру)</span><b>${money(fee)}</b></div>` : '';
    }
    const setOutside = (v) => {
      c.outside = v === false;
      refreshQuote();
    };
    // ---- Адрес: поля, подсказки улиц из своей базы OpenStreetMap, сохранённые адреса, карта
    let suggestTimer = null, suggestSeq = 0;
    function bindAddressFields() {
      const line = view.querySelector('#addr-line');
      const box = view.querySelector('[data-suggest]');
      const hide = () => { box.hidden = true; box.innerHTML = ''; };
      line.oninput = () => {
        c.parts.line = line.value; c.parts.street_id = null; c.savedIdx = null; c.point = null; setOutside(null);
        view.querySelectorAll('[data-saved]').forEach((b) => b.classList.toggle('on', b.dataset.saved === 'new'));
        clearTimeout(suggestTimer);
        if (!info.address_suggest || line.value.trim().length < 2) { hide(); return; }
        suggestTimer = setTimeout(async () => {
          const seq = ++suggestSeq;
          try {
            const r = await api('GET', `/api/address/suggest?q=${encodeURIComponent(line.value.trim())}`);
            if (seq !== suggestSeq || document.activeElement !== line) return;  // устаревший ответ
            const items = r.suggestions || [];
            if (!items.length) { hide(); return; }
            box.innerHTML = items.map((x, i) => `<button type="button" data-sug="${i}"><span>${esc(x.street)}</span><span class="small muted">${esc(x.city || '')}${x.moscow ? '' : ' · не Москва'}</span></button>`).join('');
            box.hidden = false;
            box.querySelectorAll('[data-sug]').forEach((b) => {
              b.onmousedown = (e) => e.preventDefault();  // не терять фокус поля
              b.onclick = () => {
                const x = items[+b.dataset.sug];
                line.value = x.value;
                c.parts.line = x.value;
                c.parts.street_id = x.id;
                setOutside(x.moscow);
                hide();
                haptic('light');
                const next = view.querySelector('[data-part=house]'); if (next) next.focus();
              };
            });
          } catch (e) { hide(); }
        }, 450);
      };
      line.onblur = () => setTimeout(hide, 200);
      view.querySelectorAll('[data-part]').forEach((inp) => {
        inp.oninput = () => {
          if (inp.hasAttribute('data-digits')) {
            const clean = inp.value.replace(/\D/g, '');  // только цифры, даже если вставили текст
            if (clean !== inp.value) inp.value = clean;
          }
          c.parts[inp.dataset.part] = inp.value; c.savedIdx = null;
          if (c.outside && (inp.dataset.part === 'house' || inp.dataset.part === 'building')) refreshQuote();
        };
      });
      view.querySelectorAll('[data-saved-del]').forEach((b) => {
        b.onclick = async (e) => {
          e.stopPropagation();
          const i = +b.dataset.savedDel;
          if (!(await confirmBox(`Удалить адрес «${shortAddress(saved[i])}» из сохранённых?`))) return;
          withBusy(b, async () => {
            const r = await api('DELETE', `/api/addresses/saved/${i}`, { line: saved[i].line });
            saved.splice(0, saved.length, ...(r.saved_addresses || []));
            if (c.savedIdx === i) { c.savedIdx = null; c.parts = { line: '' }; c.point = null; c.outside = false; }
            else if (c.savedIdx != null && c.savedIdx > i) c.savedIdx -= 1;
            haptic('light'); toast('Адрес удалён.'); draw();
          });
        };
      });
      view.querySelectorAll('[data-saved]').forEach((b) => {
        b.onclick = () => {
          if (b.dataset.saved === 'new') { c.parts = { line: '' }; c.point = null; c.savedIdx = null; }
          else {
            const a = saved[+b.dataset.saved];
            c.parts = { line: a.line, street_id: a.street_id || null, house: a.house || '', building: a.building || '',
              entrance: a.entrance || '', apartment: a.apartment || '', floor: a.floor || '', intercom: a.intercom || '' };
            c.point = a.lat != null ? { lat: a.lat, lon: a.lon } : null;
            c.savedIdx = +b.dataset.saved;
            c.outside = a.moscow === false;
          }
          haptic('light'); draw(); refreshQuote();
          if (b.dataset.saved === 'new') { const l = view.querySelector('#addr-line'); if (l) l.focus(); }
        };
      });
      view.querySelector('[data-act=map]').onclick = () => pickOnMap(c.point, (pt, address, moscow, found) => {
        c.point = pt;
        if (address) {
          c.parts.line = address; c.savedIdx = null;
          c.parts.street_id = found && found.street_id ? found.street_id : null;
          const digits = found && found.house ? String(found.house).match(/^\d+/) : null;
          if (digits) {
            c.parts.house = digits[0];
            const rest = String(found.house).slice(digits[0].length).replace(/^[\s,]*(к|корп\.?|с|стр\.?)\s*/i, '');
            c.parts.building = rest || c.parts.building || '';
          }
        }
        c.outside = moscow === false;
        draw(); refreshQuote();
      });
    }

    const validate = () => {
      if ((c.name || '').trim().length < 2) throw new Error('Укажите, как к вам обращаться.');
      if ((c.phone || '').replace(/\D/g, '').length !== 11) throw new Error('Телефон — 11 цифр в формате 8 916 123 45 67.');
      if (c.mode === 'address' && (c.parts.line || '').trim().length < 3) throw new Error('Укажите город и улицу.');
      if (c.mode === 'address' && !(c.parts.house || '').trim() && !/\d/.test(c.parts.line || '')) throw new Error('Укажите номер дома.');
      if (c.method === 'crypto' && !c.qrId) throw new Error('Выберите валюту и сеть для оплаты.');
      if (c.when === 'time' && !c.slot) throw new Error('Выберите, к какому времени доставить.');
    };
    // 30.10.2026: окно предоплаты — закрыть можно только кнопками «Да, готов» / «Нет, спасибо».
    const askPrepay = (q) => new Promise((resolve) => {
      let box = document.getElementById('prepay-gate');
      if (!box) { box = document.createElement('div'); box.id = 'prepay-gate'; box.className = 'gate'; document.body.appendChild(box); }
      box.innerHTML = `<div class="card stack center">
          <div style="font-size:44px">🚚</div>
          <h3>Доставка за пределы Москвы</h3>
          <div>Ваш адрес доставки находится за пределами зоны бесплатной доставки. Вы готовы внести предоплату за доставку?</div>
          <div class="note"><div style="font-size:26px;font-weight:800">${money(q.fee)}</div>
            <div class="small muted">${money(q.base)} + ${money(q.per_km)} × ${q.km} км</div></div>
          <button class="btn ok" data-act="pp-yes">Да, готов</button>
          <button class="btn ghost" data-act="pp-no">Нет, спасибо</button>
        </div>`;
      box.hidden = false;
      const done = (v) => { box.hidden = true; box.innerHTML = ''; resolve(v); };
      box.querySelector('[data-act=pp-yes]').onclick = () => done(true);
      box.querySelector('[data-act=pp-no]').onclick = () => done(false);
    });
    // Заказ на доставку: за пределы Москвы сначала спрашиваем о предоплате; «Нет» — заказ не оформляется.
    const postOrder = async (body) => {
      if (!body.kind && c.outside && c.quote && !body.prepay_agreed) {
        if (!(await askPrepay(c.quote))) return null;
        body.prepay_agreed = true;
      }
      try { return await api('POST', '/api/orders', body); } catch (e) {
        if (e.data && e.data.code === 'prepay_required' && !body.prepay_agreed) {
          if (!(await askPrepay(e.data.prepay))) return null;
          body.prepay_agreed = true;
          return await api('POST', '/api/orders', body);
        }
        throw e;
      }
    };
    const submit = (btn) => withBusy(btn, async () => {
      validate();
      if (c.isReturn) {
        const body = { ...orderBody(), kind: 'return', return_mode: state.returnPoints ? 'points' : 'cash', descent_count: c.descent || 0, return_items: state.returnItems.map((x) => ({ product_id: x.product_id, quantity: x.quantity })) };
        delete body.payment_method; delete body.floor_lift; delete body.bonus_spend; delete body.points_spend;
        const res = await api('POST', '/api/orders', body);
        state.returnItems = null; state.returnPick = null; state.returnPoints = false;
        afterOrder(res.order);
        return;
      }
      if (c.method === 'cash_on_delivery') {
        const res = await postOrder(orderBody());
        if (!res) return;  // «Нет, спасибо»: остаёмся на оформлении
        afterOrder(res.order);
        return;
      }
      if (c.outside && c.quote) {  // предоплата — до выбора способа оплаты заказа
        const agreed = await askPrepay(c.quote);
        if (!agreed) return;
        c.prepayAgreed = true;
      }
      // СБП / криптовалюта — оплата при курьере: сначала предупреждение, «Далее» оформляет заказ.
      const opt = c.method === 'crypto' ? options.find((x) => x.id === c.qrId) : null;
      const optAmt = opt ? coinAmount(opt, info.cart.total + c.lift * liftPrice - (c.bonus || 0) - (c.points || 0)) : null;
      openSheet(`<h3>${c.method === 'sbp' ? '📱 Оплата по СБП' : '💎 Оплата криптовалютой'}</h3>
        ${opt ? `<div class="card mt"><div class="small muted">Валюта и сеть</div><b>${esc(opt.label)}</b>${optAmt ? `<div class="small muted">≈ ${esc(optAmt)} ${esc(opt.currency)}</div>` : ''}</div>` : ''}
        <div class="note mt">${esc(info.pay_on_delivery_warning || '')}</div>
        <div class="stack mt"><button class="btn" data-act="pod-next">Далее</button><button class="btn ghost" data-act="pod-cancel">Изменить способ оплаты</button></div>`, (sheet) => {
        sheet.querySelector('[data-act=pod-cancel]').onclick = closeSheet;
        sheet.querySelector('[data-act=pod-next]').onclick = (e) => withBusy(e.target, async () => {
          const res = await postOrder(orderBody());
          closeSheet();
          if (!res) return;
          afterOrder(res.order);
        });
      });
    });
    draw();
    if (c.outside) refreshQuote();
  };

  function orderBody() {
    const c = state.checkout;
    const body = { phone: c.phone.trim(), payment_method: c.method, floor_lift_count: c.lift || 0, bonus_spend: c.bonus || 0, points_spend: c.points || 0,
      customer_name: (c.name || '').trim() };
    if (c.method === 'crypto' && c.qrId) body.payment_qr_id = c.qrId;
    if (!c.isReturn && c.retPick) {
      const ri = Object.entries(c.retPick).filter(([, q]) => q > 0).map(([id, q]) => ({ product_id: +id, quantity: q }));
      if (ri.length) { body.return_items = ri; body.return_mode = c.retMode === 'points' ? 'points' : 'cash'; }
    }
    if (c.prepayAgreed && c.outside) body.prepay_agreed = true;
    if ((c.note || '').trim()) body.comment = c.note.trim();
    if (c.when === 'time' && c.slot) body.deliver_time = c.slot;
    if (c.mode === 'geo') { body.lat = c.lat; body.lon = c.lon; body.address = c.comment; } else {
      const p = c.parts || {};
      body.address_parts = { line: (p.line || '').trim(), street_id: p.street_id || null, house: p.house || '', building: p.building || '',
        entrance: p.entrance || '', apartment: p.apartment || '', floor: p.floor || '', intercom: p.intercom || '' };
      if (c.point) body.point = c.point;
    }
    return body;
  }

  function afterOrder(order) {
    haptic('success');
    if (!order.is_return) setCartCount(0);  // возврат корзину не трогает
    state.checkout.lat = null; state.checkout.lon = null; state.checkout.comment = ''; state.checkout.lift = false; state.checkout.bonus = 0; state.checkout.points = 0; state.checkout.retPick = {}; state.checkout.retMode = 'cash';
    state.checkout.parts = null; state.checkout.point = null; state.checkout.savedIdx = null;  // в следующий раз — пустые поля
    state.checkout.phone = ''; state.checkout.name = '';
    state.quote = null; state.proof = null; state.checkout.prepayAgreed = false;
    go('order-done', { id: order.id, m: order.payment_method, ind: order.individual ? 1 : 0, ret: order.is_return ? 1 : 0, pp: order.prepay_amount ? 1 : 0 });
  }

  routes['order-done'] = async (params) => {
    setTitle('Заказ оформлен');
    const cash = params.get('m') === 'cash_on_delivery';
    view.innerHTML = `<div class="stack center">
      <div class="hero"><div class="big" style="font-size:52px">${params.get('ret') === '1' ? '♻️' : '✅'}</div><h1>${params.get('ret') === '1' ? 'Возврат' : 'Заказ'} №${esc(params.get('id'))} оформлен</h1>
      <p>${params.get('ret') === '1'
        ? (params.get('ind') === '1' ? '🧾 Адрес не в Москве — администратор свяжется с вами, чтобы договориться о возврате.' : 'Мы ищем свободного курьера. Он заберёт пустые баллоны и отдаст вам деньги наличными.')
        : params.get('pp') === '1'
        ? '⏳ <b>Не закрывайте приложение — вам скоро напишет курьер.</b> Предоплату за доставку вы вносите курьеру.'
        : params.get('ind') === '1'
        ? '🧾 Это индивидуальный заказ: доставка за пределы Москвы обсуждается отдельно. Администратор свяжется с вами, чтобы согласовать условия.'
        : (cash ? 'Оплата — наличными при получении. Мы ищем свободного курьера, скоро сообщим статус.' : 'Оплата — при курьере: он покажет QR-код, переведите при нём. Мы ищем свободного курьера, скоро сообщим статус.')}</p></div>
      <button class="btn" data-go="orders">🚚 Следить за заказом</button>
      <button class="btn ghost" data-go="home">На главную</button></div>`;
    view.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => go(b.dataset.go); });
  };

  // ------------------------------------------------------------ PAY (СБП / крипта)
  routes.pay = async () => {
    setTitle('Оплата');
    const q = state.quote;
    if (!q) { go('checkout'); return; }
    const draw = () => {
      view.innerHTML = `<div class="stack">
        <div class="card center"><div class="small muted">${esc(q.label)}</div>
          ${q.usdt_amount
            ? `<div style="font-size:26px;font-weight:800;margin:6px 0">${esc(q.usdt_amount)} USDT</div><div class="muted">= ${money(q.total)} · курс 1 USDT = ${Number(q.usdt_rate).toFixed(2)} ₽</div>`
            : `<div style="font-size:26px;font-weight:800;margin:6px 0">${money(q.total)}</div>`}
          ${q.floor_lift ? `<div class="small muted">включая подъём на этаж: ${money(q.floor_lift.total)}</div>` : ''}
        </div>
        <img class="qr" src="${esc(q.qr)}" alt="QR-код для оплаты" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'note',textContent:'QR-код не загрузился. Попробуйте открыть оплату заново.'}))">
        <div class="note info">Переведите <b>${q.usdt_amount ? esc(q.usdt_amount) + ' USDT' : money(q.total)}</b> по QR-коду, затем прикрепите скриншот перевода — только после этого заказ будет оформлен.</div>
        <label class="btn ghost" for="proof-input">📎 ${state.proof ? 'Выбрать другой скриншот' : 'Прикрепить скриншот оплаты'}</label>
        <input type="file" id="proof-input" accept="image/*" hidden>
        ${state.proof ? `<img class="proof-preview" src="${state.proof}" alt="Скриншот оплаты">` : ''}
        <button class="btn secondary" data-act="change">💳 Изменить способ оплаты</button>
      </div>
      <div class="bottom-bar"><div class="inner"><button class="btn" data-act="send" ${state.proof ? '' : 'disabled'}>✅ Я оплатил — отправить скриншот</button></div></div>`;
      view.querySelector('#proof-input').onchange = async (e) => {
        try { state.proof = await compressImage(e.target.files[0], 1800, 0.85); draw(); } catch (err) { toast(err.message, true); }
      };
      view.querySelector('[data-act=change]').onclick = () => go('checkout');
      view.querySelector('[data-act=send]').onclick = (e) => withBusy(e.target, async () => {
        const res = await api('POST', '/api/orders', Object.assign(orderBody(), { screenshot: state.proof }));
        afterOrder(res.order);
      });
    };
    draw();
  };

  // ------------------------------------------------------------ CUSTOMER ORDERS
  function customerOrderCard(o) {
    const courier = o.courier ? `<div class="divider"></div><div class="row spread"><div><div class="small muted">Курьер</div><b>${esc(o.courier.name)}</b>${o.courier.rating ? ` <span class="small muted">★ ${fmtRating(o.courier.rating)}</span>` : ''}</div>
      ${['accepted', 'in_transit'].includes(o.status) ? `<button class="btn small ghost" data-ochat="${o.id}">💬 Чат с курьером<span class="nbadge" data-ochat-n="${o.id}" hidden></span></button>` : ''}</div>` : '';
    const showMap = ['accepted', 'in_transit'].includes(o.status);
    const mapBlock = showMap
      ? (o.courier_location ? `<div class="map" data-map="${o.id}"></div><div class="small muted mt">ℹ️ VPN может влиять на точность отображения курьера на карте.</div>` : '<div class="note info mt">📍 Курьер скоро появится на карте</div>')
      : '';
    return `<div class="card ${timeOpen(o) ? 'ord-time' : ''}" data-order="${o.id}">
      ${timePlaque(o)}
      ${returnHead(o)}
      <div class="row spread"><h3>Заказ №${o.id}</h3><span class="badge st-${o.status}">${esc(o.status_label)}</span></div>
      ${commentNote(o)}
      ${timeline(o.status)}
      <div class="small muted">${dateText(o.created_at)}${o.is_return ? (o.return_mode === 'points' ? ' · оплата — баллами' : ' · деньги — наличными от курьера') : ` · ${payLine(o)}`}</div>
      ${itemsList(o.items)}${liftLine(o)}
      <div class="divider"></div>${totalLine(o, 'customer')}
      ${o.has_exchange && o.status !== 'delivered' && o.status !== 'cancelled' ? '<div class="note mt">♻️ В заказе есть замена баллона — приготовьте пустой баллон к приезду курьера.</div>' : ''}
      ${o.status === 'not_delivered' ? '<div class="note mt">❌ Заказ не удалось доставить. Администратор свяжется с вами.</div>' : ''}
      ${o.status_note ? `<div class="note mt">${o.individual ? '🧾' : '📵'} ${esc(o.status_note)}</div>` : ''}
      ${o.prepay_amount ? `<div class="note ${o.prepay_taken ? 'ok' : 'warn'} mt">💵 Предоплата за доставку — <b>${money(o.prepay_amount)}</b>${o.prepay_taken ? ' · курьер её получил' : ' · отдайте её курьеру'}. В «Итого» она не входит.</div>` : ''}
      ${o.courier_arrived_at && ['accepted', 'in_transit'].includes(o.status) ? `<div class="note ok mt">🚪 <b>Курьер на месте</b> — подъехал в ${timeText(o.courier_arrived_at)} и ждёт вас.</div>` : ''}
      ${courier}${mapBlock}
      <div class="stack mt">
        ${o.can_cancel ? `<button class="btn danger" data-cancel="${o.id}">❌ Отменить заказ</button>` : ''}
        ${o.can_rate ? `<div class="small muted">Оцените работу курьера:</div><div class="stars">${[1, 2, 3, 4, 5].map((r) => `<button data-rate="${o.id}:${r}">${r}★</button>`).join('')}</div>` : ''}
      </div>
      ${o.number ? `<div class="order-number">Номер вашего заказа: ${esc(o.number)}</div>` : ''}</div>`;
  }

  function bindOrderActions(reload) {
    view.querySelectorAll('[data-tg]').forEach((b) => {
      b.onclick = () => {
        const url = `https://t.me/${b.dataset.tg}`;
        try { if (tg && tg.initData) { tg.openTelegramLink(url); return; } } catch (e) { /* старый клиент */ }
        window.open(url, '_blank');
      };
    });
    view.querySelectorAll('[data-cancel]').forEach((b) => {
      b.onclick = async () => {
        if (!(await confirmBox(`Отменить заказ №${b.dataset.cancel}?`))) return;
        withBusy(b, async () => { await api('POST', `/api/orders/${b.dataset.cancel}/cancel`); toast(`Заказ №${b.dataset.cancel} отменён.`); reload(); });
      };
    });
    view.querySelectorAll('[data-rate]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => {
        const [id, r] = b.dataset.rate.split(':');
        await api('POST', `/api/orders/${id}/rate`, { rating: +r });
        haptic('success'); toast(`Спасибо! Вы поставили курьеру оценку ${r} ★.`);
        reload();
      });
    });
  }

  routes.orders = async () => {
    setTitle('Активные заказы');
    const load = async (silent) => {
      const data = await api('GET', '/api/orders?scope=active');
      const key = JSON.stringify(data.orders.map((o) => Object.assign({}, o, { courier_location: !!o.courier_location })));
      if (silent && key === state.lastJson.orders) {
        // Изменились только координаты курьера — двигаем маркеры, без перерисовки.
        data.orders.forEach((o) => {
          const entry = state.maps.find((m) => m.orderId === o.id);
          if (entry && o.courier_location && entry.courier) { entry.courier.setLatLng([o.courier_location.lat, o.courier_location.lon]); }
        });
        return;
      }
      state.lastJson.orders = key;
      state.maps.forEach((m) => { try { m.map.remove(); } catch (e) { /* */ } });
      state.maps = [];
      if (!data.orders.length) {
        view.innerHTML = '<div class="empty"><div class="big">🚚</div>Активных заказов нет.<div class="mt"><button class="btn small" data-go="catalog">В каталог</button></div></div>';
        view.querySelector('[data-go]').onclick = () => go('catalog');
        return;
      }
      view.innerHTML = `<div class="stack">${data.orders.map(customerOrderCard).join('')}</div>`;
      bindOrderActions(() => load(false));
      for (const o of data.orders) {
        const el = view.querySelector(`[data-map="${o.id}"]`);
        if (el) {
          const entry = await drawMap(el, o.courier_location, o.customer_location);
          if (entry) entry.orderId = o.id;
        }
      }
    };
    await load(false);
    every(10000, () => load(true).catch(() => {}));
  };

  routes.history = async () => {
    setTitle('История заказов');
    const load = async () => {
      const data = await api('GET', '/api/orders?scope=history');
      view.innerHTML = `<div class="stack">
        <div class="card row spread"><span class="muted">Всего покупок</span><b>${data.purchase_count}</b></div>
        ${data.orders.length ? data.orders.map(customerOrderCard).join('') : '<div class="empty"><div class="big">📋</div>История пока пуста.</div>'}
      </div>`;
      bindOrderActions(load);
    };
    await load();
  };

  // ------------------------------------------------------------ ЧАТЫ (25.10.2026)
  // Общий экран переписки: свои сообщения справа, чужие слева; текст и фото; обновление раз в 4 с.
  // RelaxGas ↔ покупатель (от имени магазина, без личных Telegram) и курьер ↔ покупатель внутри заказа.
  const chatPhotos = {};
  function chatScreen(opt) {
    // opt: { title, intro, load(): {messages, can_send?}, send(body), photoUrl(id), theirLabel(m), empty }
    setTitle(opt.title);
    document.body.classList.add('in-chat');  // нижняя панель ролей не закрывает поле ввода
    view.innerHTML = `<div class="chat-wrap">
        ${opt.intro ? `<div class="small muted center chat-intro">${opt.intro}</div>` : ''}
        <div class="chat-list" data-chat-list><div class="skeleton"></div></div>
        <div class="chat-bar" data-chat-bar>
          <label class="chat-clip" title="Фото">📎<input type="file" accept="image/*" hidden data-chat-file></label>
          <textarea rows="1" maxlength="2000" placeholder="Сообщение" data-chat-text></textarea>
          <button class="chat-send" data-chat-send aria-label="Отправить">➤</button>
        </div>
        <div class="chat-photo-prev" data-chat-prev hidden></div>
      </div>`;
    const list = view.querySelector('[data-chat-list]');
    const txt = view.querySelector('[data-chat-text]');
    const prev = view.querySelector('[data-chat-prev]');
    let photo = null, lastKey = '';
    const loadPhoto = async (img) => {
      const id = img.dataset.photo;
      if (!chatPhotos[id]) { try { chatPhotos[id] = (await api('GET', opt.photoUrl(id))).src; } catch (e) { return; } }
      img.src = chatPhotos[id];
    };
    const draw = (data) => {
      const msgs = data.messages || [];
      const key = JSON.stringify(msgs.map((m) => [m.id, m.read])) + data.can_send;
      if (key === lastKey) return;
      lastKey = key;
      const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
      list.innerHTML = msgs.length ? msgs.map((m) => `<div class="msg ${m.mine ? 'mine' : ''}">
          ${!m.mine ? `<div class="msg-who">${esc(opt.theirLabel(m))}</div>` : ''}
          ${m.photo ? `<img class="msg-photo" data-photo="${m.id}" alt="Фото">` : ''}
          ${m.text ? `<div class="msg-text">${esc(m.text)}</div>` : ''}
          <div class="msg-at">${timeText(m.at)}${m.mine ? (m.read ? ' ✓✓' : ' ✓') : ''}</div></div>`).join('')
        : `<div class="empty small">${opt.empty}</div>`;
      list.querySelectorAll('[data-photo]').forEach((img) => { loadPhoto(img); img.onclick = () => openSheet(`<img src="${img.src}" style="width:100%;border-radius:12px">`); });
      if (atBottom || !list.dataset.scrolled) { list.scrollTop = list.scrollHeight; list.dataset.scrolled = '1'; }
      view.querySelector('[data-chat-bar]').hidden = data.can_send === false;
      if (data.can_send === false && opt.closedNote) list.insertAdjacentHTML('beforeend', `<div class="small muted center mt">${opt.closedNote}</div>`);
    };
    const refresh = async () => {
      try { draw(await opt.load()); } catch (e) {
        // Заказ закрыт (чат удалён из приложения) — показываем причину вместо пустого экрана.
        if (!lastKey) { list.innerHTML = `<div class="empty small">${esc(e.message || 'Чат недоступен')}</div>`; view.querySelector('[data-chat-bar]').hidden = true; }
      }
    };
    view.querySelector('[data-chat-file]').onchange = async (e) => {
      try {
        photo = await compressImage(e.target.files[0], 1600, 0.82);
        prev.innerHTML = `<img src="${photo}"><button class="btn ghost small" data-chat-unphoto>✕ Убрать фото</button>`;
        prev.hidden = false;
        prev.querySelector('[data-chat-unphoto]').onclick = () => { photo = null; prev.hidden = true; prev.innerHTML = ''; };
      } catch (err) { toast(err.message, true); }
      e.target.value = '';
    };
    txt.oninput = () => { txt.style.height = 'auto'; txt.style.height = Math.min(txt.scrollHeight, 120) + 'px'; };
    view.querySelector('[data-chat-send]').onclick = (e) => withBusy(e.currentTarget, async () => {
      const text = txt.value.trim();
      if (!text && !photo) return;
      const body = { text };
      if (photo) body.photo = photo;
      draw(await opt.send(body));
      txt.value = ''; txt.style.height = 'auto'; photo = null; prev.hidden = true; prev.innerHTML = '';
      haptic('light');
      list.scrollTop = list.scrollHeight;
    });
    refresh();
    every(4000, refresh);
  }

  // 💬 Чат с RelaxGas — у покупателя (вместо «Позвать администратора»).
  routes.chat = async () => chatScreen({
    title: getMode() === 'courier' ? 'Чат с администрацией' : 'Чат с RelaxGas',
    intro: getMode() === 'courier' ? 'Напишите администратору — ответит здесь же' : 'Напишите вопрос, ответим здесь же',
    empty: 'Сообщений пока нет',
    load: () => api('GET', '/api/chat'),
    send: (body) => api('POST', '/api/chat', body),
    photoUrl: (id) => `/api/chat/photo/${id}`,
    theirLabel: () => (getMode() === 'courier' ? 'Администрация' : 'RelaxGas'),
  });
  routes['call-admin'] = routes.chat;  // старые ссылки
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('[data-courier-chat]');
    if (b) { e.preventDefault(); go('chat'); }
  });

  // Чат курьера и покупателя внутри заказа (Telegram друг друга не видят).
  routes['order-chat'] = async () => {
    const p = currentRoute().params;
    const id = +p.get('id');
    const as = p.get('as') || (getMode() === 'courier' ? 'courier' : getMode() === 'admin' ? 'admin' : 'customer');
    let courierName = '';
    chatScreen({
      title: as === 'courier' ? `Покупатель · заказ №${id}` : as === 'admin' ? `Переписка · заказ №${id}` : `Курьер · заказ №${id}`,
      intro: as === 'admin' ? 'Переписка курьера и покупателя, только чтение' : 'Ваш Telegram не виден собеседнику',
      empty: 'Сообщений пока нет',
      closedNote: 'Чат закрыт, заказ уже не в работе',
      load: async () => { const r = await api('GET', `/api/orders/${id}/chat?as=${as}`); courierName = r.courier_name || ''; return r; },
      send: (body) => api('POST', `/api/orders/${id}/chat`, Object.assign(body, { as })),
      photoUrl: (mid) => `/api/order-chat/photo/${mid}`,
      theirLabel: (m) => (m.from_courier ? `Курьер${courierName ? ' ' + courierName : ''}` : 'Покупатель'),
    });
  };

  // 💬 Чаты с покупателями — у админа.
  routes['admin-chats'] = async () => {
    setTitle('Чаты: покупатели и курьеры');
    const draw = (data) => {
      view.innerHTML = `<div class="stack">
        <div class="small muted">Покупатели видят ответы от имени RelaxGas</div>
        ${data.threads.length ? data.threads.map((t) => `<div class="chat-row"><button class="card chat-thread ${t.unread ? 'unread' : ''}" data-thread="${t.user_id}">
            <div class="row spread"><b>${t.is_courier ? '🚴 ' : ''}${esc(t.name)}${t.username ? ` <span class="small muted">@${esc(t.username)}</span>` : ''}</b><span class="small muted">${dateText(t.at)}</span></div>
            <div class="row spread mt"><span class="small ${t.unread ? '' : 'muted'} chat-last">${t.last_from_admin ? 'Вы: ' : ''}${esc(t.last || '')}</span>${t.unread ? `<span class="nbadge">${t.unread}</span>` : ''}</div>
          </button><button class="chat-del" data-chat-del="${t.user_id}" data-name="${esc(t.name)}" aria-label="Удалить чат">✕</button></div>`).join('') : '<div class="empty">Сообщений от покупателей пока нет</div>'}</div>`;
      view.querySelectorAll('[data-thread]').forEach((b) => { b.onclick = () => go('admin-chat', { u: b.dataset.thread }); });
      view.querySelectorAll('[data-chat-del]').forEach((b) => {
        b.onclick = async () => {
          if (!(await confirmBox(`Удалить чат с покупателем ${b.dataset.name}? Переписка удалится и у покупателя.`, true))) return;
          withBusy(b, async () => { const d = await api('DELETE', `/api/admin/chats/${b.dataset.chatDel}`); key = JSON.stringify(d); draw(d); haptic('success'); toast('Чат удалён.'); });
        };
      });
    };
    let key = '';
    const load = async () => { const d = await api('GET', '/api/admin/chats'); const k = JSON.stringify(d); if (k !== key) { key = k; draw(d); } };
    await load();
    every(6000, () => load().catch(() => {}));
  };
  routes['admin-chat'] = async () => {
    const u = +currentRoute().params.get('u');
    let names = {};
    chatScreen({
      title: 'Чат с покупателем',
      intro: 'Вы отвечаете от имени RelaxGas',
      empty: 'Сообщений нет',
      load: async () => {
        const r = await api('GET', `/api/admin/chats/${u}`);
        setTitle(r.customer.name);
        const intro = view.querySelector('.chat-intro');
        if (intro) intro.innerHTML = `Вы отвечаете от имени RelaxGas${r.customer.phone ? ` · 📞 ${esc(r.customer.phone)}` : ''}${r.orders.length ? `<br>Заказы: ${r.orders.map((o) => `№${o.id} ${esc(o.status_label)}`).join(', ')}` : ''}`;
        r.messages.forEach((m) => { if (m.admin_name) names[m.id] = m.admin_name; });
        r.messages.forEach((m) => { if (m.from_admin) m.mine = true; });
        return r;
      },
      send: (body) => api('POST', `/api/admin/chats/${u}`, body),
      photoUrl: (id) => `/api/chat/photo/${id}`,
      theirLabel: () => 'Покупатель',
    });
  };

  // ------------------------------------------------------------ COURIER
  // Ссылка на чат с покупателем: по @username, иначе по номеру телефона
  // (t.me/+<номер> откроется, только если покупатель разрешил находить себя по номеру).
  function customerChatUrl(c) {
    if (!c) return null;
    const user = (c.username || '').replace(/^@/, '').trim();
    if (/^[A-Za-z0-9_]{4,}$/.test(user)) return `https://t.me/${user}`;
    let d = String(c.phone || '').replace(/\D/g, '');
    if (d.length === 11 && d[0] === '8') d = '7' + d.slice(1);
    else if (d.length === 10 && d[0] === '9') d = '7' + d;
    return d.length >= 10 ? `https://t.me/+${d}` : null;
  }
  function openTgUrl(url) {
    try { if (tg && tg.initData) { tg.openTelegramLink(url); return; } } catch (e) { /* старый клиент */ }
    window.open(url, '_blank');
  }

  // Внешние ссылки нельзя открывать внутри окна Mini App: страница уходит на
  // чужой сайт, и кнопкой «Назад» в приложение уже не вернуться. Поэтому
  // t.me — через Telegram, остальное — во внешнем браузере (tg.openLink).
  function openExternal(url) {
    if (/^https:\/\/t\.me\//i.test(url)) { openTgUrl(url); return; }
    try { if (tg && tg.initData && tg.openLink) { tg.openLink(url); return; } } catch (e) { /* старый клиент */ }
    window.open(url, '_blank', 'noopener');
  }
  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    let url;
    try { url = new URL(a.getAttribute('href'), location.href); } catch (err) { return; }
    if (!/^https?:$/.test(url.protocol) || url.origin === location.origin) return;  // tel:, свои страницы — как обычно
    e.preventDefault();
    e.stopPropagation();
    openExternal(url.href);
  }, true);

  // Отзывы курьеров о покупателе — приходят только в ответах для курьеров и админа.
  function customerReviewsBlock(r) {
    if (!r || (!r.rating_count && !(r.notes || []).length)) return '';
    const head = r.rating_count
      ? `★ ${fmtRating(r.rating)} · ${r.rating_count} ${plural(r.rating_count, 'оценка', 'оценки', 'оценок')}`
      : 'оценок пока нет';
    const notes = (r.notes || []).map((n) => `<li><span>${esc(n.comment)}</span><span class="small muted">${n.rating ? n.rating + '★ · ' : ''}${esc(n.courier_name || 'курьер')} · ${dateText(n.created_at)}${n.order_id ? ' · №' + n.order_id : ''}</span></li>`).join('');
    const all = r.user_id && r.total ? `<button class="btn ghost small mt" data-allreviews="${r.user_id}" style="width:100%">📋 Посмотреть все отзывы (${r.total})</button>` : '';
    return `<div class="note reviews mt"><div class="row spread"><b>👤 О покупателе</b><span>${head}</span></div>
      ${notes ? `<div class="small muted mt">Последние отзывы:</div><ul class="review-notes">${notes}</ul>` : ''}
      ${all}
      <div class="small muted">🔒 Видят только курьеры и администратор.</div></div>`;
  }
  // «Посмотреть все отзывы» — все оценки и комментарии всех курьеров о покупателе.
  async function openAllReviews(userId) {
    const d = await api('GET', `/api/staff/customers/${userId}/reviews`);
    const head = d.rating_count ? `★ ${fmtRating(d.rating)} · ${d.rating_count} ${plural(d.rating_count, 'оценка', 'оценки', 'оценок')}` : 'оценок пока нет';
    const rows = d.reviews.map((n) => `<li><span>${n.rating ? '★'.repeat(n.rating) + ' ' : ''}${n.comment ? esc(n.comment) : '<span class="muted">без комментария</span>'}</span><span class="small muted">${esc(n.courier_name || 'курьер')} · ${dateText(n.created_at)}${n.order_id ? ' · №' + n.order_id : ''}</span></li>`).join('');
    openSheet(`<h3>👤 Все отзывы о покупателе</h3>
      <div class="small muted">${head} · всего отзывов: ${d.reviews.length}</div>
      ${rows ? `<ul class="review-notes mt" style="max-height:60vh;overflow:auto">${rows}</ul>` : '<div class="small muted mt">Отзывов пока нет.</div>'}
      <div class="small muted mt">🔒 Видят только курьеры и администратор.</div>
      <button class="btn ghost mt" data-act="rv-close" style="width:100%">Закрыть</button>`, (sheet) => {
      sheet.querySelector('[data-act=rv-close]').onclick = closeSheet;
    });
  }
  // Один обработчик на всё приложение — кнопка есть в карточках курьера и админа.
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('[data-allreviews]');
    if (!b) return;
    e.preventDefault();
    openAllReviews(b.dataset.allreviews).catch((err) => toast(err.message, true));
  });

  // ---------- Приём оплаты при курьере
  function qrLabel(c) {
    if (c.label) return c.label; // «💵 USDT · TRC-20 (Tron)» — собирает сервер
    if (!c.currency || c.title.startsWith(c.currency)) return c.title;
    return `${c.title} · ${c.currency}${c.network ? ' ' + c.network : ''}`;
  }
  async function openPayScreen(orderId, code, done) {
    const p = await api('GET', `/api/courier/orders/${orderId}/pay/${code.id}`);
    const amount = code.kind === 'crypto'
      ? (p.amount
        ? `<div style="font-size:26px;font-weight:800;margin:6px 0">${esc(p.amount)} ${esc(code.currency)}</div><div class="muted small">= ${money(p.total_rub)} · курс 1 ${esc(code.currency)} = ${Number(p.rate).toLocaleString('ru-RU')} ₽</div>`
        : `<div style="font-size:26px;font-weight:800;margin:6px 0">${money(p.total_rub)}</div>`)
      : `<div style="font-size:26px;font-weight:800;margin:6px 0">${money(p.total_rub)}</div>`;
    openSheet(`<h3>💳 Заказ №${orderId} · ${esc(qrLabel(code))}</h3>
      <div class="card center mt"><div class="small muted">К оплате</div>${amount}${p.rate_note ? `<div class="small muted">${esc(p.rate_note)}</div>` : ''}</div>
      <img class="qr mt" src="${esc(code.image)}" alt="QR-код для оплаты" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'note',textContent:'QR-код не загрузился.'}))">
      <div class="note info mt">Покажите QR покупателю. Когда он переведёт деньги, сфотографируйте экран его телефона с успешным переводом.</div>
      <div class="cam mt" id="cam" hidden><video id="cam-video" playsinline autoplay muted></video>
        <div class="stack mt"><button class="btn" data-act="cam-snap">📸 Сделать снимок</button><button class="btn ghost" data-act="cam-cancel">Отмена</button></div></div>
      <div id="pay-actions">
        <button class="btn mt" data-act="pay-camera" style="width:100%">📷 Сфотографировать оплату</button>
        <label class="btn ghost mt" for="pay-gallery" style="width:100%">🖼 Выбрать из галереи</label>
      </div>
      <input type="file" id="pay-camera-input" accept="image/*" capture="environment" hidden>
      <input type="file" id="pay-gallery" accept="image/*" hidden>
      <button class="btn ghost mt" data-act="pay-close" style="width:100%">Закрыть</button>`, (sheet) => {
      sheet.querySelector('[data-act=pay-close]').onclick = closeSheet;
      const actions = sheet.querySelector('#pay-actions');
      const cam = sheet.querySelector('#cam');
      const video = sheet.querySelector('#cam-video');
      const send = async (photo, busyEl) => {
        try {
          busyEl.classList.add('loading');
          await api('POST', `/api/courier/orders/${orderId}/payment-confirm`, { photo, qr_id: code.id });
          closeSheet(); haptic('success'); toast('✅ Оплата подтверждена, фото отправлено в канал.');
          if (done) done();
        } catch (e) { toast(e.message, true); } finally { busyEl.classList.remove('loading'); }
      };
      const fromInput = (input, busyEl) => async () => {
        if (!input.files || !input.files[0]) return;
        try { await send(await compressImage(input.files[0], 1600, 0.85), busyEl); } catch (e) { toast(e.message, true); }
        input.value = '';
      };
      const camInput = sheet.querySelector('#pay-camera-input');
      const camBtn = sheet.querySelector('[data-act=pay-camera]');
      camInput.onchange = fromInput(camInput, camBtn);
      const gallery = sheet.querySelector('#pay-gallery');
      gallery.onchange = fromInput(gallery, sheet.querySelector('label[for=pay-gallery]'));
      // На Android Telegram игнорирует capture у поля файла и открывает галерею
      // (bugs.telegram.org/c/44936) — там включаем камеру прямо в приложении.
      // На iPhone поле с capture сразу открывает камеру — оставляем его.
      const isAndroid = (tg && tg.platform === 'android') || /Android/i.test(navigator.userAgent);
      camBtn.onclick = async () => {
        if (!isAndroid || !(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) { camInput.click(); return; }
        try {
          stopCamera();
          camStream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
          video.srcObject = camStream;
          await video.play().catch(() => {});
          cam.hidden = false; actions.hidden = true;
        } catch (e) {
          stopCamera();
          toast('Камера недоступна — разрешите Telegram доступ к камере или выберите фото из галереи.', true);
        }
      };
      sheet.querySelector('[data-act=cam-cancel]').onclick = () => { stopCamera(); cam.hidden = true; actions.hidden = false; };
      sheet.querySelector('[data-act=cam-snap]').onclick = async (e) => {
        const w = video.videoWidth, h = video.videoHeight;
        if (!w || !h) { toast('Камера ещё не готова — подождите секунду.', true); return; }
        const scale = Math.min(1, 1600 / Math.max(w, h));
        const canvas = document.createElement('canvas');
        canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        const photo = canvas.toDataURL('image/jpeg', 0.85);
        haptic('light');
        await send(photo, e.target);
      };
    });
  }

  // Кнопки оплаты в карточке заказа курьера: появляются после «Я на месте».
  function payButtons(o) {
    if (!o.pay_on_delivery || o.payment_confirmed) return '';
    if (!o.courier_arrived_at) return '<div class="small muted center">Кнопка «💳 Получить оплату» появится после «🚪 Я на месте».</div>';
    return `<button class="btn" data-getpay="${o.id}">💳 Получить оплату${o.payment_qr ? ' · ' + esc(o.payment_qr.label) : ''}</button>
      <button class="btn ghost change-pay" data-changepay="${o.id}"><span class="bang">❗</span> Изменить валюту <span class="bang">❗</span></button>`;
  }
  // «❗ Изменить валюту ❗» — если покупатель ошибся при оформлении: любой QR из админки.
  async function chooseOtherQr(order, done) {
    const pq = await api('GET', '/api/courier/payment-qr');
    const codes = pq.codes || [];
    if (!codes.length) { toast('Администратор ещё не добавил QR-коды для оплаты.', true); return; }
    const chosenId = order.payment_qr ? order.payment_qr.id : null;
    const group = (kind, title) => {
      const list = codes.filter((c) => c.kind === kind);
      return list.length ? `<div class="small muted mt">${title}</div><div class="stack mt">${list.map((c) => `<button class="btn ${c.id === chosenId ? '' : 'ghost'}" data-pickqr="${c.id}">${esc(qrLabel(c))}${c.id === chosenId ? ' · выбрал покупатель' : ''}</button>`).join('')}</div>` : '';
    };
    openSheet(`<h3>❗ Изменить валюту · заказ №${order.id}</h3>
      <div class="small muted">Если покупатель ошибся при оформлении, выберите QR, по которому он будет платить. Сумма пересчитается по курсу на момент оформления заказа. Замена будет видна в канале.</div>
      ${group('crypto', '💎 Криптовалюта')}${group('sbp', '📱 СБП')}
      <button class="btn ghost mt" data-act="pick-close" style="width:100%">Отмена</button>`, (sheet) => {
      sheet.querySelector('[data-act=pick-close]').onclick = closeSheet;
      sheet.querySelectorAll('[data-pickqr]').forEach((x) => {
        x.onclick = () => openPayScreen(order.id, codes.find((c) => c.id === +x.dataset.pickqr), done);
      });
    });
  }

  // Заказ с оплатой при курьере (СБП / криптовалюта).
  function podNote(o) {
    const head = o.payment_method === 'sbp' ? '⚠️ <b>Внимание! Оплата через СБП</b>' : '⚠️ <b>Внимание! Оплата криптовалютой</b>';
    if (o.payment_qr) {
      const q = o.payment_qr;
      return `<div class="note ${o.payment_confirmed ? 'ok' : ''} mt">${head} — покупатель переводит при вас по QR-коду. К оплате: <b>${money(o.total)}</b>
        <div class="mt">Покупатель выбрал: <b>${esc(q.label)}</b>${q.amount ? ` — <b>${esc(q.amount)} ${esc(q.currency)}</b> <span class="muted small">(1 ${esc(q.currency)} = ${Number(q.rate).toLocaleString('ru-RU')} ₽)</span>` : ''}</div>
        ${o.payment_confirmed ? '<div class="mt">✅ Оплата подтверждена (фото отправлено в канал).</div>' : ''}</div>`;
    }
    const coins = Object.entries(o.crypto_quotes || {}).map(([k, q]) => `<div class="small">• ${esc(q.amount)} ${esc(k)} <span class="muted">(1 ${esc(k)} = ${Number(q.rate).toLocaleString('ru-RU')} ₽)</span></div>`).join('');
    return `<div class="note ${o.payment_confirmed ? 'ok' : ''} mt">${head} — покупатель переводит при вас по QR-коду. К оплате: <b>${money(o.total)}</b>${coins}
      ${o.payment_confirmed ? '<div class="mt">✅ Оплата подтверждена (фото отправлено в канал).</div>' : ''}</div>`;
  }

  function paidBadge(o) {
    return `<span class="badge ${o.paid ? 'paid' : 'unpaid'}">${o.paid ? (o.payment_method === 'cash_on_delivery' ? '💵 наличные' : '✅ оплачен') : (o.pay_on_delivery ? (o.payment_method === 'sbp' ? '⚠️ СБП при курьере' : '⚠️ крипта при курьере') : '⏳ не оплачен')}</span>`;
  }
  function staffOrderCard(o, actions) {
    return `<div class="card">${staffOrderBody(o, actions)}</div>`;
  }
  function staffOrderBody(o, actions) {
    const geo = o.customer_location
      ? `https://yandex.ru/maps/?pt=${o.customer_location.lon},${o.customer_location.lat}&z=17&l=map`
      : `https://yandex.ru/maps/?text=${encodeURIComponent(o.address || '')}`;
    return `
      <div class="row spread"><h3>Заказ №${o.id}</h3><span class="row"><span class="badge ${o.paid ? 'paid' : 'unpaid'}">${o.paid ? (o.payment_method === 'cash_on_delivery' ? '💵 наличные' : '✅ оплачен') : (o.pay_on_delivery ? (o.payment_method === 'sbp' ? '⚠️ СБП при курьере' : '⚠️ крипта при курьере') : '⏳ не оплачен')}</span><span class="badge st-${o.status}">${esc(o.status_label)}</span></span></div>
      ${returnHead(o)}
      <div class="small muted mt">${dateText(o.created_at)}${o.is_return ? '' : ` · ${payLine(o)}`}</div>
      ${o.individual ? '<div class="note warn mt">🧾 <b>Индивидуальный заказ</b> — доставка не по Москве. Курьерам не отправлен. Свяжитесь с покупателем, согласуйте условия, затем «🚴 Назначить курьера» или «🚚 Передать курьерам».</div>' : (o.individual_any ? '<div class="small muted mt">🧾 Индивидуальный заказ (не Москва) — условия согласованы.</div>' : '')}
      ${itemsList(o.items)}${liftLine(o)}
      <div class="divider"></div>${totalLine(o, 'staff')}
      ${o.payment_method !== 'cash_on_delivery' && o.paid && !o.pay_on_delivery ? '<div class="note ok mt">✅ Клиент прислал скриншот оплаты — деньги с клиента НЕ брать.</div>' : ''}
      ${o.pay_on_delivery ? podNote(o) : ''}
      ${emptyHint(o)}
      <div class="divider"></div>
      <dl class="kv">
        <dt>Клиент</dt><dd>${esc(o.customer.name)}</dd>
        <dt>Телефон</dt><dd><a href="tel:${esc(o.customer.phone)}" style="color:var(--accent)">${esc(o.customer.phone)}</a></dd>
        <dt>Адрес</dt><dd>${esc(o.address)}</dd>
        ${o.courier ? `<dt>Курьер</dt><dd>${esc(o.courier.name)}${o.courier.phone ? ' · ' + esc(o.courier.phone) : ''}</dd>` : ''}
      </dl>
      ${customerReviewsBlock(o.customer_reviews)}
      <a class="btn ghost small mt" style="width:100%;text-decoration:none" href="${geo}" target="_blank" rel="noopener">🗺 Открыть в Яндекс.Картах</a>
      ${actions ? `<div class="stack mt">${actions}</div>` : ''}`;
  }

  // Компактная карточка заказа у админа: главное + действия; всё остальное — под «Подробнее».
  function feeEditor(o) {
    if (!o.individual_any || !['new', 'accepted', 'in_transit'].includes(o.status)) return '';
    const canRelease = o.individual && !o.courier && o.status === 'new' && o.paid;
    const base = (o.delivery_fee || 0) - (o.delivery_toll || 0);
    // Платная дорога — отдельной доплатой к доставке (видят покупатель и курьер).
    const toll = `<div class="fee-box mt">
      <label class="field">🛣 Платная дорога, ₽
        <input type="text" inputmode="numeric" pattern="[0-9]*" maxlength="6" data-toll-input="${o.id}" value="${o.delivery_toll || ''}" placeholder="0"></label>
      <button class="btn ghost mt" style="width:100%" data-toll-save="${o.id}">💾 Сохранить платную дорогу</button>
      <div class="small muted mt">Прибавится к доставке${base ? ` (${money(base)}${o.delivery_km ? `, ${o.delivery_km} км` : ''})` : ''} — увидят покупатель и курьер</div>
    </div>`;
    if (o.status === 'in_transit') return toll;
    return `<div class="fee-box mt">
      <label class="field">🚚 Стоимость доставки за пределы Москвы, ₽${o.delivery_km ? ` <span class="small muted">авторасчёт · ${o.delivery_km} км</span>` : ''}
        <input type="text" inputmode="numeric" pattern="[0-9]*" maxlength="7" data-fee-input="${o.id}" value="${base || ''}" placeholder="например, 1500"></label>
      <div class="stack mt">${canRelease ? `<button class="btn" data-fee-release="${o.id}">🚚 Сохранить и передать курьерам</button>` : ''}<button class="btn ghost" data-fee-save="${o.id}">💾 Сохранить стоимость</button></div>
      <div class="small muted mt">Сумма доставки прибавится к сумме заказа — её увидят покупатель и курьеры.</div>
    </div>${toll}`;
  }
  // Связь с покупателем (индивидуальный заказ за пределами Москвы — согласовать доставку).
  function contactButtons(c) {
    const chat = customerChatUrl(c);
    const phone = String((c && c.phone) || '').replace(/[^\d+]/g, '');
    if (!chat && !phone) return '<div class="small muted mt">У покупателя нет ни @логина, ни телефона — свяжитесь через бота.</div>';
    return `<div class="btn-pair mt">${chat ? `<button class="btn" data-chat="${esc(chat)}">💬 Написать</button>` : ''}${phone ? `<a class="btn ghost" style="text-decoration:none" href="tel:${esc(phone)}">📞 Позвонить</a>` : ''}</div>`;
  }
  // Плашка сверху карточки у админа: у кого заказ и что с ним сейчас.
  function courierHead(o) {
    if (!o.courier || !['accepted', 'in_transit'].includes(o.status)) return '';
    const who = esc(o.courier.name);
    if (o.courier_arrived_at) return `<div class="courier-head arrived">🚪 Курьер ${who} на месте · с ${timeText(o.courier_arrived_at)}</div>`;
    if (o.status === 'in_transit') return `<div class="courier-head transit">🚚 Заказ доставляется · ${who}</div>`;
    return `<div class="courier-head taken">🚴 Курьер ${who} взял заказ</div>`;
  }
  function adminOrderCard(o, actions, picked = null) {
    const open = state.adminOpen && state.adminOpen.has(o.id);
    const what = o.items.map((i) => `${i.gift ? '🎁 ' : ''}${esc(i.name)}${i.gift ? ' (подарок)' : i.variant === 'exchange' ? ' (замена)' : ''} × ${i.quantity}`).join(', ');
    const flags = [
      o.is_return ? '♻️ возврат баллона' : '',
      o.has_exchange ? '♻️ замена' : '',
      o.floor_lift ? `🏢 подъём × ${o.floor_lift.floors}` : '',
      !o.is_return && o.descent ? `⬇️ спуск × ${o.descent.count}` : '',
      o.bonus_spent ? `💰 −${money(o.bonus_spent)} бонусами` : '',
      o.region_label ? `📍 ${esc(o.region_label)}` : '',
    ].filter(Boolean);
    // Цвет карточки у админа: синий — курьер не назначен, оранжевый — назначен (в работе / в пути).
    // Заказ ко времени — ярко-фиолетовая рамка вместо синей / оранжевой.
    const tone = timeOpen(o) ? 'ord-time' : o.status === 'new' ? 'ord-free' : ['accepted', 'in_transit'].includes(o.status) ? 'ord-busy' : '';
    const prepayOpen = o.prepay_amount > 0 && !o.prepay_taken && ['new', 'accepted', 'in_transit'].includes(o.status);
    return `<div class="card ${o.individual || prepayOpen ? 'outside' : ''} ${tone}">
      ${o.individual_any ? outsideHead(o) : ''}
      ${prepayOpen ? `<div class="note warn mt">💵 <b>Предоплата за доставку ${money(o.prepay_amount)}</b> — покупатель отдаёт её курьеру. Назначьте курьера; пока он не нажмёт «Взял предоплату», остальные действия у него закрыты.</div>` : (o.prepay_amount ? `<div class="small muted mt">💵 Предоплата ${money(o.prepay_amount)}${o.prepay_taken ? ' — курьер взял' : ''}</div>` : '')}
      ${o.individual ? `<div class="note warn mt">🧾 <b>Индивидуальный заказ</b> — приложение не смогло рассчитать километры и стоимость за километр. Покупателю сказано, что с ним скоро свяжется администратор.</div>` : ''}
      ${o.individual || o.prepay_amount ? `<button class="btn ghost small mt" style="width:100%" data-adminchat="${o.customer.id || ""}">💬 Чат с покупателем</button>` : ''}
      ${returnHead(o)}
      ${timePlaque(o)}
      ${courierHead(o)}
      <div class="row spread"><h3>${picked !== null ? `<label class="pick"><input type="checkbox" data-pick="${o.id}" ${picked ? 'checked' : ''}></label>` : ''}№${o.id}</h3><span class="row">${paidBadge(o)}<span class="badge st-${o.status}">${esc(o.status_label)}</span></span></div>
      <div class="small muted">${dateText(o.created_at)}${o.number ? ' · ' + esc(o.number) : ''}</div>
      <dl class="kv mt">
        <dt>Клиент</dt><dd>${esc(o.customer.name)}</dd>
        <dt>Что</dt><dd>${what || '—'}</dd>
        <dt>Куда</dt><dd>${esc(o.address || '—')}</dd>
        ${o.is_return ? `<dt>${o.return_mode === 'points' ? 'Баллы' : 'Отдать'}</dt><dd>${o.return_mode === 'points' ? `<b>${o.return_points} баллов</b> · начислятся покупателю` : `<b>${money(o.return_payout)}</b> · наличными покупателю`}</dd>`
          : `<dt>Сумма</dt><dd><b>${money(o.total)}</b> · ${esc(o.payment_label)}</dd>`}
        ${o.courier ? `<dt>Курьер</dt><dd>${esc(o.courier.name)}</dd>` : ''}
      </dl>
      ${commentNote(o)}
      ${o.courier && ['accepted', 'in_transit', 'not_delivered', 'disputed'].includes(o.status) ? `<button class="btn ghost small mt" style="width:100%" data-ochat="${o.id}">💬 Переписка курьера и покупателя</button>` : ''}
      ${flags.length ? `<div class="small mt">${flags.join(' · ')}</div>` : ''}
      ${o.individual ? '<div class="note warn mt">🧾 Курьерам не отправлен. Согласуйте с покупателем стоимость доставки, впишите её и передайте курьерам.</div>' : ''}
      ${o.individual_any ? contactButtons(o.customer) : ''}
      ${feeEditor(o)}
      ${actions ? `<div class="stack mt">${actions}</div>` : ''}
      <button class="btn ghost small mt" style="width:100%" data-amore="${o.id}">${open ? '▲ Скрыть подробности' : 'ℹ️ Подробнее'}</button>
      ${open ? `<div class="mt">${staffOrderBody(o, '')}</div>` : ''}
    </div>`;
  }

  // Компактная карточка заказа у курьера: что, куда, чем платят + главные кнопки.
  // Остальное (покупатель, телефон, отзывы, карта, переписка) — под «Подробнее».
  function courierPayShort(o) {
    const base = courierPayShortBase(o);
    if (!o.returns || !o.returns.length) return base;
    const pts = o.return_mode === 'points';
    return `${base}<div class="small mt">♻️ <b>Забрать пустой:</b> ${o.returns.map((r) => `${esc(r.name)} × ${r.quantity}`).join(', ')} — ${pts ? `наличные не отдавать, клиенту начислят ${o.return_points} баллов` : `уже зачтено в сумму (−${money(o.return_payout)})`}</div>`;
  }
  function courierPayShortBase(o) {
    if (o.is_return) return o.return_mode === 'points' ? `⭐ Наличные не отдавать: покупателю начислят <b>${o.return_points} баллов</b>${o.descent ? `<div class="small">⬇️ Спуск ${money(o.descent.total)} вы берёте из своих наличных — сумма идёт в ваш заработок.</div>` : ''}` : `♻️ Отдать покупателю <b>${money(o.return_payout)}</b> наличными`;
    const sum = money(o.total) + (o.bonus_spent ? ` <span class="muted small">(−${money(o.bonus_spent)} бонусами)</span>` : '')
      + (o.points_spent ? ` <span class="muted small">(−${money(o.points_spent)} баллами)</span>` : '');
    if (o.payment_method === 'cash_on_delivery') return `💵 Наличные · <b>${sum}</b>`;
    if (o.payment_method === 'sbp') return `📱 СБП при курьере · <b>${sum}</b>`;
    if (o.payment_qr && o.payment_qr.kind === 'crypto') {
      return `${esc(o.payment_qr.label)}${o.payment_qr.amount ? ` · <b>${esc(o.payment_qr.amount)} ${esc(o.payment_qr.currency)}</b>` : ''} <span class="muted">(${sum})</span>`;
    }
    return `💎 Криптовалюта · <b>${sum}</b>`;
  }
  // «Доставка ко времени» — фиолетовая плашка с крупным временем; комментарий покупателя.
  const timeOpen = (o) => o.deliver_time && ['new', 'accepted', 'in_transit'].includes(o.status);
  function timePlaque(o) {
    return o.deliver_time ? `<div class="deliver-time"><span>🟣 Доставка ко времени</span><b>к ${esc(o.deliver_time)}</b></div>` : '';
  }
  function commentNote(o) {
    return o.comment ? `<div class="order-comment mt"><span class="small muted">💬 Комментарий к заказу</span><div>${esc(o.comment)}</div></div>` : '';
  }
  function courierCard(o, actions, moreActions) {
    const open = state.courierOpen && state.courierOpen.has(o.id);
    const what = o.items.map((i) => `${i.gift ? '🎁 ' : ''}${esc(i.name)}${i.gift ? ' (подарок)' : i.variant === 'exchange' ? ' (замена)' : ''} × ${i.quantity}`).join(', ');
    const flags = [
      o.has_exchange ? '♻️ замена — заберите пустой баллон' : '',
      o.floor_lift ? `🏢 подъём на этаж × ${o.floor_lift.floors}` : '',
      o.region_label ? `📍 ${esc(o.region_label)}` : '',
      o.pay_on_delivery && o.payment_confirmed ? '✅ оплата подтверждена' : '',
    ].filter(Boolean);
    const geo = o.customer_location
      ? `https://yandex.ru/maps/?pt=${o.customer_location.lon},${o.customer_location.lat}&z=17&l=map`
      : `https://yandex.ru/maps/?text=${encodeURIComponent(o.address || '')}`;
    return `<div class="card courier-card ${o.individual ? 'outside' : ''} ${timeOpen(o) ? 'ord-time' : ''}">
      ${o.individual_any ? outsideHead(o) : ''}
      ${o.prepay_amount && !o.prepay_taken ? `<div class="note warn mt">💵 <b>Возьмите предоплату с клиента за доставку: ${money(o.prepay_amount)}.</b> Договоритесь с покупателем в чате.</div>` : ''}
      ${returnHead(o)}
      ${timePlaque(o)}
      <div class="row spread"><h3>№${o.id}</h3><span class="badge st-${o.status}">${esc(o.status_label)}</span></div>
      <dl class="kv mt">
        <dt>${o.is_return ? 'Забрать' : 'Что'}</dt><dd>${what || '—'}</dd>
        <dt>Куда</dt><dd>${esc(o.address || '—')}</dd>
        <dt>${o.is_return ? 'Деньги' : 'Оплата'}</dt><dd>${courierPayShort(o)}</dd>
      </dl>
      ${commentNote(o)}
      ${flags.length ? `<div class="small mt">${flags.join(' · ')}</div>` : ''}
      ${o.individual_any ? prepaidFeeNote(o) : ''}
      ${actions ? `<div class="stack mt">${actions}</div>` : ''}
      <button class="btn ghost small mt" style="width:100%" data-more="${o.id}">${open ? '▲ Скрыть подробности' : 'ℹ️ Подробнее о заказе'}</button>
      ${open ? `<div class="mt">
        <div class="small muted">${dateText(o.created_at)}${o.is_return ? '' : ` · ${payLine(o)}`}</div>
        ${itemsList(o.items)}${liftLine(o)}
        <div class="divider"></div>${totalLine(o, 'staff')}
        ${o.pay_on_delivery ? podNote(o) : ''}
        ${emptyHint(o)}
        <dl class="kv mt">
          <dt>Клиент</dt><dd>${esc(o.customer.name)}</dd>
          <dt>Телефон</dt><dd><a href="tel:${esc(o.customer.phone)}" style="color:var(--accent)">${esc(o.customer.phone)}</a></dd>
          <dt>Адрес</dt><dd>${esc(o.address)}</dd>
        </dl>
        ${customerReviewsBlock(o.customer_reviews)}
        <a class="btn ghost small mt" style="width:100%;text-decoration:none" href="${geo}" target="_blank" rel="noopener">🗺 Открыть в Яндекс.Картах</a>
        ${moreActions ? `<div class="stack mt">${moreActions}</div>` : ''}
      </div>` : ''}
    </div>`;
  }



  // «💰 Деньги» за смену (владелец 28.10.2026): взял, отдал за пустые, заработок по статьям, платная дорога;
  // курьер сдаёт в кассу после вычета своего заработка; по СБП / крипте магазин рассчитывается с ним сам.
  function shiftMoneyCard(m) {
    if (!m) return '<div class="empty"><div class="big">💰</div>Смен ещё не было.</div>';
    const line = (label, v, note, cls) => `<div class="row spread money-li ${cls || ''}"><span>${label}${note ? `<span class="small muted d-block">${note}</span>` : ''}</span><b class="${v < 0 ? 'neg' : ''}">${money(v)}</b></div>`;
    const part = (x) => (x.noncash ? `из них по СБП / крипте ${money(x.noncash)} — выплатит магазин` : '');
    const e = m.earn, n = m.counts;
    const rows = [
      ['🛢 За баллоны', e.balloons, n.balloons ? `${n.balloons} шт.` : ''],
      ['🏢 Подъёмы на этаж', e.lift, ''],
      ['⬇️ Спуски пустых баллонов', e.descent, ''],
      ['🛣 Километры за МКАД', e.km, n.km ? `${n.km} км` : ''],
      ['♻️ Поездки на возврат', e.trip, n.trips ? `${n.trips} шт.` : ''],
    ].filter(([, x]) => x.cash || x.noncash);
    const toll = m.toll.cash + m.toll.noncash;
    const owe = m.kassa < 0;
    const same = m.keep === m.earned;  // 01.11.2026: одинаковые суммы не дублируем
    return `<div class="card shift-money">
      <h3>💰 ${m.ended_at ? 'Прошлая смена' : 'Смена'} · с ${dateText(m.started_at)}</h3>
      <div class="small muted">${n.orders} заказ(ов)${n.noncash_orders ? `, из них ${n.noncash_orders} по СБП / крипте` : ''}${n.trips ? ` · возвратов ${n.trips}` : ''}</div>
      <div class="section-title">Деньги на руках</div>
      ${line('💵 Взял у покупателей наличными', m.cash)}
      ${line('♻️ Отдал покупателям за пустые', -m.returns)}
      ${line('= На руках', m.on_hand, '', 'money-sum')}
      <div class="section-title">Заработок</div>
      ${rows.length ? rows.map(([l, x, cnt]) => line(l, x.cash + x.noncash, [cnt, part(x)].filter(Boolean).join(' · '))).join('') : '<div class="small muted">Пока нет</div>'}
      ${line('💼 Чистый заработок за смену', m.earned, '', 'money-sum')}
      ${toll ? line('🛣 Платная дорога — вернуть вам', toll, m.toll.noncash ? `из них по СБП / крипте ${money(m.toll.noncash)} — выплатит магазин` : '') : ''}
      <div class="section-title">Итог</div>
      ${same ? '' : line('✋ Оставляете себе из наличных', -m.keep, 'заработок и платная дорога по наличным заказам')}
      ${owe ? line('🏦 Магазин должен вам', -m.kassa, 'наличных меньше, чем ваш заработок', 'money-sum') : line('🏦 Сдать в кассу', m.kassa, same ? 'на руках − заработок' : 'на руках − оставляете себе', 'money-sum')}
      ${m.shop_owes ? line('💳 Выплатит магазин', m.shop_owes, 'заработок по заказам с оплатой по СБП / криптой') : ''}
    </div>`;
  }

  function courierSeg(tab) {
    const sh = state.courierShift;
    // Три кнопки рядом (28.10.2026): ☕ перерыв (обычная), 🏭 склад (сигнальная лента), 🏁 конец смены (финишный флаг).
    // Перерыв и конец смены — только без заказов в работе; склад — в любой момент смены.
    const store = sh && sh.stock ? (sh.reload_pending
      ? '<button class="btn btn-tape" disabled>⏳ Склад ждёт администратора</button>'
      : '<button class="btn btn-tape" data-store>🏭 Заехать на склад</button>') : '';
    const pause = sh && sh.active ? `<div class="shift-row"><button class="btn ghost" data-break ${sh.busy ? 'disabled' : ''}>☕ Взять перерыв</button>${store}<button class="btn btn-flag" data-endshift ${sh.busy ? 'disabled' : ''}>🏁 Закончить смену</button></div>${sh.busy ? '<div class="small muted center" style="margin-top:-6px">Перерыв и конец смены — когда нет заказов в работе.</div>' : ''}` : '';
    // «🔄 Обмен», «💰 Деньги» (28.10.2026) — пять вкладок: три в первом ряду, две во втором.
    const t = (id, label) => `<button data-tab="${id}" class="${tab === id ? 'on' : ''}">${label}</button>`;
    const cu = (state.badges && state.badges.chat_unread) || 0;
    const chatBtn = `<button class="btn ghost" data-courier-chat>💬 Чат с администрацией${cu ? ` <span class="nbadge">${cu}</span>` : ''}</button>`;
    return `${chatBtn}${pause}<div class="seg seg3 tabs5">${t('current', '🚚 В работе')}${t('info', 'ℹ️ Информация')}${t('money', '💰 Деньги')}${t('transfer', '🔄 Обмен')}${t('history', '📜 История')}</div>`;
  }

  const END_SHIFT_Q = 'Закончить смену? Администратор проверит, что у вас осталось, и подтвердит. Следующая смена начнётся с этим остатком.';
  // Склад (28.10.2026): обязательное «Понял, не забуду» — после него пустые баллоны обнуляются.
  const emptiesWarn = (empties, ack) => `<div class="empties-warn"><b><i>Не забудьте сдать на склад пустые баллоны</i></b>
      ${empties && Object.values(empties).some((n) => n) ? `<div class="small">Сейчас у вас пустых: ${Object.entries(empties).filter(([, n]) => n).map(([z, n]) => `${z} л — ${n}`).join(', ')}</div>` : ''}
      <button class="btn ${ack ? 'ok' : 'danger'} mt" data-ack>${ack ? '✅ Понял, не забуду' : '👌 Понял, не забуду'}</button></div>`;

  routes.courier = async () => {
    setTitle('Для курьеров');
    // «🏭 Заехать на склад»: курьер вписывает, сколько баллонов у него после загрузки.
    const bindStore = () => {
      const es = view.querySelector('[data-endshift]');
      if (es) es.onclick = async () => {
        if (!(await confirmBox(END_SHIFT_Q))) return;
        withBusy(es, async () => {
          state.courierShift = await api('POST', '/api/courier/shift/end');
          haptic('success'); toast('🏁 Отправлено администратору — ждём подтверждения'); state.lastJson.courier = null; load(false);
        });
      };
      const pb = view.querySelector('[data-break]');
      if (pb) pb.onclick = async () => {
        if (!(await confirmBox('Взять перерыв? Пока вы на перерыве, вы офлайн и заказы вам не приходят.'))) return;
        withBusy(pb, async () => {
          state.courierShift = await api('POST', '/api/courier/shift/break');
          haptic('success'); state.lastJson.courier = null; load(false);
        });
      };
      const b = view.querySelector('[data-store]');
      if (!b) return;
      b.onclick = () => {
        const catalog = (state.courierShift && state.courierShift.stock_items) || [];
        const sizes = catalog.map((i) => i.id);
        const nm = (id) => esc((catalog.find((i) => i.id === id) || {}).name || `товар ${id}`);
        const st = { vals: {}, removed: [], ack: false };
        const draw = (sheet) => {
          const ok = sizes.every((z) => st.removed.includes(z) || /^\d+$/.test(st.vals[z] || '')) && st.ack;
          sheet.innerHTML = `<div class="inner"><div class="grabber"></div><h3>🏭 Склад</h3>
            <div class="gate-sub mt">Укажите фактическое количество товаров после загрузки на складе</div>
            ${sizes.filter((z) => !st.removed.includes(z)).map((z) => `<div class="stock-row"><span class="stock-lbl">${nm(z)}</span>
              <input type="text" inputmode="numeric" maxlength="3" placeholder="0" data-rl="${z}" value="${esc(st.vals[z] || '')}">
              <button class="stock-del" data-rl-del="${z}" aria-label="Нет такого товара">✕</button></div>`).join('')}
            ${st.removed.length ? `<div class="row mt" style="gap:6px;flex-wrap:wrap">${st.removed.map((z) => `<button class="stock-back" data-rl-back="${z}">+ ${nm(z)}</button>`).join('')}</div>` : ''}
            ${emptiesWarn(state.courierShift && state.courierShift.empties, st.ack)}
            <div class="stack mt"><button class="btn" data-act="rl-save" ${ok ? '' : 'disabled'}>💾 Отправить администратору</button><button class="btn ghost" data-act="rl-cancel">Отмена</button></div></div>`;
          const save = sheet.querySelector('[data-act=rl-save]');
          sheet.querySelectorAll('[data-rl]').forEach((i) => {
            i.oninput = () => {
              i.value = i.value.replace(/\D/g, '').slice(0, 3); st.vals[i.dataset.rl] = i.value;
              save.disabled = !(sizes.every((z) => st.removed.includes(z) || /^\d+$/.test(st.vals[z] || '')) && st.ack);
            };
          });
          sheet.querySelector('[data-ack]').onclick = () => { st.ack = true; haptic('success'); draw(sheet); };
          sheet.querySelectorAll('[data-rl-del]').forEach((x) => { x.onclick = () => { st.removed.push(+x.dataset.rlDel); delete st.vals[x.dataset.rlDel]; draw(sheet); }; });
          sheet.querySelectorAll('[data-rl-back]').forEach((x) => { x.onclick = () => { st.removed = st.removed.filter((z) => z !== +x.dataset.rlBack); draw(sheet); }; });
          sheet.querySelector('[data-act=rl-cancel]').onclick = closeSheet;
          save.onclick = (e) => withBusy(e.target, async () => {
            const stock = {};
            sizes.filter((z) => !st.removed.includes(z)).forEach((z) => { stock[z] = +st.vals[z]; });
            state.courierShift = await api('POST', '/api/courier/reload', { stock, empties_ack: true });
            closeSheet(); haptic('success'); toast('🏭 Отправлено администратору — остаток обновится после подтверждения.', false, 5000);
            state.lastJson.courier = null; load(false);
          });
        };
        openSheet('', (sheet) => draw(sheet));
      };
    };
    const courierHistory = async () => {
      const q = (offset) => `/api/courier/history?offset=${offset}`;
      const data = await api('GET', q(0));
      const key = 'history' + JSON.stringify(data);
      if (key === state.lastJson.courier) return;
      state.lastJson.courier = key;
      view.innerHTML = `<div class="stack">
        ${courierSeg('history')}
        <div class="card tight row spread"><span class="muted">Доставлено заказов: ${data.total}</span></div>
        <div class="stack" data-hist-list></div>
        <button class="btn ghost" data-hist-more hidden>Показать ещё</button>
        <div data-homescreen></div></div>`;
      homeScreenButton(view);
      bindStore();
      view.querySelectorAll('[data-tab]').forEach((b) => { b.onclick = () => { state.courierTab = b.dataset.tab; load(false); }; });
      await historyList(view, (off) => api('GET', q(off)), false, data);
    };
    // «🔄 Обмен» (владелец 28.10.2026): курьер А выбирает курьера Б (только на смене), слева — что у него есть
    // (название и количество), справа — сколько отдать; «Передать» → 30 секунд можно отменить → админ подтверждает
    // → остатки меняются у обоих (полные и пустые).
    const courierTransfer = async (silent) => {
      const data = await api('GET', '/api/courier/transfer');
      const key = 'transfer' + JSON.stringify({ ...data, pending: data.pending && { ...data.pending, cancel_left: data.pending.cancel_left > 0 } });
      if (silent && key === state.lastJson.courier) return;
      state.lastJson.courier = key;
      clearInterval(state.transferTimer);
      const d = state.transferDraft || (state.transferDraft = { to: null, items: {}, empties: {} });
      if (d.to && !data.couriers.some((c) => c.id === d.to)) d.to = null;
      const num = (v) => Math.max(0, parseInt(v, 10) || 0);
      const row = (attr, k, label, qty) => `<div class="stock-row xfer-row"><span class="stock-lbl">${label}<span class="small muted xfer-have">есть: ${qty}</span></span>
          <input type="text" inputmode="numeric" maxlength="4" placeholder="0" ${attr}="${esc(String(k))}" data-max="${qty}" value="${(attr === 'data-xi' ? d.items[k] : d.empties[k]) || ''}"></div>`;
      let body;
      const p = data.pending;
      if (!data.on_shift) {
        body = '<div class="empty"><div class="big">🔄</div>Передавать товары другому курьеру можно только на смене.</div>';
      } else if (p) {
        body = `<div class="card xfer-wait">
          <h3>🔄 Передача курьеру ${esc(p.to)}</h3>
          <div class="small mt">${esc(p.text)}</div>
          ${p.cancel_left > 0
            ? `<div class="small muted mt">Отменить можно ещё <b data-xleft>${p.cancel_left}</b> сек. — потом заявка уйдёт администратору.</div>
               <button class="btn danger mt" data-xcancel>✖️ Отменить передачу</button>`
            : '<div class="small mt">⏳ Ждём подтверждения администратора. После одобрения остатки изменятся у вас обоих.</div>'}
        </div>`;
      } else if (!data.couriers.length) {
        body = '<div class="empty"><div class="big">🔄</div>Сейчас нет других курьеров на смене.</div>';
      } else {
        const has = data.items.length || Object.keys(data.empties).length;
        body = `<div class="card">
          <h3>Кому передать</h3>
          <div class="xfer-to mt">${data.couriers.map((c) => `<button type="button" class="btn ${d.to === c.id ? '' : 'ghost'} small" data-xto="${c.id}">${d.to === c.id ? '✅ ' : ''}${esc(c.name)}</button>`).join('')}</div>
        </div>
        ${has ? `<div class="card">
          <div class="row spread small muted"><span>Что у вас есть</span><span>Сколько отдать</span></div>
          ${data.items.map((i) => row('data-xi', i.id, esc(i.name), i.qty)).join('')}
          ${Object.entries(data.empties).map(([z, n]) => row('data-xe', z, `♻️ Пустой баллон ${esc(z)} л`, n)).join('')}
        </div>
        <button class="btn" data-xsend ${d.to ? '' : 'disabled'}>🔄 Передать</button>
        <div class="small muted center">${d.to ? 'После «Передать» 30 секунд можно отменить, потом подтверждает администратор.' : 'Сначала выберите курьера.'}</div>`
          : '<div class="empty"><div class="big">📦</div>У вас нет ни товаров, ни пустых баллонов.</div>'}`;
      }
      view.innerHTML = `<div class="stack">
        ${courierSeg('transfer')}
        ${body}
        <div data-homescreen></div></div>`;
      homeScreenButton(view);
      bindStore();
      view.querySelectorAll('[data-tab]').forEach((b) => { b.onclick = () => { state.courierTab = b.dataset.tab; load(false); }; });
      view.querySelectorAll('[data-xto]').forEach((b) => { b.onclick = () => { d.to = +b.dataset.xto; haptic('select'); state.lastJson.courier = null; courierTransfer(false); }; });
      view.querySelectorAll('[data-xi],[data-xe]').forEach((inp) => {
        inp.oninput = () => {
          let v = inp.value.replace(/\D/g, '').slice(0, 4);
          if (v && +v > +inp.dataset.max) { v = inp.dataset.max; toast(`Больше ${inp.dataset.max} отдать нельзя`); }
          inp.value = v;
          if (inp.dataset.xi) d.items[inp.dataset.xi] = num(v); else d.empties[inp.dataset.xe] = num(v);
        };
      });
      const send = view.querySelector('[data-xsend]');
      if (send) send.onclick = () => withBusy(send, async () => {
        const items = {}, empties = {};
        Object.entries(d.items).forEach(([k, v]) => { if (v) items[k] = v; });
        Object.entries(d.empties).forEach(([k, v]) => { if (v) empties[k] = v; });
        if (!Object.keys(items).length && !Object.keys(empties).length) { toast('Впишите, сколько отдать', true); return; }
        await api('POST', '/api/courier/transfer', { to_id: d.to, items, empties });
        state.transferDraft = null; haptic('success');
        toast('📨 Отправлено. 30 секунд можно отменить.', false, 3000);
        state.lastJson.courier = null; await courierTransfer(false);
      });
      const cancel = view.querySelector('[data-xcancel]');
      if (cancel) cancel.onclick = () => withBusy(cancel, async () => {
        await api('POST', '/api/courier/transfer/cancel');
        haptic('success'); toast('Передача отменена.');
        state.lastJson.courier = null; await courierTransfer(false);
      });
      const left = view.querySelector('[data-xleft]');
      if (left) {
        let n = +left.textContent;
        state.transferTimer = setInterval(() => {
          n -= 1;
          if (!document.body.contains(left)) { clearInterval(state.transferTimer); return; }
          if (n <= 0) { clearInterval(state.transferTimer); state.lastJson.courier = null; courierTransfer(false); return; }
          left.textContent = n;
        }, 1000);
      }
    };
    // 28.10.2026: работа по регионам убрана — курьер регион не выбирает, только «В путь!».
    const needGate = (sh) => sh && !sh.active;
    // Перерыв: курьер офлайн; «Вернуться на смену» — с прежними регионами и баллонами.
    const showBreak = (sh) => {
      view.innerHTML = `<div class="shift-gate only">
          <div class="center" style="font-size:52px">☕</div>
          <p class="gate-text">Вы на перерыве</p>
          <div class="small muted center">с ${timeText(sh.ended_at)} · вы офлайн, заказы вам не приходят</div>
          <button class="btn go-btn" data-resume>Вернуться на смену</button>
          <button class="btn ghost" data-endday>🏁 Закончить смену</button></div>`;
      view.querySelector('[data-resume]').onclick = (e) => withBusy(e.target, async () => {
        await api('POST', '/api/courier/shift/resume'); haptic('success'); state.lastJson.courier = null; await load(false);
      });
      view.querySelector('[data-endday]').onclick = async (e) => {
        if (!(await confirmBox(END_SHIFT_Q))) return;
        withBusy(e.target, async () => { await api('POST', '/api/courier/shift/end'); state.lastJson.courier = null; await load(false); });
      };
    };
    const gateOrBreak = (sh) => (sh.status === 'break' ? showBreak(sh) : sh.status === 'ending' ? showEnding(sh)
      : sh.status === 'silent' ? showSilent(sh) : (!sh.active && sh.request ? showWait(sh) : showGate(sh)));
    // «Закончить смену» отправлен — ждём администратора (28.10.2026).
    const showEnding = (sh) => {
      view.innerHTML = `<div class="shift-gate only">
          <div class="center" style="font-size:52px">🏁</div>
          <p class="gate-text">Ждём, пока администратор подтвердит конец смены</p>
          <div class="small muted center">После подтверждения следующая смена начнётся с вашим остатком</div>
          ${sh.stock ? `<div class="card"><div class="gate-sub">Осталось с собой</div>${(sh.stock.items || []).filter((i) => i.now).map((i) => `<div class="row spread small"><span>${esc(i.name)}</span><b>${i.now}</b></div>`).join('') || '<div class="small muted">Ничего</div>'}</div>` : ''}</div>`;
    };
    // Не ответил на опрос за 10 минут — заказы не приходят, пока не ответит (или админ не закроет смену).
    const showSilent = (sh) => {
      view.innerHTML = `<div class="shift-gate only">
          <div class="center" style="font-size:52px">⚠️</div>
          <p class="gate-text">Вы не ответили «Вы ещё на смене?»</p>
          <div class="small muted center">Пока вы не ответите, заказы вам не приходят. Администратор может закрыть смену.</div>
          <button class="btn go-btn" data-still>Да, работаем!</button>
          <button class="btn btn-flag" data-endday>🏁 Закончить смену</button></div>`;
      view.querySelector('[data-still]').onclick = (e) => withBusy(e.target, async () => {
        await api('POST', '/api/courier/shift/confirm'); haptic('success'); state.lastJson.courier = null; await load(false);
      });
      view.querySelector('[data-endday]').onclick = async (e) => {
        if (!(await confirmBox(END_SHIFT_Q))) return;
        withBusy(e.target, async () => { await api('POST', '/api/courier/shift/end'); state.lastJson.courier = null; await load(false); });
      };
    };
    // «В путь!» отправлен — ждём администратора (26.10.2026). Экран проверяет ответ каждые 4 с.
    const showWait = (sh) => {
      const r = sh.request;
      view.innerHTML = `<div class="shift-gate only">
          <div class="center" style="font-size:52px">⏳</div>
          <p class="gate-text">Ждём подтверждения администратора</p>
          <div class="small muted center">Как только администратор подтвердит, можно начинать работу</div>
          <div class="card"><div class="gate-sub">${r.source === 'left' ? 'С собой (остаток прошлой смены)' : r.source === 'warehouse' ? '🏭 Со склада — вы указали' : 'Вы указали'}</div>
            ${r.items.filter((i) => i.qty).map((i) => `<div class="row spread small"><span>${esc(i.name)}</span><b>${i.qty}</b></div>`).join('') || '<div class="small muted">Ничего с собой</div>'}
          </div>
          <button class="btn ghost" data-req-edit>✏️ Изменить</button></div>`;
      view.querySelector('[data-req-edit]').onclick = (e) => withBusy(e.target, async () => {
        state.shiftStock = { vals: Object.fromEntries(r.items.map((i) => [i.id, String(i.qty)])), removed: [] };
        state.shiftWarehouse = r.source === 'warehouse';
        await api('POST', '/api/courier/shift/request/cancel');
        state.lastJson.courier = null; await load(false);
      });
    };
    // Трансляция геопозиции в чате с ботом — обязательна (24.10.2026). Нет заказов в работе —
    // экран вместо заказов; есть — красная плашка сверху (чтобы можно было доставить).
    const liveOff = (sh) => sh && sh.active && !(sh.live && sh.live.active);
    const askLive = (btn) => withBusy(btn, async () => {
      const r = await api('POST', '/api/courier/live');
      state.liveAsked = true;
      openTgUrl(r.link);
      state.lastJson.courier = null; load(false);
    });
    // Инструкция по шагам: каждый шаг с новой строки, без лишних знаков.
    const LIVE_STEPS = [
      'Нажмите кнопку <b>Открыть чат с ботом</b>',
      'Внизу чата нажмите скрепку 📎',
      'Выберите <b>Геопозиция</b>',
      'Нажмите <b>Транслировать</b>',
      'Выберите срок <b>Пока не отключу</b>',
      'Вернитесь в приложение',
    ];
    const liveSteps = () => `<ol class="live-steps">${LIVE_STEPS.map((t) => `<li>${t}</li>`).join('')}</ol>`;
    const liveBar = (sh) => (liveOff(sh) && sh.busy ? `<div class="card live-off">
        <b>📡 Трансляция выключена</b>
        <div class="small">Покупатели и администратор вас не видят</div>
        ${liveSteps()}
        <button class="btn danger mt" data-live>📡 Открыть чат с ботом</button></div>` : '');
    const showLiveGate = (sh) => {
      const ended = sh.live && sh.live.at && !sh.live.active;
      view.innerHTML = `<div class="shift-gate only">
          <div class="center" style="font-size:52px">📡</div>
          <p class="gate-text">Включите трансляцию геопозиции</p>
          <div class="small muted center">${ended ? 'Трансляция выключилась<br>' : ''}Без неё заказы не откроются</div>
          <div class="card">${liveSteps()}<div class="small muted mt">Заказы откроются сами</div></div>
          <button class="btn go-btn" data-live>📡 Открыть чат с ботом</button>
          ${state.liveAsked ? '<div class="small center">⏳ Ждём трансляцию</div>' : ''}
          <div class="btn-pair shift-pair"><button class="btn ghost" data-break>☕ Взять перерыв</button><button class="btn ghost" data-endshift>🏁 Закончить смену</button></div></div>`;
      view.querySelector('[data-live]').onclick = (e) => askLive(e.target);
      const es = view.querySelector('[data-endshift]');
      es.onclick = async () => {
        if (!(await confirmBox(END_SHIFT_Q))) return;
        withBusy(es, async () => { await api('POST', '/api/courier/shift/end'); state.lastJson.courier = null; load(false); });
      };
      const pb = view.querySelector('[data-break]');
      pb.onclick = async () => {
        if (!(await confirmBox('Взять перерыв? Пока вы на перерыве, вы офлайн и заказы вам не приходят.'))) return;
        withBusy(pb, async () => { await api('POST', '/api/courier/shift/break'); state.lastJson.courier = null; load(false); });
      };
    };
    const showGate = (sh) => {
      if (!state.shiftStock) state.shiftStock = { vals: {}, removed: [] };
      const st = state.shiftStock;
      const catalog = sh.stock_items || [];
      const sizes = catalog.map((i) => i.id);
      const nm = (id) => esc((catalog.find((i) => i.id === id) || {}).name || `товар ${id}`);
      const stockOk = (sh.stock_left && !state.shiftWarehouse) || sizes.every((z) => st.removed.includes(z) || /^\d+$/.test(st.vals[z] || ''));
      const ready = stockOk;
      // 28.10.2026: есть остаток прошлой смены — «В путь!» с ним (без ввода); «🏭 Стартую со склада» — ввод всего + сдать пустые.
      const fromLeft = sh.stock_left && !state.shiftWarehouse;
      const wh = sh.stock_left && state.shiftWarehouse;
      if (wh && st.ack === undefined) st.ack = false;
      const declined = sh.request_declined_at ? '<div class="note warn">❌ Администратор не подтвердил выход на смену. Проверьте товары и нажмите «В путь!» снова</div>' : '';
      const stockHtml = fromLeft ? `${declined}
          <div class="card stock-card"><div class="gate-sub">С собой с прошлой смены</div>
            ${sh.stock_left.filter((i) => i.qty).map((i) => `<div class="row spread small"><span>${esc(i.name)}</span><b>${i.qty}</b></div>`).join('') || '<div class="small muted">Ничего не осталось</div>'}
            ${sh.empties && Object.values(sh.empties).some((n) => n) ? `<div class="small muted mt">♻️ Пустые: ${Object.entries(sh.empties).filter(([, n]) => n).map(([z, n]) => `${z} л — ${n}`).join(', ')}</div>` : ''}
            <div class="small muted mt">После «В путь!» администратор проверит и подтвердит</div></div>` : `${declined}
          <div class="card stock-card">${wh ? '<div class="row spread"><b>🏭 Стартую со склада</b><button class="btn ghost small" data-wh-back>✕</button></div>' : ''}
            <div class="gate-sub">Введите, какие товары и сколько штук у вас с собой</div>
            ${sizes.filter((z) => !st.removed.includes(z)).map((z) => `<div class="stock-row"><span class="stock-lbl">${nm(z)}</span>
              <input type="text" inputmode="numeric" maxlength="3" placeholder="0" data-stock="${z}" value="${esc(st.vals[z] || '')}">
              <button class="stock-del" data-stock-del="${z}" aria-label="Нет такого товара">✕</button></div>`).join('')}
            ${st.removed.length ? `<div class="row mt" style="gap:6px;flex-wrap:wrap">${st.removed.map((z) => `<button class="stock-back" data-stock-back="${z}">+ ${nm(z)}</button>`).join('')}</div>` : ''}
            ${wh ? emptiesWarn(sh.empties, st.ack) : ''}
            <div class="small muted mt">После «В путь!» администратор проверит и подтвердит</div>
          </div>`;
      view.innerHTML = `<div class="shift-gate">
            ${stockHtml}
            <button class="btn go-btn" data-go-shift ${ready && !(wh && !st.ack) ? '' : 'disabled'}>В путь!</button>
            ${fromLeft ? '<button class="btn btn-tape" data-wh>🏭 Стартую со склада</button>' : ''}
            <div data-homescreen></div></div>`;
      const whBtn = view.querySelector('[data-wh]');
      if (whBtn) whBtn.onclick = () => { state.shiftWarehouse = true; st.ack = false; haptic('light'); showGate(sh); };
      const whBack = view.querySelector('[data-wh-back]');
      if (whBack) whBack.onclick = () => { state.shiftWarehouse = false; showGate(sh); };
      const ackBtn = view.querySelector('[data-ack]');
      if (ackBtn) ackBtn.onclick = () => { st.ack = true; haptic('success'); showGate(sh); };
      homeScreenButton(view);
      const go = view.querySelector('[data-go-shift]');
      const refresh = () => {
        const ok = fromLeft || sizes.every((z) => st.removed.includes(z) || /^\d+$/.test(st.vals[z] || ''));
        go.disabled = !ok || (wh && !st.ack);
      };
      view.querySelectorAll('[data-stock]').forEach((inp) => {
        inp.oninput = () => { inp.value = inp.value.replace(/\D/g, '').slice(0, 3); st.vals[inp.dataset.stock] = inp.value; refresh(); };
      });
      view.querySelectorAll('[data-stock-del]').forEach((b) => {
        b.onclick = () => { st.removed.push(+b.dataset.stockDel); delete st.vals[b.dataset.stockDel]; haptic('light'); showGate(sh); };
      });
      view.querySelectorAll('[data-stock-back]').forEach((b) => {
        b.onclick = () => { st.removed = st.removed.filter((z) => z !== +b.dataset.stockBack); showGate(sh); };
      });
      go.onclick = (e) => withBusy(e.target, async () => {
        const body = {};
        if (!fromLeft) {
          body.stock = {};
          sizes.filter((z) => !st.removed.includes(z)).forEach((z) => { body.stock[z] = +st.vals[z]; });
        }
        if (wh) { body.warehouse = true; body.empties_ack = true; }
        await api('POST', '/api/courier/shift', body);
        state.shiftStock = null; state.shiftWarehouse = false; state.lastJson.courier = null; haptic('success');
        toast('📨 Отправлено администратору — ждём подтверждения', false, 3500);
        await load(false);
      });
    };
    // «📦 С собой» — столбиком (28.10.2026): товар слева, количество справа.
    const stockList = (stock) => {
      const items = (stock.items || []).filter((i) => i.start || i.now);
      return items.length ? items.map((i) => `<div class="row spread stock-li"><span>${esc(i.name)}</span><b class="${i.now < 0 ? 'neg' : ''}">${i.now} шт.</b></div>`).join('')
        : '<div class="small muted mt">Ничего нет</div>';
    };
    const regionBar = (sh) => (sh && sh.stock ? `<div class="card tight"><h3>📦 С собой</h3><div class="stock-list mt">${stockList(sh.stock)}</div></div>` : '')
      + (sh && sh.empties ? `<div class="card tight row spread"><span>♻️ Пустые</span><span class="row" style="gap:8px">${emptyLine(sh.empties)}<button class="btn ghost small" data-empties>✏️</button></span></div>` : '');  // деньги — во вкладке «💰 Деньги» (28.10.2026)
    const emptyLine = (e) => Object.entries(e).map(([z, n]) => `${z} л · <b>${n}</b>`).join('&nbsp;&nbsp; ');
    const load = async (silent) => {
      if (state.courierTab === 'available') state.courierTab = 'current';  // 28.10.2026: «Свободных» нет — назначает админ
      const tab = state.courierTab;
      const shiftNow = tab === 'history' || tab === 'transfer' ? await api('GET', '/api/courier/shift') : null;
      if (shiftNow) { state.courierShift = shiftNow; showShiftAsk({ shift_ask: shiftNow.ask, shift_busy: shiftNow.busy }); }
      if (shiftNow && needGate(shiftNow)) {
        const gkey = 'gate' + JSON.stringify(shiftNow);
        if (!(silent && gkey === state.lastJson.courier)) { state.lastJson.courier = gkey; gateOrBreak(shiftNow); }
        return;
      }
      state.liveGate = !!(shiftNow && liveOff(shiftNow) && !shiftNow.busy);
      if (state.liveGate) {
        const gkey = 'live' + JSON.stringify(shiftNow) + state.liveAsked;
        if (!(silent && gkey === state.lastJson.courier)) { state.lastJson.courier = gkey; showLiveGate(shiftNow); }
        return;
      }
      if (tab === 'history') { await courierHistory(); return; }
      if (tab === 'transfer') { await courierTransfer(silent); return; }
      // «ℹ️ Информация» (28.10.2026): с собой, пустые, деньги, выполнено — те же данные, что у «В работе».
      const data = await api('GET', `/api/courier/orders?scope=${tab === 'info' || tab === 'money' ? 'current' : tab}`);
      state.courierShift = data.shift;
      if (data.shift) showShiftAsk({ shift_ask: data.shift.ask, shift_busy: data.shift.busy });
      if (needGate(data.shift)) {
        const gkey = 'gate' + JSON.stringify(data.shift);
        if (!(silent && gkey === state.lastJson.courier)) { state.lastJson.courier = gkey; gateOrBreak(data.shift); }
        return;
      }
      state.liveGate = liveOff(data.shift) && !data.shift.busy;
      if (state.liveGate) {
        const gkey = 'live' + JSON.stringify(data.shift) + state.liveAsked;
        if (!(silent && gkey === state.lastJson.courier)) { state.lastJson.courier = gkey; showLiveGate(data.shift); }
        return;
      }
      if (data.shift && data.shift.live && data.shift.live.active) state.liveAsked = false;
      const key = tab + JSON.stringify(data);
      if (silent && key === state.lastJson.courier) return;
      state.lastJson.courier = key;
      let body;
      if (tab === 'money') {
        body = shiftMoneyCard(data.shift && data.shift.money) + moneyCard(data.shift && data.shift.counters, data.shift && data.shift.reset_request);
      } else if (tab === 'info') {
        body = `${regionBar(data.shift)}
        <button class="card tight row spread linkcard" data-tab="history"><span class="muted">Выполнено заказов</span><b>${data.completed} ›</b></button>`;
      } else if (!data.orders.length) {
        body = '<div class="empty"><div class="big">🧘</div>Сейчас у вас нет заказов в работе. Заказы назначает администратор.</div>';
      } else {
        // Геопозиция идёт трансляцией в чате с ботом (в фоне) — здесь только короткий статус.
        const lv = data.shift && data.shift.live;
        body = (lv && lv.active ? `<div class="card tight live-on">📡 Покупатели видят вас на карте</div>` : '')
          + data.orders.map((o) => courierCard(o,
            // Сначала только «Я на месте»; «Заказ доставлен» — после неё (и после оплаты, если она при курьере).
            `<button class="btn ghost" data-ochat="${o.id}">💬 ${o.prepay_amount && !o.prepay_taken ? 'Написать покупателю' : 'Чат с покупателем'}<span class="nbadge" data-ochat-n="${o.id}" hidden></span></button>` + (o.prepay_amount && !o.prepay_taken
              ? `<button class="btn ok" data-prepay="${o.id}">💵 Взял предоплату</button><div class="small muted center">Остальные кнопки откроются после «Взял предоплату».</div>`
              : !o.courier_arrived_at
              ? `<button class="btn" data-arrived="${o.id}">🚪 Я на месте</button><div class="small muted center">Кнопка «${o.is_return ? 'Баллон забрал' : 'Заказ доставлен'}» появится после «Я на месте».</div>`
              : `<div class="small muted center">✅ Отмечено «на месте» в ${timeText(o.courier_arrived_at)}</div><button class="btn ghost small" data-arrived="${o.id}">🔔 Напомнить покупателю ещё раз</button>${payButtons(o)}<button class="btn ok" data-delivered="${o.id}" data-ret="${o.is_return ? 1 : ''}" ${o.pay_on_delivery && !o.payment_confirmed ? 'disabled' : ''}>${o.is_return ? '♻️ Баллон забрал' : '✅ Заказ доставлен'}</button>${o.pay_on_delivery && !o.payment_confirmed ? '<div class="small muted center">«Заказ доставлен» станет доступен после подтверждения оплаты.</div>' : ''}`),
            o.prepay_amount && !o.prepay_taken ? '' : `<button class="btn ghost" data-note="${o.id}">📝 Комментарий о покупателе</button>${o.can_release ? `<button class="btn secondary" data-release="${o.id}">↩️ Взял заказ по ошибке</button>` : ''}<button class="btn danger" data-nocontact="${o.id}">📵 Покупатель не выходит на связь</button>`)).join('');
      }
      view.innerHTML = `<div class="stack">
        ${liveBar(data.shift)}
        ${courierSeg(tab)}
        ${body}
        <div data-homescreen></div></div>`;
      homeScreenButton(view);  // «📲 Добавить на рабочий стол» — и у курьера
      view.querySelectorAll('[data-tab]').forEach((b) => { b.onclick = () => { state.courierTab = b.dataset.tab; load(false); }; });
      bindStore();
      if (data.shift && data.shift.counters) {
        bindCounterResets(view, data.shift.counters, '/api/courier/counters/reset', () => { state.lastJson.courier = null; load(false); }, true);
      }
      const emp = view.querySelector('[data-empties]');
      if (emp) emp.onclick = () => {
        const e = data.shift.empties;
        openSheet(`<h3>♻️ Пустые баллоны в машине</h3>
          <p class="small muted">Укажите, сколько пустых баллонов у вас сейчас. Возвраты и замены дальше прибавятся сами.</p>
          ${Object.keys(e).map((z) => `<div class="stock-row"><span class="stock-lbl">${z} л</span><input type="text" inputmode="numeric" maxlength="3" data-empty="${z}" value="${e[z]}"></div>`).join('')}
          <div class="stack mt"><button class="btn" data-act="emp-save">Сохранить</button><button class="btn ghost" data-act="emp-cancel">Отмена</button></div>`, (sheet) => {
          sheet.querySelectorAll('[data-empty]').forEach((inp) => { inp.oninput = () => { inp.value = inp.value.replace(/\D/g, '').slice(0, 3); }; });
          sheet.querySelector('[data-act=emp-cancel]').onclick = closeSheet;
          sheet.querySelector('[data-act=emp-save]').onclick = (ev) => withBusy(ev.target, async () => {
            const empty = {};
            sheet.querySelectorAll('[data-empty]').forEach((inp) => { empty[inp.dataset.empty] = +inp.value || 0; });
            await api('POST', '/api/courier/empties', { empty });
            closeSheet(); toast('Сохранено.'); state.lastJson.courier = null; load(false);
          });
        });
      };
      const lv = view.querySelector('[data-live]');
      if (lv) lv.onclick = () => askLive(lv);
      view.querySelectorAll('[data-chat]').forEach((b) => { b.onclick = () => openTgUrl(b.dataset.chat); });
      view.querySelectorAll('[data-more]').forEach((b) => {
        b.onclick = () => {
          if (!state.courierOpen) state.courierOpen = new Set();
          const id = +b.dataset.more;
          if (state.courierOpen.has(id)) state.courierOpen.delete(id); else state.courierOpen.add(id);
          haptic('light'); load(false);
        };
      });
      view.querySelectorAll('[data-getpay]').forEach((b) => {
        b.onclick = () => {
          const order = data.orders.find((o) => o.id === +b.dataset.getpay);
          const q = order.payment_qr;
          if (q && q.active) { openPayScreen(order.id, q, () => load(false)); return; }
          toast(q ? 'QR, который выбрал покупатель, админ убрал — выберите другой.' : 'Выберите QR для оплаты.', true);
          chooseOtherQr(order, () => load(false));
        };
      });
      view.querySelectorAll('[data-changepay]').forEach((b) => {
        b.onclick = () => chooseOtherQr(data.orders.find((o) => o.id === +b.dataset.changepay), () => load(false));
      });
      view.querySelectorAll('[data-prepay]').forEach((b) => {
        b.onclick = async () => {
          if (!(await confirmBox('Вы взяли предоплату с клиента за доставку? Сумма запишется в ваши наличные.', true))) return;
          withBusy(b, async () => { await api('POST', `/api/courier/orders/${b.dataset.prepay}/prepay-taken`); haptic('success'); toast('💵 Предоплата записана.'); state.lastJson.courier = null; load(false); });
        };
      });
      view.querySelectorAll('[data-arrived]').forEach((b) => {
        b.onclick = () => withBusy(b, async () => {
          const res = await api('POST', `/api/courier/orders/${b.dataset.arrived}/arrived`);
          haptic(res.notified ? 'success' : 'warning');
          toast(res.notified ? '🚪 Покупатель увидит «Курьер на месте» в приложении.' : 'Не удалось отметить — позвоните покупателю.', !res.notified);
          load(false);
        });
      });
      view.querySelectorAll('[data-note]').forEach((b) => {
        b.onclick = () => openSheet(`<h3>📝 Комментарий о покупателе</h3>
          <p class="small muted">Заказ №${b.dataset.note}. 🔒 Комментарий увидят только администратор и курьеры — покупатель его не видит.</p>
          <textarea id="note-text" maxlength="500" placeholder="Например: домофон не работает, звонить заранее"></textarea>
          <div class="stack mt"><button class="btn" data-act="note-send">Сохранить</button><button class="btn ghost" data-act="note-cancel">Отмена</button></div>`, (sheet) => {
          sheet.querySelector('[data-act=note-cancel]').onclick = closeSheet;
          sheet.querySelector('[data-act=note-send]').onclick = (e) => withBusy(e.target, async () => {
            const text = sheet.querySelector('#note-text').value.trim();
            if (text.length < 2) throw new Error('Напишите комментарий.');
            await api('POST', `/api/courier/orders/${b.dataset.note}/customer-note`, { text });
            closeSheet(); toast('Комментарий сохранён.'); load(false);
          });
        });
      });
      view.querySelectorAll('[data-delivered]').forEach((b) => {
        const ret = !!b.dataset.ret;
        b.onclick = () => ratingSheet({
          title: ret ? `♻️ Баллон по заказу №${b.dataset.delivered} забрали?` : `✅ Заказ №${b.dataset.delivered} доставлен?`,
          hint: (ret ? 'Отдайте покупателю деньги за баллон. ' : '') + 'Оцените покупателя — без оценки заказ не закрыть. 🔒 Оценку и комментарий видят только администратор и курьеры.',
          extra: '<label class="field mt">Комментарий о покупателе (необязательно)<textarea id="dl-comment" maxlength="500" placeholder="Например: вежливый, встретил у подъезда"></textarea></label>',
          confirm: ret ? '♻️ Баллон забрал' : '✅ Заказ доставлен',
          onSubmit: async (rating, sheet) => {
            await api('POST', `/api/courier/orders/${b.dataset.delivered}/delivered`, { rating, comment: sheet.querySelector('#dl-comment').value });
            closeSheet(); haptic('success'); toast(ret ? '♻️ Баллон в машине — графа «Пустые».' : 'Заказ отмечен как доставленный.'); load(false);
          },
        });
      });
      view.querySelectorAll('[data-release]').forEach((b) => {
        b.onclick = async () => {
          const id = +b.dataset.release;
          if (!(await confirmBox(`Вернуть заказ №${id}? Он снова станет свободным для всех курьеров, а с вас снимется.`))) return;
          withBusy(b, async () => {
            await api('POST', `/api/courier/orders/${id}/release`);
            haptic('success'); toast('↩️ Заказ возвращён — он снова у всех курьеров.'); load(false);
          });
        };
      });
      view.querySelectorAll('[data-nocontact]').forEach((b) => {
        b.onclick = async () => {
          const id = +b.dataset.nocontact;
          if (!(await confirmBox(`Покупатель по заказу №${id} не выходит на связь? Заказ уйдёт администратору в «Спорные заказы» и снимется с вас.`))) return;
          withBusy(b, async () => {
            await api('POST', `/api/courier/orders/${id}/no-contact`);
            haptic('success'); toast('📵 Готово. Администратор скоро разберётся с этой ситуацией.', false, 4000); load(false);
          });
        };
      });
    };
    await load(false);
    every(15000, () => load(true).catch(() => {}));
    // Пока ждём трансляцию — проверяем чаще, чтобы экран открылся сразу после включения.
    every(4000, () => {
      const sh = state.courierShift;
      if (state.liveGate || (sh && ((sh.request && !sh.active) || sh.status === 'ending' || sh.status === 'silent'))) load(true).catch(() => {});
    });
  };

  // ------------------------------------------------------------ HISTORY
  // Компактная карточка для истории: что, куда, кто отвёз (админу — ещё и клиент).
  // История заказов (админ): переписка курьера и покупателя по заказу — только чтение.
  async function orderChatArchive(orderId) {
    const d = await api('GET', `/api/admin/orders/${orderId}/chat`);
    openSheet(`<h3>💬 Заказ №${orderId}: курьер и покупатель</h3>
      <div class="small muted">Только чтение · ${d.messages.length} сообщ.</div>
      <div class="chat-archive mt">${d.messages.map((m) => `<div class="msg ${m.from_courier ? 'mine' : ''}">
          <div class="msg-who">${m.from_courier ? '🚴' : '👤'} ${esc(m.who)}</div>
          ${m.photo ? `<img class="msg-photo" data-arch-photo="${m.id}" alt="Фото">` : ''}
          ${m.text ? `<div class="msg-text">${esc(m.text)}</div>` : ''}
          <div class="msg-at">${dateText(m.at)}${m.read ? ' ✓✓' : ''}</div></div>`).join('') || '<div class="small muted">Сообщений нет.</div>'}</div>
      <button class="btn ghost mt" data-act="arch-close" style="width:100%">Закрыть</button>`, (sheet) => {
      sheet.querySelector('[data-act=arch-close]').onclick = () => closeSheet();
      sheet.querySelectorAll('[data-arch-photo]').forEach(async (img) => {
        try { img.src = (await api('GET', `/api/order-chat/photo/${img.dataset.archPhoto}`)).src; } catch (e) { img.alt = 'Фото недоступно'; }
      });
    });
  }

  function historyCard(o, forAdmin, editing) {
    const what = o.items.map((i) => `${i.gift ? '🎁 ' : ''}${esc(i.name)}${i.gift ? ' (подарок)' : i.variant === 'exchange' ? ' (замена)' : ''} × ${i.quantity}`).join(', ');
    const when = ['delivered', 'cancelled', 'not_delivered', 'disputed', 'disputed_closed'].includes(o.status) ? (o.updated_at || o.created_at) : o.created_at;
    const who = o.courier ? `🚴 ${esc(o.courier.name)}` : '<span class="muted">🚴 курьер не назначен</span>';
    const ref = forAdmin && o.extra && o.extra.referral;
    const canDel = ['delivered', 'cancelled', 'not_delivered', 'disputed_closed'].includes(o.status);
    const pick = forAdmin && editing ? (canDel ? `<label class="hist-pick"><input type="checkbox" data-hist-pick="${o.id}"> </label>` : '<span class="muted small" title="Заказ не закрыт">🔒</span>') : '';
    return `<div class="card hist">
      <div class="row spread"><b>${pick}№${o.id}</b><span class="row"><span class="small muted">${dateText(when)}</span><span class="badge st-${o.status}">${esc(o.status_label)}</span></span></div>
      ${ref ? '<div class="ref-plate mt">🤝 Покупка по реф. программе</div>' : ''}
      <dl class="kv mt">
        <dt>Что</dt><dd>${what || '—'}${o.floor_lift ? ` · 🏢 подъём × ${o.floor_lift.floors}` : ''}${!o.is_return && o.descent ? ` · ⬇️ спуск × ${o.descent.count}` : ''}</dd>
        <dt>Куда</dt><dd>${esc(o.address || '—')}</dd>
        ${forAdmin ? `<dt>Кто отвёз</dt><dd>${who}</dd>` : ''}
        ${forAdmin && o.customer ? `<dt>Клиент</dt><dd>${esc(o.customer.name)}${o.customer.phone ? ' · ' + esc(o.customer.phone) : ''}</dd>` : ''}
        <dt>Сумма</dt><dd>${money(o.total)} · ${esc(o.payment_label)}${o.bonus_spent ? ` · 💰 −${money(o.bonus_spent)} бонусами` : ''}</dd>
      </dl>
      ${forAdmin && o.chat_count ? `<button class="btn ghost small mt hist-more" data-hist-chat="${o.id}">💬 Переписка курьера и покупателя · ${o.chat_count}</button>` : ''}
      ${forAdmin ? `<button class="btn ghost small mt hist-more" data-hist-details="${o.id}">ℹ️ Подробнее</button><div class="hist-details" data-hist-box="${o.id}" hidden>${historyDetails(o)}</div>` : ''}
      ${o.number ? `<div class="order-number">Номер заказа: ${esc(o.number)}</div>` : ''}
    </div>`;
  }
  // Полная информация о заказе в истории (только админ).
  function historyDetails(o) {
    const x = o.extra || {};
    const ref = x.referral;
    const c = o.customer || {};
    const stars = (n) => (n ? `${'★'.repeat(n)}${'☆'.repeat(5 - n)} ${n}/5` : '—');
    const geo = o.customer_location ? `https://yandex.ru/maps/?pt=${o.customer_location.lon},${o.customer_location.lat}&z=17&l=map` : null;
    let pay = esc(o.payment_label);
    if (o.payment_qr) {
      pay += ` · ${esc(o.payment_qr.label)}`;
      if (o.payment_qr.amount) pay += ` · ${esc(o.payment_qr.amount)} ${esc(o.payment_qr.currency)}`;
    }
    if (o.pay_on_delivery) pay += o.payment_confirmed ? ' · ✅ оплата подтверждена' : ' · ⏳ оплата не подтверждена';
    return `
      ${ref ? `<div class="note info mt">🤝 <b>Покупка по реферальной программе</b><br>Партнёр: ${esc(ref.name)}${ref.username ? ` (@${esc(ref.username)})` : ''}${ref.points ? `<br>Партнёру начислено за этот заказ: <b>${ref.points} ⭐</b>` : '<br>Баллы партнёру за этот заказ не начислялись.'}</div>` : ''}
      <div class="section-title">🧾 Состав заказа</div>
      ${itemsList(o.items)}${liftLine(o)}
      <div class="divider"></div><div class="total"><span>К оплате</span><span>${money(o.total)}</span></div>
      <dl class="kv mt">
        <dt>Оплата</dt><dd>${pay}</dd>
        <dt>💰 Бонусы</dt><dd>списано: ${o.bonus_spent || 0}${x.bonus_refunded ? ` (возвращено ${x.bonus_refunded})` : ''} · начислено: ${x.bonus_accrued || 0}</dd>
        <dt>Оформлен</dt><dd>${dateText(o.created_at)}</dd>
        ${o.courier_arrived_at ? `<dt>Курьер на месте</dt><dd>${dateText(o.courier_arrived_at)}</dd>` : ''}
        <dt>Закрыт</dt><dd>${['delivered', 'cancelled', 'not_delivered', 'disputed_closed'].includes(o.status) ? dateText(o.updated_at) : '—'}</dd>
        <dt>Покупатель</dt><dd>${esc(c.name || '—')}${c.username ? ` · <a href="https://t.me/${esc(c.username)}" style="color:var(--accent)">@${esc(c.username)}</a>` : ''}${c.telegram_id ? ` · ID ${c.telegram_id}` : ''}</dd>
        ${c.phone ? `<dt>Телефон</dt><dd><a href="tel:${esc(c.phone)}" style="color:var(--accent)">${esc(c.phone)}</a></dd>` : ''}
        <dt>Адрес</dt><dd style="white-space:pre-line">${esc(o.address || '—')}${geo ? `<br><a href="${geo}" style="color:var(--accent)">🗺 Открыть на карте</a>` : ''}</dd>
        <dt>Курьер</dt><dd>${o.courier ? esc(o.courier.name) + (o.courier.phone ? ` · <a href="tel:${esc(o.courier.phone)}" style="color:var(--accent)">${esc(o.courier.phone)}</a>` : '') : '—'}</dd>
        <dt>Оценка курьеру</dt><dd>${stars(x.courier_rating)}</dd>
        <dt>Оценка покупателю</dt><dd>${stars(x.customer_rating)}</dd>
        ${x.courier_comment ? `<dt>Комментарий курьера</dt><dd>${esc(x.courier_comment)}</dd>` : ''}
      </dl>`;
  }

  // Список с кнопкой «Показать ещё»: fetchPage(offset) → { orders, has_more }.
  async function historyList(container, fetchPage, forAdmin, first) {
    let data = first;
    const listEl = container.querySelector('[data-hist-list]');
    const moreEl = container.querySelector('[data-hist-more]');
    const draw = (orders, append) => {
      const htmlRows = orders.map((o) => historyCard(o, forAdmin, state.historyEdit)).join('');
      if (append) listEl.insertAdjacentHTML('beforeend', htmlRows); else listEl.innerHTML = htmlRows || '<div class="empty"><div class="big">🗂</div>Заказов нет.</div>';
      moreEl.hidden = !data.has_more;
    };
    draw(data.orders, false);
    // «ℹ️ Подробнее» / «▲ Скрыть» — раскрыть полную информацию о заказе.
    listEl.onclick = (e) => {
      const chat = e.target.closest('[data-hist-chat]');
      if (chat) { withBusy(chat, () => orderChatArchive(+chat.dataset.histChat)); return; }
      const btn = e.target.closest('[data-hist-details]');
      if (!btn) return;
      const box = listEl.querySelector(`[data-hist-box="${btn.dataset.histDetails}"]`);
      box.hidden = !box.hidden;
      btn.textContent = box.hidden ? 'ℹ️ Подробнее' : '▲ Скрыть';
    };
    moreEl.onclick = () => withBusy(moreEl, async () => {
      data = await fetchPage(data.offset + data.orders.length);
      draw(data.orders, true);
    });
  }

  // Пароль «Разработчика» для опасных действий (удаление истории): окно ввода, пустой ответ — отмена.
  function askDevPassword() {
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (done) return; done = true; closeSheet(); resolve(v); };
      openSheet(`<h3>🔒 Введите пароль</h3><div class="small muted">Тот же, что защищает «Разработчик».</div>
        <label class="field mt"><input type="password" data-pw autocomplete="current-password" maxlength="64"></label>
        <div class="stack mt"><button class="btn danger" data-act="pw-ok">Удалить</button><button class="btn ghost" data-act="pw-no">Отмена</button></div>`, (sheet) => {
        const inp = sheet.querySelector('[data-pw]');
        sheet.querySelector('[data-act=pw-ok]').onclick = () => finish(inp.value || null);
        sheet.querySelector('[data-act=pw-no]').onclick = () => finish(null);
        inp.onkeydown = (e) => { if (e.key === 'Enter') finish(inp.value || null); };
        document.getElementById('sheet-backdrop').onclick = () => finish(null);
        setTimeout(() => inp.focus(), 50);
      });
    });
  }

  routes['admin-history'] = async () => {
    setTitle('История заказов');
    const f = state.historyFilter;
    const q = (offset) => `/api/admin/history?status=${f.status}&courier=${f.courier}&offset=${offset}&q=${encodeURIComponent(f.q || '')}`;
    const data = await api('GET', q(0));
    const seg = [['delivered', '✅ Доставлены'], ['not_delivered', '❌ Не доставлены'], ['cancelled', '🚫 Отменены'], ['disputed', '⚠️ Спорные'], ['all', 'Все']];
    view.innerHTML = `<div class="stack">
      <form class="search-row" data-hsearch><input type="search" id="hf-q" placeholder="🔍 № заказа, ID_№, имя, @логин или телефон" value="${esc(f.q || '')}" enterkeyhint="search"><button class="btn small" type="submit">Найти</button>${f.q ? '<button class="btn small secondary" type="button" data-hclear>✕</button>' : ''}</form>
      ${f.q && f.status !== 'all' ? `<div class="small muted">Поиск «${esc(f.q)}» только среди: ${esc((seg.find(([k]) => k === f.status) || [0, ''])[1])}. Чтобы искать везде — выберите «Все».</div>` : ''}
      <div class="seg wrap">${seg.map(([k, l]) => `<button data-hf="${k}" class="${f.status === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <label class="field">Курьер<select id="hf-courier"><option value="all">Все курьеры</option>${data.couriers.map((c) => `<option value="${c.id}" ${String(c.id) === String(f.courier) ? 'selected' : ''}>${esc(c.name)}${c.active ? '' : ' (неактивен)'}</option>`).join('')}</select></label>
      <div class="card tight row spread"><span class="muted">Найдено: ${data.total}</span>${data.delivered_sum ? `<b>Доставлено на ${money(data.delivered_sum)}</b>` : ''}</div>
      <button class="btn ghost" data-hist-edit>${state.historyEdit ? '✔️ Готово' : '✏️ Редактировать историю'}</button>
      ${state.historyEdit ? '<div class="row"><button class="btn small secondary" data-hist-all>Выбрать все</button><button class="btn small danger" data-hist-del>🗑 Удалить выбранные</button></div>' : ''}
      <div class="stack" data-hist-list></div>
      <button class="btn ghost" data-hist-more hidden>Показать ещё</button>
    </div>`;
    view.querySelectorAll('[data-hf]').forEach((b) => { b.onclick = () => { f.status = b.dataset.hf; render(); }; });
    view.querySelector('#hf-courier').onchange = (e) => { f.courier = e.target.value; render(); };
    view.querySelector('[data-hsearch]').onsubmit = (e) => {
      e.preventDefault();
      f.q = view.querySelector('#hf-q').value.trim();
      if (f.q && f.status !== 'all') f.status = 'all';  // ищем по всем заказам
      render();
    };
    view.querySelector('[data-hist-edit]').onclick = () => { state.historyEdit = !state.historyEdit; render(); };
    const hAll = view.querySelector('[data-hist-all]');
    if (hAll) hAll.onclick = () => view.querySelectorAll('[data-hist-pick]').forEach((c) => { c.checked = true; });
    const hDel = view.querySelector('[data-hist-del]');
    if (hDel) hDel.onclick = () => withBusy(hDel, async () => {
      const ids = [...view.querySelectorAll('[data-hist-pick]:checked')].map((c) => +c.dataset.histPick);
      if (!ids.length) { toast('Отметьте заказы галочками.'); return; }
      if (!(await confirmBox(`Удалить заказы ${ids.map((i) => '№' + i).join(', ')} безвозвратно?\nОни исчезнут у покупателей, курьеров и из Excel. Заработок курьера, наличные, бонусы и баллы по этим заказам тоже отменятся — как будто заказов не было.`))) return;
      const password = await askDevPassword();
      if (!password) return;
      const res = await api('POST', '/api/admin/history/delete', { order_ids: ids, confirm: true, password });
      toast(`Удалено: ${res.deleted.length}${res.skipped.length ? `. Не удалено: ${res.skipped.map((s) => `№${s.id} — ${s.reason}`).join('; ')}` : ''}`);
      render();
    });
    const hclear = view.querySelector('[data-hclear]');
    if (hclear) hclear.onclick = () => { f.q = ''; render(); };
    await historyList(view, (off) => api('GET', q(off)), true, data);
  };

  // ------------------------------------------------------------ 🤝 РЕФЕРАЛЬНАЯ ПРОГРАММА
  // Пока — только заявки: покупатель/курьер оставляет заявку, админ видит
  // список и связывается. Условия программы владелец продумает позже.
  // «🤝 Партнёрская программа»: покупатель/курьер оставляет заявку, админ видит
  // список и решает. Программы лояльности для клиентов нет (убрана 20.10) —
  // позже будет бонусная программа за покупки.
  const PARTNER_TEXT = 'Если хочешь стать нашим партнёром, оставь заявку и получи личную реферальную ссылку, по которой будешь получать процент с каждого заказа твоего клиента.';
  // ------------------------------------------------------------ БОНУСЫ ПОКУПАТЕЛЯ
  function bonusHistoryText(h) {
    const order = h.order_id ? ` №${h.order_id}` : '';
    const note = h.reason ? ` · ${esc(h.reason)}` : '';
    if (h.kind === 'accrual') {
      return `за заказ${order}${h.order_total ? ` · ${String(h.percent).replace('.', ',')} % за ${money(h.order_total)}` : ''}${note}`;
    }
    if (h.kind === 'spend') return `оплата заказа${order}${note}`;
    if (h.kind === 'refund') return `возврат: заказ${order} отменён${note}`;
    if (h.kind === 'transfer_out') return `отправлено другу${h.who ? ' ' + esc(h.who) : ''}${h.fee ? ` · комиссия ${h.fee}` : ''}`;
    if (h.kind === 'transfer_in') return `подарок от ${h.who ? esc(h.who) : 'друга'}`;
    return `${h.amount > 0 ? 'начислено' : 'списано'} администратором${note}`;
  }
  function pctText(own, def) {
    return own != null ? `${String(own).replace('.', ',')} % (свой)` : `${String(def).replace('.', ',')} % (по умолчанию)`;
  }
  function bonusHistoryList(history) {
    return (history || []).map((h) => `<li><span>${h.amount > 0 ? '+' : '−'}${Math.abs(h.amount)} 💰 ${bonusHistoryText(h)}</span><span class="small muted">${dateText(h.created_at)}</span></li>`).join('');
  }
  routes.bonus = async () => {
    setTitle('Мои бонусы');
    const d = await api('GET', '/api/bonus');
    const hist = bonusHistoryList(d.history);
    view.innerHTML = `<div class="stack">
      <div class="card center"><div class="small muted">💰 Мои бонусы</div>
        <div style="font-size:34px;font-weight:800;margin:4px 0">${Number(d.balance).toLocaleString('ru-RU')}</div>
        <button class="btn mt" style="width:100%" data-act="gift" ${d.balance >= d.transfer_min ? '' : 'disabled'}>🎁 Отправить бонусы другу</button>
        <div class="small muted mt">Комиссия ${d.transfer_fee_percent} %, отправить можно от ${d.transfer_min} бонусов.</div>
        </div>
      <div class="card"><h3>Как это работает?</h3>
        <ul class="review-notes mt">
          <li><span>1 бонус = 1 ₽.</span></li>
          <li><span>За каждую совершённую покупку от <span style="white-space:nowrap">${money(d.min_items_total)}</span> вам начисляется кэшбэк в виде ${String(d.percent).replace('.', ',')} % от стоимости товаров.</span></li>
          <li><span>Оплачивайте бонусами часть следующего заказа.</span></li>
          <li><span>Отменили заказ — бонусы вернутся.</span></li>
          <li><span>Можно отправить бонусы другу по его логину в Telegram.</span></li>
        </ul></div>
      <div class="card"><h3>📜 История</h3>${hist ? `<ul class="review-notes mt">${hist}</ul>` : '<div class="small muted mt">Пока начислений нет.</div>'}</div>
    </div>`;
    view.querySelector('[data-act=gift]').onclick = () => giftSheet(d);
  };
  // «🎁 Отправить бонусы другу»: логин → сумма → подтверждение (с отправителя N, другу N − 10 %).
  function giftSheet(d) {
    const fee = (n) => Math.ceil(n * d.transfer_fee_percent / 100);
    openSheet(`<h3>🎁 Отправить бонусы другу</h3>
      <p class="small muted">У вас ${Number(d.balance).toLocaleString('ru-RU')} 💰. Комиссия ${d.transfer_fee_percent} % — друг получит сумму за её вычетом.</p>
      <label class="field mt">Введите @ и логин Telegram друга<input type="text" id="g-user" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="@friend" value="@"></label>
      <label class="field mt">Сколько бонусов отправить<input type="text" id="g-amt" inputmode="numeric" maxlength="9" placeholder="от ${d.transfer_min}"></label>
      <div class="small mt" id="g-calc"></div>
      <div class="stack mt"><button class="btn" data-act="g-next">Далее</button><button class="btn ghost" data-act="g-cancel">Отмена</button></div>`, (sheet) => {
      const u = sheet.querySelector('#g-user'), a = sheet.querySelector('#g-amt'), calc = sheet.querySelector('#g-calc');
      u.oninput = () => { u.value = '@' + u.value.replace(/[^A-Za-z0-9_]/g, '').slice(0, 32); };
      a.oninput = () => {
        a.value = a.value.replace(/\D/g, '');
        const n = +a.value;
        calc.innerHTML = n ? (n < d.transfer_min ? `Минимум ${d.transfer_min}.` : n > d.balance ? `У вас только ${d.balance}.`
          : `С вас <b>${n}</b>, комиссия ${fee(n)}, друг получит <b>${n - fee(n)}</b> 💰`) : '';
      };
      sheet.querySelector('[data-act=g-cancel]').onclick = closeSheet;
      sheet.querySelector('[data-act=g-next]').onclick = (e) => withBusy(e.target, async () => {
        const body = { username: u.value, amount: a.value };
        const p = await api('POST', '/api/bonus/transfer', { ...body, preview: true });
        if (p.sent > p.balance) throw new Error(`Не хватает бонусов: у вас ${p.balance}.`);
        if (!(await confirmBox(`Отправить ${p.friend} (@${p.username})?\n\nС вас: ${p.sent} 💰\nКомиссия ${d.transfer_fee_percent} %: ${p.fee}\nДруг получит: ${p.received} 💰`))) return;
        const r = await api('POST', '/api/bonus/transfer', body);
        closeSheet(); haptic('success');
        toast(`🎁 Отправлено! ${r.friend} получил ${r.received} 💰. У вас осталось ${r.balance}.`, false, 5000);
        render();
      });
    });
  }

  routes.referral = async () => {
    setTitle('Партнёрская программа');
    const asCustomer = getMode() === 'customer';
    const draw = (d) => {
      if (d.admin) {
        const list = d.requests;
        view.innerHTML = `<div class="stack">
          <div class="section-title">📨 Заявки на партнёрскую программу · ${list.length}</div>
          ${list.length ? list.map((r) => {
            const link = r.username ? `https://t.me/${esc(r.username)}` : '';
            return `<div class="card">
              <div class="row spread"><h3>${esc(r.name)}</h3>${r.is_courier ? '<span class="badge paid">🚴 Курьер</span>' : '<span class="badge">🛍 Покупатель</span>'}</div>
              <dl class="kv mt">
                ${r.username ? `<dt>Username</dt><dd>@${esc(r.username)}</dd>` : ''}
                ${r.is_courier ? `<dt>Курьер</dt><dd>${esc(r.courier_name || r.name)}${r.courier_active ? '' : ' (неактивен)'}</dd>` : ''}
                ${r.phone ? `<dt>Телефон</dt><dd><a href="tel:${esc(r.phone)}" style="color:var(--accent)">${esc(r.phone)}</a></dd>` : ''}
                <dt>Покупок</dt><dd>${r.purchases}</dd>
                <dt>Заявка</dt><dd>${dateText(r.created_at)}</dd>
              </dl>
              ${r.comment ? `<div class="order-comment mt"><span class="small muted">💬 Комментарий к заявке</span><div>${esc(r.comment)}</div></div>` : '<div class="small muted mt">Комментария нет (заявка подана раньше, чем он стал обязательным)</div>'}
              ${link ? `<button class="btn mt" style="width:100%" data-open="${link}">💬 Связаться</button>`
                : (r.phone ? `<a class="btn mt" style="width:100%;text-decoration:none" href="tel:${esc(r.phone)}">📞 Позвонить</a>`
                  : '<div class="small muted mt">У человека нет username и телефона — свяжитесь через бота по Telegram ID ' + r.user_id + '.</div>')}
              <div class="stack mt"><button class="btn ok" data-approve="${r.id}" data-name="${esc(r.name)}">✅ Одобрить и выдать ссылку</button><button class="btn ghost" data-decline="${r.id}" data-mode="dismiss" data-name="${esc(r.name)}">✖️ Просто отклонить</button><button class="btn secondary" data-decline="${r.id}" data-mode="temp" data-name="${esc(r.name)}">⏳ Отклонить на 7 дней</button><button class="btn danger" data-decline="${r.id}" data-mode="forever" data-name="${esc(r.name)}">⛔ Отклонить навсегда</button></div>
            </div>`;
          }).join('') : '<div class="empty"><div class="big">📭</div>Заявок пока нет.</div>'}
          <div class="section-title">🤝 Партнёры · ${(d.partners || []).length}</div>
          ${(d.partners || []).length ? '<button class="btn secondary" style="width:100%" data-act="link-refresh">📣 Выслать всем партнёрам новую ссылку</button><div class="small muted">У каждого партнёра появится красное окно «ссылка обновлена», которое не закрыть, пока он не нажмёт «Я разослал(а) ссылку».</div>' : ''}
          ${(d.partners || []).length ? d.partners.map((p) => `<div class="card">
              <div class="row spread"><h3>${esc(p.name)}</h3><span class="badge paid">⭐ ${Number(p.balance).toLocaleString('ru-RU')}</span></div>
              <dl class="kv mt">
                ${p.username ? `<dt>Username</dt><dd>@${esc(p.username)}${p.is_courier ? ' · 🚴 курьер' : ''}</dd>` : ''}
                ${p.phone ? `<dt>Телефон</dt><dd><a href="tel:${esc(p.phone)}" style="color:var(--accent)">${esc(p.phone)}</a></dd>` : ''}
                <dt>Ссылка</dt><dd style="word-break:break-all">${esc(p.link)}</dd>
                <dt>Программа</dt><dd>${p.personal && p.personal.length ? `личная: ${p.personal.map((r) => `${esc(r.name)} — ${r.points} ⭐`).join(', ')}; остальное — общая` : 'общая'}</dd>
                <dt>Пришло клиентов</dt><dd>${p.invited}</dd>
                <dt>Засчитано заказов</dt><dd>${p.rewarded_orders}</dd>
                <dt>Начислено / списано</dt><dd>${p.earned} / ${p.written_off}</dd>
                <dt>Партнёр с</dt><dd>${dateText(p.approved_at)}</dd>
              </dl>
              <div class="stack mt">
                ${p.username ? `<button class="btn ghost" data-open="https://t.me/${esc(p.username)}">💬 Связаться</button>` : ''}
                <button class="btn ghost" data-personal="${p.user_id}" data-name="${esc(p.name)}">⭐ Личная программа</button>
                <button class="btn secondary" data-writeoff="${p.user_id}" data-bal="${p.balance}" data-name="${esc(p.name)}" ${p.balance > 0 ? '' : 'disabled'}>➖ Списать баллы</button>
              </div></div>`).join('') : '<div class="small muted">Пока нет одобренных партнёров.</div>'}
          <div class="card"><h3>⭐ Общая программа: баллы за 1 баллон</h3>${rewardTable(d.rewards)}
            <div class="small muted mt">Одинаково за новый и за замену. Действует для всех партнёров, кроме тех, кому задана «⭐ Личная программа». Изменить общую — Админка → Товары → баллон → «Изменить».</div></div>
        </div>`;
        const pointsSheet = (title, hint, value, withReason, onOk) => openSheet(`<h3>${title}</h3><p class="small muted">${hint}</p>
            <label class="field mt">Баллы<input type="number" id="pts" inputmode="numeric" min="1" step="1" value="${esc(String(value || ''))}"></label>
            ${withReason ? '<label class="field mt">Причина (необязательно)<input type="text" id="pts-reason" maxlength="200" placeholder="Например: выдано скидкой"></label>' : ''}
            <div class="stack mt"><button class="btn" data-act="pts-ok">Сохранить</button><button class="btn ghost" data-act="pts-cancel">Отмена</button></div>`, (sheet) => {
          sheet.querySelector('[data-act=pts-cancel]').onclick = closeSheet;
          sheet.querySelector('[data-act=pts-ok]').onclick = (e) => withBusy(e.target, async () => {
            const n = Math.floor(Number(sheet.querySelector('#pts').value));
            if (!n || n < 1) throw new Error('Введите число баллов больше нуля.');
            const reason = withReason ? sheet.querySelector('#pts-reason').value : '';
            await onOk(n, reason);
          });
        });
        const lr = view.querySelector('[data-act=link-refresh]');
        if (lr) lr.onclick = async () => {
          if (!(await confirmBox('Выслать всем партнёрам новую ссылку? У каждого появится окно, которое не закрыть, пока он не подтвердит рассылку.'))) return;
          try { const r = await api('POST', '/api/admin/partners/link-refresh'); toast(`📣 Окно показано ${r.partners} партнёрам.`); } catch (e) { toast(e.message || 'Не получилось', true); }
        };
        view.querySelectorAll('[data-approve]').forEach((b) => {
          b.onclick = async () => {
            if (!(await confirmBox(`Одобрить ${b.dataset.name}? Человек получит свою партнёрскую ссылку. Баллы — по таблице «за 1 баллон».`))) return;
            withBusy(b, async () => { draw(await api('POST', `/api/admin/referral/${b.dataset.approve}/approve`)); haptic('success'); toast('✅ Партнёр одобрен — ссылка выдана.'); });
          };
        });
        view.querySelectorAll('[data-personal]').forEach((b) => {
          b.onclick = () => {
            const partner = d.partners.find((x) => x.user_id === +b.dataset.personal);
            const mine = Object.fromEntries((partner.personal || []).map((r) => [r.product_id, r.points]));
            if (!(d.rewards || []).length) { toast('В каталоге нет баллонов.', true); return; }
            openSheet(`<h3>⭐ Личная программа: ${esc(b.dataset.name)}</h3>
              <p class="small muted">Сколько баллов этот партнёр получает за 1 баллон в заказе своего клиента (новый и замена — одинаково). Пусто — общая ставка. Действует на следующие доставленные заказы.</p>
              <div class="stack mt">${d.rewards.map((r) => `<label class="field">${esc(r.name)}<input type="text" inputmode="numeric" maxlength="7" data-pr="${r.product_id}" value="${mine[r.product_id] != null ? mine[r.product_id] : ''}" placeholder="общая: ${r.general}"></label>`).join('')}</div>
              <div class="stack mt"><button class="btn" data-act="pr-save">💾 Сохранить</button><button class="btn ghost" data-act="pr-reset">↩️ Всё по общей программе</button><button class="btn ghost" data-act="pr-cancel">Отмена</button></div>`, (sheet) => {
              sheet.querySelectorAll('[data-pr]').forEach((i) => { i.oninput = () => { i.value = i.value.replace(/\D/g, ''); }; });
              sheet.querySelector('[data-act=pr-cancel]').onclick = closeSheet;
              const save = (btn, reset) => withBusy(btn, async () => {
                const rewards = {};
                sheet.querySelectorAll('[data-pr]').forEach((i) => { rewards[i.dataset.pr] = reset ? '' : i.value.trim(); });
                draw(await api('PUT', `/api/admin/partners/${b.dataset.personal}/rewards`, { rewards }));
                closeSheet(); toast(reset ? 'Партнёр снова на общей программе.' : '⭐ Личная программа сохранена.');
              });
              sheet.querySelector('[data-act=pr-save]').onclick = (e) => save(e.target, false);
              sheet.querySelector('[data-act=pr-reset]').onclick = (e) => save(e.target, true);
            });
          };
        });
        view.querySelectorAll('[data-writeoff]').forEach((b) => {
          b.onclick = () => pointsSheet(`➖ Списать баллы: ${b.dataset.name}`, `Сейчас у партнёра ${b.dataset.bal} баллов. Списать можно не больше.`,
            b.dataset.bal, true, async (n, reason) => { draw(await api('POST', `/api/admin/partners/${b.dataset.writeoff}/writeoff`, { points: n, reason })); closeSheet(); toast(`➖ Списано ${n} баллов.`); });
        });
        view.querySelectorAll('[data-open]').forEach((b) => { b.onclick = () => openTgUrl(b.dataset.open); });
        view.querySelectorAll('[data-decline]').forEach((b) => {
          b.onclick = async () => {
            const mode = b.dataset.mode;
            const text = {
              dismiss: `Убрать заявку ${b.dataset.name} из списка? Человек сможет подать её снова в любой момент.`,
              temp: `Отклонить заявку ${b.dataset.name} на 7 дней? Через 7 дней человек сможет подать её снова.`,
              forever: `Отклонить заявку ${b.dataset.name} навсегда? Подать её снова будет нельзя.`,
            }[mode];
            if (!(await confirmBox(text))) return;
            withBusy(b, async () => {
              draw(await api('POST', `/api/admin/referral/${b.dataset.decline}/decline`, { mode }));
              toast({ dismiss: '✖️ Заявка убрана из списка.', temp: '⏳ Заявка отклонена на 7 дней.', forever: '⛔ Заявка отклонена навсегда.' }[mode]);
            });
          };
        });
        return;
      }
      if (d.partner) {
        const p = d.partner;
        const hist = (p.history || []).map((h) => `<li><span>${h.points > 0 ? '+' : ''}${h.points} ⭐ ${h.kind === 'accrual' ? `за заказ${h.order_id ? ' №' + h.order_id : ''}${h.who ? ' · ' + esc(h.who) : ''}${h.order_total ? ' · ' + money(h.order_total) : ''}${h.reason ? ' · ' + esc(h.reason) : ''}` : h.kind === 'spend' ? `оплата товаров${h.order_id ? ' · заказ №' + h.order_id : ''}` : h.kind === 'refund' ? `возврат: заказ${h.order_id ? ' №' + h.order_id : ''} отменён` : `списано${h.reason ? ': ' + esc(h.reason) : ''}`}</span><span class="small muted">${dateText(h.created_at)}</span></li>`).join('');
        view.innerHTML = `<div class="stack">
          <div class="card center"><div class="small muted">⭐ Моя реферальная программа</div><div style="font-size:34px;font-weight:800;margin:4px 0">${Number(p.points).toLocaleString('ru-RU')}</div>
            <div class="small muted">За каждый заказ вашего клиента вы получаете:</div>
            ${rewardTable(p.rewards)}</div>
          <div class="card"><h3>🤝 Моя партнёрская ссылка</h3>
            <div class="note mt" style="word-break:break-all">${esc(p.link)}</div>
            <div class="btn-pair mt"><button class="btn" data-act="share">📤 Поделиться</button><button class="btn ghost" data-act="copy">📋 Скопировать</button></div>
            <button class="btn ghost mt" style="width:100%" data-act="my-clients">👥 Мои покупатели</button>
            <div class="small muted mt">Отправьте ссылку знакомым. Кто впервые придёт по ней в магазин, закрепится за вами навсегда.</div>
            <dl class="kv mt"><dt>Пришло по ссылке</dt><dd>${p.invited}</dd></dl></div>
          <div class="card"><h3>📜 История баллов</h3>${hist ? `<ul class="review-notes mt">${hist}</ul>` : '<div class="small muted mt">Пока начислений нет.</div>'}</div>
        </div>`;
        view.querySelector('[data-act=share]').onclick = () => openTgUrl(`https://t.me/share/url?url=${encodeURIComponent(p.link)}&text=${encodeURIComponent('Relax Gas — доставка газа. Заказывайте по моей ссылке:')}`);
        view.querySelector('[data-act=my-clients]').onclick = openPartnerClients;
        view.querySelector('[data-act=copy]').onclick = async () => {
          try { await navigator.clipboard.writeText(p.link); toast('📋 Ссылка скопирована.'); } catch (e) { toast('Не удалось скопировать — выделите ссылку и скопируйте вручную.', true); }
        };
        return;
      }
      const r = (d.requests || {}).partner;
      let status;
      if (!r || r.status === 'can_reapply') status = '<button class="btn mt" style="width:100%" data-apply="partner">📨 Оставить заявку</button>';
      else if (r.status === 'pending') status = `<div class="note ok mt">✅ Заявка отправлена ${dateText(r.created_at)}. Администратор обязательно с вами свяжется.</div>`;
      else if (r.status === 'declined_temp') {
        const dd = new Date(r.decline_until);
        const day = isNaN(dd) ? '' : dd.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
        status = `<div class="note mt">Заявка пока отклонена. Подать снова можно будет с ${esc(day)}.</div>`;
      } else status = '<div class="note mt">Заявка на партнёрскую программу отклонена.</div>';
      view.innerHTML = `<div class="stack"><div class="card">
          <div class="row" style="gap:10px;align-items:flex-start"><span style="font-size:30px;line-height:1">🤝</span><div><h3>Партнёрская программа</h3><div class="small muted mt">${esc(PARTNER_TEXT)}</div></div></div>
          ${status}
        </div></div>`;
      const btn = view.querySelector('[data-apply]');
      // «Отправить» → окно с обязательным комментарием: без него заявка не уходит.
      if (btn) btn.onclick = () => openSheet(`<h3>💬 Комментарий к заявке</h3>
          <div class="small muted mt">Без комментария заявку не отправить</div>
          <label class="field mt">Комментарий<textarea id="ref-comment" rows="4" maxlength="300" placeholder="Напишите комментарий"></textarea></label>
          <div class="stack mt"><button class="btn" data-act="ref-send" disabled>📨 Отправить заявку</button><button class="btn ghost" data-act="ref-cancel">Отмена</button></div>`, (sheet) => {
        const t = sheet.querySelector('#ref-comment');
        const send = sheet.querySelector('[data-act=ref-send]');
        t.oninput = () => { send.disabled = t.value.trim().length < 2; };
        setTimeout(() => t.focus(), 200);
        sheet.querySelector('[data-act=ref-cancel]').onclick = closeSheet;
        send.onclick = () => withBusy(send, async () => {
          const comment = t.value.trim();
          if (comment.length < 2) throw new Error('Напишите комментарий к заявке.');
          const res = await api('POST', '/api/referral/apply', { kind: 'partner', comment });
          closeSheet(); draw(res);
          haptic('success'); toast('📨 Заявка отправлена. Администратор обязательно с вами свяжется.', false, 3500);
        });
      });
    };
    draw(await api('GET', asCustomer ? '/api/referral?view=customer' : '/api/referral'));
  };

  // ------------------------------------------------------------ ⚠️ СПОРНЫЕ ЗАКАЗЫ (админ)
  function tgOrPhone(username, phone) {
    if (username) return { url: `https://t.me/${String(username).replace(/^@/, '')}` };
    if (phone) return { tel: phone };
    return null;
  }
  routes.disputes = async () => {
    setTitle('Спорные заказы');
    if (!state.me.roles.admin) { go('home'); return; }
    const draw = (data) => {
      view.innerHTML = `<div class="stack">
        <div class="note">📵 Здесь заказы, по которым курьер нажал «Покупатель не выходит на связь». Покупатель видит: «администрация скоро с вами свяжется».</div>
        ${data.orders.length ? data.orders.map((o) => {
          const what = o.items.map((i) => `${i.gift ? '🎁 ' : ''}${esc(i.name)}${i.gift ? ' (подарок)' : i.variant === 'exchange' ? ' (замена)' : ''} × ${i.quantity}`).join(', ');
          const cour = o.courier ? tgOrPhone(o.courier.username, o.courier.phone) : null;
          const btn = (c, label) => !c ? '' : c.url
            ? `<button class="btn ghost" data-open="${esc(c.url)}">${label}</button>`
            : `<a class="btn ghost" style="text-decoration:none" href="tel:${esc(c.tel)}">${label} (позвонить)</a>`;
          return `<div class="card">
            <div class="row spread"><h3>Заказ №${o.id}</h3><span class="badge st-disputed">📵 спорный</span></div>
            <div class="small muted">Курьер отметил «не выходит на связь»: ${dateText(o.disputed_at || o.updated_at)}</div>
            <dl class="kv mt">
              <dt>Что вёз</dt><dd>${what || '—'}${o.floor_lift ? ` · 🏢 подъём × ${o.floor_lift.floors}` : ''}${!o.is_return && o.descent ? ` · ⬇️ спуск × ${o.descent.count}` : ''}</dd>
              <dt>Сумма</dt><dd>${money(o.total)} · ${payLine(o)}</dd>
              <dt>Кому</dt><dd>${esc(o.customer.name || '—')}</dd>
              <dt>Телефон</dt><dd><a href="tel:${esc(o.customer.phone)}" style="color:var(--accent)">${esc(o.customer.phone || '—')}</a></dd>
              <dt>Адрес</dt><dd>${esc(o.address || '—')}</dd>
              <dt>Кто вёз</dt><dd>${o.courier ? `${esc(o.courier.name)}${o.courier.phone ? ' · ' + esc(o.courier.phone) : ''}` : '—'}</dd>
              <dt>Оформлен</dt><dd>${dateText(o.created_at)}</dd>
            </dl>
            <div class="stack mt">
              ${o.customer.phone ? `<a class="btn ghost" style="text-decoration:none" href="tel:${esc(String(o.customer.phone).replace(/[^\d+]/g, ''))}">📞 Позвонить покупателю</a>` : ''}
              ${o.customer.telegram_id ? `<button class="btn ghost" data-dchat="${o.customer.telegram_id}">💬 Чат с покупателем</button>` : ''}
              ${btn(cour, '💬 Написать курьеру')}
              <button class="btn" data-dreturn="${o.id}">🔁 Вернуть в работу</button>
              <button class="btn danger" data-dclose="${o.id}">🗑 Удалить заказ</button>
            </div></div>`;
        }).join('') : '<div class="empty"><div class="big">✅</div>Спорных заказов нет.</div>'}
      </div>`;
      view.querySelectorAll('[data-open]').forEach((b) => { b.onclick = () => openTgUrl(b.dataset.open); });
      // Чат «RelaxGas» внутри приложения: покупателю придёт уведомление в бот, ответ — в «Чатах с покупателями».
      view.querySelectorAll('[data-dchat]').forEach((b) => { b.onclick = () => { haptic('light'); go('admin-chat', { u: b.dataset.dchat }); }; });
      view.querySelectorAll('[data-dreturn]').forEach((b) => {
        b.onclick = async () => {
          const id = b.dataset.dreturn;
          if (!(await confirmBox(`Вернуть заказ №${id} в работу? Он снова станет свободным для всех курьеров.`))) return;
          withBusy(b, async () => { draw(await api('POST', `/api/admin/disputes/${id}/return`)); toast('🔁 Заказ возвращён в работу — он снова у всех курьеров.'); pollBadges(); });
        };
      });
      view.querySelectorAll('[data-dclose]').forEach((b) => {
        b.onclick = async () => {
          const id = b.dataset.dclose;
          if (!(await confirmBox(`Удалить заказ №${id} из спорных? У покупателя он исчезнет из активных и останется в истории со статусом «Спорный — закрыт». В выгрузку Excel он не попадёт.`))) return;
          withBusy(b, async () => { draw(await api('POST', `/api/admin/disputes/${id}/close`)); toast('🗑 Заказ закрыт.'); pollBadges(); });
        };
      });
    };
    draw(await api('GET', '/api/admin/disputes'));
  };

  // ------------------------------------------------------------ ADMIN
  function adminActions(o) {
    const edit = ['new', 'accepted', 'in_transit'].includes(o.status) ? `<button class="btn ghost" data-edit="${o.id}">✏️ Изменить</button>` : '';
    return edit + adminActionsBase(o);
  }
  function adminActionsBase(o) {
    const b = (label, status, cls) => `<button class="btn ${cls || 'ghost'}" data-status="${o.id}:${status}">${label}</button>`;
    if (['delivered', 'cancelled', 'disputed', 'disputed_closed'].includes(o.status)) return '';
    if (o.status === 'not_delivered') return b('🔁 Передать заново (снять курьера)', 'new') + b('✅ Всё-таки доставлен', 'delivered', 'ok') + b('❌ Отменить', 'cancelled', 'danger');
    if (!o.courier) {
      return (o.paid ? `<button class="btn" data-assign="${o.id}">🚴 Назначить курьера</button>` : '<div class="note">⏳ Ожидается скриншот оплаты — курьерам заказ пока не показан.</div>')
        + b('❌ Отменить', 'cancelled', 'danger');
    }
    return (o.status !== 'in_transit' ? b('🚚 В пути', 'in_transit') : '') + b('✅ Доставлен', 'delivered', 'ok') + b('↩️ Снять курьера', 'new') + b('❌ Отменить', 'cancelled', 'danger');
  }

  // ---------- 🚫 Чёрный список: покупатель не может оформлять заказы
  function blockButton(uid, name, blocked) {
    if (!uid) return '';
    return blocked
      ? `<button class="btn ghost small mt" style="width:100%" data-block="${uid}" data-blocked="1" data-name="${esc(name || '')}">✅ Убрать из ЧС</button>`
      : `<button class="btn ghost small mt" style="width:100%;color:var(--danger,#e53935)" data-block="${uid}" data-blocked="0" data-name="${esc(name || '')}">🚫 В чёрный список</button>`;
  }
  function bindBlockButtons(container, reload) {
    container.querySelectorAll('[data-block]').forEach((b) => {
      b.onclick = () => {
        const uid = +b.dataset.block, name = b.dataset.name, blocked = b.dataset.blocked === '1';
        if (blocked) {
          confirmBox(`Убрать ${name || 'покупателя'} из чёрного списка? Он снова сможет оформлять заказы.`).then((ok) => {
            if (ok) withBusy(b, async () => { await api('PUT', `/api/admin/customers/${uid}/block`, { blocked: false }); toast('✅ Покупатель убран из чёрного списка.'); reload(); });
          });
          return;
        }
        openSheet(`<h3>🚫 В чёрный список</h3>
          <p class="small muted">${esc(name || 'Покупатель')} не сможет оформлять заказы ни в приложении, ни в боте. <b>Все его активные заказы будут отменены</b> — курьеры и покупатель получат уведомление об отмене. Убрать из списка можно в любой момент.</p>
          <label class="field mt">Причина (видит только администратор)<textarea id="block-reason" maxlength="300" placeholder="Например: не открыл дверь, грубил курьеру"></textarea></label>
          <div class="stack mt"><button class="btn danger" data-act="block-ok">🚫 Заблокировать</button><button class="btn ghost" data-act="block-cancel">Отмена</button></div>`, (sheet) => {
          sheet.querySelector('[data-act=block-cancel]').onclick = closeSheet;
          sheet.querySelector('[data-act=block-ok]').onclick = (e) => withBusy(e.target, async () => {
            const res = await api('PUT', `/api/admin/customers/${uid}/block`, { blocked: true, reason: sheet.querySelector('#block-reason').value });
            const n = (res.cancelled || []).length;
            closeSheet(); toast(`🚫 Покупатель в чёрном списке.${n ? ` Отменено заказов: ${n} (№${res.cancelled.join(', №')}).` : ''}`); reload();
          });
        });
      };
    });
  }

  // Переключатель регионов в виде креста (просьба владельца 23.10): Центр посередине,
  // Север сверху, Юг снизу, Запад слева, Восток справа; «Все заказы» — над крестом.
  function regionCross(regions, reg, cnt) {
    const label = Object.fromEntries(regions.map((r) => [r.id, r.label]));
    const btn = (id, area) => `<button data-reg="${id}" style="grid-area:${area}" class="${reg === id ? 'on' : ''}">${esc(label[id] || 'Все заказы')}<b>${cnt(id)}</b></button>`;
    // 30.10.2026: слева сверху — красная кнопка «Индивидуальные»: заказы за пределами Москвы.
    const outBtn = `<button data-reg="out" style="grid-area:i" class="out ${reg === 'out' ? 'on' : ''}">🧾 Не Москва<b>${cnt('out')}</b></button>`;
    return `<div class="region-cross">${btn('all', 'all')}${outBtn}${btn('north', 'n')}${btn('west', 'w')}${btn('center', 'c')}${btn('east', 'e')}${btn('south', 's')}</div>`;
  }
  // ✏️ «Изменить» заказ (31.10.2026, только админ): товары, подъёмы; в возврате — состав и спуски.
  async function openOrderEdit(orderId, after) {
    let d;
    try { d = await api('GET', `/api/admin/orders/${orderId}/edit-options`); } catch (e) { toast(e.message, true); return; }
    const qty = {};  // ключ "<id>:<variant>" → штук
    d.items.forEach((i) => { qty[`${i.product_id}:${d.is_return ? 'new' : i.variant}`] = i.quantity; });
    let lift = d.lift_floors, descent = d.descent_count, addPid = '', addVar = 'new';
    // 01.11.2026: своя сумма выкупа за баллон (пусто — цена из карточки баллона): ключ — "<id>:new" (возврат) или "r<id>" (возврат в заказе)
    const cp = {};
    const defRet = (id) => { const p = d.products.find((x) => x.id === id); return p ? p.price : null; };
    if (d.is_return) d.items.forEach((i) => { const df = defRet(i.product_id); if (df != null && i.unit_price !== df) cp[`${i.product_id}:new`] = String(i.unit_price); });
    const rq = {};  // возврат в обычном заказе: id → штук
    (d.returns || []).forEach((r) => { rq[r.product_id] = r.quantity; });
    let rmode = d.return_mode === 'points' ? 'points' : 'cash';
    const hadReturns = (d.returns || []).length > 0;
    const defMix = (id) => { const x = d.return_choices.find((y) => y.id === id); return x ? (rmode === 'points' ? x.points : x.cash) : null; };
    if (hadReturns) (d.returns || []).forEach((r) => { const df = defMix(r.product_id); if (df != null && r.unit_price !== df) cp[`r${r.product_id}`] = String(r.unit_price); });
    const pname = (id) => (d.products.find((p) => p.id === id) || d.items.find((i) => i.product_id === id) || { name: `товар ${id}` }).name;
    const vlabel = (v) => (d.is_return ? '' : v === 'exchange' ? ' · Замена' : ' · Новый');
    const unit = d.is_return ? (d.return_mode === 'points' ? ' баллов' : ' ₽') : ' ₽';
    const draw = (sheet) => {
      const keys = Object.keys(qty);
      const balloons = keys.reduce((s, k) => s + (/балл/i.test(pname(+k.split(':')[0])) ? qty[k] : 0), 0);
      const sel = d.products.find((p) => String(p.id) === String(addPid));
      sheet.querySelector('.inner').innerHTML = `<div class="grabber"></div><h3>✏️ Изменить заказ №${orderId}</h3><div class="stack mt">
        ${keys.map((k) => { const [pid, v] = k.split(':'); return `<div class="row spread"><span><b>${esc(pname(+pid))}</b><span class="small muted">${vlabel(v)}</span></span>
          ${d.is_return ? `<input type="text" class="price-in" data-cp="${k}" inputmode="numeric" maxlength="7" placeholder="${defRet(+pid) ?? ''}" value="${esc(cp[k] ?? '')}" title="Сумма выкупа за 1 баллон (пусто — по карточке)">` : ''}
          <span class="stepper"><button type="button" data-q="${k}:-1" ${qty[k] ? '' : 'disabled'}>−</button><span>${qty[k]}</span><button type="button" data-q="${k}:1" ${qty[k] >= 99 ? 'disabled' : ''}>+</button></span></div>`; }).join('')}
        <div class="card flat-form"><div class="small muted">➕ Добавить товар</div>
          <select id="ed-add"><option value="">— выберите —</option>${d.products.map((p) => `<option value="${p.id}" ${String(p.id) === String(addPid) ? 'selected' : ''}>${esc(p.name)} — ${p.price}${unit}</option>`).join('')}</select>
          ${!d.is_return && sel && sel.price_exchange != null ? `<div class="lift-pair mt"><button type="button" class="lift-opt ${addVar !== 'exchange' ? 'on' : ''}" data-av="new">Новый ${sel.price} ₽</button><button type="button" class="lift-opt ${addVar === 'exchange' ? 'on' : ''}" data-av="exchange">Замена ${sel.price_exchange} ₽</button></div>` : ''}
          <button class="btn ghost small mt" data-act="ed-add" ${addPid ? '' : 'disabled'}>Добавить в заказ</button></div>
        ${d.is_return
          ? `<div class="row spread"><span><b>⬇️ Спуск пустого баллона</b><div class="small muted">${d.descent_price}${unit} за баллон</div></span><span class="stepper"><button type="button" data-ds="-1" ${descent ? '' : 'disabled'}>−</button><span>${descent}</span><button type="button" data-ds="1" ${descent < balloons || !balloons ? '' : 'disabled'}>+</button></span></div>`
          : `<div class="row spread"><span><b>🏢 Подъём на этаж</b><div class="small muted">${d.lift_price ? `${d.lift_price} ₽ за подъём` : 'услуга выключена (цена 0)'}</div></span><span class="stepper"><button type="button" data-ls="-1" ${lift ? '' : 'disabled'}>−</button><span>${lift}</span><button type="button" data-ls="1" ${d.lift_price && lift < 99 ? '' : 'disabled'}>+</button></span></div>`}
        ${d.is_return ? '' : `<div class="card flat-form"><div class="small muted">♻️ Возврат пустых баллонов в этом заказе</div>
          ${!hadReturns && d.return_choices.some((x) => x.points > 0) ? `<div class="row mt" style="gap:8px"><button type="button" class="chip ${rmode === 'points' ? '' : 'on'}" data-rm="cash">💵 Зачесть деньгами</button><button type="button" class="chip ${rmode === 'points' ? 'on' : ''}" data-rm="points">⭐ Баллами</button></div>` : (hadReturns ? `<div class="small muted mt">Способ: ${rmode === 'points' ? '⭐ баллами' : '💵 зачёт деньгами'} (задан при оформлении)</div>` : '')}
          ${d.return_choices.filter((x) => rmode !== 'points' || x.points > 0).map((x) => `<div class="return-row mt"><span><b>${esc(x.name)}</b><div class="small muted">${rmode === 'points' ? `+${x.points} баллов` : `−${x.cash} ₽`}</div></span>
            <input type="text" class="price-in" data-cp="r${x.id}" inputmode="numeric" maxlength="7" placeholder="${rmode === 'points' ? x.points : x.cash}" value="${esc(cp['r' + x.id] ?? '')}" title="Сумма выкупа за 1 баллон (пусто — по карточке)">
            <span class="stepper"><button type="button" data-rq="${x.id}:-1" ${rq[x.id] ? '' : 'disabled'}>−</button><span>${rq[x.id] || 0}</span><button type="button" data-rq="${x.id}:1" ${(rq[x.id] || 0) >= 20 ? 'disabled' : ''}>+</button></span></div>`).join('') || '<div class="small muted mt">Нет баллонов, принимаемых на возврат.</div>'}
        </div>`}
        <div class="small muted">Цена позиций, которые уже были в заказе, не меняется; новые — по текущему каталогу. Бонусы и баллы клиента не пересчитываются. Курьеру и покупателю уйдёт сообщение об изменении.</div>
        <button class="btn" data-act="ed-save">Сохранить</button></div>`;
      sheet.querySelectorAll('[data-q]').forEach((b) => { b.onclick = () => { const [pid, v, dl] = b.dataset.q.split(':'); const k = `${pid}:${v}`; qty[k] = Math.max(0, Math.min(99, qty[k] + +dl)); draw(sheet); }; });
      sheet.querySelector('#ed-add').onchange = (e) => { addPid = e.target.value; addVar = 'new'; draw(sheet); };
      sheet.querySelectorAll('[data-av]').forEach((b) => { b.onclick = () => { addVar = b.dataset.av; draw(sheet); }; });
      sheet.querySelector('[data-act=ed-add]').onclick = () => { if (!addPid) return; const k = `${addPid}:${d.is_return ? 'new' : addVar}`; qty[k] = Math.min(99, (qty[k] || 0) + 1); addPid = ''; addVar = 'new'; draw(sheet); };
      sheet.querySelectorAll('[data-cp]').forEach((i) => { i.oninput = () => { i.value = i.value.replace(/\D/g, '').slice(0, 7); cp[i.dataset.cp] = i.value; }; });
      sheet.querySelectorAll('[data-rm]').forEach((b) => { b.onclick = () => { rmode = b.dataset.rm; Object.keys(cp).filter((k) => k.startsWith('r')).forEach((k) => delete cp[k]); draw(sheet); }; });
      sheet.querySelectorAll('[data-rq]').forEach((b) => { b.onclick = () => { const [id, dl] = b.dataset.rq.split(':'); rq[id] = Math.max(0, Math.min(20, (rq[id] || 0) + +dl)); draw(sheet); }; });
      sheet.querySelectorAll('[data-ls]').forEach((b) => { b.onclick = () => { lift = Math.max(0, Math.min(99, lift + +b.dataset.ls)); draw(sheet); }; });
      sheet.querySelectorAll('[data-ds]').forEach((b) => { b.onclick = () => { descent = Math.max(0, descent + +b.dataset.ds); draw(sheet); }; });
      sheet.querySelector('[data-act=ed-save]').onclick = (e) => withBusy(e.target, async () => {
        const items = Object.keys(qty).map((k) => { const [pid, v] = k.split(':'); const row = { product_id: +pid, variant: v, quantity: qty[k] }; if (d.is_return) row.unit_price = (cp[k] || '') === '' ? null : +cp[k]; return row; });
        const body = { items };
        if (d.is_return) body.descent_count = descent;
        else {
          body.lift_floors = lift;
          body.returns = Object.entries(rq).filter(([, q]) => q > 0).map(([id, q]) => ({ product_id: +id, quantity: q, unit_price: (cp[`r${id}`] || '') === '' ? null : +cp[`r${id}`] }));
          body.return_mode = rmode;
        }
        const res = await api('POST', `/api/admin/orders/${orderId}/edit`, body);
        closeSheet(); haptic('success'); toast('✏️ Состав заказа изменён.');
        (res.warnings || []).forEach((w) => toast(`⚠️ ${w}`, true, 8000));
        after();
      });
    };
    openSheet('', (sheet) => draw(sheet));
  }
  async function adminOrders(container) {
    const scope = state.adminOrdersScope;
    const data = await api('GET', `/api/admin/orders?scope=${scope}`);
    // Фильтр по регионам: «Все заказы» + 5 регионов. Отмеченные заказы — одному курьеру.
    const reg = state.adminRegion || 'all';
    if (!state.adminSel) state.adminSel = new Set();
    const pickable = (o) => scope === 'open' && o.status === 'new' && !o.courier && o.paid;
    const live = new Set(data.orders.filter(pickable).map((o) => o.id));
    [...state.adminSel].forEach((id) => { if (!live.has(id)) state.adminSel.delete(id); });
    const inReg = (o, r) => r === 'all' || (r === 'out' ? !!o.individual_any : o.region === r);
    const list = data.orders.filter((o) => inReg(o, reg));
    const cnt = (r) => data.orders.filter((o) => inReg(o, r)).length;
    const sel = state.adminSel;
    container.innerHTML = `<div class="seg"><button data-scope="open" class="${scope === 'open' ? 'on' : ''}">Активные</button><button data-scope="all" class="${scope === 'all' ? 'on' : ''}">История</button></div>
      ${regionCross(data.regions, reg, cnt)}
      ${scope === 'open' ? '<div class="small muted">Курьеры сами заказы не берут — назначайте их здесь. Назначить можно только курьеру на смене, у которого хватает товаров.</div>' : ''}
      ${list.length ? list.map((o) => adminOrderCard(o, scope === 'open' ? adminActions(o) : '', pickable(o) ? sel.has(o.id) : null)).join('') : '<div class="empty"><div class="big">🗂</div>Заказов нет.</div>'}
      ${sel.size ? `<div class="bulk-bar"><div class="inner"><button class="btn" data-bulk>🚴 Назначить курьера · ${sel.size} ${plural(sel.size, 'заказ', 'заказа', 'заказов')}</button><button class="btn ghost small" data-bulk-clear>Снять выбор</button></div></div>` : ''}`;
    const reload = () => adminOrders(container);
    container.querySelectorAll('[data-scope]').forEach((b) => { b.onclick = () => { state.adminOrdersScope = b.dataset.scope; reload(); }; });
    container.querySelectorAll('[data-reg]').forEach((b) => { b.onclick = () => { state.adminRegion = b.dataset.reg; haptic('light'); reload(); }; });
    container.querySelectorAll('[data-pick]').forEach((c) => {
      c.onchange = () => { const id = +c.dataset.pick; if (c.checked) sel.add(id); else sel.delete(id); reload(); };
    });
    const bulkClear = container.querySelector('[data-bulk-clear]');
    if (bulkClear) bulkClear.onclick = () => { sel.clear(); reload(); };
    // 28.10.2026: хватает ли курьеру товаров на выбранные заказы (сервер всё равно проверит).
    const stockFit = (c, ids) => {
      if (c.status === 'silent') return { ok: false, text: '⛔ не отвечает на опрос' };
      if (!['online', 'break'].includes(c.status)) return { ok: false, text: '⛔ не на смене' };
      if (!c.stock) return { ok: false, text: '⛔ нет учёта товаров' };
      const need = {}, names = {};
      ids.map((id) => data.orders.find((o) => o.id === id)).filter((o) => o && !o.is_return && !(o.courier && o.courier.id === c.id))
        .forEach((o) => o.items.forEach((i) => { if (i.product_id != null) { need[i.product_id] = (need[i.product_id] || 0) + i.quantity; names[i.product_id] = i.name; } }));
      const short = Object.entries(need).filter(([pid, n]) => n > Math.max(0, +(c.stock.now[pid] || 0)))
        .map(([pid, n]) => `${esc(names[pid])} — нужно ${n}, есть ${Math.max(0, +(c.stock.now[pid] || 0))}`);
      return short.length ? { ok: false, text: `⛔ не хватает: ${short.join(', ')}` } : { ok: true, text: '✅ товаров хватает' };
    };
    const courierSheet = (ids, done) => {
      if (!data.couriers.length) { toast('Сначала добавьте активного курьера во вкладке «Курьеры».', true); return; }
      const stockTxt = (st) => (st ? (st.items || []).filter((i) => i.start || i.now).map((i) => `${esc(i.name)} · ${i.now}`).join(', ') || 'ничего нет' : '');
      openSheet(`<h3>${ids.length > 1 ? `Курьер для ${ids.length} заказов` : `Курьер для заказа №${ids[0]}`}</h3>
        <div class="stack mt">${data.couriers.map((c) => { const fit = stockFit(c, ids); return `<button class="btn ghost courier-pick" data-c="${c.id}">
          <span>🚴 ${esc(c.name)}</span>
          <span class="small muted">${c.on_shift ? `🟢 на смене${c.stock ? ' · 📦 ' + stockTxt(c.stock) : ''}` : c.status === 'break' ? '🔴 перерыв' : c.status === 'silent' ? '🟥 не отвечает на опрос' : '🔴 не на смене'}</span>
          <span class="small ${fit.ok ? 'ok-text' : 'warn-text'}">${fit.text}</span></button>`; }).join('')}</div>`, (sheet) => {
        sheet.querySelectorAll('[data-c]').forEach((cb) => {
          cb.onclick = () => withBusy(cb, async () => {
            if (ids.length === 1) await api('POST', `/api/admin/orders/${ids[0]}/assign`, { courier_id: +cb.dataset.c });
            else {
              const r = await api('POST', '/api/admin/orders/assign', { order_ids: ids, courier_id: +cb.dataset.c });
              const shortIds = (r.short || []).map((x) => x.id);
              const taken = r.failed.filter((i) => !shortIds.includes(i));
              const msgs = [];
              if ((r.short || []).length) msgs.push(r.short.map((x) => `№${x.id}: ${esc(x.reason)}`).join('<br>'));
              if (taken.length) msgs.push(`Не назначены (уже взяты): №${taken.join(', №')}`);
              if (msgs.length) toast(msgs.join('<br>'), true, 8000);
            }
            closeSheet(); toast(ids.length > 1 ? `Назначено заказов: ${ids.length}.` : 'Курьер назначен.'); done();
          });
        });
      });
    };
    const bulk = container.querySelector('[data-bulk]');
    if (bulk) bulk.onclick = () => courierSheet([...sel], () => { sel.clear(); reload(); });
    container.querySelectorAll('[data-chat]').forEach((b) => { b.onclick = () => openTgUrl(b.dataset.chat); });
    container.querySelectorAll('[data-edit]').forEach((b) => { b.onclick = () => openOrderEdit(+b.dataset.edit, reload); });
    container.querySelectorAll('[data-amore]').forEach((b) => {
      b.onclick = () => {
        if (!state.adminOpen) state.adminOpen = new Set();
        const id = +b.dataset.amore;
        if (state.adminOpen.has(id)) state.adminOpen.delete(id); else state.adminOpen.add(id);
        haptic('light'); reload();
      };
    });
    container.querySelectorAll('[data-fee-input]').forEach((i) => { i.oninput = () => { i.value = i.value.replace(/\D/g, '').slice(0, 7); }; });
    const saveFee = (b, id, release) => withBusy(b, async () => {
      const inp = container.querySelector(`[data-fee-input="${id}"]`);
      const v = (inp.value || '').trim();
      if (!/^\d+$/.test(v)) { inp.closest('label').classList.add('need'); toast('Впишите стоимость доставки цифрами (0 — бесплатно).', true); return; }
      const res = await api('POST', `/api/admin/orders/${id}/delivery-fee`, { fee: +v, release });
      if (release) toast(res.sent ? `Стоимость сохранена, заказ передан курьерам (${res.sent}).` : 'Стоимость сохранена. Курьерам отправить не удалось — нет активных курьеров с Telegram ID.', !res.sent);
      else toast('Стоимость доставки сохранена.');
      reload();
    });
    container.querySelectorAll('[data-fee-save]').forEach((b) => { b.onclick = () => saveFee(b, +b.dataset.feeSave, false); });
    container.querySelectorAll('[data-toll-save]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => {
        const id = +b.dataset.tollSave;
        const inp = container.querySelector(`[data-toll-input="${id}"]`);
        const v = (inp.value || '').trim() || '0';
        if (!/^\d+$/.test(v)) { inp.closest('label').classList.add('need'); toast('Впишите сумму цифрами (0 — убрать).', true); return; }
        const res = await api('POST', `/api/admin/orders/${id}/delivery-toll`, { toll: +v });
        toast(+v ? `🛣 Платная дорога ${money(+v)} добавлена. Доставка: ${money(res.order.delivery_fee)}.` : 'Платная дорога убрана.');
        reload();
      });
    });
    container.querySelectorAll('[data-fee-release]').forEach((b) => { b.onclick = () => saveFee(b, +b.dataset.feeRelease, true); });
    container.querySelectorAll('[data-status]').forEach((b) => {
      b.onclick = async () => {
        const [id, status] = b.dataset.status.split(':');
        if (status === 'cancelled' && !(await confirmBox(`Отменить заказ №${id}?`))) return;
        withBusy(b, async () => {
          try { await api('POST', `/api/admin/orders/${id}/status`, { status }); toast('Статус изменён.'); reload(); } catch (e) {
            if (!(e.data && e.data.code === 'prepay_cancel')) throw e;
            // По заказу внесена предоплата: красное предупреждение, данные клиента и чат.
            const cu = e.data.customer || {};
            let box = document.getElementById('prepay-cancel');
            if (!box) { box = document.createElement('div'); box.id = 'prepay-cancel'; box.className = 'gate link-gate'; document.body.appendChild(box); }
            box.innerHTML = `<div class="card stack center">
                <div style="font-size:44px">⚠️</div>
                <h3>Вы отменяете заказ, по которому внесена предоплата</h3>
                <div class="small">Предоплата ${money(e.data.prepay)} будет списана у курьера. Клиенту деньги вы возвращаете сами.</div>
                <div class="note" style="text-align:left"><b>${esc(cu.name || 'Покупатель')}</b>${cu.username ? `<br>@${esc(cu.username)}` : ''}${cu.phone ? `<br>${esc(cu.phone)}` : ''}</div>
                <button class="btn danger" data-act="pc-yes">Отменить и написать клиенту</button>
                <button class="btn ghost" data-act="pc-no">Не отменять</button>
              </div>`;
            box.hidden = false;
            box.querySelector('[data-act=pc-no]').onclick = () => { box.hidden = true; box.innerHTML = ''; };
            box.querySelector('[data-act=pc-yes]').onclick = async (ev) => {
              try {
                await api('POST', `/api/admin/orders/${id}/status`, { status, confirm_prepay: true });
                box.hidden = true; box.innerHTML = '';
                toast('Заказ отменён, предоплата списана у курьера.');
                if (cu.user_id) go('admin-chat', { u: cu.user_id }); else reload();
              } catch (err) { toast(err.message, true); }
            };
          }
        });
      };
    });
    container.querySelectorAll('[data-reoffer]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => { await api('POST', `/api/admin/orders/${b.dataset.reoffer}/reoffer`); toast('Заказ передан курьерам.'); });
    });
    container.querySelectorAll('[data-assign]').forEach((b) => {
      b.onclick = () => courierSheet([+b.dataset.assign], reload);
    });
  }

  function productForm(p, catalog) {
    const isNew = !p;
    const giftChoices = (catalog || []).filter((x) => x.is_active && (!p || x.id !== p.id));
    p = p || { name: '', description: '', price: '', price_exchange: '', bonus_text: '' };
    openSheet(`<h3>${isNew ? '➕ Новый товар' : '✏️ ' + esc(p.name)}</h3>
      <div class="stack mt">
        <label class="field">Название<input type="text" id="f-name" value="${esc(p.name)}"></label>
        <label class="field">Описание<textarea id="f-desc">${esc(p.description)}</textarea></label>
        <div class="row"><label class="field grow">Цена «Новый», ₽<input type="number" id="f-price" inputmode="numeric" min="0" value="${esc(p.price)}"></label>
        <label class="field grow">Цена «Замена», ₽<input type="number" id="f-ex" inputmode="numeric" min="0" value="${p.price_exchange == null ? '' : esc(p.price_exchange)}" placeholder="нет"></label></div>
        <div class="small muted">Оставьте «Замену» пустой, если у товара нет такого варианта (шарики, промо и т. п.).</div>
        <div class="card bonus-box">
          <h3>🎁 Бонус к товару</h3>
          <div id="b-off" ${p.bonus_product_id ? 'hidden' : ''}><div class="small muted">Бонуса нет.</div>${giftChoices.length ? '<button type="button" class="btn small ghost mt" data-act="b-add">➕ Добавить бонус</button>' : '<div class="small muted mt">Сначала добавьте в каталог товар, который будет подарком.</div>'}</div>
          <div id="b-on" class="stack mt" ${p.bonus_product_id ? '' : 'hidden'}>
            <label class="field">Товар-подарок из каталога<select id="f-bgift"><option value="">— выберите товар —</option>${giftChoices.map((x) => `<option value="${x.id}" ${x.id === p.bonus_product_id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select></label>
            <label class="field">Сколько подарков на 1 шт. товара<input type="text" id="f-bqty" inputmode="numeric" maxlength="3" value="${p.bonus_qty || 1}"></label>
            <div class="small muted">Подарок кладётся в заказ отдельной строкой за 0 ₽ за каждую штуку товара и списывается с остатка курьера. Если у курьера подарка не хватает — заказ ему не назначить.</div>
            <label class="field">Описание бонуса<textarea id="f-bdesc" maxlength="1000" placeholder="Подробности для покупателя (необязательно)">${esc(p.bonus_desc || '')}</textarea></label>
            <div id="b-scope">
              <div class="small muted">К чему применяется бонус:</div>
              <label class="switch-row"><span><b>🆕 Новый</b></span><span class="switch"><input type="checkbox" id="f-bnew" ${!p.bonus_product_id || p.bonus_new ? 'checked' : ''}><i></i></span></label>
              <label class="switch-row"><span><b>🔄 Замена</b></span><span class="switch"><input type="checkbox" id="f-bex" ${!p.bonus_product_id || p.bonus_exchange ? 'checked' : ''}><i></i></span></label>
            </div>
            <button type="button" class="btn small danger" data-act="b-rm">🗑 Убрать бонус</button>
          </div>
        </div>
        </div>
        <div id="f-pr-box">
          <label class="field">⭐ Баллов партнёру за 1 баллон<input type="text" id="f-pr" inputmode="numeric" maxlength="7" value="${p.partner_reward_custom ? esc(String(p.partner_reward)) : ''}"></label>
          <div class="small muted" id="f-pr-note"></div>
          <label class="field mt">♻️ Цена возврата, ₽<input type="text" id="f-ret" inputmode="numeric" maxlength="7" value="${p.return_price ? esc(String(p.return_price)) : ''}" placeholder="0 — не принимается"></label>
          <div class="small muted">Сколько курьер отдаёт покупателю за пустой такой баллон («♻️ Возврат баллона» на главной). Пусто или 0 — этот баллон на возврат не принимается.</div>
          <label class="field mt">⭐ Баллы за возврат<input type="text" id="f-retp" inputmode="numeric" maxlength="7" value="${p.return_points ? esc(String(p.return_points)) : ''}" placeholder="0 — баллами не принимается"></label>
          <div class="small muted">Сколько баллов (бонусов) получит клиент, если вместо наличных выберет возврат баллами. Спуск пустого баллона тогда вычитается в баллах (1 балл = 1 ₽).</div>
        </div>
        <div class="section-title">Фото в приложении</div>
        ${p.photo ? `<img src="${esc(p.photo)}" alt="" style="max-height:140px;border-radius:12px;object-fit:cover">` : ''}
        ${!isNew && p.has_bot_photo && !p.has_app_photo ? '<div class="small muted">Сейчас показывается фото, загруженное через бота.</div>' : ''}
        <label class="btn ghost" for="f-photo">🖼 Загрузить фото</label><input type="file" id="f-photo" accept="image/*" hidden>
        <div id="f-photo-note" class="small muted"></div>
        ${!isNew && p.has_app_photo ? '<button class="btn secondary" data-act="rm-photo">Убрать фото приложения</button>' : ''}
        <button class="btn" data-act="save">${isNew ? 'Добавить товар' : 'Сохранить'}</button>
      </div>`, (sheet) => {
      let photo;
      const prBox = sheet.querySelector('#f-pr-box'), prInput = sheet.querySelector('#f-pr'), nameInput = sheet.querySelector('#f-name');
      const syncPr = () => {
        const name = nameInput.value, balloon = isBalloonName(name), def = defaultPartnerReward(name);
        prBox.hidden = !balloon;
        prInput.placeholder = `по умолчанию: ${def}`;
        sheet.querySelector('#f-pr-note').textContent = `Это баллон — партнёр получает баллы за каждую штуку в заказе своего клиента, одинаково за новый и за замену. Пусто — по умолчанию (2 л — 100, 5 л — 150, 10 л — 200; другие объёмы — 0).`;
      };
      prInput.oninput = () => { prInput.value = prInput.value.replace(/\D/g, ''); };
      // 🎁 Бонус: добавить / убрать; галочки «Новый»/«Замена» — только если у товара есть «Замена».
      const bOn = sheet.querySelector('#b-on'), bOff = sheet.querySelector('#b-off'), exInput = sheet.querySelector('#f-ex');
      const syncScope = () => { sheet.querySelector('#b-scope').hidden = exInput.value.trim() === ''; };
      exInput.addEventListener('input', syncScope); syncScope();
      const bAdd = sheet.querySelector('[data-act=b-add]');
      if (bAdd) bAdd.onclick = () => { bOff.hidden = true; bOn.hidden = false; sheet.querySelector('#f-bgift').focus(); };
      const bQty = sheet.querySelector('#f-bqty');
      bQty.oninput = () => { bQty.value = bQty.value.replace(/\D/g, '').slice(0, 3); };
      sheet.querySelector('[data-act=b-rm]').onclick = () => {
        sheet.querySelector('#f-bgift').value = ''; bQty.value = '1'; sheet.querySelector('#f-bdesc').value = '';
        sheet.querySelector('#f-bnew').checked = true; sheet.querySelector('#f-bex').checked = true;
        bOn.hidden = true; bOff.hidden = false; toast('Бонус будет убран после «Сохранить».');
      };
      const retInput = sheet.querySelector('#f-ret');
      retInput.oninput = () => { retInput.value = retInput.value.replace(/\D/g, ''); };
      const retpInput = sheet.querySelector('#f-retp');
      retpInput.oninput = () => { retpInput.value = retpInput.value.replace(/\D/g, ''); };
      nameInput.addEventListener('input', syncPr);
      syncPr();
      sheet.querySelector('#f-photo').onchange = async (e) => {
        try { photo = await compressImage(e.target.files[0], 900, 0.82); sheet.querySelector('#f-photo-note').textContent = '✅ Фото выбрано, нажмите «Сохранить».'; } catch (err) { toast(err.message, true); }
      };
      const rm = sheet.querySelector('[data-act=rm-photo]');
      if (rm) rm.onclick = () => { photo = ''; sheet.querySelector('#f-photo-note').textContent = 'Фото приложения будет убрано после сохранения.'; };
      sheet.querySelector('[data-act=save]').onclick = (e) => withBusy(e.target, async () => {
        if (!bOn.hidden && !sheet.querySelector('#f-bgift').value) throw new Error('Выберите товар-подарок или нажмите «Убрать бонус».');
        if (!bOn.hidden && !(+bQty.value >= 1)) throw new Error('Количество подарков — целое число от 1.');
        if (!bOn.hidden && exInput.value.trim() !== '' && !sheet.querySelector('#f-bnew').checked && !sheet.querySelector('#f-bex').checked)
          throw new Error('Отметьте, к чему применяется бонус: к «Новому», к «Замене» или к обоим.');
        const body = {
          name: sheet.querySelector('#f-name').value,
          description: sheet.querySelector('#f-desc').value,
          price: sheet.querySelector('#f-price').value,
          price_exchange: sheet.querySelector('#f-ex').value,
          bonus_product_id: bOn.hidden ? null : +sheet.querySelector('#f-bgift').value,
          bonus_qty: +bQty.value || 1,
          bonus_desc: bOn.hidden ? '' : sheet.querySelector('#f-bdesc').value.trim(),
          bonus_new: sheet.querySelector('#f-bnew').checked,
          bonus_exchange: sheet.querySelector('#f-bex').checked,
          partner_reward: isBalloonName(sheet.querySelector('#f-name').value) ? sheet.querySelector('#f-pr').value.trim() : '',
          return_price: isBalloonName(sheet.querySelector('#f-name').value) ? (sheet.querySelector('#f-ret').value.trim() || 0) : 0,
          return_points: isBalloonName(sheet.querySelector('#f-name').value) ? (sheet.querySelector('#f-retp').value.trim() || 0) : 0,
        };
        if (photo !== undefined) body.photo = photo || null;
        if (isNew) await api('POST', '/api/admin/products', body);
        else {
          const res = await api('PATCH', `/api/admin/products/${p.id}`, body);
          if (res.removed_cart_rows) toast(`Вариант «Замена» убран — из корзин удалено позиций: ${res.removed_cart_rows}.`);
        }
        closeSheet(); toast(isNew ? 'Товар добавлен.' : 'Сохранено.'); render();
      });
    });
  }

  // Перетаскивание карточек за ручку ⠿ (мышь и палец). onDone(ids) — только если порядок изменился.
  function makeSortable(list, onDone) {
    const order = () => [...list.children].map((c) => c.dataset.pid);
    // ▲ / ▼ — на шаг выше/ниже (если перетаскивание на устройстве не срабатывает)
    list.querySelectorAll('[data-mv]').forEach((b) => {
      b.onclick = () => {
        const item = b.closest('[data-pid]');
        const sib = +b.dataset.mv < 0 ? item.previousElementSibling : item.nextElementSibling;
        if (!sib) return;
        if (+b.dataset.mv < 0) list.insertBefore(item, sib); else list.insertBefore(sib, item);
        haptic('select');
        item.classList.add('moved'); setTimeout(() => item.classList.remove('moved'), 500);
        item.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        onDone(order());
      };
    });
    list.querySelectorAll('[data-drag]').forEach((h) => {
      h.onpointerdown = (e) => {
        if (e.button) return;
        e.preventDefault();
        const item = h.closest('[data-pid]');
        const before0 = order().join();
        item.classList.add('dragging'); list.classList.add('sorting'); haptic('light');
        let scroll = 0, lastY = e.clientY, timer = null;
        const place = (y) => {
          const others = [...list.children].filter((c) => c !== item);
          const next = others.find((c) => { const r = c.getBoundingClientRect(); return y < r.top + r.height / 2; }) || null;
          if (item.nextElementSibling !== next) { list.insertBefore(item, next); haptic('select'); }
        };
        const tick = () => { if (scroll) { window.scrollBy(0, scroll); place(lastY); } };
        // Слушаем весь документ: карточка переставляется в DOM, и «захват» указателя на ручке теряется.
        const onMove = (ev) => {
          if (ev.pointerId !== e.pointerId) return;
          ev.preventDefault();
          lastY = ev.clientY;
          scroll = lastY < 90 ? -14 : lastY > window.innerHeight - 110 ? 14 : 0;
          if (scroll && !timer) timer = setInterval(tick, 30);
          if (!scroll && timer) { clearInterval(timer); timer = null; }
          place(lastY);
        };
        const end = (ev) => {
          if (ev.pointerId !== e.pointerId) return;
          document.removeEventListener('pointermove', onMove); document.removeEventListener('pointerup', end); document.removeEventListener('pointercancel', end);
          if (timer) clearInterval(timer);
          item.classList.remove('dragging'); list.classList.remove('sorting');
          if (order().join() !== before0) onDone(order());
        };
        document.addEventListener('pointermove', onMove, { passive: false });
        document.addEventListener('pointerup', end); document.addEventListener('pointercancel', end);
      };
    });
  }

  async function adminProducts(container) {
    const data = await api('GET', '/api/admin/products');
    const card = (p) => `<div class="card compact" ${p.is_active ? `data-pid="${p.id}"` : ''}>
      <div class="row">${p.is_active && data.products.length > 1 ? '<div class="drag-col"><button class="mv-btn" data-mv="-1" aria-label="Выше">▲</button><button class="drag-h" data-drag aria-label="Перетащить">⠿</button><button class="mv-btn" data-mv="1" aria-label="Ниже">▼</button></div>' : ''}<div class="mini-ph">${photoBox(p)}</div>
        <div class="grow"><h3>${esc(p.name)}</h3><div class="small">Новый ${money(p.price)}${p.price_exchange != null ? ` · Замена ${money(p.price_exchange)}` : ''}</div>${p.bonus_text ? `<div class="small bonus">🎁 ${esc(p.bonus_text)}${bonusScope(p) ? ` · ${bonusScope(p)}` : ''}</div>` : ''}${p.is_balloon ? `<div class="small muted">⭐ партнёру за баллон: ${p.partner_reward}${p.partner_reward_custom ? '' : ' (по умолчанию)'}</div><div class="small muted">♻️ возврат: ${p.return_price ? money(p.return_price) : 'не принимается'}${p.return_points ? ` · ⭐ ${p.return_points} баллов` : ''}</div>` : ''}</div></div>
      ${xMore(`p${p.id}`, `
        ${p.description ? `<div class="small mt">${esc(p.description)}</div>` : ''}
        ${p.bonus_text ? `<div class="bonus mt">🎁 ${esc(p.bonus_text)}${bonusScope(p) ? ` · ${bonusScope(p)}` : ''}</div>${p.bonus_desc ? `<div class="small muted">${esc(p.bonus_desc)}</div>` : ''}` : ''}
        <div class="row mt">${p.is_active
          ? `<button class="btn small ghost grow" data-edit="${p.id}">✏️ Изменить</button><button class="btn small secondary" data-hide="${p.id}">🙈 Скрыть</button>`
          : `<button class="btn small ghost grow" data-restore="${p.id}">♻️ Вернуть в каталог</button><button class="btn small danger" data-purge="${p.id}">💀 Удалить</button>`}</div>`)}</div>`;
    container.innerHTML = `
      <button class="btn" data-act="add">➕ Добавить товар</button>
      ${data.products.length > 1 ? '<div class="small muted center">Зажмите ⠿ и перетащите товар или нажимайте ▲ ▼ — в таком порядке товары увидят покупатели в каталоге</div>' : ''}
      ${data.products.length ? `<div class="stack sort-list" data-sortable>${data.products.map(card).join('')}</div>` : '<div class="empty">Активных товаров нет.</div>'}
      ${data.archived.length ? `<div class="section-title">📦 Архив (сняты с продажи)</div>${data.archived.map(card).join('')}` : ''}
      <div class="small muted center">Удалить навсегда можно товар из архива. Массовые действия — во вкладке «⚙️ Ещё».</div>`;
    const all = [...data.products, ...data.archived];
    bindExpand(container);
    // Telegram на телефоне сворачивает приложение свайпом вниз и перехватывает жест раньше,
    // чем его увидит страница, — на вкладке «Товары» свайп выключен (вернётся на другом экране).
    try { tg.disableVerticalSwipes(); } catch (_) { /* старый клиент */ }
    const sortList = container.querySelector('[data-sortable]');
    if (sortList) makeSortable(sortList, async (ids) => {
      try {
        await api('PUT', '/api/admin/products/order', { ids: ids.map(Number) });
        haptic('success'); toast('Порядок товаров сохранён');
      } catch (e) { adminProducts(container); }
    });
    container.querySelector('[data-act=add]').onclick = () => productForm(null, data.products);
    container.querySelectorAll('[data-edit]').forEach((b) => { b.onclick = () => productForm(all.find((p) => p.id === +b.dataset.edit), data.products); });
    container.querySelectorAll('[data-hide]').forEach((b) => {
      b.onclick = async () => {
        if (!(await confirmBox('Скрыть товар из каталога? Он уйдёт в архив и пропадёт из корзин покупателей.'))) return;
        withBusy(b, async () => { await api('POST', `/api/admin/products/${b.dataset.hide}/hide`); toast('Товар скрыт.'); adminProducts(container); });
      };
    });
    container.querySelectorAll('[data-purge]').forEach((b) => {
      b.onclick = async () => {
        const p = all.find((x) => x.id === +b.dataset.purge);
        if (!(await confirmBox(`💀 Удалить «${p ? p.name : 'товар'}» навсегда?\n\nТовар исчезнет из базы и из корзин. В уже оформленных заказах он останется (название и цена там сохранены). Отменить нельзя.`))) return;
        withBusy(b, async () => { await api('DELETE', `/api/admin/products/${b.dataset.purge}`); toast('Товар удалён навсегда.'); adminProducts(container); });
      };
    });
    container.querySelectorAll('[data-restore]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => { await api('POST', `/api/admin/products/${b.dataset.restore}/restore`); toast('Товар возвращён в каталог.'); adminProducts(container); });
    });
  }

  // ---------- 🗺 Интерактивная карта курьеров (24.10.2026): у каждого курьера свой цвет (15 цветов).
  const COURIER_COLORS = ['#e6194b', '#3cb44b', '#ffe119', '#4363d8', '#f58231', '#911eb4', '#42d4f4', '#f032e6',
    '#bfef45', '#fabed4', '#469990', '#dcbeff', '#9a6324', '#800000', '#000075'];
  const STALE_MIN = 10;  // точка старше — «нет обновлений»
  function colorDot(i, letter = '') {
    // 30 меток: номера 0–14 — кружок, 15–29 — квадрат того же цвета.
    const c = COURIER_COLORS[i % COURIER_COLORS.length];
    const square = Math.floor(i / COURIER_COLORS.length) % 2 === 1;
    const light = ['#ffe119', '#bfef45', '#fabed4', '#dcbeff', '#42d4f4'].includes(c);
    return `<span class="cdot ${square ? 'sq' : ''}" style="background:${c};color:${light ? '#111' : '#fff'}">${esc(letter)}</span>`;
  }
  function agoText(iso) {
    if (!iso) return '';
    const min = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (min < 1) return 'только что';
    if (min < 60) return `${min} мин назад`;
    const h = Math.floor(min / 60);
    return h < 24 ? `${h} ч ${min % 60} мин назад` : dateText(iso);
  }
  const isStale = (iso) => !iso || Date.now() - new Date(iso).getTime() > STALE_MIN * 60000;
  function liveText(live) {
    if (!live || !live.active) return live && live.at ? `⚠️ Трансляция выключена · последняя точка ${agoText(live.at)}` : '⚠️ Трансляция не включена';
    return isStale(live.at) ? `⚠️ Трансляция включена, но нет обновлений ${agoText(live.at).replace(' назад', '')}` : `📡 Трансляция идёт · ${agoText(live.at)}`;
  }
  // ---------- 🖥 Карта для ПК (/dispatch, 25.10.2026): одноразовая ссылка от бота, вход на сутки.
  async function pcCard(box) {
    if (!box) return;
    const draw = (d) => {
      const list = d.sessions.map((s) => `<div class="row spread small mt" style="gap:8px"><span>💻 ${esc(s.browser)}${s.ip ? ' · ' + esc(s.ip) : ''}<br><span class="muted">вошёл ${dateText(s.created_at)} · до ${dateText(s.expires_at)}</span></span><button class="btn ghost small" style="width:auto" data-pc-out="${s.id}">Выйти</button></div>`).join('');
      box.innerHTML = `<div class="card compact">
        <div class="row spread" style="gap:8px"><b>💻 Диспетчерская на компьютере</b><button class="btn small" style="width:auto" data-pc-link>🖥 Карта для ПК</button></div>
        <div class="small muted mt">Бот пришлёт одноразовую ссылку. Откройте её на компьютере — вход на ${d.hours} ч. Повторно и у другого человека ссылка не сработает.</div>
        ${d.pending_link ? `<div class="small mt">⏳ Ссылка от ${dateText(d.pending_link.created_at)} ещё не открыта. Новая ссылка её отменит.</div>` : ''}
        ${list ? `<div class="mt"><b class="small">Активные входы: ${d.sessions.length}</b>${list}</div>` : '<div class="small muted mt">Активных входов на ПК нет.</div>'}
        ${list || d.pending_link ? '<button class="btn ghost small mt" data-pc-all>🚪 Выйти везде</button>' : ''}
        <div data-pc-url></div>
      </div>`;
      box.querySelector('[data-pc-link]').onclick = (e) => withBusy(e.target, async () => {
        const r = await api('POST', '/api/admin/dispatch/link');
        toast(r.sent ? '📨 Ссылка отправлена вам в чат с ботом.' : 'Бот не смог отправить ссылку — скопируйте её ниже.', !r.sent, 4000);
        await reload();
        const slot = box.querySelector('[data-pc-url]');
        slot.innerHTML = `<div class="small mt muted">Ссылка (одноразовая, не пересылайте):</div><div class="small" style="word-break:break-all;user-select:text;-webkit-user-select:text">${esc(r.url)}</div><button class="btn ghost small mt" data-pc-copy>📋 Скопировать</button>`;
        slot.querySelector('[data-pc-copy]').onclick = async () => {
          try { await navigator.clipboard.writeText(r.url); toast('📋 Ссылка скопирована.'); } catch (err) { toast('Не удалось скопировать — выделите ссылку и скопируйте вручную.', true); }
        };
      });
      box.querySelectorAll('[data-pc-out]').forEach((b) => {
        b.onclick = () => withBusy(b, async () => { draw(await api('POST', `/api/admin/dispatch/sessions/${b.dataset.pcOut}/revoke`)); toast('Вход закрыт.'); });
      });
      const all = box.querySelector('[data-pc-all]');
      if (all) {
        all.onclick = async () => {
          if (!(await confirmBox('Закрыть все входы в карту на ПК и отменить неоткрытую ссылку?'))) return;
          withBusy(all, async () => { draw(await api('POST', '/api/admin/dispatch/sessions/revoke-all')); toast('Все входы на ПК закрыты.'); });
        };
      }
    };
    const reload = async () => draw(await api('GET', '/api/admin/dispatch/sessions'));
    try { await reload(); } catch (e) { box.innerHTML = ''; }
  }

  async function adminMap(container, silent = false) {
    const data = await api('GET', '/api/admin/map');
    const st = state.adminMap && state.adminMap.container === container ? state.adminMap : null;
    const list = data.couriers;
    const onMap = list.filter((c) => c.status !== 'offline' && c.live.lat != null);
    const statusTxt = (c) => (c.status === 'online' ? `🟢 На смене${c.started_at ? ' с ' + timeText(c.started_at) : ''}` : c.status === 'break' ? `🔴 Перерыв${c.ended_at ? ' с ' + timeText(c.ended_at) : ''}`
      : c.status === 'silent' ? '🟥 Не отвечает на опрос' : c.status === 'ending' ? '🏁 Закончил смену' : '🔴 Не на смене');
    const cards = list.map((c) => `<div class="card compact map-card" data-mc="${c.id}">
        <div class="row" style="gap:8px">${colorDot(c.color, (c.name || '?')[0])}<h3 style="margin:0">${esc(c.name)}</h3></div>
        <div class="small mt">${statusTxt(c)}</div>
        ${c.status !== 'offline' ? `<div class="small ${c.live.active && !isStale(c.live.at) ? 'muted' : 'warn-text'}">${liveText(c.live)}</div>` : ''}
        ${c.orders.length ? `<div class="small mt">${c.orders.map((o) => `🚚 №${o.id} · ${esc(o.status_label)}${o.region ? ' · 📍 ' + esc(o.region) : ''}<div class="muted">${esc(o.address)}</div>`).join('')}</div>` : ''}
        ${c.status !== 'offline' && c.live.lat != null ? `<button class="btn ghost small mt" data-mfocus="${c.id}">🎯 Показать на карте</button>` : ''}
      </div>`).join('');
    const legendKey = JSON.stringify(list.map((c) => [c.id, c.status, c.live.active, isStale(c.live.at), Math.round((Date.now() - new Date(c.live.at || 0).getTime()) / 60000), c.orders]));
    if (!st) {
      container.innerHTML = `<div data-pc></div><div class="small muted">На карте — курьеры на смене и на перерыве, у каждого свой цвет. Обновляется каждые 10 секунд.</div>
        <div class="map admin-map" data-amap></div>
        <div class="small muted" data-amap-empty ${onMap.length ? 'hidden' : ''}>Пока никого нет на карте: курьеры появятся, когда выйдут на смену и включат трансляцию.</div>
        <div class="stack" data-amap-list>${cards || '<div class="empty">Курьеров пока нет.</div>'}</div>`;
      pcCard(container.querySelector('[data-pc]'));
      const el = container.querySelector('[data-amap]');
      try { await loadMaps(); } catch (e) { el.innerHTML = '<div class="empty small">Карта не загрузилась. Проверьте интернет.</div>'; return; }
      const ol = window.ol;
      const map = new ol.Map({
        target: el,
        layers: [new ol.layer.Tile({ source: new ol.source.OSM({ attributions: '<a href="https://www.openstreetmap.org/copyright">© участники OpenStreetMap, ODbL</a>' }) })],
        controls: [new ol.control.Attribution({ collapsible: false }), new ol.control.Zoom()],
        view: new ol.View({ center: ol.proj.fromLonLat([37.6176, 55.7558]), zoom: 10 }),
      });
      state.adminMap = { container, map, pins: {}, legendKey, fitted: false };
      state.maps.push({ map: { remove: () => map.setTarget(null) } });
    } else if (legendKey !== st.legendKey) {
      container.querySelector('[data-amap-list]').innerHTML = cards;
      container.querySelector('[data-amap-empty]').hidden = !!onMap.length;
      st.legendKey = legendKey;
    }
    const m = state.adminMap;
    if (!m || !window.ol) return;
    const ol = window.ol;
    const seen = new Set();
    onMap.forEach((c) => {
      seen.add(c.id);
      const stale = !c.live.active || isStale(c.live.at) || c.status === 'break';
      let pin = m.pins[c.id];
      if (!pin) {
        const el = document.createElement('div');
        el.className = 'cmark';
        const overlay = new ol.Overlay({ element: el, positioning: 'bottom-center', stopEvent: false });
        m.map.addOverlay(overlay);
        pin = m.pins[c.id] = { el, overlay };
        el.addEventListener('click', () => {
          const card = container.querySelector(`[data-mc="${c.id}"]`);
          if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.classList.add('flash'); setTimeout(() => card.classList.remove('flash'), 1200); }
        });
      }
      pin.el.innerHTML = `${colorDot(c.color, (c.name || '?')[0])}<span class="cmark-name">${esc(c.name)}${c.orders.length ? ' · 🚚' + c.orders.length : ''}</span>`;
      pin.el.classList.toggle('stale', stale);
      pin.overlay.setPosition(ol.proj.fromLonLat([c.live.lon, c.live.lat]));
    });
    Object.keys(m.pins).forEach((id) => {
      if (!seen.has(+id)) { m.map.removeOverlay(m.pins[id].overlay); delete m.pins[id]; }
    });
    if (!m.fitted && onMap.length) {
      m.fitted = true;
      const ext = ol.extent.boundingExtent(onMap.map((c) => ol.proj.fromLonLat([c.live.lon, c.live.lat])));
      m.map.getView().fit(ext, { padding: [60, 40, 40, 40], maxZoom: 15, duration: 0 });
    }
    container.querySelectorAll('[data-mfocus]').forEach((b) => {
      b.onclick = () => {
        const c = onMap.find((x) => x.id === +b.dataset.mfocus);
        if (!c) return;
        m.map.getView().animate({ center: ol.proj.fromLonLat([c.live.lon, c.live.lat]), zoom: 15, duration: 300 });
        container.querySelector('[data-amap]').scrollIntoView({ behavior: 'smooth', block: 'start' });
      };
    });
  }

  // Статус курьера у админа: 🟢 на смене / 🔴 перерыв, закончил, не ответил, не выходил.
  function shiftStatusText(sh) {
    if (!sh || sh.status === 'offline') {
      if (!sh || !sh.ended_at) return '🔴 Не выходил на смену';
      const why = sh.end_reason === 'auto' ? 'Не ответил «Вы ещё на смене?» — офлайн' : sh.end_reason === 'admin' ? 'Смену закрыл администратор' : 'Закончил смену';
      return `🔴 ${why} · ${dateText(sh.ended_at)}`;
    }
    if (sh.status === 'break') return `🔴 Перерыв с ${timeText(sh.ended_at)}`;
    if (sh.status === 'ending') return '🏁 Закончил смену — ждёт вашего подтверждения (уведомление на главной)';
    if (sh.status === 'silent') return `<span class="st-silent">🟥 Не отвечает на опрос с ${timeText(sh.silent_since)} — заказы не назначаются</span>`;
    return `🟢 На смене с ${timeText(sh.started_at)}${sh.busy ? ` · 🚚 в работе ${sh.busy}` : ''}`;
  }

  // ---------- 📋 Журнал смен курьера (25.10.2026)
  const SHIFT_END = { self: 'закончил сам', admin: 'закрыл администратор', auto: 'закрыта автоматически: не ответил «Вы ещё на смене?»', restart: 'закрыта при новом «В путь!»' };
  const stockTxt = (st) => (st ? Object.entries(st).filter(([, n]) => +n).map(([z, n]) => `${/^\d+$/.test(z) ? `${z} л` : esc(z)} · ${n}`).join(', ') || 'нет' : '—');
  function shiftLength(a, b) {
    const min = Math.max(0, Math.round(((b ? new Date(b) : new Date()) - new Date(a)) / 60000));
    return `${Math.floor(min / 60)} ч ${min % 60} мин`;
  }
  function shiftEventText(e) {
    const d = e.data || {}, no = e.order_id || d.order_no;
    const ord = no ? `№${no}` : '';
    const what = d.items ? `<div class="small muted">${esc(d.items)}${d.address ? ` · ${esc(d.address)}` : ''}</div>` : '';
    const who = (x) => (x.by === 'admin' ? `админ ${esc(x.name || '')}` : 'сам курьер');
    switch (e.kind) {
      case 'shift_start': return `🚀 «В путь!»${d.regions && d.regions.length ? ` · регионы: ${esc(d.regions.join(', '))}` : ''}<div class="small muted">баллоны с собой: ${stockTxt(d.stock)}</div>`;
      case 'break': return '☕ Перерыв';
      case 'resume': return '▶️ Вернулся на смену';
      case 'take': return `📦 ${d.by === 'admin' ? 'Админ назначил' : 'Взял'} ${d.kind === 'return' ? 'возврат' : 'заказ'} ${ord}${what}`;
      case 'released': return `↩️ Заказ ${ord} снят с курьера${what}`;
      case 'cancelled': return `🚫 Заказ ${ord} отменён${what}`;
      case 'not_delivered': return `❌ Заказ ${ord} не доставлен${what}`;
      case 'disputed': return `📵 Заказ ${ord}: покупатель не вышел на связь${what}`;
      case 'delivered': return `✅ Доставил заказ ${ord}${what}<div class="small">+${money(e.earned)} заработок${e.cash ? ` · ${money(e.cash)} наличными за товары` : ''}</div>`;
      case 'return_picked': return `♻️ Забрал возврат ${ord}${what}<div class="small">+${money(e.earned)} заработок · ${e.data && e.data.return_mode === 'points' ? `покупателю начислено ${e.data.return_points} баллов` : `отдал покупателю ${money(-e.returns)}`}</div>`;
      case 'prepaid': return `💵 Взял предоплату за доставку ${ord}${what}<div class="small">${money(e.cash)} наличными · сдать в кассу вместе с остальными</div>`;
      case 'prepaid_reverse': return `↩️ Заказ ${ord} отменён админом — предоплата ${money(-e.cash)} списана с курьера${what}`;
      case 'reload': return `🏭 Склад: загрузка подтверждена · ${stockTxt(d.stock)}`;
      case 'reload_rejected': return `🏭 Склад: загрузка отклонена · ${stockTxt(d.stock)}`;
      case 'regions': return `📍 Сменил регионы: ${esc((d.regions || []).join(', '))}`;
      case 'empties_set': return `♻️ Указал пустые баллоны: ${stockTxt(d.empty)}`;
      case 'empties_handed': return `♻️ «Понял, не забуду» — сдаёт пустые на склад: ${stockTxt(d.before)}`;
      case 'transfer_out': return `🔄 Передал курьеру ${esc(d.to || '')}: ${stockTxt(d.items)}${Object.values(d.empties || {}).some((n) => +n) ? ` · пустые ${stockTxt(Object.fromEntries(Object.entries(d.empties).map(([z, n]) => [z, -n])))}` : ''}`;
      case 'transfer_in': return `🔄 Принял от курьера ${esc(d.from || '')}: ${stockTxt(d.items)}${Object.values(d.empties || {}).some((n) => +n) ? ` · пустые ${stockTxt(d.empties)}` : ''}`;
      case 'reset_earned': case 'reset_cash': case 'reset_returns': {
        const row = COUNTERS.find((x) => 'reset_' + x[0] === e.kind);
        return `🧮 Обнулено «${row ? row[1].replace(/^\S+\s/, '') : ''}» — было ${money(d.before || 0)} · ${who(d)}`;
      }
      case 'shift_end': return `🏁 Смена закончена · ${esc(SHIFT_END[d.reason] || '')}<div class="small muted">полные: ${stockTxt(d.stock)} · пустые: ${stockTxt(d.empty)}</div>`;
      default: return esc(e.kind);
    }
  }
  function shiftTotals(sh) {
    return `<div class="small">✅ доставок ${sh.deliveries || 0} · ♻️ возвратов ${sh.pickups || 0}</div>
      <div class="small">💵 собрано ${money(sh.cash || 0)} · ♻️ отдано ${money(-(sh.returns || 0))} · 🏦 сдать ${money((sh.cash || 0) + (sh.returns || 0) - (sh.keep || 0))} · 💼 ${money(sh.earned || 0)}</div>`;
  }
  async function shiftsSheet(c) {
    const list = await api('GET', `/api/admin/couriers/${c.id}/shifts`);
    openSheet(`<h3>📋 Смены · ${esc(c.name)}</h3>
      ${list.shifts.length ? list.shifts.map((sh) => `<button class="card linkcard mt" data-shift="${sh.id}">
          <div class="row spread"><b>${dateText(sh.started_at)} — ${sh.open ? 'идёт' : dateText(sh.ended_at)}</b>${sh.open ? '<span class="badge paid">🟢 идёт</span>' : ''}</div>
          <div class="small muted">${shiftLength(sh.started_at, sh.ended_at)}${sh.region_labels.length ? ` · 📍 ${esc(sh.region_labels.join(', '))}` : ''}</div>
          ${shiftTotals(sh)}</button>`).join('') : '<div class="empty">Смен пока нет — журнал ведётся с 25.10.2026.</div>'}`, (sheet) => {
      sheet.querySelectorAll('[data-shift]').forEach((b) => {
        b.onclick = () => withBusy(b, async () => {
          const d = await api('GET', `/api/admin/shifts/${b.dataset.shift}`);
          const sh = d.shift;
          const box = sheet.querySelector('.inner') || sheet;
          box.innerHTML = `<button class="btn ghost small" data-back>← Все смены</button>
            <h3 class="mt">📋 ${esc(c.name)} · ${dateText(sh.started_at)}</h3>
            <div class="card mt">
              <div class="small">${sh.open ? '🟢 Смена идёт' : `🏁 ${esc(SHIFT_END[sh.end_reason] || 'закончена')} · ${dateText(sh.ended_at)}`} · ${shiftLength(sh.started_at, sh.ended_at)}</div>
              ${sh.region_labels.length ? `<div class="small">📍 ${esc(sh.region_labels.join(', '))}</div>` : ''}
              <div class="small">🛢 на начало: ${stockTxt(sh.stock_start)}${sh.open ? '' : ` · на конец: ${stockTxt(sh.stock_end)} · пустые: ${stockTxt(sh.empty_end)}`}</div>
              <div class="divider"></div>
              <div class="row spread"><span>💵 Собрано за заказы</span><b>${money(sh.cash)}</b></div>
              <div class="row spread"><span>♻️ Отдано за пустые баллоны</span><b>${money(-sh.returns)}</b></div>
              ${sh.keep ? `<div class="row spread"><span>✋ Оставил себе (заработок по наличным)</span><b>${money(-sh.keep)}</b></div>` : ''}
              <div class="row spread"><span>🏦 Сдать в кассу</span><b>${money(sh.cash + sh.returns - (sh.keep || 0))}</b></div>
              <div class="row spread"><span>💼 Заработал</span><b>${money(sh.earned)}</b></div>
              <div class="small muted mt">доставок ${sh.deliveries} · возвратов ${sh.pickups}</div>
            </div>
            <div class="section-title">Что было за смену</div>
            <ul class="shift-log">${d.events.map((e) => `<li><span class="small muted">${timeText(e.at)}</span><div>${shiftEventText(e)}</div></li>`).join('')}</ul>`;
          box.querySelector('[data-back]').onclick = () => shiftsSheet(c);
        });
      });
    });
  }

  // 📊 Статистика курьеров (28.10.2026): неделя пн–вс или календарный месяц, таблица с сортировкой,
  // по нажатию на курьера — по дням (дни без работы — пустые).
  const STAT_COLS = [['earned', 'Заработал', true], ['to_hand', 'Сдать в кассу', true], ['orders', 'Заказов', false],
    ['balloons', 'Баллонов', false], ['cash', 'Собрано', true], ['paid', 'Отдано за пустые', true]];
  routes['courier-stats'] = async () => {
    setTitle('Статистика курьеров');
    const st = state.cstats || (state.cstats = { period: 'week', offset: 0, sort: 'earned', asc: false });
    const d = await api('GET', `/api/admin/courier-stats?period=${st.period}&offset=${st.offset}`);
    const fmt = (k, v, money_) => (v ? (money_ ? money(v) : v) : '<span class="muted">—</span>');
    const rows = [...d.couriers].sort((a, b) => (st.sort === 'name' ? a.name.localeCompare(b.name, 'ru') : a[st.sort] - b[st.sort]) * (st.asc ? 1 : -1));
    const arrow = (k) => (st.sort === k ? (st.asc ? ' ▲' : ' ▼') : '');
    view.innerHTML = `<div class="stack">
      <div class="seg">${[['week', 'Неделя (пн–вс)'], ['month', 'Месяц']].map(([k, l]) => `<button data-sp="${k}" class="${st.period === k ? 'on' : ''}">${l}</button>`).join('')}</div>
      <div class="card tight row spread"><button class="btn ghost small" data-so="-1">‹</button><b>${esc(d.label)}</b><button class="btn ghost small" data-so="1" ${st.offset >= 0 ? 'disabled' : ''}>›</button></div>
      <div class="card"><div class="stat-total">
        <div><span class="small muted">Заказов</span><b>${d.totals.orders}</b></div><div><span class="small muted">Баллонов</span><b>${d.totals.balloons}</b></div>
        <div><span class="small muted">Сдать в кассу</span><b>${money(d.totals.to_hand)}</b></div><div><span class="small muted">Заработали курьеры</span><b>${money(d.totals.earned)}</b></div></div></div>
      <div class="small muted">Нажмите на заголовок — сортировка; на курьера — по дням.</div>
      <div class="stat-wrap"><table class="stat-table">
        <thead><tr><th data-ss="name">Курьер${arrow('name')}</th>${STAT_COLS.map(([k, l]) => `<th data-ss="${k}">${l}${arrow(k)}</th>`).join('')}</tr></thead>
        <tbody>${rows.map((c) => `<tr data-sc="${c.id}"><td><span class="stat-name">${colorDot(c.color, (c.name || '?')[0])} <span>${esc(c.name)}${c.active ? '' : ' (откл.)'}</span></span></td>
          ${STAT_COLS.map(([k, , m]) => `<td>${fmt(k, c[k], m)}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${STAT_COLS.length + 1}" class="muted">Курьеров нет</td></tr>`}</tbody>
      </table></div>
      <div class="small muted">Считаются доставленные заказы и забранные пустые баллоны (по времени доставки, МСК). «Сдать в кассу» = собрано наличными − отдано покупателям за пустые.</div>
    </div>`;
    view.querySelectorAll('[data-sp]').forEach((b) => { b.onclick = () => { st.period = b.dataset.sp; st.offset = 0; render(); }; });
    view.querySelectorAll('[data-so]').forEach((b) => { b.onclick = () => { st.offset = Math.min(0, st.offset + +b.dataset.so); render(); }; });
    view.querySelectorAll('[data-ss]').forEach((th) => {
      th.onclick = () => { if (st.sort === th.dataset.ss) st.asc = !st.asc; else { st.sort = th.dataset.ss; st.asc = th.dataset.ss === 'name'; } render(); };
    });
    view.querySelectorAll('[data-sc]').forEach((tr) => {
      tr.onclick = () => {
        const c = d.couriers.find((x) => x.id === +tr.dataset.sc);
        openSheet(`<h3>${colorDot(c.color, (c.name || '?')[0])} ${esc(c.name)} · ${esc(d.label)}</h3>
          <div class="stat-wrap mt"><table class="stat-table">
            <thead><tr><th>День</th>${STAT_COLS.map(([, l]) => `<th>${l}</th>`).join('')}</tr></thead>
            <tbody>${d.days.map((day) => { const r = c.days[day.date]; return `<tr class="${r ? '' : 'empty-day'}"><td>${day.label}</td>${STAT_COLS.map(([k, , m]) => `<td>${r ? fmt(k, r[k], m) : ''}</td>`).join('')}</tr>`; }).join('')}</tbody>
            <tfoot><tr><td>Итого</td>${STAT_COLS.map(([k, , m]) => `<td>${fmt(k, c[k], m)}</td>`).join('')}</tr></tfoot>
          </table></div>
          <button class="btn ghost mt" style="width:100%" data-act="st-close">Закрыть</button>`, (sheet) => { sheet.querySelector('[data-act=st-close]').onclick = () => closeSheet(); });
      };
    });
  };

  // 🏢 Подъём на этаж и 🚚 Доставка за пределы Москвы — во вкладке «Курьеры» (28.10.2026, из «Товаров»).
  function deliveryCards(settings) {
    const lift = settings.floor_lift_price || 0;
    const out = settings.outside || { base: 500, per_km: 0, key: false };
    return `<div class="card rates-card">
        <div class="row spread"><div class="grow"><h3>🏢 Подъём на этаж</h3><div class="small muted">${lift ? `${money(lift)} за подъём одного баллона — покупатель выбирает, сколько подъёмов (не больше баллонов в заказе)` : 'Услуга выключена — покупатели её не видят'}</div></div>
          <button class="btn small ghost" data-act="lift">✏️ Цена</button></div></div>
      <div class="card rates-card">
        <div class="row spread"><div class="grow"><h3>🚚 Доставка за пределы Москвы</h3><div class="small muted">${out.per_km
          ? `${money(out.base)} + ${money(out.per_km)} за км по дорогам за Москвой`
          : 'Авторасчёт выключен, такие заказы индивидуальные'}${out.per_km && !out.key ? '<br>⚠️ Не задан ключ маршрутов ORS_API_KEY в Amvera, расчёт не работает' : ''}</div></div>
          <button class="btn small ghost" data-act="outside">✏️ Цена</button></div></div>`;
  }
  function bindDeliveryCards(container, settings, after) {
    const lift = settings.floor_lift_price || 0;
    const out = settings.outside || { base: 500, per_km: 0, key: false };
    container.querySelector('[data-act=lift]').onclick = () => openSheet(`<h3>🏢 Подъём на этаж</h3><div class="stack mt">
        <label class="field">Цена за подъём одного баллона, ₽<input type="number" id="lift-price" inputmode="numeric" min="0" step="1" value="${lift}"></label>
        <div class="small muted">Покупатель выбирает, сколько баллонов поднять (не больше, чем баллонов в заказе): 3 подъёма = 3 × цена. Этажи не учитываются. Деньги за подъём идут курьеру в заработок. 0 — услуга выключена. Новая цена — для новых заказов.</div>
        <button class="btn" data-act="save">Сохранить</button></div>`, (sheet) => {
      sheet.querySelector('[data-act=save]').onclick = (e) => withBusy(e.target, async () => {
        const v = sheet.querySelector('#lift-price').value.trim();
        if (!/^\d+$/.test(v)) throw new Error('Введите цену целым числом рублей (0 — выключить).');
        const res = await api('PUT', '/api/admin/settings', { floor_lift_price: +v });
        closeSheet(); toast(res.floor_lift_price ? `Подъём на этаж: ${money(res.floor_lift_price)}.` : 'Услуга подъёма выключена.'); after();
      });
    });
    container.querySelector('[data-act=outside]').onclick = () => openSheet(`<h3>🚚 Доставка за пределы Москвы</h3><div class="stack mt">
        <label class="field">Базовая цена, ₽<input type="text" id="out-base" inputmode="numeric" maxlength="6" value="${out.base}"></label>
        <label class="field">Цена за 1 км, ₽<input type="text" id="out-km" inputmode="numeric" maxlength="5" value="${out.per_km}"></label>
        <div class="small muted">Километры считаются по дорогам только за границей Москвы, округляются вверх до целого<br>Цена за км 0 выключает авторасчёт, заказ станет индивидуальным</div>
        <button class="btn" data-act="save">Сохранить</button></div>`, (sheet) => {
      sheet.querySelector('[data-act=save]').onclick = (e) => withBusy(e.target, async () => {
        const b = sheet.querySelector('#out-base').value.trim(), k = sheet.querySelector('#out-km').value.trim();
        if (!/^\d+$/.test(b) || !/^\d+$/.test(k)) throw new Error('Введите цены целыми числами рублей.');
        const res = await api('PUT', '/api/admin/outside-delivery', { base: +b, per_km: +k });
        settings.outside = res;
        closeSheet(); toast(res.per_km ? `Доставка за МКАД: ${money(res.base)} + ${money(res.per_km)} за км.` : 'Авторасчёт выключен.'); after();
      });
    });
  }

  // 🚴 Ставки курьеру (28.10.2026) — во вкладке «Курьеры», каждая ставка отдельной карточкой с фиолетовой рамкой.
  const RATE_LABELS = { balloon: 'Ставка курьеру за баллон в заказе', return: 'Ставка курьеру за поездку на возврат пустых баллонов',
    descent: 'Ставка курьеру за спуск пустого баллона' };
  const RATE_ICONS = { balloon: '🔥', return: '♻️', descent: '⬇️' };
  const RATE_UNITS = { balloon: 'за каждый баллон', return: 'за одну поездку', descent: 'за каждый спущенный баллон' };
  const RATE_HELP = {
    balloon: 'Две графы: ставка за ПЕРВЫЙ баллон в заказе и за КАЖДЫЙ СЛЕДУЮЩИЙ в том же заказе — и при покупке нового, и при обмене. Пример: 1500 и 1000 → заказ на 3 баллона = 1500 + 1000 + 1000 = 3500. Подъём на этаж оплачивается отдельно и тоже идёт курьеру. Попадает в «Заработал» и в статистику курьеров. 0 — не начислять. Новая ставка действует для заказов, выполненных после изменения.',
    return: 'Начисляется курьеру один раз за выполненный заказ на возврат пустых баллонов (выкуп у покупателя), независимо от количества баллонов в заказе. Спуск баллонов оплачивается отдельной ставкой. Попадает в «Заработал» и в статистику курьеров. 0 — не начислять. Новая ставка действует для заказов, выполненных после изменения.',
    descent: 'Только в заказе на возврат пустых баллонов: покупатель может попросить спустить пустые баллоны (не больше, чем баллонов в заказе). Сумма «количество × ставка» вычитается из выплаты покупателю и начисляется курьеру в «Заработал». При «Замене» за спуск покупатель не платит. 0 — услуга выключена, покупатель её не видит. Новая ставка — для новых заказов.',
  };
  const ratesCard = (cr) => ['balloon', 'return', 'descent'].map((k) => `<div class="card rates-card">
      <div class="row spread"><div class="grow"><h3>${RATE_ICONS[k]} ${RATE_LABELS[k]}</h3><div class="small muted">${k === 'balloon' ? ((cr.balloon || 0) || (cr.balloon_next || 0) ? `1-й баллон: ${money(cr.balloon || 0)} · каждый следующий: ${money(cr.balloon_next ?? cr.balloon ?? 0)}` : 'Не начисляется (0 ₽)') : (cr[k] || 0) ? `${money(cr[k])} ${RATE_UNITS[k]}` : 'Не начисляется (0 ₽)'}</div></div>
        <button class="btn small ghost" data-rate="${k}">✏️ Цена</button></div></div>`).join('');
  function bindRates(container, cr, after) {
    container.querySelectorAll('[data-rate]').forEach((btn) => {
      const k = btn.dataset.rate;
      btn.onclick = () => openSheet(`<h3>${RATE_ICONS[k]} ${RATE_LABELS[k]}</h3><div class="stack mt">
          ${k === 'balloon'
            ? `<label class="field">Первый баллон в заказе, ₽<input type="text" id="r-val" inputmode="numeric" maxlength="7" value="${cr.balloon || 0}"></label>
               <label class="field">Каждый следующий баллон в том же заказе, ₽<input type="text" id="r-next" inputmode="numeric" maxlength="7" value="${cr.balloon_next ?? cr.balloon ?? 0}"></label>`
            : `<label class="field">Ставка ${RATE_UNITS[k]}, ₽<input type="text" id="r-val" inputmode="numeric" maxlength="7" value="${cr[k] || 0}"></label>`}
          <div class="small muted">${RATE_HELP[k]}</div>
          <button class="btn" data-act="save">Сохранить</button></div>`, (sheet) => {
        const inp = sheet.querySelector('#r-val');
        const nx = sheet.querySelector('#r-next');
        [inp, nx].filter(Boolean).forEach((el) => { el.oninput = () => { el.value = el.value.replace(/\D/g, '').slice(0, 7); }; });
        sheet.querySelector('[data-act=save]').onclick = (e) => withBusy(e.target, async () => {
          const v = inp.value.trim();
          if (!/^\d+$/.test(v)) throw new Error('Введите ставку целым числом рублей (0 — не начислять).');
          const body = { balloon: +(cr.balloon || 0), balloon_next: +(cr.balloon_next ?? cr.balloon ?? 0), return: +(cr.return || 0), descent: +(cr.descent || 0) };
          body[k] = +v;
          if (nx) {
            const w = nx.value.trim();
            if (!/^\d+$/.test(w)) throw new Error('Введите ставку за следующий баллон целым числом рублей.');
            body.balloon_next = +w;
          }
          await api('PUT', '/api/admin/courier-rates', body);
          closeSheet(); haptic('success'); toast('Ставка сохранена.'); after();
        });
      });
    });
  }

  async function adminCouriers(container) {
    const [data, settings] = await Promise.all([api('GET', '/api/admin/couriers'), api('GET', '/api/admin/settings')]);
    const reqs = data.couriers.filter((c) => c.shift && c.shift.change === 'requested');
    const working = data.couriers.filter((c) => c.is_active);
    const online = working.filter((c) => c.shift && c.shift.status === 'online').length;
    const loads = data.couriers.filter((c) => c.reload_pending);
    const loadTxt = (st) => Object.entries(st || {}).filter(([, v]) => +v).map(([k, v]) => `${esc(k)} — ${v}`).join(', ') || 'ничего нет';
    container.innerHTML = `${loads.map((c) => `<div class="card warn-card">
        <div><b>🏭 ${esc(c.name)}</b> загрузился на складе</div>
        <div class="small">${esc(loadTxt(c.reload_pending))}</div>
        ${c.shift && c.shift.stock ? `<div class="small muted">Сейчас по учёту ${esc(loadTxt(c.shift.stock.now))}</div>` : ''}
        <div class="btn-pair mt"><button class="btn ok" data-rload="${c.id}" data-yes="1">✅ Подтвердить</button><button class="btn ghost" data-rload="${c.id}" data-yes="0">✖️ Отклонить</button></div></div>`).join('')}
      ${working.length ? `<div class="card tight row spread"><span>🟢 На смене сейчас</span><b>${online} из ${working.length}</b></div>` : ''}
      ${ratesCard(data.courier_rates || { balloon: 0, return: 0, descent: 0 })}
      ${deliveryCards(settings)}
      ${online ? '<button class="btn ghost" data-act="askall">📣 Опросить всех курьеров на смене</button>' : ''}
      <button class="btn ghost" data-act="stats">📊 Статистика курьеров: неделя / месяц</button>
      <button class="btn" data-act="add">➕ Добавить курьера</button>
      ${data.couriers.length ? `<input type="search" class="list-search" data-search placeholder="🔍 Имя, @логин или телефон" value="${esc(state.courierSearch || '')}"><div class="small muted" data-search-empty hidden>Никого не нашли.</div>` : ''}
      ${data.couriers.length ? data.couriers.map((c) => `<div class="card compact" data-search-text="${esc(searchText(c.name, c.username, [c.phone], c.telegram_id))}">
        <div class="row spread"><h3 class="row" style="gap:8px">${colorDot(c.color, (c.name || '?')[0])}${esc(c.name)}</h3><span class="badge ${c.is_active ? 'paid' : 'unpaid'}">${c.is_active ? 'Активен' : 'Отключён'}</span></div>
        <div class="small muted">${c.rating ? `★ ${fmtRating(c.rating)} · ${c.rating_count} ${plural(c.rating_count, 'оценка', 'оценки', 'оценок')}` : 'Рейтинг пока не выставлен'}${c.username ? ` · @${esc(c.username)}` : ''}</div>
        <div class="small mt">${shiftStatusText(c.shift)}</div>
        ${c.shift && c.shift.status !== 'offline' ? `<div class="small ${c.live && c.live.active && !isStale(c.live.at) ? 'muted' : 'warn-text'}">${liveText(c.live)}</div>` : ''}
        ${c.shift && c.shift.stock ? `<div class="stock-admin mt">${(c.shift.stock.items || []).filter((i) => i.start || i.now || i.reload).map((i) => `<div><b>${esc(i.name)}</b> <span class="muted">начало</span> ${i.start}${i.reload != null ? ` <span class="muted">· склад</span> ${i.reload}` : ''} <span class="muted">· сейчас</span> <b class="${i.now < 0 ? 'neg' : ''}">${i.now}</b></div>`).join('') || '<div class="muted">📦 Товаров с собой нет</div>'}</div>` : ''}
        ${c.empties ? `<div class="small mt">♻️ Пустые: ${Object.entries(c.empties).map(([z, n]) => `${z} л · <b>${n}</b>`).join('&nbsp;&nbsp; ')}</div>` : ''}
        <div class="mt" data-cmoney="${c.id}">${moneyCard(c.counters)}</div>
        <button class="btn ghost small mt" style="width:100%" data-shifts="${c.id}">📋 Смены</button>
        ${c.telegram_id ? '' : '<div class="small mt" style="color:var(--danger,#e53935)">⚠️ Telegram ID не задан — курьер не увидит заказы</div>'}
        ${xMore(`c${c.id}`, `<dl class="kv mt">
          ${c.phone ? `<dt>Телефон</dt><dd><a href="tel:${esc(c.phone)}" style="color:var(--accent)">${esc(c.phone)}</a></dd>` : ''}${c.telegram_id ? `<dt>Telegram ID</dt><dd>${c.telegram_id}</dd>` : ''}${c.username ? `<dt>Username</dt><dd>@${esc(c.username)}</dd>` : ''}</dl>
          <div class="row mt"><button class="btn ${c.is_active ? 'secondary' : 'ok'} small grow" data-toggle="${c.id}:${c.is_active ? 0 : 1}">${c.is_active ? '⛔ Отключить' : '✅ Включить'}</button><button class="btn danger small" data-cdel="${c.id}">🗑 Удалить</button></div>`)}</div>`).join('') : '<div class="empty">Курьеров пока нет.</div>'}`;
    bindListSearch(container, 'courierSearch');
    bindExpand(container);
    container.querySelector('[data-act=stats]').onclick = () => { haptic('light'); go('courier-stats'); };
    bindRates(container, data.courier_rates || { balloon: 0, return: 0, descent: 0 }, () => adminCouriers(container));
    bindDeliveryCards(container, settings, () => adminCouriers(container));
    const askAll = container.querySelector('[data-act=askall]');
    if (askAll) askAll.onclick = async () => {
      if (!(await confirmBox('Отправить «Вы ещё на смене?» всем курьерам на смене прямо сейчас? Отсчёт часа начнётся заново, на ответ — 10 минут.'))) return;
      withBusy(askAll, async () => { const r = await api('POST', '/api/admin/couriers/ask-all'); haptic('success'); toast(`📣 Опрос отправлен: ${r.asked}`); });
    };
    container.querySelectorAll('[data-cmoney]').forEach((el) => {
      const c = data.couriers.find((x) => x.id === +el.dataset.cmoney);
      if (c && c.counters) bindCounterResets(el, c.counters, `/api/admin/couriers/${c.id}/counters/reset`, () => adminCouriers(container));
    });
    container.querySelectorAll('[data-shifts]').forEach((b) => {
      b.onclick = () => {
        const c = data.couriers.find((x) => x.id === +b.dataset.shifts);
        shiftsSheet(c);
      };
    });
    container.querySelectorAll('[data-rload]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => {
        const approve = b.dataset.yes === '1';
        await api('POST', `/api/admin/couriers/${b.dataset.rload}/reload`, { approve });
        toast(approve ? '✅ Загрузка подтверждена, остаток курьера обновлён.' : 'Загрузка отклонена.');
        adminCouriers(container);
      });
    });
    container.querySelector('[data-act=add]').onclick = () => openSheet(`<h3>➕ Новый курьер</h3><div class="stack mt">
        <label class="field">Имя<input type="text" id="c-name"></label>
        <label class="field">Телефон (необязательно)<input type="tel" id="c-phone"></label>
        <label class="field">Telegram ID<input type="number" id="c-tg" inputmode="numeric" placeholder="курьер может узнать его командой /id в боте"></label>
        <label class="field">Username без @ (необязательно)<input type="text" id="c-user"></label>
        <button class="btn" data-act="save">Добавить</button></div>`, (sheet) => {
      sheet.querySelector('[data-act=save]').onclick = (e) => withBusy(e.target, async () => {
        await api('POST', '/api/admin/couriers', {
          name: sheet.querySelector('#c-name').value, phone: sheet.querySelector('#c-phone').value,
          telegram_id: sheet.querySelector('#c-tg').value, username: sheet.querySelector('#c-user').value,
        });
        closeSheet(); toast('Курьер добавлен.'); adminCouriers(container);
      });
    });
    container.querySelectorAll('[data-cdel]').forEach((b) => {
      b.onclick = async () => {
        const c = data.couriers.find((x) => x.id === +b.dataset.cdel);
        if (!(await confirmBox(`🗑 Удалить курьера «${c ? c.name : ''}» безвозвратно?\n\nЕго оценки от покупателей удалятся вместе с ним. Старые заказы останутся в истории, но поле «Курьер» в них станет пустым. Отменить нельзя.`))) return;
        withBusy(b, async () => { await api('DELETE', `/api/admin/couriers/${b.dataset.cdel}`); toast('Курьер удалён.'); adminCouriers(container); });
      };
    });
    container.querySelectorAll('[data-toggle]').forEach((b) => {
      b.onclick = () => withBusy(b, async () => {
        const [id, active] = b.dataset.toggle.split(':');
        await api('POST', `/api/admin/couriers/${id}/active`, { active: active === '1' });
        adminCouriers(container);
      });
    });
  }

  routes.admin = async () => {
    setTitle('Админка');
    if (!state.me.roles.admin) { go('home'); return; }
    const tab = state.adminTab;
    view.innerHTML = `<div class="stack">
      <div class="seg tabs5">${[['orders', '🆕 Заказы'], ['products', '📦 Товары'], ['couriers', '🚴 Курьеры'], ['map', '🗺 Карта'], ['customers', '👥 Клиенты'], ['more', '⚙️ Ещё']].map(([k, l]) => `<button data-tab="${k}" class="${tab === k ? 'on' : ''}">${l}${k === 'couriers' ? '<span class="nbadge tab-badge" data-badge="regreq" hidden></span>' : ''}</button>`).join('')}</div>
      <div class="stack" id="admin-body"><div class="skeleton"></div></div></div>`;
    view.querySelectorAll('[data-tab]').forEach((b) => { b.onclick = () => { state.adminTab = b.dataset.tab; render(); }; });
    const body = view.querySelector('#admin-body');
    applyBadges();  // бейдж «просят сменить регион» на вкладке «Курьеры» — сразу
    if (tab === 'orders') { await adminOrders(body); every(20000, () => { if (state.adminOrdersScope === 'open' && document.getElementById('sheet').hidden) adminOrders(body).catch(() => {}); }); }
    if (tab === 'products') await adminProducts(body);
    if (tab === 'couriers') await adminCouriers(body);
    if (tab === 'map') { await adminMap(body); every(10000, () => adminMap(body, true).catch(() => {})); }
    if (tab === 'customers') await adminCustomers(body);
    if (tab === 'more') await adminMore(body);
  };

  // ---------- 👥 Клиенты (как «Покупатели» в боте: оценка и заметка админа)
  async function adminCustomers(container) {
    const data = await api('GET', '/api/admin/customers');
    if (!data.customers.length) { container.innerHTML = '<div class="empty">Покупателей пока нет.</div>'; return; }
    container.innerHTML = `<input type="search" class="list-search" data-search placeholder="🔍 Имя, @логин или телефон" value="${esc(state.customerSearch || '')}">
      <div class="small muted" data-search-empty hidden>Никого не нашли.</div>` + data.customers.map((c) => `<div class="card compact" data-search-text="${esc(searchText(c.name, c.username, c.phones, c.telegram_id))}">
        <div class="row spread"><h3>${esc(c.name)}</h3><span class="row">${c.blocked ? '<span class="badge unpaid">🚫 ЧС</span>' : ''}${c.admin_rating ? `<span class="badge paid">★ ${c.admin_rating}/5</span>` : ''}</span></div>
        <div class="small muted">${c.username ? `@${esc(c.username)} · ` : ''}покупок: ${c.purchases} · 💰 ${Number(c.bonus_balance || 0).toLocaleString('ru-RU')}</div>
        <button class="btn small mt" data-cust-chat="${c.telegram_id}">💬 Чат с клиентом</button>
        ${xMore(`u${c.telegram_id}`, `
        ${c.blocked ? `<div class="note mt">🚫 <b>В чёрном списке</b>${c.blocked_at ? ' с ' + dateText(c.blocked_at) : ''} — не может оформлять заказы.${c.blocked_reason ? `<div class="small">Причина: ${esc(c.blocked_reason)}</div>` : ''}</div>` : ''}
        <dl class="kv mt">
          <dt>Telegram ID</dt><dd>${c.telegram_id}</dd>
          ${c.username ? `<dt>Username</dt><dd><a href="https://t.me/${esc(c.username)}" style="color:var(--accent)">@${esc(c.username)}</a></dd>` : ''}
          <dt>Покупок</dt><dd>${c.purchases}</dd>
          <dt>💰 Бонусы</dt><dd><b>${Number(c.bonus_balance || 0).toLocaleString('ru-RU')}</b> · ${pctText(c.bonus_percent, data.bonus_default_percent)}</dd>
          ${c.admin_note ? `<dt>Заметка</dt><dd>${esc(c.admin_note)}</dd>` : ''}
        </dl>
        ${customerReviewsBlock(c.reviews)}
        <button class="btn ghost small mt" style="width:100%" data-ubonus="${c.telegram_id}">💰 Бонусы: процент и начисление</button>
        <button class="btn ghost small mt" style="width:100%" data-urate="${c.telegram_id}">⭐ Оценка и заметка админа</button>
        ${blockButton(c.telegram_id, c.name, c.blocked)}`)}</div>`).join('');
    bindExpand(container);
    bindBlockButtons(container, () => adminCustomers(container));
    bindListSearch(container, 'customerSearch');
    container.querySelectorAll('[data-cust-chat]').forEach((b) => { b.onclick = () => { haptic('light'); go('admin-chat', { u: b.dataset.custChat }); }; });
    container.querySelectorAll('[data-ubonus]').forEach((b) => {
      const c = data.customers.find((x) => x.telegram_id === +b.dataset.ubonus);
      b.onclick = () => withBusy(b, async () => {
        const h = await api('GET', `/api/admin/customers/${c.telegram_id}/bonus`);
        const hist = bonusHistoryList(h.history);
        openSheet(`<h3>💰 Бонусы: ${esc(c.name)}</h3>
          <div class="card mt center"><div class="small muted">Остаток</div><div style="font-size:28px;font-weight:800">${Number(h.balance).toLocaleString('ru-RU')}</div></div>
          <div class="section-title">Процент с заказа</div>
          <p class="small muted">Начисляется за доставленный заказ от ${money(data.bonus_min_items_total)} — от суммы товаров по исходным ценам (подъём не считается), вниз до целого. По умолчанию — ${pctText(null, data.bonus_default_percent)}.</p>
          <label class="field">Процент этого покупателя<input type="text" id="b-pct" inputmode="decimal" placeholder="${String(data.bonus_default_percent).replace('.', ',')} (по умолчанию)" value="${c.bonus_percent != null ? String(c.bonus_percent).replace('.', ',') : ''}"></label>
          <div class="btn-pair mt"><button class="btn" data-act="pct-save">Сохранить процент</button><button class="btn ghost" data-act="pct-default" ${c.bonus_percent == null ? 'disabled' : ''}>По умолчанию</button></div>
          <div class="section-title">Начислить или списать вручную</div>
          <label class="field">Бонусы<input type="number" id="b-amt" inputmode="numeric" min="1" step="1" placeholder="Например: 500"></label>
          <label class="field mt">Причина (необязательно)<input type="text" id="b-reason" maxlength="200" placeholder="Например: компенсация за задержку"></label>
          <div class="btn-pair mt"><button class="btn ok" data-act="b-add">➕ Начислить</button><button class="btn secondary" data-act="b-sub" ${h.balance > 0 ? '' : 'disabled'}>➖ Списать</button></div>
          <div class="section-title">История</div>
          ${hist ? `<ul class="review-notes">${hist}</ul>` : '<div class="small muted">Пока пусто.</div>'}
          <button class="btn ghost mt" style="width:100%" data-act="b-close">Закрыть</button>`, (sheet) => {
          const done = (msg) => { closeSheet(); toast(msg); adminCustomers(container); };
          sheet.querySelector('[data-act=b-close]').onclick = closeSheet;
          const savePct = (percent) => withBusy(sheet.querySelector('[data-act=pct-save]'), async () => {
            const r = await api('PUT', `/api/admin/customers/${c.telegram_id}/bonus-percent`, { percent });
            done(`Процент бонусов: ${pctText(r.bonus_percent, data.bonus_default_percent)}.`);
          });
          sheet.querySelector('[data-act=pct-save]').onclick = () => {
            const v = sheet.querySelector('#b-pct').value.trim();
            savePct(v === '' ? null : v);
          };
          sheet.querySelector('[data-act=pct-default]').onclick = () => savePct(null);
          const adjust = (sign, btn) => withBusy(btn, async () => {
            const n = Math.floor(Number(sheet.querySelector('#b-amt').value));
            if (!n || n < 1) throw new Error('Введите число бонусов больше нуля.');
            const r = await api('POST', `/api/admin/customers/${c.telegram_id}/bonus`, { amount: sign * n, reason: sheet.querySelector('#b-reason').value });
            done(`${sign > 0 ? '➕ Начислено' : '➖ Списано'} ${n} 💰. Остаток: ${r.bonus_balance}.`);
          });
          sheet.querySelector('[data-act=b-add]').onclick = (e) => adjust(1, e.target);
          sheet.querySelector('[data-act=b-sub]').onclick = (e) => adjust(-1, e.target);
        });
      });
    });
    container.querySelectorAll('[data-urate]').forEach((b) => {
      const c = data.customers.find((x) => x.telegram_id === +b.dataset.urate);
      b.onclick = () => ratingSheet({
        title: `⭐ ${esc(c.name)}`,
        hint: 'Оценка покупателя от администратора и короткая заметка. Покупатель их не видит.',
        extra: `<label class="field mt">Заметка (необязательно)<textarea id="u-note" maxlength="500">${esc(c.admin_note || '')}</textarea></label>`,
        confirm: 'Сохранить',
        onSubmit: async (rating, sheet) => {
          await api('PUT', `/api/admin/customers/${c.telegram_id}/rating`, { rating, note: sheet.querySelector('#u-note').value });
          closeSheet(); toast('Оценка и заметка сохранены.'); adminCustomers(container);
        },
      });
    });
  }

  // ---------- ⚙️ Ещё: фото каталога, QR оплаты, связь с каналом, массовые действия
  function pickImage(onPicked) {
    const input = document.createElement('input');
    input.type = 'file'; input.accept = 'image/*';
    input.onchange = async () => {
      try { await onPicked(await compressImage(input.files[0], 1600, 0.92)); } catch (e) { toast(e.message, true); }
    };
    input.click();
  }
  function dangerSheet({ title, text, confirm, path, done }) {
    openSheet(`<h3>${title}</h3><p class="small muted">${text}</p>
      <label class="field mt">Пароль для подтверждения (тот же, что в боте)<input type="password" id="danger-pass" autocomplete="off"></label>
      <div class="stack mt"><button class="btn danger" data-act="danger-ok">${confirm}</button><button class="btn ghost" data-act="danger-cancel">Отмена</button></div>`, (sheet) => {
      sheet.querySelector('[data-act=danger-cancel]').onclick = closeSheet;
      sheet.querySelector('[data-act=danger-ok]').onclick = (e) => withBusy(e.target, async () => {
        const res = await api('POST', path, { password: sheet.querySelector('#danger-pass').value });
        closeSheet(); done(res);
      });
    });
  }
  async function adminMore(container) {
    const [st, pq, adm] = await Promise.all([api('GET', '/api/admin/settings'), api('GET', '/api/admin/payment-qr'), api('GET', '/api/admin/admins').catch(() => null)]);
    const imgCard = (key, title, url, emptyText) => `<div class="card">
        <div class="row spread"><h3>${title}</h3><span class="badge ${url ? 'paid' : 'unpaid'}">${url ? 'задано' : 'не задано'}</span></div>
        ${url ? `<img class="admin-img mt" src="${esc(url)}" alt="" onerror="this.remove()">` : `<div class="small muted mt">${emptyText}</div>`}
        <div class="row mt"><button class="btn small grow" data-img="${key}">📤 ${url ? 'Заменить' : 'Загрузить'}</button>${url ? `<button class="btn small secondary" data-imgdel="${key}">🗑 Убрать</button>` : ''}</div></div>`;
    const sw = st.payment_switches || { sbp: true, crypto: true };
    const hasQr = (k) => pq.codes.some((c) => c.kind === k);
    const switchRow = (k, label) => `<label class="switch-row">
        <span><b>${label}</b><div class="small muted">${!sw[k] ? 'Выключено — покупатели не видят этот способ.' : (hasQr(k) ? 'Включено — покупатели могут выбрать.' : 'Включено, но нет QR — способ скрыт, пока не добавите QR ниже.')}</div></span>
        <span class="switch"><input type="checkbox" data-switch="${k}" ${sw[k] ? 'checked' : ''}><i></i></span></label>`;
    const ann = st.announcement;
    container.innerHTML = `
      <div class="section-title">🎚 Переключатели</div>
      <div class="card">
        ${switchRow('sbp', '📱 Оплата по СБП')}
        ${switchRow('crypto', '💎 Оплата криптовалютой')}
        <div class="small muted mt">💵 Наличные доступны всегда. Переключатели действуют сразу для всех покупателей; уже оформленные заказы не меняются.</div>
      </div>
      <div class="section-title">📢 Объявление на главном экране</div>
      <div class="card">
        <div class="row spread"><span class="small muted">Видят все покупатели в меню магазина, в красной рамке.</span><span class="badge ${ann ? 'paid' : 'unpaid'}">${ann ? 'показывается' : 'нет'}</span></div>
        ${ann ? `<div class="mt">${announceBox(ann)}</div>` : ''}
        <label class="field mt">Текст объявления<textarea id="ann-text" maxlength="500" placeholder="Например: Внимание, магазин может работать некорректно — ведутся технические работы.">${esc(ann ? ann.text : '')}</textarea></label>
        <div class="btn-pair mt"><button class="btn" data-act="ann-save">📢 ${ann ? 'Обновить' : 'Опубликовать'}</button><button class="btn secondary" data-act="ann-del" ${ann ? '' : 'disabled'}>🗑 Убрать</button></div>
      </div>
      ${adm ? `<div class="section-title">👮 Администраторы · ${adm.admins.length} из ${adm.max}</div>
      <div class="card">
        ${adm.admins.map((a) => `<div class="row spread mt"><div><b>${a.name ? esc(a.name) : 'ещё не заходил в приложение'}</b>${a.me ? ' <span class="badge paid">это вы</span>' : ''}
          <div class="small muted">ID ${a.telegram_id}${a.username ? ' · @' + esc(a.username) : ''}</div></div><span class="small muted">действий: ${a.actions}</span></div>`).join('')}
        <div class="small muted mt">Добавить или убрать админа — в Amvera, переменная ADMIN_ID: Telegram ID через запятую, до ${adm.max}, затем «Перезапустить». Кто кому начислял или списывал бонусы — в выгрузке Excel, лист «Администраторы».</div>
      </div>` : ''}
      <div class="section-title">🗺 База адресов (OpenStreetMap)</div>
      <div class="card">
        <div class="small muted">Подсказки улиц, адрес по точке на карте и «Москва или нет». Сама база не обновляется — только по кнопке «🛠 Обновить карты»: GitHub собирает её из свежего OpenStreetMap (6–10 минут), сервер скачивает сам. Данные © участники OpenStreetMap, ODbL.</div>
        <button class="btn small mt" style="width:100%" data-act="addr-rebuild">🛠 Обновить карты</button>
        <div class="btn-pair mt"><button class="btn ghost small" data-act="addr-check">🔎 Состояние</button><button class="btn ghost small" data-act="addr-update">⬇️ Скачать готовую</button></div>
        <div class="small mt" data-addr-check></div>
      </div>
      <div class="section-title">👋 Приветствие в шапке магазина</div>
      <div class="card">
        <div class="small muted">Текст под «Привет, имя!» на главном экране. Пусто — стандартный: «${esc(st.home_greeting_default || '')}»</div>
        <label class="field mt">Текст приветствия<textarea id="greet-text" maxlength="300" placeholder="${esc(st.home_greeting_default || '')}">${esc(st.home_greeting || '')}</textarea></label>
        <div class="btn-pair mt"><button class="btn" data-act="greet-save">💾 Сохранить</button><button class="btn secondary" data-act="greet-reset" ${st.home_greeting ? '' : 'disabled'}>↩️ Стандартный</button></div>
      </div>
      <div class="section-title">📊 Выгрузка в Excel</div>
      <div class="card">
        <div class="small muted">Полная выгрузка всего, что было в магазине: лист «Продажи» (бухгалтерия — доставленные заказы), «Все заказы» любого статуса, «Клиенты» (контакты, что заказывали, адреса, рейтинг, комментарии курьеров), «Курьеры» (рейтинг, что, когда и куда возили) и «Крипта». Нужное выбирайте фильтрами в таблице.</div>
        <label class="field mt">Пароль выгрузки<input type="password" id="exp-pass" autocomplete="off"></label>
        <div class="btn-pair mt"><button class="btn" data-act="exp-download">⬇️ Скачать</button><button class="btn" data-act="exp-chat">📨 В чат</button></div>
        <button class="btn btn-burgundy mt" style="width:100%" data-act="exp-reset">⬇️🗑 Скачать и обнулить</button>
        <div class="small muted mt">Таблица №2: скачивается заполненная, затем начинается с чистого листа (остаются покупатели, партнёры, курьеры, админы и накопленные баллы; у курьеров итоги с нуля). Копия файла придёт и в чат с ботом. Таблица «⬇️ Скачать» ведётся постоянно, не обнуляется и отмечает периоды. Пароль тот же.</div>
        <div class="small muted mt">Строка — товар или услуга из доставленного заказа, по времени размещения. Сверху — итог по выборке и итог по всему; оплаты криптой — с суммой в монете и курсом на момент выгрузки.</div>
      </div>
      <div class="section-title">Картинки</div>
      ${imgCard('catalog', '🖼 Фото шапки каталога', st.catalog_photo, 'Сейчас каталог без шапки.')}
      <div class="section-title">💳 QR для оплаты при курьере</div>
      <div class="card">
        <div class="small muted">Покупатель выбирает валюту из этих QR при оформлении, а курьер показывает выбранный QR на месте («💳 Получить оплату»). Пока нет ни одного QR вида — этот способ оплаты скрыт у покупателей.</div>
        ${[['sbp', '📱 СБП'], ['crypto', '💎 Криптовалюта']].map(([k, l]) => {
          const codes = pq.codes.filter((c) => c.kind === k);
          return `<div class="section-title">${l}</div>${codes.length ? codes.map((c) => `<div class="row spread mt"><div class="row"><img class="qr-thumb" src="${esc(c.image)}" alt="" onerror="this.remove()"><div><b>${esc(c.title)}</b>${c.currency ? `<div class="small muted">${esc(c.currency_label || c.currency)}</div>` : ''}</div></div><button class="btn small secondary" data-qrdel="${c.id}">🗑</button></div>`).join('') : '<div class="small muted mt">Пока нет — способ скрыт у покупателей.</div>'}`;
        }).join('')}
        <button class="btn mt" style="width:100%" data-act="qr-add">➕ Добавить QR-код</button>
      </div>
      <div class="section-title">Связь</div>
      <div class="card"><h3>📡 Тест связи с каналом</h3><div class="small muted mt">Бот отправит проверочное сообщение в лог-канал (LOG_GROUP_CHAT_ID).</div>
        <button class="btn ghost small mt" style="width:100%" data-act="test-log">Отправить тестовое сообщение</button></div>
      <div class="section-title">⚠️ Опасная зона</div>
      <div class="card stack">
        <button class="btn danger" data-act="wipe-products">❌ Удалить все товары и описания</button>
        <button class="btn danger" data-act="wipe-orders">❌ Очистить всю историю заказов</button>
        <div class="small muted">Эти действия нельзя отменить.</div>
      </div>`;
    const reload = () => adminMore(container);
    const exportBody = (deliver, kind = 'all') => ({
      kind, deliver,
      password: container.querySelector('#exp-pass').value,
    });
    const summaryText = (s) => `Доставок: ${s.deliveries ?? '—'}, баллонов: ${s.quantity}, подъёмов: ${s.lifts ?? 0}, сумма: ${money(s.total)}${s.unfinished ? ` · не завершено заказов: ${s.unfinished}` : ''}`;
    const sendToChat = async () => {
      const res = await api('POST', '/api/admin/export', exportBody('chat'));
      toast(`📨 Файл отправлен в чат с ботом.\n${summaryText(res.summary)}`, false, 5000);
    };
    container.querySelectorAll('[data-switch]').forEach((inp) => {
      inp.onchange = async () => {
        const method = inp.dataset.switch, enabled = inp.checked;
        inp.disabled = true;
        try {
          await api('PUT', '/api/admin/payment-switch', { method, enabled });
          haptic('light');
          toast(`${method === 'sbp' ? '📱 СБП' : '💎 Криптовалюта'}: ${enabled ? 'включено' : 'выключено'} для всех покупателей.`);
          adminMore(container);
        } catch (e) { inp.checked = !enabled; inp.disabled = false; toast(e.message, true); }
      };
    });
    const showAddr = (r) => {
      container.querySelector('[data-addr-check]').innerHTML = `<div class="note ${r.ok ? 'ok' : 'warn'}">${r.ok ? '✅' : '⚠️'} ${esc(r.message)}</div>`;
    };
    container.querySelector('[data-act=addr-check]').onclick = (e) => withBusy(e.target, async () => showAddr(await api('GET', '/api/admin/address-check')));
    container.querySelector('[data-act=addr-rebuild]').onclick = async (e) => {
      if (!(await confirmBox('Собрать карты заново из свежего OpenStreetMap? Это займёт 6–10 минут, приложение всё это время работает как обычно.'))) return;
      withBusy(e.target, async () => { showAddr(await api('POST', '/api/admin/address-rebuild')); toast('🛠 Сборка запущена — база обновится сама.'); });
    };
    container.querySelector('[data-act=addr-update]').onclick = (e) => withBusy(e.target, async () => {
      showAddr(await api('POST', '/api/admin/address-update'));
      toast('🔄 База адресов обновлена.');
    });
    const saveGreeting = (btn, text) => withBusy(btn, async () => {
      await api('PUT', '/api/admin/home-greeting', { text });
      if (state.me) state.me.greeting = text || null;
      toast(text ? '👋 Приветствие сохранено.' : '👋 Вернули стандартное приветствие.');
      adminMore(container);
    });
    container.querySelector('[data-act=greet-save]').onclick = (e) => saveGreeting(e.target, container.querySelector('#greet-text').value.trim());
    container.querySelector('[data-act=greet-reset]').onclick = (e) => saveGreeting(e.target, '');
    container.querySelector('[data-act=ann-save]').onclick = (e) => withBusy(e.target, async () => {
      const text = container.querySelector('#ann-text').value.trim();
      if (!text) throw new Error('Введите текст объявления или нажмите «Убрать».');
      await api('PUT', '/api/admin/announcement', { text });
      toast('📢 Объявление опубликовано — его видят все покупатели.');
      adminMore(container);
    });
    container.querySelector('[data-act=ann-del]').onclick = async (e) => {
      if (!(await confirmBox('Убрать объявление с главного экрана?'))) return;
      withBusy(e.target, async () => { await api('PUT', '/api/admin/announcement', { text: '' }); toast('Объявление убрано.'); adminMore(container); });
    };
    container.querySelector('[data-act=exp-reset]').onclick = async (e) => {
      if (!container.querySelector('#exp-pass').value) { toast('Введите пароль выгрузки.', true); return; }
      if (!(await confirmBox('⬇️🗑 Скачать таблицу №2 и обнулить её?\n\nФайл скачается заполненным (копия — в чат с ботом), дальше таблица №2 начнётся с чистого листа. Покупатели, партнёры, курьеры и админы с накопленными баллами остаются. Постоянная таблица «Скачать» и данные приложения не меняются.'))) return;
      withBusy(e.target, async () => {
        const res = await api('POST', '/api/admin/export', exportBody('download', 'reset'));
        toast(`🗑 Таблица №2 обнулена.${res.summary.backup_sent ? ' Копия файла отправлена в чат с ботом.' : ' ⚠️ Копию в чат отправить не удалось — сохраните файл сейчас.'}\n${summaryText(res.summary)}`, false, 6000);
        container.querySelector('#exp-pass').value = '';
        const a = document.createElement('a');
        a.href = res.url; a.download = res.file_name; document.body.appendChild(a); a.click(); a.remove();
      });
    };
    container.querySelector('[data-act=exp-chat]').onclick = (e) => withBusy(e.target, sendToChat);
    container.querySelector('[data-act=exp-download]').onclick = (e) => withBusy(e.target, async () => {
      const canDownload = tg && tg.initData && tg.downloadFile && tg.isVersionAtLeast && tg.isVersionAtLeast('8.0');
      if (tg && tg.initData && !canDownload) {
        // Старый Telegram без скачивания из мини-приложений — присылаем файл в чат.
        await sendToChat();
        return;
      }
      const res = await api('POST', '/api/admin/export', exportBody('download'));
      const url = location.origin + res.url;
      if (canDownload) {
        tg.downloadFile({ url, file_name: res.file_name });
      } else {
        const a = document.createElement('a');
        a.href = url; a.download = res.file_name; document.body.appendChild(a); a.click(); a.remove();
      }
      toast(summaryText(res.summary), false, 5000);
    });
    const target = { catalog: '/api/admin/catalog-photo', sbp: '/api/admin/payment-qr/sbp', crypto: '/api/admin/payment-qr/crypto' };
    container.querySelectorAll('[data-qrdel]').forEach((b) => {
      b.onclick = async () => {
        const c = pq.codes.find((x) => x.id === +b.dataset.qrdel);
        if (!(await confirmBox(`Убрать QR «${c.title}»? Курьеры перестанут его видеть.`))) return;
        withBusy(b, async () => { await api('DELETE', `/api/admin/payment-qr/${c.id}`); toast('QR убран.'); reload(); });
      };
    });
    container.querySelector('[data-act=qr-add]').onclick = () => {
      const f = { kind: 'sbp', image: null, currency: (pq.currencies[0] || {}).code };
      const nets = () => ((pq.currencies.find((c) => c.code === f.currency) || {}).networks || []);
      const draw = (sheet) => {
        if (!nets().includes(f.network)) f.network = nets()[0];
        sheet.innerHTML = `<div class="inner"><div class="grabber"></div><h3>➕ Новый QR-код</h3>
          <div class="seg mt"><button data-qk="sbp" class="${f.kind === 'sbp' ? 'on' : ''}">📱 СБП</button><button data-qk="crypto" class="${f.kind === 'crypto' ? 'on' : ''}">💎 Криптовалюта</button></div>
          ${f.kind === 'sbp'
            ? `<label class="field mt">Банк (необязательно — если QR СБП несколько)<input type="text" id="qr-title" maxlength="60" placeholder="Например: СБП — Сбер" value="${esc(f.title || '')}"></label>`
            : `<label class="field mt">Криптовалюта<select id="qr-cur">${pq.currencies.map((c) => `<option value="${c.code}" ${f.currency === c.code ? 'selected' : ''}>${esc(c.label)}</option>`).join('')}</select></label>
          <label class="field mt">Сеть<select id="qr-net">${nets().map((n) => `<option value="${esc(n)}" ${f.network === n ? 'selected' : ''}>${esc(n)}</option>`).join('')}</select></label>
          <div class="small muted mt">Курьеры увидят: <b>${esc(f.currency)} · ${esc(f.network || nets()[0] || '')}</b></div>`}
          <button class="btn ghost mt" style="width:100%" data-act="qr-pick">${f.image ? '✅ Картинка выбрана — заменить' : '🖼 Выбрать картинку QR-кода'}</button>
          ${f.image ? `<img class="admin-img mt" src="${f.image}" alt="">` : ''}
          <div class="stack mt"><button class="btn" data-act="qr-save">Сохранить</button><button class="btn ghost" data-act="qr-cancel">Отмена</button></div></div>`;
        const keep = () => {
          const t = sheet.querySelector('#qr-title'); if (t) f.title = t.value;
          const cur = sheet.querySelector('#qr-cur'); if (cur) f.currency = cur.value;
          const net = sheet.querySelector('#qr-net'); if (net) f.network = net.value;
        };
        const curSel = sheet.querySelector('#qr-cur');
        if (curSel) curSel.onchange = () => { keep(); f.network = null; draw(sheet); };
        const netSel = sheet.querySelector('#qr-net');
        if (netSel) netSel.onchange = () => { keep(); draw(sheet); };
        sheet.querySelectorAll('[data-qk]').forEach((b) => { b.onclick = () => { keep(); f.kind = b.dataset.qk; draw(sheet); }; });
        sheet.querySelector('[data-act=qr-pick]').onclick = () => { keep(); pickImage((url) => { f.image = url; draw(sheet); }); };
        sheet.querySelector('[data-act=qr-cancel]').onclick = closeSheet;
        sheet.querySelector('[data-act=qr-save]').onclick = (e) => withBusy(e.target, async () => {
          keep();
          if (!f.image) throw new Error('Выберите картинку QR-кода.');
          await api('POST', '/api/admin/payment-qr', { kind: f.kind, title: f.kind === 'sbp' ? f.title : '', currency: f.currency, network: f.network, image: f.image });
          closeSheet(); toast('QR-код добавлен — курьеры уже его видят.'); reload();
        });
      };
      openSheet('', (sheet) => draw(sheet));
    };
    container.querySelectorAll('[data-img]').forEach((b) => {
      b.onclick = () => pickImage((dataUrl) => withBusy(b, async () => { await api('PUT', target[b.dataset.img], { image: dataUrl }); toast('Картинка сохранена.'); reload(); }));
    });
    container.querySelectorAll('[data-imgdel]').forEach((b) => {
      b.onclick = async () => {
        if (!(await confirmBox(b.dataset.imgdel === 'catalog' ? 'Убрать фото шапки каталога?' : 'Убрать QR-код? Этот способ оплаты скроется у покупателей.'))) return;
        withBusy(b, async () => { await api('PUT', target[b.dataset.imgdel], { image: null }); toast('Убрано.'); reload(); });
      };
    });
    const q = (sel) => container.querySelector(sel);
    q('[data-act=test-log]').onclick = (e) => withBusy(e.target, async () => { await api('POST', '/api/admin/test-log'); toast('Отправлено ✅'); });
    q('[data-act=wipe-products]').onclick = () => dangerSheet({
      title: '❌ Удалить ВСЕ товары и описания?',
      text: 'Будут безвозвратно удалены все товары (включая архивные), их описания, цены, фото и бонусы, а также все корзины покупателей. История оформленных заказов не изменится. Отменить нельзя.',
      confirm: '☠️ Удалить все товары', path: '/api/admin/wipe-products',
      done: (r) => toast(r.deleted ? '☠️ Готово. Все товары удалены, корзины очищены.' : 'Товаров и так не было.'),
    });
    q('[data-act=wipe-orders]').onclick = () => dangerSheet({
      title: '❌ Очистить ВСЮ историю заказов?',
      text: 'Будут безвозвратно удалены все заказы — у всех покупателей, курьеров и администратора — вместе с оценками курьеров и сохранёнными данными «последнего заказа». Товары, курьеры и покупатели не затрагиваются. Отменить нельзя.',
      confirm: '☠️ Очистить историю', path: '/api/admin/wipe-orders',
      done: (r) => toast(r.deleted ? '☠️ Готово. История заказов удалена.' : 'Заказов и так не было.'),
    });
  }

  // ------------------------------------------------------------ DEVELOPER (theme editor)
  routes.dev = async () => {
    setTitle('Разработчик');
    if (!state.me.roles.admin) { go('home'); return; }
    if (!state.devPass) {  // 01.11.2026: раздел закрыт паролем; пока он не задан, первый ввод его создаёт
      view.innerHTML = `<div class="stack"><div class="card"><h3>🔒 Раздел защищён паролем</h3>
        <div class="small muted mt">Если пароль ещё не задан — придумайте его сейчас (от 6 символов): первый ввод станет паролем.</div>
        <label class="field mt">Пароль<input type="password" data-dev-pw autocomplete="current-password" maxlength="64"></label>
        <button class="btn mt" data-dev-go>Войти</button></div></div>`;
      const inp = view.querySelector('[data-dev-pw]');
      const btn = view.querySelector('[data-dev-go]');
      const submit = () => withBusy(btn, async () => {
        const r = await api('POST', '/api/admin/dev/unlock', { password: inp.value });
        state.devPass = inp.value;
        toast(r.created ? '🔒 Пароль задан. Запомните его.' : '🔓 Открыто');
        render();
      });
      btn.onclick = submit;
      inp.onkeydown = (e) => { if (e.key === 'Enter') submit(); };
      return;
    }
    const saved = Object.assign({}, DEFAULT_THEME, state.me.theme);
    const t = JSON.parse(JSON.stringify(saved));
    const names = { catalog: '📦 Каталог', cart: '🛒 Корзина', orders_active: '🚚 Активные заказы', orders_history: '📋 История', call_admin: '💬 Чат с RelaxGas' };
    const opt = (key, values) => values.map(([v, l]) => `<button data-k="${key}" data-v="${v}" class="${t[key] === v ? 'on' : ''}">${l}</button>`).join('');
    const draw = () => {
      applyTheme(t);
      view.innerHTML = `<div class="stack">
        <div class="note info">Изменения видны сразу (только у вас). Для всех пользователей они применятся после «Сохранить».</div>
        <div class="section-title">Цвета</div>
        <div class="swatches">
          <label class="swatch">Фон<input type="color" data-color="bg_color" value="${t.bg_color}"></label>
          <label class="swatch">Градиент<input type="color" data-color="bg_gradient_to" value="${t.bg_gradient_to}"></label>
          <label class="swatch">Акцент<input type="color" data-color="accent_color" value="${t.accent_color}"></label>
        </div>
        <div class="section-title">Форма кнопок</div><div class="seg">${opt('button_style', [['rounded', 'Скруглённые'], ['pill', 'Таблетка'], ['square', 'Квадратные']])}</div>
        <div class="section-title">Заливка кнопок</div><div class="seg">${opt('button_fill', [['lux', 'Премиум'], ['solid', 'Сплошная'], ['outline', 'Контур'], ['glass', 'Стекло']])}</div>
        <div class="section-title">Анимация нажатия</div><div class="seg">${opt('button_animation', [['premium', 'Премиум'], ['bounce', 'Пружина'], ['none', 'Без']])}</div>
        <div class="row"><button class="btn">Пример кнопки</button><button class="btn ghost">Вторичная</button></div>
        <div class="section-title">Порядок пунктов меню</div>
        <div class="order-list">${t.menu_order.map((k, i) => `<div class="item"><span>${names[k]}</span><button data-up="${i}" ${i === 0 ? 'disabled' : ''}>↑</button><button data-down="${i}" ${i === t.menu_order.length - 1 ? 'disabled' : ''}>↓</button></div>`).join('')}</div>
        <div class="section-title">Фоновая картинка</div>
        <label class="btn ghost" for="bg-file">🖼 ${t.bg_image_data_url ? 'Заменить картинку' : 'Загрузить картинку'}</label><input type="file" id="bg-file" accept="image/*" hidden>
        ${t.bg_image_data_url ? `<label class="field">Положение по горизонтали: ${t.bg_image_pos_x}%<input type="range" min="0" max="100" data-pos="bg_image_pos_x" value="${t.bg_image_pos_x}"></label>
          <label class="field">Положение по вертикали: ${t.bg_image_pos_y}%<input type="range" min="0" max="100" data-pos="bg_image_pos_y" value="${t.bg_image_pos_y}"></label>
          <button class="btn secondary" data-act="rm-bg">Убрать картинку</button>` : ''}
        <button class="btn secondary" data-act="reset">↺ Вернуть стандартную тему</button>
      </div>
      <div class="bottom-bar"><div class="inner"><button class="btn" data-act="save">💾 Сохранить для всех</button></div></div>`;
      view.querySelectorAll('[data-color]').forEach((inp) => { inp.oninput = () => { t[inp.dataset.color] = inp.value; applyTheme(t); }; });
      view.querySelectorAll('[data-k]').forEach((b) => { b.onclick = () => { t[b.dataset.k] = b.dataset.v; draw(); }; });
      view.querySelectorAll('[data-up]').forEach((b) => { b.onclick = () => { const i = +b.dataset.up; [t.menu_order[i - 1], t.menu_order[i]] = [t.menu_order[i], t.menu_order[i - 1]]; draw(); }; });
      view.querySelectorAll('[data-down]').forEach((b) => { b.onclick = () => { const i = +b.dataset.down; [t.menu_order[i + 1], t.menu_order[i]] = [t.menu_order[i], t.menu_order[i + 1]]; draw(); }; });
      view.querySelectorAll('[data-pos]').forEach((r) => {
        r.oninput = () => { t[r.dataset.pos] = +r.value; r.parentNode.firstChild.textContent = `${r.dataset.pos === 'bg_image_pos_x' ? 'Положение по горизонтали' : 'Положение по вертикали'}: ${r.value}%`; applyTheme(t); };
      });
      view.querySelector('#bg-file').onchange = async (e) => {
        try { t.bg_image_data_url = await compressImage(e.target.files[0], 1400, 0.8); draw(); } catch (err) { toast(err.message, true); }
      };
      const rm = view.querySelector('[data-act=rm-bg]');
      if (rm) rm.onclick = () => { t.bg_image_data_url = null; draw(); };
      view.querySelector('[data-act=reset]').onclick = () => { Object.assign(t, JSON.parse(JSON.stringify(DEFAULT_THEME))); draw(); };
      view.querySelector('[data-act=save]').onclick = (e) => withBusy(e.target, async () => {
        const res = await api('PUT', '/api/admin/theme', { theme: t, dev_password: state.devPass });
        state.me.theme = res.theme; Object.assign(saved, res.theme);
        haptic('success'); toast('Тема сохранена для всех пользователей.');
      });
    };
    draw();
    // Уход с экрана без сохранения — вернуть сохранённую тему.
    const restore = () => { if (currentRoute().name !== 'dev') { applyTheme(state.me.theme); window.removeEventListener('hashchange', restore); } };
    window.addEventListener('hashchange', restore);
  };

  // ---- Партнёрская ссылка обновлена (30.10.2026): красное окно, пока партнёр не подтвердит рассылку.
  async function copyPlain(text, okMsg) {
    try { await navigator.clipboard.writeText(text); toast(okMsg); } catch (e) { toast('Не удалось скопировать — выделите текст и скопируйте вручную.', true); }
  }
  async function openPartnerClients() {
    let res;
    try { res = await api('GET', '/api/partner/clients'); } catch (e) { toast(e.message || 'Не удалось получить список', true); return; }
    let box = document.getElementById('clients-gate');
    if (!box) { box = document.createElement('div'); box.id = 'clients-gate'; box.className = 'gate'; box.style.zIndex = 1001; document.body.appendChild(box); }
    box.innerHTML = `<div class="card stack">
        <h3>👥 Мои покупатели · ${res.count}</h3>
        ${res.count ? `<textarea readonly rows="10" style="width:100%;font:15px/1.5 var(--font)">${esc(res.text)}</textarea>
          <div class="small muted">Выделите текст и скопируйте, затем разошлите им вашу новую ссылку.</div>
          <button class="btn ghost" data-act="cl-copy">📋 Скопировать список</button>` : '<div class="small muted">Пока никто не закреплён за вашей ссылкой.</div>'}
        <button class="btn ok" data-act="cl-done">Готово</button>
      </div>`;
    box.hidden = false;
    const cp = box.querySelector('[data-act=cl-copy]');
    if (cp) cp.onclick = () => copyPlain(res.text, '📋 Список скопирован.');
    box.querySelector('[data-act=cl-done]').onclick = () => { box.hidden = true; box.innerHTML = ''; };
  }
  async function showLinkGate() {
    const need = !!(state.me && state.me.partner && state.me.partner.link_notice);
    let box = document.getElementById('link-gate');
    if (!need) { if (box) { box.hidden = true; box.innerHTML = ''; } return; }
    if (box && !box.hidden) return;
    let link = '';
    try { const d = await api('GET', '/api/referral?view=customer'); link = (d.partner && d.partner.link) || ''; } catch (e) { return; }
    if (!box) { box = document.createElement('div'); box.id = 'link-gate'; box.className = 'gate link-gate'; document.body.appendChild(box); }
    box.innerHTML = `<div class="card stack center">
        <div style="font-size:44px">🔗</div>
        <h3>Ваша реферальная ссылка обновлена</h3>
        <div class="small">Пожалуйста, разошлите её своим действующим покупателям и новым.</div>
        <div class="note" style="word-break:break-all">${esc(link)}</div>
        <button class="btn" data-act="lg-copy">📋 Скопировать</button>
        <button class="btn ghost" data-act="lg-clients">👥 Мои покупатели</button>
        <button class="btn ok" data-act="lg-done">Я разослал(а) ссылку</button>
      </div>`;
    box.hidden = false;
    if (!link) { box.querySelector('.note').textContent = 'Ссылка пока недоступна — закройте и откройте приложение заново.'; }
    box.querySelector('[data-act=lg-copy]').onclick = () => copyPlain(link, '📋 Ссылка скопирована.');
    box.querySelector('[data-act=lg-clients]').onclick = openPartnerClients;
    box.querySelector('[data-act=lg-done]').onclick = async () => {
      try { await api('POST', '/api/partner/link-ack'); } catch (e) { toast(e.message || 'Не получилось', true); return; }
      state.me.partner.link_notice = false;
      box.hidden = true; box.innerHTML = '';
    };
  }

  // ------------------------------------------------------------ boot
  async function loadMe() {
    state.me = await api('GET', '/api/me');
    if (state.mode === undefined || state.mode === null) {
      let m = null;
      try { m = sessionStorage.getItem(MODE_KEY) || null; } catch (e) { /* приватный режим */ }
      state.mode = m || state.me.mode || null; // последний выбор хранится на сервере
    }
    applyTheme(state.me.theme);
    setCartCount(state.me.cart_count);
    // Заказ доставлен курьером, но не оценён — окно оценки на любом экране.
    showRatingGate(state.me.pending_rating || null);
    showLinkGate();
  }

  async function setupDevBar() {
    // Только локальный dev_server.py: вне Telegram даёт выбрать роль.
    let info;
    try { info = await (await fetch('/api/dev/info')).json(); } catch (e) { return false; }
    if (!info || !info.dev) return false;
    state.dev = info;
    const saved = Number(sessionStorage.getItem('devUser')) || info.users[0].id;
    state.devUser = saved;
    const bar = document.getElementById('dev-bar');
    const drawBar = () => {
      bar.innerHTML = `<b>DEV</b> вход как:${info.users.map((u) => `<button data-u="${u.id}" class="${u.id === state.devUser ? 'on' : ''}">${esc(u.label)}</button>`).join('')}`;
      bar.querySelectorAll('[data-u]').forEach((b) => {
        b.onclick = async () => {
          state.devUser = Number(b.dataset.u);
          try { sessionStorage.setItem('devUser', String(state.devUser)); } catch (e) { /* приватный режим */ }
          drawBar(); setMode(null); await loadMe(); go('home');
        };
      });
    };
    bar.hidden = false;
    drawBar();
    return true;
  }

  async function boot() {
    if (tg) {
      try { tg.ready(); tg.expand(); } catch (e) { /* */ }
      try { tg.BackButton.onClick(goBack); } catch (e) { /* */ }
    }
    backBtn.onclick = goBack;
    homeBtn.onclick = () => { haptic('light'); go('home'); };
    cartPill.onclick = () => go('cart');
    if (!(tg && tg.initData)) {
      const isDev = await setupDevBar();
      if (!isDev) {
        view.innerHTML = '<div class="empty"><div class="big">📱</div>Откройте приложение из Telegram — кнопкой «🖥 Открыть приложение» в боте.</div>';
        return;
      }
    }
    try {
      await loadMe();
    } catch (e) {
      view.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
      return;
    }
    window.addEventListener('hashchange', render);
    startBadgePolling();
    render();
  }

  boot();
})();
