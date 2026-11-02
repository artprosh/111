Warning: truncated output (original token count: 107225)
Total output lines: 5162

/* Relax Gas — web client shared by Telegram and the standalone mobile app.
   Data uses /api; standalone sessions are held in a Secure HttpOnly cookie. */
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
        ${!tg ? '<button class="btn ghost" data-signout>Выйти из аккаунта</button>' : ''}
        ${APP_VERSION ? `<div class="small muted center app-version">версия ${esc(APP_VERSION)}</div>` : ''}
      </div>`;
    view.querySelectorAll('[data-go]').forEach((b) => { b.onclick = () => { haptic('light'); go(b.dataset.go); }; });
    view.querySelectorAll('[data-mode]').forEach((b) => { b.onclick = () => switchMode(b.dataset.mode); });
    const signout = view.querySelector('[data-signout]');
    if (signout) signout.onclick = async () => {
      try { await fetch('/api/auth/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); } catch (e) { /* logout locally even if server is offline */ }
      window.location.reload();
    };
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
      if (a.kind === 'transfer') return `<button class="btn ok small" data-al="transfer" data-id="${a.id}" data-yes="1">✅ Подтвердить</button><button class="btn ghost small…77225 tokens truncated…api/admin/couriers'), api('GET', '/api/admin/settings')]);
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

  async function showStandaloneLogin() {
    view.innerHTML = `<section class="card" style="max-width:440px;margin:8vh auto;padding:22px">
      <h2 style="margin-top:0">Вход в Relax Gas</h2>
      <p class="muted">Введите телефон. Мы отправим одноразовый код по SMS.</p>
      <label class="field">Телефон<input id="login-phone" type="tel" autocomplete="tel" inputmode="tel" placeholder="+7 900 000-00-00"></label>
      <button class="btn mt" id="login-send">Получить код</button>
      <div id="login-code-area" hidden>
        <label class="field mt">Код из SMS<input id="login-code" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000"></label>
        <label class="field mt">Ваше имя <span class="muted">(только при первом входе)</span><input id="login-name" type="text" autocomplete="given-name" maxlength="64" placeholder="Имя"></label>
        <button class="btn mt" id="login-confirm">Войти</button>
        <button class="btn ghost mt" id="login-resend">Отправить код ещё раз</button>
      </div>
      <div class="small muted mt">Продолжая, вы подтверждаете, что этот номер принадлежит вам.</div>
    </section>`;
    const phone = view.querySelector('#login-phone');
    const codeArea = view.querySelector('#login-code-area');
    const send = view.querySelector('#login-send');
    const resend = view.querySelector('#login-resend');
    const confirm = view.querySelector('#login-confirm');
    const post = async (path, data) => {
      const response = await fetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin', body: JSON.stringify(data),
      });
      let result = {};
      try { result = await response.json(); } catch (e) { /* пустой ответ */ }
      if (!response.ok) throw new Error(result.error || `Ошибка ${response.status}`);
      return result;
    };
    const requestCode = async (button) => {
      button.disabled = true;
      try {
        await post('/api/auth/request-code', { phone: phone.value });
        codeArea.hidden = false;
        view.querySelector('#login-code').focus();
        toast('Код отправлен по SMS.');
      } catch (e) {
        toast(e.message || 'Не удалось отправить код.', true);
      } finally {
        setTimeout(() => { if (button.isConnected) button.disabled = false; }, 30000);
      }
    };
    send.onclick = () => requestCode(send);
    resend.onclick = () => requestCode(resend);
    confirm.onclick = async () => {
      confirm.disabled = true;
      try {
        await post('/api/auth/verify-code', {
          phone: phone.value,
          code: view.querySelector('#login-code').value,
          name: view.querySelector('#login-name').value,
        });
        await loadMe();
        startAuthenticatedApp();
      } catch (e) {
        toast(e.message || 'Не удалось войти.', true);
      } finally {
        if (confirm.isConnected) confirm.disabled = false;
      }
    };
  }

  function startAuthenticatedApp() {
    window.addEventListener('hashchange', render);
    startBadgePolling();
    render();
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
        try {
          await loadMe();
        } catch (e) {
          if (e.status === 401) { await showStandaloneLogin(); return; }
          view.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
          return;
        }
        startAuthenticatedApp();
        return;
      }
    }
    try {
      await loadMe();
    } catch (e) {
      view.innerHTML = `<div class="empty"><div class="big">⚠️</div>${esc(e.message)}</div>`;
      return;
    }
    startAuthenticatedApp();
  }

  boot();
})();
