(() => {
  'use strict';

  const form = document.querySelector('#request-form');
  const resultsPanel = document.querySelector('#results-panel');
  const resultState = document.querySelector('#result-state');
  const resultList = document.querySelector('#result-list');
  const retryActions = document.querySelector('#retry-actions');
  const retryButton = document.querySelector('#retry-button');
  const submitButton = document.querySelector('#submit-button');
  const controls = {
    city: document.querySelector('#city'),
    date: document.querySelector('#date'),
    event_type: document.querySelector('#event-type'),
    category: document.querySelector('#category'),
    budget_kzt: document.querySelector('#budget'),
    duration_hours: document.querySelector('#duration'),
    language: document.querySelector('#language'),
  };
  const fieldErrors = {
    city: document.querySelector('#city-error'),
    date: document.querySelector('#date-error'),
    event_type: document.querySelector('#event-type-error'),
    category: document.querySelector('#category-error'),
    budget_kzt: document.querySelector('#budget-error'),
    duration_hours: document.querySelector('#duration-error'),
  };

  let optionsReady = false;
  let lastRequest = null;
  let busy = false;

  function addOptions(select, values, placeholder, selectedValue = '') {
    select.replaceChildren();
    if (placeholder !== null) {
      const initial = document.createElement('option');
      initial.value = '';
      initial.textContent = placeholder;
      select.append(initial);
    }
    for (const value of values || []) {
      const option = document.createElement('option');
      option.value = String(value);
      option.textContent = String(value);
      if (String(value) === selectedValue) option.selected = true;
      select.append(option);
    }
  }

  function setFieldError(name, message) {
    const control = controls[name];
    const error = fieldErrors[name];
    if (!error || !control) return;
    error.textContent = message;
    control.setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function clearErrors() {
    for (const name of Object.keys(fieldErrors)) setFieldError(name, '');
  }

  function formatNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? new Intl.NumberFormat('ru-RU').format(number) : '—';
  }

  function setState(title, detail, kind = 'info') {
    resultState.replaceChildren();
    resultState.className = `state state--${kind}`;
    if (title) {
      const heading = document.createElement('h3');
      heading.textContent = title;
      resultState.append(heading);
    }
    if (detail) {
      const paragraph = document.createElement('p');
      paragraph.textContent = detail;
      resultState.append(paragraph);
    }
    resultState.hidden = !title && !detail;
  }

  function describeRejections(counts = {}) {
    const reasons = [
      ['busy', 'заняты на эту дату'],
      ['budget', 'не подходят по бюджету'],
      ['format', 'не работают с этим форматом'],
      ['language', 'не подходят по языку'],
      ['duration', 'не подходят по длительности'],
    ];
    const nonzero = reasons
      .map(([key, label]) => [Number(counts[key]) || 0, label])
      .filter(([count]) => count > 0)
      .map(([count, label]) => `${count} ${label}`);
    if (!nonzero.length) return 'Других подходящих профилей в каталоге нет.';
    return `Почему вариантов меньше: ${nonzero.join('; ')}. Для каждого профиля показана первая проверка, которую он не прошёл.`;
  }

  function appendCard(profile) {
    const article = document.createElement('article');
    article.className = 'result-card';

    const top = document.createElement('div');
    top.className = 'result-card__top';
    const name = document.createElement('h3');
    name.className = 'result-card__name';
    name.textContent = profile.anon_name || 'Подрядчик';
    top.append(name);
    if (profile.synthetic) {
      const badge = document.createElement('span');
      badge.className = 'badge badge--synthetic';
      badge.textContent = 'Синтетический профиль';
      top.append(badge);
    }

    const meta = document.createElement('p');
    meta.className = 'result-card__meta';
    const category = profile.category || 'Категория не указана';
    const city = profile.city || 'Город не указан';
    meta.textContent = `${category} · ${city}`;

    const price = document.createElement('p');
    price.className = 'result-card__price';
    price.textContent = `от ${formatNumber(profile.price_from_kzt)} ₸`;

    const explanation = document.createElement('p');
    explanation.className = 'result-card__explanation';
    explanation.textContent = profile.explanation || 'Профиль прошёл заданные условия подбора.';

    article.append(top, meta, price, explanation);
    resultList.append(article);
  }

  function renderRecommendations(data) {
    resultList.replaceChildren();
    retryActions.hidden = true;
    const results = Array.isArray(data.results) ? data.results.slice(0, 3) : [];
    const counts = data.rejection_counts || {};

    if (data.outcome === 'no_category_in_city') {
      setState('В этом городе такой категории нет', `В каталоге не нашли подрядчиков категории «${data.requested?.category || controls.category.value}» в городе «${data.requested?.city || controls.city.value}». Попробуйте выбрать другой город или категорию.`, 'empty');
      return;
    }

    if (data.outcome === 'no_eligible_candidates' || results.length === 0) {
      setState('Кандидаты есть, но условиям не соответствует никто', describeRejections(counts), 'empty');
      return;
    }

    for (const profile of results) appendCard(profile);
    const count = Number.isFinite(Number(data.total_eligible)) ? Number(data.total_eligible) : results.length;
    const detail = count < 3
      ? `Нашли ${count} ${count === 1 ? 'подходящий вариант' : 'подходящих варианта'}. ${describeRejections(counts)}`
      : 'Показаны три подходящих варианта. Объяснения основаны на условиях запроса и данных профиля.';
    setState('Подобрали варианты', detail, 'success');
    if (data.degraded) {
      const note = document.createElement('p');
      note.className = 'degraded-note';
      note.textContent = 'Семантический поиск сейчас недоступен; подбор выполнен по доступным параметрам профиля.';
      resultState.append(note);
    }
  }

  function setBusy(value) {
    busy = value;
    submitButton.disabled = value || !optionsReady;
    retryButton.disabled = value;
    form.setAttribute('aria-busy', String(value));
    resultsPanel.setAttribute('aria-busy', String(value));
  }

  async function loadOptions() {
    optionsReady = false;
    submitButton.disabled = true;
    for (const select of [controls.city, controls.event_type, controls.category, controls.language]) select.disabled = true;
    controls.date.disabled = true;
    setState('Загружаем каталог', 'Получаем доступные города, категории и даты.', 'loading');
    try {
      const response = await fetch('/api/options', { headers: { Accept: 'application/json' } });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const options = await response.json();
      addOptions(controls.city, options.cities, 'Выберите город');
      addOptions(controls.event_type, options.event_formats, 'Выберите тип мероприятия');
      addOptions(controls.category, options.categories, 'Выберите категорию');
      addOptions(controls.language, options.languages, 'Любой язык');
      if (options.date_min) controls.date.min = options.date_min;
      if (options.date_max) controls.date.max = options.date_max;
      optionsReady = true;
      for (const select of [controls.city, controls.event_type, controls.category, controls.language]) select.disabled = false;
      controls.date.disabled = false;
      submitButton.disabled = false;
      setState('Подбор появится здесь', 'Заполните условия заказа, чтобы увидеть подходящих подрядчиков.', 'info');
    } catch (error) {
      console.error('Could not load recommendation options', error);
      setState('Не удалось загрузить каталог', 'Проверьте соединение и попробуйте загрузить список ещё раз.', 'error');
      retryActions.hidden = false;
      retryButton.textContent = 'Загрузить каталог';
      retryButton.onclick = loadOptions;
    }
  }

  function validate() {
    clearErrors();
    let firstInvalid = null;
    const required = [
      ['city', 'Выберите город.'],
      ['date', 'Выберите дату мероприятия.'],
      ['event_type', 'Выберите тип мероприятия.'],
      ['category', 'Выберите категорию подрядчика.'],
      ['budget_kzt', 'Укажите бюджет.'],
    ];
    for (const [name, message] of required) {
      const control = controls[name];
      if (!control.value) {
        setFieldError(name, message);
        firstInvalid ||= control;
      }
    }
    const budgetValue = controls.budget_kzt.value;
    if (budgetValue && (!Number.isFinite(Number(budgetValue)) || Number(budgetValue) < 0)) {
      setFieldError('budget_kzt', 'Бюджет должен быть числом не меньше 0.');
      firstInvalid ||= controls.budget_kzt;
    }
    const durationValue = controls.duration_hours.value;
    if (durationValue && (!Number.isFinite(Number(durationValue)) || Number(durationValue) <= 0 || Number(durationValue) > 24)) {
      setFieldError('duration_hours', 'Укажите длительность от 0,5 до 24 часов.');
      firstInvalid ||= controls.duration_hours;
    }
    const dateValue = controls.date.value;
    if (dateValue && ((controls.date.min && dateValue < controls.date.min) || (controls.date.max && dateValue > controls.date.max))) {
      setFieldError('date', `Выберите дату в календарном диапазоне: ${controls.date.min} — ${controls.date.max}.`);
      firstInvalid ||= controls.date;
    }
    if (firstInvalid) {
      firstInvalid.focus();
      return false;
    }
    return true;
  }

  function readRequest() {
    const request = {
      city: controls.city.value,
      date: controls.date.value,
      event_type: controls.event_type.value,
      category: controls.category.value,
      budget_kzt: Number(controls.budget_kzt.value),
    };
    if (controls.duration_hours.value) request.duration_hours = Number(controls.duration_hours.value);
    if (controls.language.value) request.language = controls.language.value;
    return request;
  }

  async function submitRequest(request) {
    if (busy) return;
    lastRequest = request;
    setBusy(true);
    resultList.replaceChildren();
    retryActions.hidden = true;
    retryButton.textContent = 'Повторить запрос';
    retryButton.onclick = null;
    setState('Ищем подходящих подрядчиков', 'Проверяем доступность, бюджет и условия профилей.', 'loading');
    try {
      const response = await fetch('/api/recommendations', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(request),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(data?.detail || data?.error || `HTTP ${response.status}`);
      if (!data || !['recommended', 'no_category_in_city', 'no_eligible_candidates'].includes(data.outcome)) {
        throw new Error('Сервер вернул ответ в неожиданном формате.');
      }
      renderRecommendations(data);
    } catch (error) {
      console.error('Recommendation request failed', error);
      setState('Не удалось получить подбор', 'Проверьте соединение. Ваш запрос сохранён — можно повторить его.', 'error');
      retryActions.hidden = false;
      retryButton.textContent = 'Повторить запрос';
      retryButton.onclick = () => submitRequest(lastRequest);
    } finally {
      setBusy(false);
    }
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!optionsReady || !validate()) return;
    submitRequest(readRequest());
  });

  for (const name of Object.keys(fieldErrors)) {
    controls[name].addEventListener('input', () => {
      if (controls[name].getAttribute('aria-invalid') === 'true') setFieldError(name, '');
    });
    controls[name].addEventListener('change', () => {
      if (controls[name].getAttribute('aria-invalid') === 'true') setFieldError(name, '');
    });
  }

  retryButton.addEventListener('click', () => {
    if (retryButton.onclick) retryButton.onclick();
    else if (lastRequest) submitRequest(lastRequest);
  });
  loadOptions();
})();
