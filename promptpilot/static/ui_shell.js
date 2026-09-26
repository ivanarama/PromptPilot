/* PromptPilot UI shell add-on.
 *
 * Presentation-only layer: locale, compact composer, grouped toolbar, and a
 * linear drag-and-drop review editor. The add-on consumes the existing DOM and
 * API; it does not create a worker, change workflow transitions, or open a
 * second port.
 */
(function promptPilotUiShell() {
  'use strict';

  const PREF_KEY = 'promptpilot.ui-shell.v1';
  const dictionaries = {
    ru: {
      locale: 'Язык интерфейса',
      localeAuto: 'Авто',
      localeRu: 'Русский',
      localeEn: 'English',
      scopeBrowser: 'Сохраняется в этом браузере',
      more: 'Инструменты',
      advanced: 'Дополнительные параметры',
      addTask: 'Добавить задачу',
      prompt: 'Опишите задачу…',
      files: 'Файлы',
      provider: 'Исполнитель',
      model: 'Модель',
      effort: 'Глубина рассуждений',
      machine: 'Машина',
      session: 'Сессия',
      priority: 'Приоритет',
      schedule: 'Расписание',
      project: 'Проект / папка',
      recurrence: 'Повторение',
      timeout: 'Лимит времени',
      permissions: 'Не спрашивать разрешения',
      keepPane: 'Оставить сессию открытой',
      worktree: 'Отдельная рабочая ветка',
      all: 'Все', pending: 'Ожидают', running: 'Работают', completed: 'Завершены',
      failed: 'Ошибки', rateLimited: 'Лимиты', pause: 'Пауза', resume: 'Продолжить',
      settings: 'Настройки', theme: 'Тема', pipelines: 'Конвейеры', scheduleButton: 'Расписание',
      workflows: 'Workflows', parallel: 'Parallel', providers: 'Провайдеры', oneC: '1С',
      appearance: 'Оформление', review: 'Проверка кода', helpers: 'Помощники', autopilot: 'Автопилот',
      close: 'Закрыть', copy: 'Копировать', result: 'Результат', taskPrompt: 'Задача',
      queued: 'В очереди', scheduled: 'Запланирована', paused: 'На паузе',
      reviewRole: 'ревью', executionRole: 'исполнение', action: 'ДЕЙСТВИЕ',
      languageHint: 'Переводится интерфейс; имена моделей, пути и команды остаются без изменений.',
      dragHint: 'Перетащите карточку за маркер, чтобы изменить порядок.',
    },
    en: {
      locale: 'Interface language',
      localeAuto: 'Auto',
      localeRu: 'Russian',
      localeEn: 'English',
      scopeBrowser: 'Saved in this browser',
      more: 'Tools',
      advanced: 'Additional parameters',
      addTask: 'Add task',
      prompt: 'Describe the task…',
      files: 'Files',
      provider: 'Executor',
      model: 'Model',
      effort: 'Reasoning effort',
      machine: 'Machine',
      session: 'Session',
      priority: 'Priority',
      schedule: 'Schedule',
      project: 'Project / folder',
      recurrence: 'Recurrence',
      timeout: 'Time limit',
      permissions: 'Skip permission prompts',
      keepPane: 'Keep session open',
      worktree: 'Separate worktree',
      all: 'All', pending: 'Pending', running: 'Running', completed: 'Completed',
      failed: 'Failed', rateLimited: 'Rate limited', pause: 'Pause', resume: 'Resume',
      settings: 'Settings', theme: 'Theme', pipelines: 'Pipelines', scheduleButton: 'Schedule',
      workflows: 'Workflows', parallel: 'Parallel', providers: 'Providers', oneC: '1C',
      appearance: 'Appearance', review: 'Code review', helpers: 'Helpers', autopilot: 'Autopilot',
      close: 'Close', copy: 'Copy', result: 'Result', taskPrompt: 'Prompt',
      queued: 'Queued', scheduled: 'Scheduled', paused: 'Paused',
      reviewRole: 'review', executionRole: 'execution', action: 'ACTION',
      languageHint: 'The interface is translated; model names, paths, and commands stay unchanged.',
      dragHint: 'Drag a card by its handle to change the order.',
    },
  };

  const termPairs = [
    ['No tasks', 'Нет задач'], ['Result', 'Результат'], ['Prompt', 'Задача'],
    ['Copy', 'Копировать'], ['Error', 'Ошибка'], ['Reason', 'Причина'],
    ['running', 'Работает'], ['queued', 'В очереди'], ['scheduled', 'Запланирована'],
    ['paused', 'На паузе'], ['pending', 'Ожидает'], ['completed', 'Завершена'],
    ['failed', 'Ошибка'], ['rate limited', 'Лимит'], ['review', 'ревью'],
    ['execution', 'исполнение'], ['ACTION', 'ДЕЙСТВИЕ'],
  ];

  let prefs = loadPrefs();
  let dragCard = null;

  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem(PREF_KEY) || '{}') || {}; }
    catch (_) { return {}; }
  }
  function savePrefs() { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); }
  function browserLocale() { return /^ru(?:-|$)/i.test(navigator.language || '') ? 'ru' : 'en'; }
  function currentLocale() {
    const value = prefs.locale || 'auto';
    return value === 'auto' ? browserLocale() : (value === 'en' ? 'en' : 'ru');
  }
  function tr(key) { return (dictionaries[currentLocale()] || dictionaries.ru)[key] || key; }

  function setElementText(selector, key) {
    const element = document.querySelector(selector);
    if (!element) return;
    element.textContent = tr(key);
  }
  function setLabelText(inputId, key) {
    const input = document.getElementById(inputId);
    const label = input && input.closest('label');
    if (!label) return;
    const textNode = [...label.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
    if (textNode) textNode.textContent = `\n        ${tr(key)}\n        `;
  }
  function updateButton(selector, key) { setElementText(selector, key); }

  function installLocaleControl() {
    const pane = document.getElementById('set-appearance');
    if (!pane) return;
    const existing = document.getElementById('pp-locale-select');
    if (existing) {
      const row = existing.closest('.pp-preference-row');
      if (row) {
        const label = row.querySelector('label');
        const scope = row.querySelector('.pp-scope-note');
        const notes = row.querySelectorAll('.pp-scope-note');
        if (label) label.textContent = tr('locale');
        const options = {
          auto: tr('localeAuto'),
          ru: tr('localeRu'),
          en: tr('localeEn'),
        };
        Object.entries(options).forEach(([value, text]) => {
          const option = existing.querySelector(`option[value="${value}"]`);
          if (option) option.textContent = text;
        });
        if (scope) scope.textContent = tr('scopeBrowser');
        if (notes[1]) notes[1].textContent = tr('languageHint');
      }
      existing.value = prefs.locale || 'auto';
      return;
    }
    const row = document.createElement('div');
    row.className = 'pp-preference-row';
    row.innerHTML = `
      <label for="pp-locale-select">${tr('locale')}</label>
      <select id="pp-locale-select">
        <option value="auto">${tr('localeAuto')}</option>
        <option value="ru">${tr('localeRu')}</option>
        <option value="en">${tr('localeEn')}</option>
      </select>
      <span class="pp-scope-note">${tr('scopeBrowser')}</span>
      <div class="pp-scope-note" style="flex-basis:100%">${tr('languageHint')}</div>`;
    pane.appendChild(row);
    const select = row.querySelector('#pp-locale-select');
    select.value = prefs.locale || 'auto';
    select.addEventListener('change', () => {
      prefs.locale = select.value;
      savePrefs();
      applyLocale();
    });
  }

  function applyLocale() {
    const locale = currentLocale();
    document.documentElement.lang = locale;
    installLocaleControl();

    setElementText('.add-form .btn-primary', 'addTask');
    const prompt = document.getElementById('promptInput');
    if (prompt) prompt.placeholder = tr('prompt');
    const fileButton = document.querySelector('.add-form .btn-skills');
    if (fileButton) fileButton.textContent = `📎 ${tr('files')}`;
    setLabelText('providerInput', 'provider');
    setLabelText('modelInput', 'model');
    setLabelText('effortInput', 'effort');
    setLabelText('machineInput', 'machine');
    setLabelText('herdrTargetInput', 'session');
    setLabelText('priorityInput', 'priority');
    setLabelText('scheduleInput', 'schedule');
    setLabelText('workdirInput', 'project');
    setLabelText('recurrenceInput', 'recurrence');
    setLabelText('timeoutInput', 'timeout');

    const permission = document.querySelector('#skipPermissions + span');
    if (permission) permission.textContent = `⚠ ${tr('permissions')}`;
    const keepPane = document.querySelector('#keepPane + span');
    if (keepPane) keepPane.textContent = `🖥 ${tr('keepPane')}`;
    const worktree = document.querySelector('#worktree + span');
    if (worktree) worktree.textContent = `🌿 ${tr('worktree')}`;

    const filterKeys = { '': 'all', pending: 'pending', running: 'running', completed: 'completed', failed: 'failed', rate_limited: 'rateLimited' };
    Object.entries(filterKeys).forEach(([value, key]) => {
      const button = document.querySelector(`#filterBar .filter-btn[data-filter="${value}"]`);
      if (button) button.textContent = tr(key);
    });
    const pauseButton = document.getElementById('pauseBtn');
    if (pauseButton) {
      const paused = pauseButton.classList.contains('paused');
      pauseButton.textContent = `${paused ? '▶' : '⏸'} ${tr(paused ? 'resume' : 'pause')}`;
    }
    updateButton('button[onclick="openSettings()"]', 'settings');
    updateButton('button[onclick="openPipelines()"]', 'pipelines');
    updateButton('button[onclick="openSchedule()"]', 'scheduleButton');
    updateButton('button[onclick="openWorkflows()"]', 'workflows');
    updateButton('button[onclick="openParallel()"]', 'parallel');
    updateButton('button[onclick="openProviders()"]', 'providers');
    updateButton('button[onclick="openEpf()"]', 'oneC');
    const toolsLabel = document.querySelector('[data-pp-tools-label]');
    if (toolsLabel) toolsLabel.textContent = tr('more');
    const themeButton = document.getElementById('themeBtn');
    if (themeButton) {
      const light = document.body.classList.contains('theme-light');
      themeButton.textContent = light ? `🌙 ${currentLocale() === 'en' ? 'Dark' : 'Тёмная'}` : `☀ ${currentLocale() === 'en' ? 'Light' : 'Светлая'}`;
    }
    document.querySelectorAll('.set-tab').forEach(button => {
      const key = button.dataset.tab;
      if (key && dictionaries[locale][key]) button.textContent = dictionaries[locale][key];
    });
    const settingsTitle = document.querySelector('#settingsModal h2');
    if (settingsTitle) {
      const close = settingsTitle.querySelector('button');
      settingsTitle.childNodes[0].textContent = `⚙ ${tr('settings')} `;
      if (close) close.textContent = tr('close');
    }
    translateDynamicTerms();
  }

  function translateDynamicTerms() {
    const locale = currentLocale();
    const target = locale === 'en' ? 0 : 1;
    const pairs = new Map(termPairs.flatMap(pair => [[pair[0], pair[target]], [pair[1], pair[target]]]));
    const selectors = '.task-status, .role-chip, .wf-human-flag, .detail-label, .copy-btn';
    document.querySelectorAll(selectors).forEach(element => {
      const value = element.textContent.trim();
      const translated = pairs.get(value.toLowerCase()) || pairs.get(value);
      if (translated && translated !== value) element.textContent = translated;
    });
  }

  function groupToolbar() {
    const bar = document.getElementById('filterBar');
    if (!bar || bar.dataset.ppGrouped) return;
    bar.dataset.ppGrouped = '1';
    const filters = [...bar.querySelectorAll('.filter-btn')];
    const pause = document.getElementById('pauseBtn');
    const settings = bar.querySelector('button[onclick="openSettings()"]');
    const theme = document.getElementById('themeBtn');
    const tools = [...bar.querySelectorAll('button')].filter(button =>
      !filters.includes(button) && ![pause, settings, theme].includes(button));
    const filterGroup = document.createElement('div');
    filterGroup.className = 'pp-filter-group';
    filters.forEach(button => filterGroup.appendChild(button));
    const systemGroup = document.createElement('div');
    systemGroup.className = 'pp-system-group';
    [pause, settings, theme].filter(Boolean).forEach(button => systemGroup.appendChild(button));
    const details = document.createElement('details');
    details.className = 'pp-tools-menu';
    details.innerHTML = `<summary>🧰 <span data-pp-tools-label>${tr('more')}</span></summary>`;
    const toolGroup = document.createElement('div');
    toolGroup.className = 'pp-tools-group';
    tools.forEach(button => toolGroup.appendChild(button));
    details.appendChild(toolGroup);
    bar.replaceChildren(filterGroup, systemGroup, details);
  }

  function compactComposer() {
    const row = document.querySelector('.add-form .form-row');
    if (!row || row.dataset.ppCompact) return;
    row.dataset.ppCompact = '1';
    const quick = document.createElement('div');
    quick.className = 'pp-quick-fields';
    ['providerInput', 'modelInput', 'effortInput', 'machineInput', 'herdrTargetInput'].forEach(id => {
      const input = document.getElementById(id);
      if (input?.closest('label')) quick.appendChild(input.closest('label'));
    });
    const advanced = document.createElement('details');
    advanced.className = 'pp-advanced';
    advanced.innerHTML = `<summary>⚙ ${tr('advanced')}</summary><div class="pp-advanced-grid"></div>`;
    const advancedGrid = advanced.querySelector('.pp-advanced-grid');
    ['priorityInput', 'scheduleInput', 'workdirInput', 'workdirSelect', 'recurrenceInput', 'timeoutInput', 'skipPermissions', 'keepPane', 'worktree', 'providerModeHint'].forEach(id => {
      const input = document.getElementById(id);
      const holder = input?.closest('label') || input;
      if (holder && holder.parentElement !== advancedGrid) advancedGrid.appendChild(holder);
    });
    const actions = document.createElement('div');
    actions.className = 'pp-composer-actions';
    const add = row.querySelector('.btn-primary');
    if (add) actions.appendChild(add);
    row.replaceChildren(quick, advanced, actions);
  }

  function reindexCascadeCards() {
    const cards = [...document.querySelectorAll('#cz-steps .cz-card')];
    cards.forEach((card, index) => {
      const old = card.dataset.czSlot;
      card.dataset.czSlot = String(index);
      if (old !== undefined && old !== String(index)) {
        card.querySelectorAll('[id]').forEach(element => {
          element.id = element.id.replace(new RegExp(`-${old}$`), `-${index}`);
        });
        card.querySelectorAll('*').forEach(element => ['onclick', 'onchange'].forEach(attribute => {
          const value = element.getAttribute(attribute);
          if (value) element.setAttribute(attribute, value.replace(/cz(RemoveStep|FixerChanged|WindowChanged)\(\d+\)/g, (_, name) => `cz${name}(${index})`));
        }));
      }
      const title = card.querySelector('.cz-label');
      if (title && /Ревью-ступень|Review step/i.test(title.textContent)) {
        title.textContent = currentLocale() === 'en' ? `Review step ${index + 1}` : `Ревью-ступень ${index + 1}`;
      }
    });
  }

  function enhanceCascadeEditor() {
    const cards = document.querySelectorAll('#cz-steps .cz-card');
    cards.forEach(card => {
      if (card.dataset.ppDragReady) return;
      card.dataset.ppDragReady = '1';
      card.draggable = true;
      const firstRow = card.querySelector('.cz-row');
      if (firstRow && !firstRow.querySelector('.pp-drag-handle')) {
        const handle = document.createElement('span');
        handle.className = 'pp-drag-handle';
        handle.textContent = '⠿';
        handle.title = tr('dragHint');
        handle.setAttribute('aria-label', tr('dragHint'));
        firstRow.prepend(handle);
      }
      card.addEventListener('dragstart', event => {
        if (!event.target.closest('.pp-drag-handle')) { event.preventDefault(); return; }
        dragCard = card;
        card.classList.add('pp-dragging');
        event.dataTransfer.effectAllowed = 'move';
      });
      card.addEventListener('dragend', () => {
        card.classList.remove('pp-dragging');
        document.querySelectorAll('#cz-steps .pp-drop-target').forEach(node => node.classList.remove('pp-drop-target'));
        dragCard = null;
      });
      card.addEventListener('dragover', event => {
        if (!dragCard || dragCard === card) return;
        event.preventDefault();
        card.classList.add('pp-drop-target');
      });
      card.addEventListener('dragleave', () => card.classList.remove('pp-drop-target'));
      card.addEventListener('drop', event => {
        event.preventDefault();
        card.classList.remove('pp-drop-target');
        if (!dragCard || dragCard === card) return;
        const rect = card.getBoundingClientRect();
        const before = event.clientY < rect.top + rect.height / 2;
        card.parentElement.insertBefore(dragCard, before ? card : card.nextSibling);
        reindexCascadeCards();
        enhanceCascadeEditor();
      });
    });
  }

  function wrapCascadeMutators() {
    if (window.__ppUiShellWrapped) return;
    window.__ppUiShellWrapped = true;
    ['czAddStep', 'czRemoveStep'].forEach(name => {
      if (typeof window[name] !== 'function') return;
      const original = window[name];
      window[name] = function wrappedCascadeMutator(...args) {
        const result = original.apply(this, args);
        reindexCascadeCards();
        enhanceCascadeEditor();
        return result;
      };
    });
  }

  function init() {
    groupToolbar();
    compactComposer();
    installLocaleControl();
    applyLocale();
    wrapCascadeMutators();
    enhanceCascadeEditor();

    const observer = new MutationObserver(() => {
      installLocaleControl();
      enhanceCascadeEditor();
      translateDynamicTerms();
    });
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('storage', event => {
      if (event.key !== PREF_KEY) return;
      prefs = loadPrefs(); applyLocale();
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
  else init();
})();
