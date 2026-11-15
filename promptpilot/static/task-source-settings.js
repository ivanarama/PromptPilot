// Owner-managed connections. Keys are sent once and never loaded back into a form.
let taskSourceState = {revision: null, sources: [], editable: false, id: null, busy: false, generation: 0, probe: 0};

function taskSourceElement(id) { return document.getElementById(id); }

function taskSourceMessage(message, state = '') {
  const element = taskSourceElement('taskSourceMessage');
  element.textContent = message;
  element.dataset.state = state;
}

function taskSourceControls() {
  const disabled = taskSourceState.busy || !taskSourceState.editable;
  for (const id of ['taskSourceAdd','taskSourceSave','taskSourceProbe','taskSourceDelete']) {
    taskSourceElement(id).disabled = disabled;
  }
  taskSourceElement('taskSourceRefresh').disabled = taskSourceState.busy;
  for (const element of taskSourceElement('taskSourceForm').querySelectorAll('input,select')) element.disabled = disabled;
}

function taskSourceRenderList() {
  const list = taskSourceElement('taskSourceList');
  list.replaceChildren();
  if (!taskSourceState.sources.length) {
    const empty = document.createElement('p');
    empty.className = 'feed-muted';
    empty.textContent = 'Источников пока нет. Добавьте первый экземпляр PromptPilot.';
    list.append(empty);
  }
  for (const source of taskSourceState.sources) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'source-settings-item';
    button.setAttribute('aria-current', String(source.id === taskSourceState.id));
    button.disabled = taskSourceState.busy;
    button.append(document.createTextNode(source.label));
    const hint = document.createElement('small');
    hint.textContent = source.base_url;
    button.append(hint);
    button.addEventListener('click', () => taskSourceEdit(source.id));
    list.append(button);
  }
}

function taskSourceEdit(id) {
  if (taskSourceState.busy) return;
  const source = taskSourceState.sources.find(item => item.id === id);
  taskSourceState.id = source?.id || null;
  taskSourceState.generation++;
  taskSourceState.probe++;
  taskSourceElement('taskSourceLabel').value = source?.label || '';
  taskSourceElement('taskSourceUrl').value = source?.base_url || '';
  taskSourceElement('taskSourceType').value = 'promptpilot';
  taskSourceElement('taskSourceToken').value = '';
  taskSourceElement('taskSourceAuth').value = source?.has_token ? 'preserve' : 'none';
  taskSourceElement('taskSourceAuth').querySelector('option[value="preserve"]').disabled = !source;
  taskSourceElement('taskSourceDelete').hidden = !source;
  taskSourceElement('taskSourceProbeResult').textContent = '';
  taskSourceAuthChanged();
  taskSourceRenderList();
  taskSourceControls();
}

function taskSourceNew() {
  taskSourceEdit(null);
  taskSourceElement('taskSourceLabel').focus();
}

function taskSourceAuthChanged() {
  const token = taskSourceElement('taskSourceAuth').value === 'token';
  taskSourceElement('taskSourceTokenLabel').hidden = !token;
  taskSourceElement('taskSourceToken').required = token;
  if (!token) taskSourceElement('taskSourceToken').value = '';
  const mode = taskSourceElement('taskSourceAuth').value;
  taskSourceElement('taskSourceAuthHint').textContent = mode === 'preserve'
    ? 'Текущий доступ сохранится. При смене адреса укажите новый токен или выберите подключение без авторизации.'
    : mode === 'token' ? 'Токен сохранится на сервере. При следующем открытии его значение не показывается.'
    : 'Подходит для источника, который принимает запросы без токена.';
  taskSourceState.probe++;
  taskSourceElement('taskSourceProbeResult').textContent = '';
}

async function taskSourceLoad() {
  if (taskSourceState.busy) return;
  const generation = ++taskSourceState.generation;
  taskSourceState.busy = true;
  taskSourceControls();
  taskSourceMessage('Загрузка источников…');
  try {
    const data = await fetchJSON(`${API}/task-sources`);
    if (generation !== taskSourceState.generation || !taskSourceElement('taskSourceDialog').open) return;
    taskSourceState.revision = data.revision;
    taskSourceState.sources = data.sources || [];
    taskSourceState.editable = data.editable === true;
    taskSourceState.busy = false;
    const selected = taskSourceState.id;
    taskSourceEdit(taskSourceState.sources.some(item => item.id === selected) ? selected : null);
    taskSourceMessage((data.errors || []).map(error => error.message).join('\n') || 'Настройки применяются после сохранения.', data.editable ? '' : 'error');
  } catch (_) {
    if (generation !== taskSourceState.generation || !taskSourceElement('taskSourceDialog').open) return;
    taskSourceState.editable = false;
    taskSourceMessage('Не удалось загрузить источники. Нажмите «Обновить список».', 'error');
  } finally {
    if (generation === taskSourceState.generation) {
      taskSourceState.busy = false;
      taskSourceControls();
      taskSourceRenderList();
    }
  }
}

function openTaskSourceSettings() {
  const dialog = taskSourceElement('taskSourceDialog');
  if (!dialog.open) dialog.showModal();
  taskSourceState.busy = false;
  taskSourceState.editable = false;
  taskSourceEdit(null);
  taskSourceLoad();
}

function closeTaskSourceSettings() { taskSourceElement('taskSourceDialog').close(); }

function taskSourceDraft() {
  const form = taskSourceElement('taskSourceForm');
  if (!form.reportValidity()) return null;
  const id = taskSourceState.id || ('source-' + (globalThis.crypto?.randomUUID?.() || (Date.now().toString(16) + Math.random().toString(16).slice(2))).replaceAll('-', '').slice(0, 16));
  const mode = taskSourceElement('taskSourceAuth').value;
  const body = {id, label: taskSourceElement('taskSourceLabel').value.trim(), base_url: taskSourceElement('taskSourceUrl').value.trim(), type: 'promptpilot', auth_mode: mode};
  if (mode === 'token') body.token = taskSourceElement('taskSourceToken').value;
  return body;
}

async function taskSourceCheck() {
  if (taskSourceState.busy || !taskSourceState.editable) return;
  const draft = taskSourceDraft();
  if (!draft) return;
  const generation = taskSourceState.generation;
  const probe = ++taskSourceState.probe;
  const signature = JSON.stringify(draft);
  const result = taskSourceElement('taskSourceProbeResult');
  result.textContent = 'Проверка доступности и совместимости…';
  result.dataset.state = '';
  taskSourceState.busy = true;
  taskSourceControls();
  taskSourceRenderList();
  try {
    const data = await fetchJSON(`${API}/task-sources/probe`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({draft})});
    if (generation !== taskSourceState.generation || probe !== taskSourceState.probe || !taskSourceElement('taskSourceDialog').open) return;
    const current = taskSourceDraft();
    if (!current || JSON.stringify({...current, id: draft.id}) !== signature) return;
    result.textContent = data.message || (data.status === 'online' ? 'Подключение работает. Доступен полный список задач.' : 'Не удалось подтвердить подключение.');
    result.dataset.state = data.status === 'unavailable' ? 'error' : data.status;
  } catch (error) {
    if (generation === taskSourceState.generation && probe === taskSourceState.probe) {
      result.textContent = error.message || 'Не удалось проверить подключение.';
      result.dataset.state = 'error';
    }
  } finally {
    if (generation === taskSourceState.generation) {
      taskSourceState.busy = false;
      taskSourceControls();
      taskSourceRenderList();
    }
  }
}

async function taskSourceMutate(url, method, body, nextId) {
  taskSourceState.busy = true;
  const generation = ++taskSourceState.generation;
  taskSourceState.probe++;
  taskSourceControls();
  taskSourceRenderList();
  taskSourceMessage('Сохранение…');
  try {
    const data = await fetchJSON(url, {method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (generation !== taskSourceState.generation) return;
    taskSourceState.busy = false;
    taskSourceState.revision = data.revision;
    taskSourceState.sources = data.sources || [];
    taskSourceState.editable = data.editable === true;
    taskSourceEdit(nextId);
    taskSourceMessage(method === 'DELETE' ? 'Источник отключён. Его задачи сохранены на исходном сервере.' : 'Источник сохранён.');
    if (typeof feedSource !== 'undefined' && feedSource && !taskSourceState.sources.some(source => source.id === feedSource)) {
      feedSource = '';
      feedOffset = 0;
    }
    loadUnifiedTaskFeed();
  } catch (error) {
    if (generation !== taskSourceState.generation) return;
    taskSourceState.editable = false;
    taskSourceMessage((error.message || 'Сохранение не подтверждено.') + ' Обновите список перед следующей попыткой.', 'error');
  } finally {
    if (generation === taskSourceState.generation) {
      taskSourceState.busy = false;
      taskSourceControls();
      taskSourceRenderList();
    }
  }
}

async function taskSourceSubmitForm(event) {
  event.preventDefault();
  if (taskSourceState.busy || !taskSourceState.editable) return false;
  const draft = taskSourceDraft();
  if (draft) await taskSourceMutate(`${API}/task-sources/${encodeURIComponent(draft.id)}`, 'PUT', {...draft, revision:taskSourceState.revision}, draft.id);
  return false;
}

async function taskSourceDisconnect() {
  if (taskSourceState.busy || !taskSourceState.editable || !taskSourceState.id) return;
  const source = taskSourceState.sources.find(item => item.id === taskSourceState.id);
  if (!confirm(`Отключить источник «${source?.label || ''}»? Его задачи останутся на исходном сервере.`)) return;
  await taskSourceMutate(`${API}/task-sources/${encodeURIComponent(taskSourceState.id)}`, 'DELETE', {revision:taskSourceState.revision}, null);
}

document.getElementById('taskSourceDialog').addEventListener('close', () => {
  taskSourceState.generation++;
  taskSourceState.probe++;
  taskSourceElement('taskSourceToken').value = '';
});
