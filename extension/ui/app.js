/* UI/API layer for Ozon Price Tracker. No third-party scripts are loaded. */
'use strict';

const API_BASE = 'http://127.0.0.1:8765';
const TOKEN_KEY = 'accessToken';
const TEST_FIXTURE_PREFIX = '[ТЕСТОВЫЕ ДАННЫЕ]';
const PRODUCT_REQUEST_TIMEOUT_MS = 140_000;
const state = {
  token: null,
  user: null,
  authMode: 'login',
  products: [],
  chartProduct: null,
  chartDays: 30,
  chartPoints: [],
  chartFocus: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
}

function formatPrice(value, currency = 'RUB') {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '—';
  return new Intl.NumberFormat('ru-RU', {
    style: 'currency', currency, minimumFractionDigits: 0, maximumFractionDigits: 2
  }).format(numeric);
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Дата неизвестна';
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'
  }).format(date);
}

function formatDateShort(value, includeYear = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit', month: 'short', ...(includeYear ? { year: '2-digit' } : {})
  }).format(date).replace(' г.', '');
}

function safeOzonUrl(raw) {
  try {
    const url = new URL(raw);
    if (url.protocol === 'https:' && /(^|\.)ozon\.ru$/i.test(url.hostname)) return url.href;
  } catch (_) { /* Use the known public homepage below. */ }
  return 'https://www.ozon.ru/';
}

function setMessage(element, text, kind = '') {
  element.textContent = text || '';
  element.classList.toggle('error', kind === 'error');
  element.classList.toggle('success', kind === 'success');
}

function notify(message, kind = '') {
  const region = $('#toast-region');
  const node = document.createElement('div');
  node.className = `toast${kind === 'error' ? ' error' : ''}`;
  node.textContent = message;
  region.appendChild(node);
  window.setTimeout(() => node.remove(), 5200);
}

async function clientLog(level, message, details = '') {
  const safeMessage = String(message).replace(/[\r\n\t]/g, ' ').slice(0, 500);
  const safeDetails = String(details).replace(/[\r\n\t]/g, ' ').slice(0, 1000);
  const entry = { level, message: safeMessage, details: safeDetails || null };
  const consoleMethod = level === 'ERROR' ? 'error' : level === 'WARNING' ? 'warn' : 'info';
  console[consoleMethod](`[Ozon Price Tracker] ${safeMessage}`, safeDetails || '');
  try {
    await fetch(`${API_BASE}/api/client-log`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry), signal: AbortSignal.timeout(2500)
    });
  } catch (_) {
    // Server startup failures are also useful in the browser console; avoid recursive logging.
  }
}

class ApiError extends Error {
  constructor(message, statusCode = 0) {
    super(message);
    this.name = 'ApiError';
    this.statusCode = statusCode;
  }
}

async function api(path, options = {}) {
  const { timeoutMs = 30000, ...requestOptions } = options;
  const headers = { Accept: 'application/json', ...(requestOptions.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (requestOptions.body !== undefined) headers['Content-Type'] = 'application/json';
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...requestOptions,
      headers,
      signal: requestOptions.signal || AbortSignal.timeout(timeoutMs),
    });
  } catch (error) {
    const timedOut = error && error.name === 'TimeoutError';
    const isProductOperation = path === '/api/products' || /^\/api\/products\/\d+\/refresh$/.test(path);
    const description = timedOut
      ? (isProductOperation
          ? 'Поиск или обновление товара не завершилось за отведённое время. Проверьте logs/application.log и parser_output/page.html.'
          : 'Локальный сервер не ответил вовремя.')
      : 'Не удалось подключиться к локальному серверу. Запустите backend и попробуйте снова.';
    await clientLog('ERROR', `API connection failed: ${requestOptions.method || 'GET'} ${path}`, error?.name || 'network error');
    throw new ApiError(description, 0);
  }
  if (response.status === 204) return null;
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) {
    const detail = payload.detail;
    const message = typeof detail === 'string'
      ? detail
      : (detail && typeof detail === 'object' && typeof detail.message === 'string'
        ? detail.message : `Ошибка API (${response.status}).`);
    const diagnostic = detail && typeof detail === 'object'
      ? JSON.stringify({ code: detail.code, provider: detail.provider, http_status: detail.http_status,
          retryable: detail.retryable, diagnostics: detail.diagnostics })
      : '';
    await clientLog(response.status >= 500 ? 'ERROR' : 'WARNING', `API error: ${options.method || 'GET'} ${path}`,
      `HTTP ${response.status}: ${message}${diagnostic ? ` | diagnostic=${diagnostic}` : ''}`);
    if (response.status === 401 && state.token && !path.startsWith('/api/auth/')) {
      await browser.storage.local.remove(TOKEN_KEY);
      state.token = null;
      state.user = null;
      showAuth();
      setMessage($('#auth-message'), 'Сессия завершилась. Войдите снова.', 'error');
    }
    throw new ApiError(message, response.status);
  }
  return payload;
}

async function checkServer() {
  const statusBox = $('#server-status');
  const label = statusBox.querySelector('span:last-child');
  try {
    const response = await fetch(`${API_BASE}/api/health`, { signal: AbortSignal.timeout(2500) });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const health = await response.json();
    statusBox.classList.remove('is-offline');
    label.textContent = `Локальный сервер доступен · Playwright · HTML сохраняется в parser_output/page.html · автообновление ${health.refresh_interval_hours || 12} ч`;
    return true;
  } catch (_) {
    statusBox.classList.add('is-offline');
    label.textContent = 'Сервер не запущен — сначала запустите backend';
    return false;
  }
}

function showAuth() {
  $('#auth-view').classList.remove('hidden');
  $('#dashboard-view').classList.add('hidden');
  closeModal('add-modal');
  closeModal('chart-modal');
  closeModal('tests-modal');
}

function showDashboard() {
  $('#auth-view').classList.add('hidden');
  $('#dashboard-view').classList.remove('hidden');
  $('#current-user').textContent = state.user?.email || '';
  loadProducts().catch((error) => {
    setMessage($('#dashboard-message'), error.message || 'Не удалось загрузить товары.', 'error');
  });
}

function setAuthMode(mode) {
  state.authMode = mode === 'register' ? 'register' : 'login';
  const isRegister = state.authMode === 'register';
  $('#login-mode').classList.toggle('is-active', !isRegister);
  $('#register-mode').classList.toggle('is-active', isRegister);
  $('#login-mode').setAttribute('aria-selected', String(!isRegister));
  $('#register-mode').setAttribute('aria-selected', String(isRegister));
  $('#auth-card-title').textContent = isRegister ? 'Создать аккаунт' : 'С возвращением';
  $('#auth-subtitle').textContent = isRegister ? 'Аккаунт и данные будут храниться на этом компьютере.' : 'Войдите, чтобы открыть список товаров.';
  $('#auth-password').autocomplete = isRegister ? 'new-password' : 'current-password';
  $('#confirm-password-wrap').classList.toggle('hidden', !isRegister);
  $('#auth-confirm-password').required = isRegister;
  $('#auth-submit').textContent = isRegister ? 'Зарегистрироваться' : 'Войти';
  setMessage($('#auth-message'), '');
}

async function submitAuth(event) {
  event.preventDefault();
  const form = $('#auth-form');
  if (!form.reportValidity()) return;
  const email = $('#auth-email').value.trim();
  const password = $('#auth-password').value;
  if (state.authMode === 'register' && password !== $('#auth-confirm-password').value) {
    setMessage($('#auth-message'), 'Пароли не совпадают.', 'error');
    return;
  }
  const button = $('#auth-submit');
  button.disabled = true;
  button.textContent = state.authMode === 'register' ? 'Создаём аккаунт…' : 'Входим…';
  setMessage($('#auth-message'), '');
  try {
    const path = state.authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
    const result = await api(path, { method: 'POST', body: JSON.stringify({ email, password }) });
    state.token = result.access_token;
    state.user = result.user;
    await browser.storage.local.set({ [TOKEN_KEY]: state.token });
    setMessage($('#auth-message'), 'Успешно. Открываем список товаров…', 'success');
    await clientLog('INFO', `User ${state.authMode} completed`);
    showDashboard();
  } catch (error) {
    setMessage($('#auth-message'), error.message || 'Не удалось выполнить операцию.', 'error');
  } finally {
    button.disabled = false;
    button.textContent = state.authMode === 'register' ? 'Зарегистрироваться' : 'Войти';
  }
}

async function loadProducts() {
  const response = await api('/api/products');
  state.products = response.items || [];
  renderProducts();
  setMessage($('#dashboard-message'), '');
}

function renderProducts() {
  const container = $('#products-list');
  $('#summary-count').textContent = `${state.products.length} ${pluralize(state.products.length, 'товар', 'товара', 'товаров')}`;
  $('#empty-state').classList.toggle('hidden', state.products.length > 0);
  container.classList.toggle('hidden', state.products.length === 0);
  container.innerHTML = state.products.map((product) => {
    const lastChecked = formatDateTime(product.last_checked_at);
    const isTestFixture = String(product.name || '').startsWith(TEST_FIXTURE_PREFIX);
    const lastError = product.last_error
      ? `<span class="product-status-error">Не удалось обновить цену: ${escapeHtml(product.last_error)}</span>` : '';
    const refreshAction = isTestFixture
      ? '<span class="test-fixture-note">Демо-цены · обновление отключено</span>'
      : `<button class="button button-quiet" type="button" data-action="refresh" data-id="${Number(product.id)}">Обновить цену</button>`;
    return `
      <article class="product-card" data-product-id="${Number(product.id)}">
        <div class="product-main">
          <h3 class="product-name">${escapeHtml(product.name)}</h3>
          <div class="product-meta"><span class="product-id">ID ${escapeHtml(product.ozon_id)}</span><span>Добавлен ${escapeHtml(formatDateShort(product.created_at, true))}</span>${lastError}</div>
          <div class="product-actions">
            <button class="button button-secondary" type="button" data-action="chart" data-id="${Number(product.id)}">↗ График цены</button>
            ${refreshAction}
            <button class="icon-button" type="button" data-action="delete" data-id="${Number(product.id)}" aria-label="Удалить товар" title="Удалить товар">×</button>
          </div>
        </div>
        <div class="product-right">
          <div><p class="product-price">${escapeHtml(formatPrice(product.current_price, product.currency))}</p><div class="product-checked">Проверено: ${escapeHtml(lastChecked)}</div></div>
        </div>
      </article>`;
  }).join('');
}

function pluralize(number, one, few, many) {
  const n = Math.abs(number) % 100;
  const last = n % 10;
  if (n > 10 && n < 20) return many;
  if (last > 1 && last < 5) return few;
  if (last === 1) return one;
  return many;
}

function openModal(id) {
  $(`#${id}`).classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeModal(id) {
  $(`#${id}`).classList.add('hidden');
  if ($$('.modal:not(.hidden)').length === 0) document.body.style.overflow = '';
}

function openTestsModal() {
  $('#tests-email').value = state.user?.email || '';
  $('#tests-results').replaceChildren();
  setMessage($('#tests-message'), '');
  $('#start-tests-button').disabled = false;
  $('#start-tests-button').textContent = 'Запустить проверки';
  openModal('tests-modal');
  window.setTimeout(() => $('#tests-email').focus(), 30);
}

function appendTestResult(name, passed, detail = '') {
  const row = document.createElement('div');
  row.className = `test-result ${passed ? 'is-pass' : 'is-fail'}`;
  const mark = document.createElement('span');
  mark.className = 'test-result-mark';
  mark.textContent = passed ? '✓' : '×';
  const content = document.createElement('div');
  const title = document.createElement('strong');
  title.className = 'test-result-title';
  title.textContent = name;
  content.appendChild(title);
  if (detail) {
    const text = document.createElement('span');
    text.className = 'test-result-detail';
    text.textContent = detail;
    content.appendChild(text);
  }
  row.append(mark, content);
  $('#tests-results').appendChild(row);
}

async function runExtensionChecks(event) {
  event.preventDefault();
  const form = $('#tests-form');
  if (!form.reportValidity()) return;
  const email = $('#tests-email').value.trim().toLowerCase();
  if (!state.token || !state.user) {
    setMessage($('#tests-message'), 'Сначала войдите в аккаунт.', 'error');
    return;
  }
  if (email !== String(state.user.email || '').toLowerCase()) {
    setMessage($('#tests-message'), 'Email должен совпадать с аккаунтом, в который вы вошли. Для другого email используйте scripts/run_checks.py.', 'error');
    return;
  }

  const button = $('#start-tests-button');
  button.disabled = true;
  button.textContent = 'Выполняем проверки…';
  $('#tests-results').replaceChildren();
  setMessage($('#tests-message'), 'Проверяем backend, авторизацию, хранение данных и историю цен. Парсер Ozon не запускается.');
  let passedCount = 0;
  let failedCount = 0;

  const runCheck = async (name, callback) => {
    try {
      const detail = await callback();
      appendTestResult(name, true, detail || 'Проверка пройдена.');
      passedCount += 1;
      return { ok: true, detail };
    } catch (error) {
      appendTestResult(name, false, error?.message || String(error));
      failedCount += 1;
      return { ok: false, error };
    }
  };

  try {
    const healthCheck = await runCheck('1. Локальный backend доступен', async () => {
      const health = await api('/api/health', { timeoutMs: 5000 });
      if (health.status !== 'ok') throw new Error('Backend вернул неожиданный статус.');
      return `API доступен; интервал автообновления: ${Number(health.refresh_interval_hours) || 12} ч.`;
    });
    if (!healthCheck.ok) {
      setMessage($('#tests-message'), 'Backend не отвечает или вернул ошибку. Проверьте run.ps1 и logs/application.log.', 'error');
      return;
    }

    const identityCheck = await runCheck('2. Текущая сессия и email подтверждены', async () => {
      const me = await api('/api/me', { timeoutMs: 5000 });
      if (String(me.email || '').toLowerCase() !== email) throw new Error('Email ответа API не совпадает с введённым.');
      return `Авторизован пользователь ${me.email}.`;
    });
    if (!identityCheck.ok) {
      setMessage($('#tests-message'), 'Проверка сессии не пройдена. Выйдите и войдите снова.', 'error');
      return;
    }

    const seedCheck = await runCheck('3. Фиктивные товары созданы в SQLite', async () => {
      const seeded = await api('/api/test-tools/seed', {
        method: 'POST', body: JSON.stringify({ email }), timeoutMs: 10000
      });
      if (!Array.isArray(seeded.fixtures) || seeded.fixtures.length !== 3) {
        throw new Error('Backend не вернул три фиктивных товара.');
      }
      return `В базе есть ${seeded.fixtures.length} демо-товара; новых создано: ${seeded.products_created}; новых точек истории: ${seeded.history_points_created}.`;
    });
    if (!seedCheck.ok) {
      setMessage($('#tests-message'), 'Не удалось добавить тестовые данные. Убедитесь, что backend запущен через run.ps1 или run.sh с OZON_ENABLE_TEST_TOOLS=1.', 'error');
      return;
    }

    let fixtureProducts = [];
    const listCheck = await runCheck('4. Список товаров читается из базы', async () => {
      const result = await api('/api/products', { timeoutMs: 5000 });
      fixtureProducts = (result.items || []).filter((item) => String(item.name || '').startsWith(TEST_FIXTURE_PREFIX));
      if (fixtureProducts.length < 3) throw new Error(`Ожидалось минимум 3 фиктивных товара, получено ${fixtureProducts.length}.`);
      return `Получено ${result.count} товаров аккаунта; фиктивных товаров: ${fixtureProducts.length}.`;
    });
    if (!listCheck.ok) {
      setMessage($('#tests-message'), 'Не удалось прочитать созданные тестовые товары.', 'error');
      return;
    }

    const historyExpectations = [
      { days: 7, minimum: 2 },
      { days: 30, minimum: 4 },
      { days: 180, minimum: 8 },
    ];
    for (const expectation of historyExpectations) {
      await runCheck(`5. История цены за ${expectation.days} дней`, async () => {
        const history = await api(`/api/products/${Number(fixtureProducts[0].id)}/history?days=${expectation.days}`, { timeoutMs: 5000 });
        if (!Array.isArray(history.items) || history.items.length < expectation.minimum) {
          throw new Error(`Ожидалось не менее ${expectation.minimum} точек, получено ${history.items?.length || 0}.`);
        }
        if (history.items.some((point) => !Number.isFinite(Number(point.price)) || !point.observed_at)) {
          throw new Error('История содержит цену или дату в неверном формате.');
        }
        return `Получено ${history.items.length} точек с числовой ценой и датой.`;
      });
    }

    await runCheck('6. Защита от повторного добавления работает', async () => {
      const duplicateResponse = await fetch(`${API_BASE}/api/products`, {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json', Authorization: `Bearer ${state.token}` },
        body: JSON.stringify({ ozon_id: fixtureProducts[0].ozon_id }),
        signal: AbortSignal.timeout(5000),
      });
      if (duplicateResponse.status !== 409) {
        throw new Error(`Ожидался HTTP 409 для дубликата, получено HTTP ${duplicateResponse.status}.`);
      }
      return 'Повторный ID отклонён до обращения к парсеру.';
    });

    await runCheck('7. API защищён авторизацией', async () => {
      const unauthenticated = await fetch(`${API_BASE}/api/products`, {
        headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(5000)
      });
      if (unauthenticated.status !== 401) {
        throw new Error(`Без токена ожидался HTTP 401, получено HTTP ${unauthenticated.status}.`);
      }
      return 'Запрос без токена отклонён с HTTP 401.';
    });

    const uiCheck = await runCheck('8. Тестовые товары отображаются в интерфейсе', async () => {
      await loadProducts();
      const rendered = Array.from($('#products-list').querySelectorAll('.product-card'))
        .filter((node) => node.querySelector('.product-name')?.textContent.startsWith(TEST_FIXTURE_PREFIX));
      if (rendered.length < 3) throw new Error(`В интерфейсе показано только ${rendered.length} из 3 демо-товаров.`);
      return `В списке отрисовано ${rendered.length} фиктивных карточек. Откройте любую, чтобы проверить график.`;
    });

    if (failedCount === 0 && uiCheck.ok) {
      setMessage($('#tests-message'), `Все ${passedCount} проверок пройдены. Демо-товары отображаются в списке; цены фиктивные и не получены с Ozon.`, 'success');
      await clientLog('INFO', 'Extension checks completed', `passed=${passedCount} failed=${failedCount}`);
    } else {
      setMessage($('#tests-message'), `Проверка завершена: пройдено ${passedCount}, ошибок ${failedCount}. Подробнее смотрите ниже и в logs/application.log.`, 'error');
      await clientLog('WARNING', 'Extension checks completed with failures', `passed=${passedCount} failed=${failedCount}`);
    }
  } catch (error) {
    setMessage($('#tests-message'), error?.message || 'Неожиданная ошибка тестирования.', 'error');
    await clientLog('ERROR', 'Extension check runner failed', error?.name || 'unknown error');
  } finally {
    button.disabled = false;
    button.textContent = 'Запустить проверки';
  }
}

function openAddModal() {
  $('#add-product-form').reset();
  setMessage($('#add-product-message'), '');
  $('#find-product-button').disabled = false;
  $('#find-product-button').textContent = 'Найти товар';
  openModal('add-modal');
  window.setTimeout(() => $('#ozon-id').focus(), 30);
}

async function submitAddProduct(event) {
  event.preventDefault();
  const form = $('#add-product-form');
  if (!form.reportValidity()) return;
  const ozonId = $('#ozon-id').value.trim();
  const button = $('#find-product-button');
  button.disabled = true;
  button.textContent = 'Ищем на Ozon…';
  setMessage($('#add-product-message'), 'Получаем HTML карточки и проверяем цену. Иногда это занимает до двух минут.');
  try {
    const result = await api('/api/products', { method: 'POST', body: JSON.stringify({ ozon_id: ozonId }), timeoutMs: PRODUCT_REQUEST_TIMEOUT_MS });
    closeModal('add-modal');
    await loadProducts();
    notify(`Добавлен товар: ${result.item.name}`, 'success');
    await clientLog('INFO', 'Product added through extension UI', `product_id=${result.item.id}`);
  } catch (error) {
    setMessage($('#add-product-message'), error.message || 'Не удалось найти товар.', 'error');
  } finally {
    button.disabled = false;
    button.textContent = 'Найти товар';
  }
}

async function handleProductAction(event) {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const productId = Number(button.dataset.id);
  const action = button.dataset.action;
  const product = state.products.find((item) => item.id === productId);
  if (!product) return;
  if (action === 'chart') {
    await openChart(product);
    return;
  }
  if (action === 'delete') {
    const confirmed = window.confirm(`Удалить «${product.name}» и всю его историю цен?`);
    if (!confirmed) return;
    button.disabled = true;
    try {
      await api(`/api/products/${productId}`, { method: 'DELETE' });
      await loadProducts();
      notify('Товар и история цен удалены.');
      await clientLog('INFO', 'Product deleted through extension UI', `product_id=${productId}`);
    } catch (error) {
      notify(error.message || 'Не удалось удалить товар.', 'error');
      button.disabled = false;
    }
    return;
  }
  if (action === 'refresh') {
    button.disabled = true;
    const oldText = button.textContent;
    button.textContent = 'Обновление…';
    try {
      await api(`/api/products/${productId}/refresh`, { method: 'POST', timeoutMs: PRODUCT_REQUEST_TIMEOUT_MS });
      await loadProducts();
      notify('Цена товара обновлена.');
    } catch (error) {
      notify(error.message || 'Не удалось обновить цену.', 'error');
      button.disabled = false;
      button.textContent = oldText;
      await loadProducts().catch(() => {});
    }
  }
}

async function openChart(product) {
  state.chartProduct = product;
  state.chartDays = 30;
  state.chartPoints = [];
  $('#chart-title').textContent = product.name;
  $('#chart-product-link').href = safeOzonUrl(product.url);
  $('#chart-product-link').classList.toggle('hidden', String(product.name || '').startsWith(TEST_FIXTURE_PREFIX));
  $$('.range-button').forEach((button) => {
    const active = Number(button.dataset.days) === state.chartDays;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  openModal('chart-modal');
  await loadChart(state.chartDays);
}

async function loadChart(days) {
  if (!state.chartProduct) return;
  state.chartDays = days;
  $('#chart-loading').classList.remove('hidden');
  $('#chart-tooltip').classList.add('hidden');
  setMessage($('#chart-message'), '');
  $$('.range-button').forEach((button) => {
    const active = Number(button.dataset.days) === days;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  try {
    const response = await api(`/api/products/${state.chartProduct.id}/history?days=${days}`);
    state.chartPoints = (response.items || []).map((point) => ({
      price: Number(point.price), currency: point.currency || 'RUB', observed_at: point.observed_at,
      time: new Date(point.observed_at).getTime()
    })).filter((point) => Number.isFinite(point.price) && Number.isFinite(point.time)).sort((a, b) => a.time - b.time);
    renderChart(state.chartPoints);
    if (state.chartPoints.length === 0) {
      setMessage($('#chart-message'), `За последние ${days} дней нет сохранённых измерений цены. После первого успешного измерения график появится здесь.`);
    }
  } catch (error) {
    setMessage($('#chart-message'), error.message || 'Не удалось загрузить историю цены.', 'error');
    $('#price-chart').innerHTML = '';
    $('#chart-stats').innerHTML = '';
  } finally {
    $('#chart-loading').classList.add('hidden');
  }
}

function svgNumber(value) {
  return Number(value).toFixed(2).replace(/\.00$/, '');
}

function compactPrice(value) {
  return new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0, notation: Math.abs(value) >= 100000 ? 'compact' : 'standard' }).format(value);
}

function renderChart(points) {
  const svg = $('#price-chart');
  const stats = $('#chart-stats');
  const tooltip = $('#chart-tooltip');
  tooltip.classList.add('hidden');
  if (!points.length) {
    svg.innerHTML = '<text x="450" y="170" text-anchor="middle" fill="#87948c" font-size="15">Нет данных за выбранный период</text>';
    stats.innerHTML = '';
    return;
  }

  const values = points.map((point) => point.price);
  const actualMin = Math.min(...values);
  const actualMax = Math.max(...values);
  let minValue = actualMin;
  let maxValue = actualMax;
  if (minValue === maxValue) {
    const padding = Math.max(minValue * 0.04, 1);
    minValue = Math.max(0, minValue - padding);
    maxValue += padding;
  } else {
    const padding = (maxValue - minValue) * 0.14;
    minValue = Math.max(0, minValue - padding);
    maxValue += padding;
  }

  const W = 900, H = 350;
  const margin = { left: 80, right: 24, top: 22, bottom: 55 };
  const plotW = W - margin.left - margin.right;
  const plotH = H - margin.top - margin.bottom;
  const minTime = points[0].time;
  const maxTime = points[points.length - 1].time;
  const timeRange = Math.max(maxTime - minTime, 1);
  const x = (time) => margin.left + ((time - minTime) / timeRange) * plotW;
  const y = (price) => margin.top + (1 - (price - minValue) / (maxValue - minValue)) * plotH;
  const plotted = points.map((point) => ({ ...point, x: x(point.time), y: y(point.price) }));

  let markup = `
    <defs>
      <linearGradient id="price-area-gradient" x1="0" x2="0" y1="0" y2="1">
        <stop offset="0%" stop-color="#43a66b" stop-opacity=".2" />
        <stop offset="100%" stop-color="#43a66b" stop-opacity=".015" />
      </linearGradient>
    </defs>`;

  const tickCount = 5;
  for (let index = 0; index < tickCount; index += 1) {
    const ratio = index / (tickCount - 1);
    const tickY = margin.top + ratio * plotH;
    const tickValue = maxValue - ratio * (maxValue - minValue);
    markup += `<line class="chart-grid" x1="${margin.left}" y1="${svgNumber(tickY)}" x2="${W - margin.right}" y2="${svgNumber(tickY)}" />`;
    markup += `<text class="chart-axis-text" x="${margin.left - 12}" y="${svgNumber(tickY + 4)}" text-anchor="end">${escapeHtml(compactPrice(tickValue))} ₽</text>`;
  }

  const linePath = plotted.map((point, index) => `${index === 0 ? 'M' : 'L'}${svgNumber(point.x)},${svgNumber(point.y)}`).join(' ');
  const areaPath = `${linePath} L${svgNumber(plotted[plotted.length - 1].x)},${svgNumber(margin.top + plotH)} L${svgNumber(plotted[0].x)},${svgNumber(margin.top + plotH)} Z`;
  markup += `<path class="chart-area-fill" d="${areaPath}" /><path class="chart-line" d="${linePath}" />`;

  const tickIndexes = [...new Set([0, Math.floor((points.length - 1) / 4), Math.floor((points.length - 1) / 2), Math.floor(3 * (points.length - 1) / 4), points.length - 1])];
  for (const index of tickIndexes) {
    const point = plotted[index];
    const label = formatDateShort(point.observed_at, state.chartDays === 180);
    const anchor = index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle';
    markup += `<line class="chart-grid" x1="${svgNumber(point.x)}" y1="${margin.top + plotH}" x2="${svgNumber(point.x)}" y2="${margin.top + plotH + 5}" />`;
    markup += `<text class="chart-axis-text" x="${svgNumber(point.x)}" y="${H - 20}" text-anchor="${anchor}">${escapeHtml(label)}</text>`;
  }
  if (plotted.length === 1) {
    markup += `<circle cx="${svgNumber(plotted[0].x)}" cy="${svgNumber(plotted[0].y)}" r="5" class="chart-focus-dot" />`;
  }
  svg.innerHTML = markup;
  svg.setAttribute('aria-label', `Изменение цены за ${state.chartDays} дней. ${points.length} измерений.`);

  const first = points[0].price;
  const last = points[points.length - 1].price;
  const delta = first === 0 ? 0 : ((last - first) / first) * 100;
  const deltaMarkup = `<span class="stat-delta ${delta < 0 ? 'down' : delta > 0 ? 'up' : ''}">${delta > 0 ? '+' : ''}${delta.toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%</span>`;
  stats.innerHTML = `
    <div class="stat-card"><span class="stat-label">Текущая цена</span><strong class="stat-value">${escapeHtml(formatPrice(last))}</strong><span class="stat-label">Последнее измерение</span></div>
    <div class="stat-card"><span class="stat-label">Минимум за период</span><strong class="stat-value">${escapeHtml(formatPrice(actualMin))}</strong><span class="stat-label">${escapeHtml(formatDateShort(points[values.indexOf(actualMin)].observed_at, true))}</span></div>
    <div class="stat-card"><span class="stat-label">Изменение за период</span><strong class="stat-value">${deltaMarkup}</strong><span class="stat-label">От первого к последнему измерению</span></div>`;

  svg.onmousemove = (event) => {
    const rect = svg.getBoundingClientRect();
    const svgX = (event.clientX - rect.left) * (W / rect.width);
    let nearest = plotted[0];
    let distance = Math.abs(nearest.x - svgX);
    for (let index = 1; index < plotted.length; index += 1) {
      const nextDistance = Math.abs(plotted[index].x - svgX);
      if (nextDistance < distance) { nearest = plotted[index]; distance = nextDistance; }
    }
    state.chartFocus = nearest;
    const focused = svg.querySelector('.chart-focus-markers');
    if (focused) focused.remove();
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    marker.setAttribute('class', 'chart-focus-markers');
    marker.innerHTML = `<line class="chart-focus-line" x1="${svgNumber(nearest.x)}" y1="${margin.top}" x2="${svgNumber(nearest.x)}" y2="${margin.top + plotH}" /><circle class="chart-focus-dot" cx="${svgNumber(nearest.x)}" cy="${svgNumber(nearest.y)}" r="5" />`;
    svg.appendChild(marker);
    $('#tooltip-price').textContent = formatPrice(nearest.price, nearest.currency);
    $('#tooltip-date').textContent = formatDateTime(nearest.observed_at);
    const areaRect = $('#chart-area').getBoundingClientRect();
    const proposedLeft = event.clientX - areaRect.left;
    const proposedTop = event.clientY - areaRect.top;
    tooltip.style.left = `${Math.max(0, Math.min(areaRect.width - 245, proposedLeft))}px`;
    tooltip.style.top = `${Math.max(0, Math.min(areaRect.height - 68, proposedTop))}px`;
    tooltip.classList.remove('hidden');
  };
  svg.onmouseleave = () => {
    tooltip.classList.add('hidden');
    const marker = svg.querySelector('.chart-focus-markers');
    if (marker) marker.remove();
  };
}

async function logout() {
  await browser.storage.local.remove(TOKEN_KEY);
  state.token = null;
  state.user = null;
  state.products = [];
  showAuth();
  setAuthMode('login');
  $('#auth-password').value = '';
  await checkServer();
  await clientLog('INFO', 'User logged out');
}

async function initialize() {
  $('#auth-form').addEventListener('submit', submitAuth);
  $('#login-mode').addEventListener('click', () => setAuthMode('login'));
  $('#register-mode').addEventListener('click', () => setAuthMode('register'));
  $('#add-product-button').addEventListener('click', openAddModal);
  $('#empty-add-button').addEventListener('click', openAddModal);
  $('#add-product-form').addEventListener('submit', submitAddProduct);
  $('#products-list').addEventListener('click', handleProductAction);
  $('#logout-button').addEventListener('click', logout);
  $('#run-checks-button').addEventListener('click', openTestsModal);
  $('#tests-form').addEventListener('submit', runExtensionChecks);
  $('#refresh-list-button').addEventListener('click', () => loadProducts().then(() => notify('Список обновлён.')).catch((error) => notify(error.message, 'error')));

  $$('[data-close-modal]').forEach((button) => button.addEventListener('click', () => closeModal(button.dataset.closeModal)));
  $$('.range-button').forEach((button) => button.addEventListener('click', () => loadChart(Number(button.dataset.days))));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') $$('.modal:not(.hidden)').forEach((modal) => closeModal(modal.id));
  });

  const serverOnline = await checkServer();
  try {
    const saved = await browser.storage.local.get(TOKEN_KEY);
    state.token = saved[TOKEN_KEY] || null;
  } catch (error) {
    state.token = null;
    await clientLog('WARNING', 'Could not read extension local storage', error?.name || 'storage error');
  }

  if (state.token && serverOnline) {
    try {
      state.user = await api('/api/me');
      showDashboard();
      return;
    } catch (error) {
      if (!(error instanceof ApiError) || error.statusCode !== 401) {
        setMessage($('#auth-message'), error.message, 'error');
      }
    }
  }
  showAuth();
  if (!serverOnline) {
    setMessage($('#auth-message'), 'Не удаётся связаться с Python API. Установите зависимости и запустите backend по инструкции в README.md.', 'error');
  }
  await clientLog('INFO', 'Extension UI initialized');
}

document.addEventListener('DOMContentLoaded', () => {
  initialize().catch((error) => {
    console.error('[Ozon Price Tracker] UI initialization error:', error);
    setMessage($('#auth-message'), 'Ошибка запуска интерфейса. Откройте консоль расширения для диагностики.', 'error');
    clientLog('ERROR', 'UI initialization failed', error?.stack || error?.message || 'unknown error');
  });
});
