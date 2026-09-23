(() => {
  'use strict';

  const TIMEOUT = 10000;
  const form = document.querySelector('#request-form');
  const resultsPanel = document.querySelector('#results-panel');
  const resultState = document.querySelector('#result-state');
  const resultList = document.querySelector('#result-list');
  const retryActions = document.querySelector('#retry-actions');
  const retryButton = document.querySelector('#retry-button');
  const submitButton = document.querySelector('#submit-button');
  const querySummary = document.querySelector('#query-summary');
  const rankingNote = document.querySelector('#ranking-note');
  const catalogBanner = document.querySelector('#catalog-banner');
  const demoPanel = document.querySelector('#demo-panel');
  const demoList = document.querySelector('#demo-list');
  const controls = {
    city: document.querySelector('#city'),
    date: document.querySelector('#date'),
    event_type: document.querySelector('#event-type'),
    category: document.querySelector('#category'),
    budget_kzt: document.querySelector('#budget'),
    duration_hours: document.querySelector('#duration'),
    language: document.querySelector('#language')
  };
  const errors = {
    city: document.querySelector('#city-error'),
    date: document.querySelector('#date-error'),
    event_type: document.querySelector('#event-type-error'),
    category: document.querySelector('#category-error'),
    budget_kzt: document.querySelector('#budget-error'),
    duration_hours: document.querySelector('#duration-error')
  };
  let ready = false;
  let pending = false;
  let lastRequest = null;
  let displayedRequest = null;
  let retryAction = null;

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? new Intl.NumberFormat('ru-RU').format(parsed) : '—';
  }

  function dateText(value) {
    if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return value || 'выбранную дату';
    const date = new Date(value + 'T00:00:00');
    return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).format(date);
  }

  function setState(title, detail, kind) {
    resultState.replaceChildren();
    resultState.className = 'state ' + (kind || 'info');
    const heading = document.createElement('h3');
    heading.textContent = title;
    resultState.append(heading);
    if (detail) {
      const text = document.createElement('p');
      text.textContent = detail;
      resultState.append(text);
    }
    resultState.hidden = false;
  }

  function setPending(value) {
    pending = value;
    submitButton.disabled = value || !ready;
    retryButton.disabled = value;
    form.setAttribute('aria-busy', String(value));
    resultsPanel.setAttribute('aria-busy', String(value));
  }

  function setError(name, message) {
    errors[name].textContent = message;
    controls[name].setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function clearErrors() {
    Object.keys(errors).forEach((name) => setError(name, ''));
  }

  function addOptions(select, values, placeholder) {
    select.replaceChildren();
    const first = document.createElement('option');
    first.value = '';
    first.textContent = placeholder;
    select.append(first);
    (values || []).forEach((value) => {
      const option = document.createElement('option');
      option.value = String(value);
      option.textContent = String(value);
      select.append(option);
    });
  }

  async function requestJson(url, init) {
    const abort = new AbortController();
    const timer = window.setTimeout(() => abort.abort(), TIMEOUT);
    try {
      const response = await fetch(url, Object.assign({}, init || {}, {
        signal: abort.signal,
        headers: Object.assign({ Accept: 'application/json' }, init && init.headers ? init.headers : {})
      }));
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error((data && (data.message || data.detail || data.error)) || ('HTTP ' + response.status));
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Время ожидания ответа истекло. Повторите запрос.');
      throw error;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function showCatalog(catalog) {
    if (!catalog || !Number.isFinite(Number(catalog.total_profiles))) return;
    const source = catalog.source === 'primary' ? 'Исходный каталог' : 'Демонстрационный каталог';
    const synthetic = Number(catalog.synthetic_profiles);
    catalogBanner.textContent = source + ': ' + catalog.total_profiles + ' профилей' + (synthetic > 0 ? ', синтетических: ' + synthetic : '') + '.';
    catalogBanner.hidden = false;
  }

  function readRequest() {
    const request = {
      city: controls.city.value,
      date: controls.date.value,
      event_type: controls.event_type.value,
      category: controls.category.value,
      budget_kzt: Number(controls.budget_kzt.value)
    };
    if (controls.duration_hours.value) request.duration_hours = Number(controls.duration_hours.value);
    if (controls.language.value) request.language = controls.language.value;
    return request;
  }

  function requestLabel(request) {
    return [request.city, dateText(request.date), request.event_type, request.category, 'до ' + number(request.budget_kzt) + ' ₸', request.duration_hours ? request.duration_hours + ' ч' : '', request.language || ''].filter(Boolean).join(' · ');
  }

  function matchesForm(request) {
    const current = readRequest();
    return ['city', 'date', 'event_type', 'category', 'budget_kzt', 'duration_hours', 'language'].every((key) => String(current[key] || '') === String(request[key] || ''));
  }

  function showQuery(request) {
    querySummary.replaceChildren();
    if (!request) {
      querySummary.hidden = true;
      return;
    }
    const title = document.createElement('strong');
    title.textContent = 'Подбор выполнен по запросу: ';
    const body = document.createElement('span');
    body.textContent = requestLabel(request);
    querySummary.append(title, body);
    if (!matchesForm(request)) {
      const changed = document.createElement('p');
      changed.textContent = 'Поля формы были изменены после отправки. Карточки относятся к запросу выше.';
      querySummary.append(changed);
    }
    querySummary.hidden = false;
  }

  function showRanking(ranking) {
    rankingNote.hidden = true;
    rankingNote.textContent = '';
    if (!ranking) return;
    rankingNote.textContent = ranking.mode === 'semantic'
      ? 'Для порядка карточек учтено смысловое совпадение описания с запросом.'
      : 'Смысловое ранжирование не подключено: используется локальное сопоставление слов. Условия заказа проверены.';
    rankingNote.hidden = false;
  }

  function rejectionText(counts) {
    const labels = {
      busy: 'заняты на выбранную дату',
      budget: 'выходят за бюджет',
      format: 'не берут этот формат',
      language: 'не подходят по языку',
      duration: 'не подходят по длительности'
    };
    return Object.keys(labels).map((key) => [Number(counts[key]) || 0, labels[key]])
      .filter((entry) => entry[0] > 0)
      .map((entry) => entry[0] + ' — ' + entry[1]).join('; ');
  }

  function appendEvidence(card, evidence) {
    if (!Array.isArray(evidence) || !evidence.length) return;
    const details = document.createElement('details');
    details.className = 'evidence';
    const summary = document.createElement('summary');
    summary.textContent = 'Основания рекомендации';
    const list = document.createElement('ul');
    evidence.forEach((item) => {
      if (!item || !item.value) return;
      const line = document.createElement('li');
      line.textContent = item.label ? item.label + ': ' + item.value : item.value;
      list.append(line);
    });
    if (!list.childElementCount) return;
    details.append(summary, list);
    card.append(details);
  }

  function appendCard(profile) {
    const card = document.createElement('article');
    card.className = 'result-card';
    const top = document.createElement('div');
    top.className = 'result-card__top';
    const name = document.createElement('h3');
    name.textContent = profile.anon_name || 'Подрядчик';
    top.append(name);
    if (profile.synthetic) {
      const badge = document.createElement('span');
      badge.className = 'badge synthetic';
      badge.textContent = 'Синтетический профиль';
      top.append(badge);
    }
    const meta = document.createElement('p');
    meta.className = 'meta';
    meta.textContent = (profile.category || 'Категория не указана') + ' · ' + (profile.city || 'Город не указан');
    const price = document.createElement('p');
    price.className = 'price';
    price.textContent = 'от ' + number(profile.price_from_kzt) + ' ₸ за мероприятие';
    const explanation = document.createElement('p');
    explanation.className = 'explanation';
    explanation.textContent = profile.explanation || 'Профиль прошёл условия этого запроса.';
    card.append(top, meta, price, explanation);
    appendEvidence(card, profile.evidence);
    resultList.append(card);
  }

  function render(data) {
    resultList.replaceChildren();
    retryActions.hidden = true;
    retryAction = null;
    showCatalog(data.catalog);
    displayedRequest = data.requested || lastRequest;
    showQuery(displayedRequest);
    showRanking(data.ranking);
    const results = Array.isArray(data.results) ? data.results.slice(0, 3) : [];
    const counts = data.rejection_counts || {};
    const availability = data.availability_summary || {};
    const busyCount = Number(availability.busy_excluded ?? counts.busy ?? 0) || 0;
    const busyDetail = 'На ' + dateText(availability.date || data.requested?.date) + ' заняты: ' + busyCount + '.';
    const request = data.requested || lastRequest || {};
    if (data.outcome === 'no_category_in_city') {
      setState('В этом городе такой категории нет', 'В каталоге нет категории «' + (request.category || '') + '» в городе «' + (request.city || '') + '». Выберите другой город или категорию.', 'empty');
      return;
    }
    if (data.outcome === 'no_eligible_candidates' || !results.length) {
      const reasons = rejectionText(counts);
      setState('Кандидаты есть, но условиям не соответствует никто', busyDetail + (reasons ? ' Причины: ' + reasons + '.' : ''), 'empty');
      return;
    }
    results.forEach(appendCard);
    const eligibleCount = Number(data.total_eligible);
    const eligible = Number.isFinite(eligibleCount) ? eligibleCount : results.length;
    const candidateCount = Number(data.candidate_count);
    let detail = busyDetail;
    if (Number.isFinite(candidateCount)) detail += ' В городе в этой категории: ' + candidateCount + '; прошли условия: ' + eligible + '.';
    if (eligible > results.length) detail += ' Ещё подходящих в каталоге: ' + (eligible - results.length) + '.';
    if (eligible < 3) {
      const reasons = rejectionText(counts);
      detail += ' Показываем ' + results.length + ' из максимум трёх карточек.' + (reasons ? ' Причины отсева: ' + reasons + '. Каждый профиль учтён по первой причине.' : ' Других профилей этой категории в городе нет.');
    }
    setState('Подобрали варианты', detail, 'success');
  }

  function validate() {
    clearErrors();
    let first = null;
    const required = [['city', 'Выберите город.'], ['date', 'Выберите дату мероприятия.'], ['event_type', 'Выберите тип мероприятия.'], ['category', 'Выберите категорию.'], ['budget_kzt', 'Укажите бюджет.']];
    required.forEach((item) => {
      if (!controls[item[0]].value) {
        setError(item[0], item[1]);
        first ||= controls[item[0]];
      }
    });
    if (controls.budget_kzt.value && (!Number.isFinite(Number(controls.budget_kzt.value)) || Number(controls.budget_kzt.value) < 0)) {
      setError('budget_kzt', 'Бюджет должен быть числом не меньше 0.');
      first ||= controls.budget_kzt;
    }
    if (controls.duration_hours.value && (!Number.isFinite(Number(controls.duration_hours.value)) || Number(controls.duration_hours.value) <= 0 || Number(controls.duration_hours.value) > 24)) {
      setError('duration_hours', 'Длительность должна быть больше 0 и не больше 24 часов.');
      first ||= controls.duration_hours;
    }
    if (controls.date.value && ((controls.date.min && controls.date.value < controls.date.min) || (controls.date.max && controls.date.value > controls.date.max))) {
      setError('date', 'Выберите дату в диапазоне ' + controls.date.min + ' — ' + controls.date.max + '.');
      first ||= controls.date;
    }
    if (first) first.focus();
    return !first;
  }

  async function submit(request) {
    if (pending) return;
    lastRequest = request;
    displayedRequest = null;
    setPending(true);
    resultList.replaceChildren();
    querySummary.hidden = true;
    rankingNote.hidden = true;
    retryActions.hidden = true;
    retryAction = null;
    setState('Ищем подходящих подрядчиков', 'Проверяем доступность, бюджет и условия профилей.', 'loading');
    try {
      const data = await requestJson('/api/recommendations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request)
      });
      if (!data || !['recommended', 'no_category_in_city', 'no_eligible_candidates'].includes(data.outcome)) throw new Error('Сервер вернул ответ в неожиданном формате.');
      render(data);
    } catch (error) {
      setState('Не удалось получить подбор', (error.message || 'Проверьте соединение.') + ' Запрос сохранён — его можно повторить.', 'error');
      retryAction = () => submit(lastRequest);
      retryButton.textContent = 'Повторить запрос';
      retryActions.hidden = false;
    } finally {
      setPending(false);
    }
  }

  function fill(request) {
    Object.values(controls).forEach((control) => { control.value = ''; });
    Object.keys(request || {}).forEach((key) => {
      if (controls[key] && request[key] !== null && request[key] !== undefined) controls[key].value = String(request[key]);
    });
  }

  async function loadDemos() {
    try {
      const data = await requestJson('/api/demo-scenarios');
      const scenarios = Array.isArray(data.scenarios) ? data.scenarios : [];
      if (!scenarios.length) return;
      demoList.replaceChildren();
      scenarios.forEach((scenario) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'demo-button';
        const title = document.createElement('strong');
        title.textContent = scenario.title || 'Демонстрационный запрос';
        const detail = document.createElement('span');
        detail.textContent = scenario.description || '';
        button.append(title, detail);
        button.addEventListener('click', () => {
          if (pending || !ready) return;
          fill(scenario.request);
          clearErrors();
          submit(readRequest());
        });
        demoList.append(button);
      });
      demoPanel.hidden = false;
    } catch (error) {
      console.info('Demo scenarios unavailable', error);
    }
  }

  async function loadOptions() {
    if (pending) return;
    ready = false;
    setPending(true);
    retryActions.hidden = true;
    retryAction = null;
    [controls.city, controls.date, controls.event_type, controls.category, controls.language].forEach((control) => { control.disabled = true; });
    setState('Загружаем каталог', 'Получаем доступные города, категории и даты.', 'loading');
    try {
      const options = await requestJson('/api/options');
      addOptions(controls.city, options.cities, 'Выберите город');
      addOptions(controls.event_type, options.event_formats, 'Выберите тип мероприятия');
      addOptions(controls.category, options.categories, 'Выберите категорию');
      addOptions(controls.language, options.languages, 'Любой язык');
      controls.date.min = options.date_min || '';
      controls.date.max = options.date_max || '';
      showCatalog(options.catalog);
      [controls.city, controls.date, controls.event_type, controls.category, controls.language].forEach((control) => { control.disabled = false; });
      ready = true;
      submitButton.disabled = false;
      setState('Подбор появится здесь', 'Заполните условия заказа, чтобы увидеть подходящих подрядчиков.', 'info');
      loadDemos();
    } catch (error) {
      setState('Не удалось загрузить каталог', (error.message || 'Проверьте соединение.') + ' Можно повторить загрузку.', 'error');
      retryAction = loadOptions;
      retryButton.textContent = 'Загрузить каталог';
      retryActions.hidden = false;
    } finally {
      setPending(false);
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (ready && validate()) submit(readRequest());
  });
  Object.keys(errors).forEach((name) => {
    ['input', 'change'].forEach((eventName) => controls[name].addEventListener(eventName, () => {
      if (controls[name].getAttribute('aria-invalid') === 'true') setError(name, '');
    }));
  });
  Object.values(controls).forEach((control) => {
    ['input', 'change'].forEach((eventName) => control.addEventListener(eventName, () => {
      if (displayedRequest && !pending) showQuery(displayedRequest);
    }));
  });
  retryButton.addEventListener('click', () => {
    if (!pending && retryAction) retryAction();
  });
  loadOptions();
})();
