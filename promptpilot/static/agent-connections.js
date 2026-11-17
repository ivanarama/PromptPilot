/* Owner settings for scoped MCP clients. No requests or writes at script load. */
(function (global) {
  'use strict';
  const DEFAULTS = Object.freeze({default_executor: 'zcode', reviewer: 'codex', branch_prefix: 'agent/'});
  const API = '/api/agent-control';
  const MCP = '/api/agents/mcp';
  let current = null;

  function endpoint(base) {
    const url = new URL(MCP, base);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('Invalid server URL');
    return url.href;
  }
  function clientConfig(kind, url, token) {
    if (!token) throw new Error('Create a connection first');
    if (kind === 'codex') {
      return '[mcp_servers.promptpilot]\nurl = ' + JSON.stringify(url) +
        '\nhttp_headers = { Authorization = ' + JSON.stringify('Bearer ' + token) + ' }\n';
    }
    if (kind !== 'http') throw new Error('Unknown client format');
    return JSON.stringify({mcpServers: {promptpilot: {type: 'http', url, headers: {Authorization: 'Bearer ' + token}}}}, null, 2);
  }
  function lines(value) { return value.split('\n').map(item => item.trim()).filter(Boolean); }
  async function copyText(text) {
    if (global.navigator.clipboard?.writeText) {
      try { await global.navigator.clipboard.writeText(text); return; } catch (_) { /* explicit-click fallback below */ }
    }
    const previous = document.activeElement;
    const temporary = document.createElement('textarea');
    temporary.readOnly = true; temporary.value = text;
    temporary.style.cssText = 'position:fixed;left:-9999px;top:0';
    temporary.setAttribute('aria-hidden', 'true');
    (previous?.closest('dialog[open]') || document.body).append(temporary);
    try {
      temporary.select();
      if (!document.execCommand('copy')) throw new Error('Clipboard unavailable');
    } finally { temporary.value = ''; temporary.remove(); previous?.focus(); }
  }
  function element(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (key === 'text') node.textContent = value;
      else if (key === 'className') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (key in node) node[key] = value;
      else node.setAttribute(key, value);
    }
    node.append(...children);
    return node;
  }

  function mount(options = {}) {
    const request = options.request || (async (path, init = {}) => {
      const response = await fetch(path, {credentials: 'same-origin', cache: 'no-store', ...init});
      if (!response.ok) { const error = new Error('Request not confirmed'); error.status = response.status; throw error; }
      return response.status === 204 ? null : response.json();
    });
    const copy = options.copy || copyText;
    const ask = options.confirm || (text => global.confirm(text));
    const url = options.endpoint || endpoint(options.baseUrl || global.location.href);
    const dialog = element('dialog', {className: 'modal agent-connections', id: 'agentConnectionsDialog', 'aria-labelledby': 'agentConnectionsTitle'});
    const status = element('p', {className: 'agent-status', role: 'status', 'aria-live': 'polite'});
    const projectList = element('div', {className: 'agent-project-list', 'aria-label': 'Зарегистрированные проекты'});
    const connectionList = element('div', {className: 'agent-connection-list', 'aria-label': 'Подключённые агенты'});
    const projectForm = element('form', {className: 'agent-project-form'});
    const connectionForm = element('form', {className: 'agent-connection-form'});
    const grants = element('fieldset', {className: 'agent-grants'}, element('legend', {text: 'Доступ к проектам'}));
    const fields = {};
    let projects = [], connections = [], selected = null, busy = false, editable = false, generation = 0;
    // Secret lives only in this dialog closure and the one-time input value.
    let issuedToken = null, issuedId = null;
    const tokenInput = element('input', {type: 'password', readOnly: true, autocomplete: 'off', 'aria-label': 'Одноразовый токен подключения'});
    const tokenBox = element('section', {className: 'agent-issued', hidden: true, 'aria-label': 'Новое подключение'});
    const closeButton = element('button', {type: 'button', className: 'btn btn-sm btn-ghost', text: 'Закрыть', onclick: close});
    function message(text, error = false) { status.textContent = text; status.dataset.state = error ? 'error' : ''; }
    function field(name, title, props = {}, textarea = false) {
      const input = element(textarea ? 'textarea' : 'input', {name, ...props});
      fields[name] = input;
      return element('label', {text: title}, input);
    }
    function controls() {
      for (const node of dialog.querySelectorAll('button,input,textarea,select')) {
        if (node !== closeButton) node.disabled = busy || (!editable && node.dataset.readonly !== 'true');
      }
      for (const button of tokenBox.querySelectorAll('button')) button.disabled = busy || !issuedToken;
    }
    function forgetToken() {
      issuedToken = null; issuedId = null; tokenInput.value = ''; tokenInput.type = 'password'; tokenBox.hidden = true;
      showToken.textContent = 'Показать токен';
    }
    function edit(project) {
      if (busy) return;
      selected = project?.id || null;
      fields.id.value = project?.id || ''; fields.id.readOnly = !!project;
      for (const name of ['name', 'repository_url', 'repository_path']) fields[name].value = project?.[name] || '';
      for (const [name, fallback] of Object.entries(DEFAULTS)) fields[name].value = project?.[name] || fallback;
      fields.deliverables.value = (project?.deliverables || ['RESULT.md']).join('\n');
      fields.readable_paths.value = (project?.readable_paths || ['README.md', 'AGENTS.md']).join('\n');
      fields.allowed_paths.value = (project?.allowed_paths || []).join('\n');
      fields.enabled.checked = project?.enabled !== false;
      fields.workflow_config.value = JSON.stringify(project?.workflow_config || {}, null, 2);
      renderProjects(); controls();
    }
    function renderProjects() {
      projectList.replaceChildren();
      if (!projects.length) projectList.append(element('p', {className: 'agent-muted', text: 'Сначала зарегистрируйте проект. Это делается один раз.'}));
      for (const project of projects) {
        projectList.append(element('button', {type: 'button', className: 'agent-project', 'aria-current': String(project.id === selected),
          onclick: () => edit(project)}, element('strong', {text: project.name}),
        element('small', {text: project.id + (project.enabled === false ? ' · отключён' : '')})));
      }
      grants.replaceChildren(element('legend', {text: 'Доступ к проектам'}));
      for (const project of projects.filter(project => project.enabled !== false)) {
        grants.append(element('label', {}, element('input', {type: 'checkbox', name: 'project_id', value: project.id}),
          document.createTextNode(project.name + ' (' + project.id + ')')));
      }
      if (!projects.some(project => project.enabled !== false)) grants.append(element('p', {text: 'Нет доступных проектов.'}));
    }
    function renderConnections() {
      connectionList.replaceChildren();
      if (!connections.length) connectionList.append(element('p', {className: 'agent-muted', text: 'Подключений пока нет.'}));
      for (const connection of connections) {
        const revoked = !!connection.revoked_at;
        const row = element('div', {className: 'agent-connection'}, element('strong', {text: connection.name}),
          element('span', {text: (connection.project_ids || []).join(', ')}),
          element('small', {text: revoked ? 'Доступ отозван' : 'Токен скрыт'}));
        if (!revoked) row.append(element('button', {type: 'button', className: 'btn btn-sm btn-ghost', text: 'Отозвать', onclick: async () => {
          if (!ask('Отозвать доступ этого подключения? Сохранённые задачи останутся.')) return;
          await mutate('/connections/' + encodeURIComponent(connection.id), 'DELETE', undefined, () => {
            if (issuedId === connection.id) forgetToken();
          });
        }}));
        connectionList.append(row);
      }
    }
    async function load() {
      if (busy) return;
      const epoch = ++generation;
      busy = true; editable = false; controls(); message('Загрузка проектов и подключений…');
      try {
        const [p, c] = await Promise.all([request(API + '/projects'), request(API + '/connections')]);
        if (epoch !== generation || !dialog.open) return;
        if (!Array.isArray(p?.projects) || !Array.isArray(c?.connections)) throw new Error('Invalid settings response');
        projects = p.projects; connections = c.connections; editable = true;
        renderProjects(); renderConnections(); message('Клиент получает доступ к задачам выбранных проектов, включая задачи других подключённых клиентов.');
      } catch (error) {
        if (epoch === generation && dialog.open) {
          editable = false;
          message(error.status === 403 ? 'Для подключений агентов настройте PP_API_TOKEN на сервере и войдите как владелец.'
            : error.status === 401 ? 'Требуется вход в PromptPilot. Авторизуйтесь и обновите список.'
            : 'Не удалось загрузить настройки. Обновите список.', true);
        }
      } finally { if (epoch === generation) { busy = false; controls(); } }
    }
    async function mutate(path, method, body, accept) {
      if (busy || !editable) return;
      const epoch = generation;
      busy = true; controls(); message('Сохранение…');
      try {
        const result = await request(API + path, {method, headers: {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
        if (epoch !== generation || !dialog.open) return;
        if (accept) accept(result);
        busy = false;
        await load();
      } catch (error) {
        if (epoch === generation && dialog.open) {
          editable = false;
          message(error.status === 403 ? 'Сервер отклонил операцию. Настройте доступ владельца для подключений агентов.'
            : 'Операция не подтверждена. Обновите список перед повторением. Секретный токен повторно не выдаётся.', true);
        }
      } finally { if (epoch === generation) { busy = false; controls(); } }
    }
    projectForm.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || !editable || !projectForm.reportValidity()) return;
      let config;
      try { config = JSON.parse(fields.workflow_config.value); if (!config || Array.isArray(config) || typeof config !== 'object') throw new Error(); }
      catch (_) { message('Расширенные настройки должны быть JSON-объектом.', true); return; }
      const body = {id: fields.id.value.trim(), name: fields.name.value.trim(), repository_url: fields.repository_url.value.trim() || null,
        repository_path: fields.repository_path.value.trim(), default_executor: fields.default_executor.value.trim(),
        reviewer: fields.reviewer.value.trim(), branch_prefix: fields.branch_prefix.value.trim(),
        deliverables: lines(fields.deliverables.value), readable_paths: lines(fields.readable_paths.value),
        allowed_paths: lines(fields.allowed_paths.value), enabled: fields.enabled.checked, workflow_config: config};
      await mutate('/projects', 'POST', body, result => { if (result?.project) { busy = false; edit(result.project); } });
    });
    connectionForm.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || !editable || !connectionForm.reportValidity()) return;
      const project_ids = [...grants.querySelectorAll('input:checked')].map(input => input.value);
      if (!project_ids.length) { message('Выберите хотя бы один проект.', true); return; }
      forgetToken();
      await mutate('/connections', 'POST', {name: connectionName.value.trim(), project_ids}, result => {
        if (typeof result?.token !== 'string' || !result.token || !result.connection?.id) throw new Error('Missing one-time token');
        issuedToken = result.token; issuedId = result.connection.id;
        tokenInput.value = issuedToken; tokenBox.hidden = false;
      });
    });
    async function copyConfig(kind) {
      if (!issuedToken) return;
      try { await copy(clientConfig(kind, url, issuedToken)); message('Конфигурация скопирована. Сохраните её в настройках клиента.'); }
      catch (_) { message('Буфер обмена недоступен. Разрешите копирование для этой страницы и нажмите ещё раз.', true); }
    }
    projectForm.append(field('id', 'ID проекта', {required: true, maxlength: 64, placeholder: 'my-project'}),
      field('name', 'Название', {required: true, maxlength: 120}),
      field('repository_url', 'GitHub-репозиторий (если будете передавать issues по URL)', {type: 'url', placeholder: 'https://github.com/owner/repository'}),
      field('repository_path', 'Локальный репозиторий на компьютере PromptPilot', {required: true}),
      field('default_executor', 'Внешний клиент-исполнитель', {required: true}), field('reviewer', 'Проверяющий провайдер', {required: true}),
      field('deliverables', 'Файлы результата (по одному пути на строке)', {rows: 3}, true),
      element('p', {className: 'agent-muted', text: 'Начальная проверка подтверждает получение файлов и их хеши. Содержимое проверяет отдельный проверяющий. Проектные тесты и правила патча можно добавить в расширенных настройках.'}),
      field('readable_paths', 'Контекст, доступный агенту (файлы или каталоги с / на конце)', {rows: 3}, true),
      element('p', {className: 'agent-muted', text: 'README.md и AGENTS.md — начальный контекст. Это список чтения, а не разрешение менять файлы.'}),
      field('branch_prefix', 'Префикс веток', {required: true}),
      element('label', {className: 'agent-check'}, fields.enabled = element('input', {type: 'checkbox', name: 'enabled'}), document.createTextNode('Проект включён')),
      element('details', {className: 'agent-advanced'}, element('summary', {text: 'Расширенные настройки проекта'}),
        field('allowed_paths', 'Область изменений: пути (по одному на строке)', {rows: 3}, true),
        field('workflow_config', 'JSON workflow: проверки и политика выполнения', {rows: 10, spellcheck: false}, true),
        element('p', {className: 'agent-muted', text: 'Шаблон переиспользуется для новых задач. Политику задаёт владелец; MCP-клиент не может её подменить. Входящий план требует подтверждения владельца.'})),
      element('button', {type: 'submit', className: 'btn btn-primary', text: 'Сохранить проект'}));
    const connectionName = element('input', {name: 'connection_name', required: true, maxlength: 120, placeholder: 'Мой клиент'});
    connectionForm.append(element('label', {text: 'Название подключения'}, connectionName), grants,
      element('button', {type: 'submit', className: 'btn btn-primary', text: 'Создать подключение'}));
    const showToken = element('button', {type: 'button', className: 'btn btn-sm btn-ghost', text: 'Показать токен', onclick: () => {
      tokenInput.type = tokenInput.type === 'password' ? 'text' : 'password';
      showToken.textContent = tokenInput.type === 'password' ? 'Показать токен' : 'Скрыть токен';
    }});
    tokenBox.append(element('h3', {text: 'Сохраните новое подключение'}),
      element('p', {text: 'Токен выдан один раз. После закрытия окна его нельзя посмотреть снова. Скопируйте конфигурацию в приватные настройки клиента.'}), tokenInput, showToken,
      element('div', {className: 'agent-actions'},
        element('button', {type: 'button', className: 'btn btn-ghost', text: 'Скопировать HTTP MCP JSON', onclick: () => copyConfig('http')}),
        element('button', {type: 'button', className: 'btn btn-ghost', text: 'Скопировать Codex TOML', onclick: () => copyConfig('codex')})));
    const refresh = element('button', {type: 'button', className: 'btn btn-sm btn-ghost', text: 'Обновить список', onclick: load, 'data-readonly': 'true'});
    dialog.append(element('header', {}, element('h2', {id: 'agentConnectionsTitle', text: 'Подключения агентов'}), closeButton),
      element('p', {className: 'agent-muted', text: 'Зарегистрируйте проект один раз и подключите совместимый MCP-клиент. Модель выбирается в клиенте.'}),
      element('p', {className: 'agent-endpoint'}, document.createTextNode('HTTP MCP: '), element('code', {text: url})), status,
      element('div', {className: 'agent-actions'}, refresh, element('button', {type: 'button', className: 'btn btn-sm btn-ghost', text: 'Новый проект', onclick: () => edit(null)})),
      element('div', {className: 'agent-layout'}, projectList, projectForm),
      element('h3', {text: 'Доступ клиентов'}), connectionForm, tokenBox, connectionList,
      element('p', {className: 'agent-muted', text: 'MCP ставит задачи и читает их состояние. Внешний исполнитель подключается отдельно; этот токен не заменяет его учётные данные.'}));
    function close() { cleanup(); if (dialog.open) dialog.close(); }
    function cleanup() { generation++; forgetToken(); busy = false; editable = false; }
    dialog.addEventListener('close', () => { if (!dialog.open) cleanup(); });
    dialog.addEventListener('cancel', cleanup);
    global.addEventListener('pagehide', close);
    (options.root || document.body).append(dialog);
    edit(null); controls();
    return {
      async open() { forgetToken(); if (!dialog.open) dialog.showModal(); busy = false; editable = false; edit(null); await load(); },
      close, destroy() { close(); cleanup(); global.removeEventListener('pagehide', close); dialog.remove(); }, dialog,
    };
  }
  const exported = {mount, endpoint, clientConfig};
  global.AgentConnections = exported;
  global.openAgentConnections = function () { if (!current) current = mount(); return current.open(); };
  global.closeAgentConnections = function () { current?.close(); };
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
