/* One business item per row. Execution attempts stay inside its history. */
let feedItems = [];
let feedExpanded = null;
let feedDetails = new Map();
let feedSource = '';
let feedSearch = '';
let feedLimit = 100;
let feedOffset = 0;
let feedTotal = 0;
let feedLoading = false;
let feedReload = false;
let feedSearchTimer = null;

const FEED_LABELS = {
  queued: 'В очереди', active: 'В работе', blocked: 'Заблокирована',
  needs_attention: 'Требует решения', completed: 'Готово', cancelled: 'Отменена',
};
const FEED_RUN_LABELS = {
  pending: 'В очереди', running: 'Запущен', completed: 'Завершён', failed: 'Ошибка',
  cancelled: 'Отменён', rate_limited: 'Ожидает лимита', awaiting_external: 'Ожидает внешнего результата',
};

function feedEsc(value) { return esc(String(value ?? '')); }
function feedAttr(value) { return escAttr(String(value ?? '')); }
function feedTime(value) {
  if (!value) return 'Нет данных';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 'Нет данных' : date.toLocaleString('ru-RU');
}

function feedRememberDetails() {
  const state = new Set();
  document.querySelectorAll('#taskList details[open][data-feed-section]').forEach(el => {
    state.add((el.closest('article[data-feed-key]')?.dataset.feedKey || '') + ':' + el.dataset.feedSection);
  });
  return state;
}

function feedRestoreDetails(state) {
  document.querySelectorAll('#taskList details[data-feed-section]').forEach(el => {
    el.open = state.has((el.closest('article[data-feed-key]')?.dataset.feedKey || '') + ':' + el.dataset.feedSection);
  });
}

function feedCounts(counts, total) {
  const badges = document.getElementById('badges');
  if (!badges) return;
  badges.innerHTML = Object.entries(counts || {}).filter(([key, count]) => FEED_LABELS[key] && count > 0)
    .map(([key, count]) => `<span class="badge" data-feed-status="${key}">${Number(count)} ${feedEsc(FEED_LABELS[key])}</span>`).join('');
  if (!badges.innerHTML && total === 0) badges.textContent = 'Задач нет';
}

function feedRenderSources(sources, errors = []) {
  const select = document.getElementById('feedSource');
  if (select) {
    select.innerHTML = '<option value="">Все компьютеры и источники</option>' + sources.map(source =>
      `<option value="${feedAttr(source.id)}">${feedEsc(source.label || source.id)}</option>`).join('');
    select.value = feedSource;
  }
  const status = document.getElementById('feedSourceStatus');
  if (!status) return;
  const issues = sources.filter(source => source.status !== 'online');
  status.innerHTML = issues.map(source => `<span class="feed-source-note" data-state="${feedAttr(source.status)}"><b>${feedEsc(source.label || source.id)}:</b> ${feedEsc(source.message || source.error?.message || (source.status === 'partial' ? 'Доступна часть данных' : 'Нет свежих данных'))}</span>`).join('') + errors.map(error => `<span class="feed-source-note" data-state="unavailable">${feedEsc(error.message || 'Не удалось прочитать настройки подключений')}</span>`).join('');
  status.hidden = issues.length === 0 && errors.length === 0;
}

function feedRenderHistory(detail) {
  const history = detail.history || {};
  const runs = Array.isArray(history.runs) ? history.runs : [];
  const events = Array.isArray(history.events) ? history.events : [];
  const runRows = runs.map(run => `<div class="feed-history-row"><b>${feedEsc(run.role_label || {planner:'Планировщик',executor:'Исполнитель',reviewer:'Проверяющий',gate:'Проверки'}[run.role] || 'Запуск')}</b><span>${feedEsc(FEED_RUN_LABELS[run.status] || run.status || 'Нет данных')}</span><span>${run.round_no ? 'Раунд ' + Number(run.round_no) : ''}${run.attempt_no ? ' · попытка ' + Number(run.attempt_no) : ''}</span><time>${feedEsc(feedTime(run.completed_at || run.started_at))}</time></div>`).join('');
  const eventRows = events.map(event => `<div class="feed-event"><time>${feedEsc(feedTime(event.created_at))}</time><span>${feedEsc(event.label || event.event_label || event.event_type || 'Событие')}</span></div>`).join('');
  return `<details class="feed-history" data-feed-section="history"><summary>История выполнения${runs.length ? ' · ' + runs.length + ' запусков' : ''}</summary>${runRows || '<p class="feed-muted">Запусков пока нет.</p>'}${events.length ? `<details data-feed-section="events"><summary>События процесса · ${events.length}</summary>${eventRows}</details>` : ''}</details>`;
}

function feedRenderDetail(item) {
  const cached = feedDetails.get(item.id);
  if (!cached) return '<div class="feed-body feed-muted" role="status">Загрузка карточки…</div>';
  if (cached.error) return `<div class="feed-body feed-error" role="alert">${feedEsc(cached.error)}</div>`;
  const detail = cached.detail;
  const value = detail.item || item;
  const executor = value.executor || {};
  const result = detail.result || {};
  const artifacts = Array.isArray(detail.artifacts) ? detail.artifacts : [];
  const execution = detail.external_execution;
  const delivery = detail.external_result;
  let connection = '';
  if (execution) {
    connection = `<div class="feed-fact"><span>Внешний исполнитель</span><b>${feedEsc(execution.client_label || execution.client_id || 'Не указан')}</b></div><div class="feed-fact"><span>Последний сигнал</span><b>${feedEsc(feedTime(execution.last_seen_at))}</b></div><p class="feed-muted">${feedEsc(execution.status_label || execution.message || '')}</p>`;
  }
  const delivered = Array.isArray(delivery?.artifacts) ? delivery.artifacts : [];
  const files = artifacts.length || delivered.length ? `<div class="feed-files"><h4>Файлы и материалы</h4>${[...delivered, ...artifacts].map(artifact => {
    const name = artifact.name || artifact.filename || String(artifact.path || '').split(/[/\\]/).pop() || artifact.kind || 'Материал';
    const url = feedArtifactUrl(artifact.download_url);
    return `<div class="feed-file"><span>${url ? `<a href="${feedAttr(url)}" download="${feedAttr(name)}">${feedEsc(name)}</a>` : feedEsc(name)}</span>${Number.isFinite(artifact.size_bytes) ? `<span>${Number(artifact.size_bytes).toLocaleString('ru-RU')} байт</span>` : ''}</div>`;
  }).join('')}</div>` : '';
  const report = delivery?.text || (cached.task ? parseResult(visibleResult(cached.task.result))?.text : '');
  const reportHtml = report ? `<details class="feed-history" data-feed-section="result"><summary>Отчёт исполнителя</summary><div class="result-block"><pre>${feedEsc(report)}</pre></div>${delivery?.text_truncated ? '<p class="feed-muted">Показана часть отчёта. Полные материалы доступны в файлах.</p>' : ''}</details>` : '';
  const localControls = item.source_id === 'local' && !item.readonly;
  const manage = localControls && item.kind === 'workflow'
    ? `<button type="button" class="btn btn-sm btn-ghost" data-workflow="${feedAttr(item.object_id)}" onclick="feedManageWorkflow(this.dataset.workflow)">Управление процессом</button>` : '';
  let advanced = '';
  if (localControls && item.kind === 'task' && cached.task) {
    const template = document.createElement('template');
    template.innerHTML = renderLegacyTaskRow(cached.task);
    const legacy = template.content.querySelector('.task-detail');
    if (legacy) advanced = `<details class="feed-advanced" data-feed-section="advanced"><summary>Параметры и управление заданием</summary>${legacy.outerHTML}</details>`;
  }
  return `<div class="feed-body"><p class="feed-objective">${feedEsc(value.objective || item.objective || '')}</p>
    <div class="feed-current"><b>${feedEsc(value.status_label || FEED_LABELS[value.status] || 'Нет данных')}</b><span>${feedEsc(value.status_reason || '')}</span></div>
    <div class="feed-facts"><div class="feed-fact"><span>Источник</span><b>${feedEsc(item.source_label)}</b></div>${executor.machine ? `<div class="feed-fact"><span>Компьютер</span><b>${feedEsc(executor.machine)}</b></div>` : ''}${executor.provider ? `<div class="feed-fact"><span>Исполнитель</span><b>${feedEsc(executor.provider)}${executor.model ? ' · ' + feedEsc(executor.model) : ''}</b></div>` : ''}${connection}</div>
    ${result.available ? `<p class="feed-result-note">Результат запуска получен${Number.isFinite(result.characters) ? ' · ' + Number(result.characters).toLocaleString('ru-RU') + ' символов' : ''}. ${item.kind === 'workflow' && item.status !== 'completed' ? 'Приёмка процесса ещё не завершена.' : ''}</p>` : ''}
    ${files}${reportHtml}${feedRenderHistory(detail)}${advanced}
    <div class="feed-detail-footer"><span>Обновлено: ${feedEsc(feedTime(value.updated_at || value.created_at))}</span>${manage}${item.readonly ? '<span>Просмотр подключённого источника</span>' : ''}</div>
  </div>`;
}

function feedArtifactUrl(value) {
  if (typeof value !== 'string' || !value.startsWith('/api/external-clients/')) return '';
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin && !url.username && !url.password ? url.pathname + url.search : '';
  } catch (_) { return ''; }
}

function feedRender(items, total) {
  const list = document.getElementById('taskList');
  const scrolls = uiSnapScrolls(list);
  const opened = feedRememberDetails();
  if (!items.length) {
    list.innerHTML = '<div class="empty"><div class="empty-icon">&#9744;</div><div>Задач по выбранным условиям нет.</div></div>';
  } else {
    list.innerHTML = items.map(item => `<article class="task-row feed-row${feedExpanded === item.id ? ' expanded' : ''}" data-feed-key="${feedAttr(item.id)}">
      <button type="button" class="task-header feed-toggle" data-feed-key="${feedAttr(item.id)}" onclick="toggleFeedItem(this.dataset.feedKey)" aria-expanded="${feedExpanded === item.id}">
        <span class="feed-state" data-status="${feedAttr(item.status)}">${feedEsc(FEED_LABELS[item.status] || item.status_label || 'Нет данных')}</span><span class="task-prompt">${feedEsc(item.title)}</span><span class="feed-source">${feedEsc(item.source_label)}</span><span class="feed-chevron" aria-hidden="true">${feedExpanded === item.id ? '⌃' : '⌄'}</span>
      </button>${feedExpanded === item.id ? feedRenderDetail(item) : ''}</article>`).join('');
  }
  const more = document.getElementById('feedMore');
  if (more) more.hidden = feedOffset + items.length >= total;
  const previous = document.getElementById('feedPrevious');
  if (previous) previous.hidden = feedOffset === 0;
  const page = document.getElementById('feedPageStatus');
  if (page) page.textContent = total ? `${feedOffset + 1}–${feedOffset + items.length} из ${Number(total)}` : '';
  feedRestoreDetails(opened);
  uiRestoreScrolls(list, scrolls);
}

async function feedFetchDetail(item) {
  const url = `${API}/task-feed/${encodeURIComponent(item.source_id)}/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.object_id)}`;
  const detail = await fetchJSON(url);
  const task = item.kind === 'task' && item.source_id === 'local' && !item.readonly
    ? await fetchJSON(`${API}/tasks/${encodeURIComponent(item.object_id)}`) : null;
  feedDetails.set(item.id, {detail, task});
}

async function loadUnifiedTaskFeed() {
  if (feedLoading) { feedReload = true; return; }
  feedLoading = true;
  const selected = [currentFilter, feedSource, feedSearch, feedLimit, feedOffset].join('|');
  const params = new URLSearchParams({limit:String(feedLimit), offset:String(feedOffset)});
  if (currentFilter) params.set('status', currentFilter);
  if (feedSource) params.set('source_id', feedSource);
  if (feedSearch.trim()) params.set('q', feedSearch.trim());
  try {
    const result = await fetchJSON(`${API}/task-feed?${params}`);
    if (selected !== [currentFilter, feedSource, feedSearch, feedLimit, feedOffset].join('|')) { feedReload = true; return; }
    if (result.total > 0 && feedOffset >= result.total) {
      feedOffset = Math.floor((result.total - 1) / feedLimit) * feedLimit;
      feedReload = true; return;
    }
    const incoming = Array.isArray(result.items) ? result.items : [];
    const active = incoming.find(item => item.id === feedExpanded);
    if (active) {
      try { await feedFetchDetail(active); }
      catch (_) { feedDetails.set(active.id, {error:'Карточка временно недоступна. Последнее состояние списка сохранено.'}); }
    }
    if (selected !== [currentFilter, feedSource, feedSearch, feedLimit, feedOffset].join('|')) { feedReload = true; return; }
    feedItems = incoming;
    feedTotal = Number(result.total || 0);
    feedRenderSources(Array.isArray(result.sources) ? result.sources : [], Array.isArray(result.errors) ? result.errors : []);
    feedCounts(result.counts, result.total);
    if (!incoming.some(item => item.id === feedExpanded)) { feedExpanded = null; expandedId = null; }
    feedRender(feedItems, feedTotal);
  } catch (_) {
    if (selected !== [currentFilter, feedSource, feedSearch, feedLimit, feedOffset].join('|')) { feedReload = true; return; }
    document.getElementById('feedSourceStatus').hidden = false;
    document.getElementById('feedSourceStatus').textContent = 'Список временно недоступен. Показаны последние полученные данные.';
  } finally {
    feedLoading = false;
    if (feedReload) { feedReload = false; loadUnifiedTaskFeed(); }
  }
}

async function toggleFeedItem(key) {
  const item = feedItems.find(row => row.id === key);
  if (!item) return;
  if (feedExpanded === key) { feedExpanded = null; expandedId = null; feedRender(feedItems, feedTotal); return; }
  feedExpanded = key;
  expandedId = item.kind === 'task' && item.source_id === 'local' ? Number(item.object_id) : null;
  feedRender(feedItems, feedTotal);
  try { await feedFetchDetail(item); }
  catch (_) { feedDetails.set(key, {error:'Карточка временно недоступна.'}); }
  if (feedExpanded === key) { feedRender(feedItems, feedTotal); pollScreen(); }
}

function feedManageWorkflow(id) { selectedWorkflowId = id; openWorkflows(); }
async function feedOpenTask(id) {
  if (!Number.isSafeInteger(Number(id)) || Number(id) < 1) return;
  currentFilter = ''; feedSource = ''; feedSearch = ''; feedOffset = 0;
  document.getElementById('feedSearch').value = '';
  document.getElementById('feedSource').value = '';
  document.querySelectorAll('.filter-btn').forEach(button => button.classList.toggle('active', button.dataset.filter === ''));
  const task = await fetchJSON(`${API}/tasks/${Number(id)}`);
  if (task.workflow_id) { feedManageWorkflow(task.workflow_id); return; }
  // Direct links can target an older standalone task outside the first page.
  const detail = await fetchJSON(`${API}/task-feed/local/task/${Number(id)}`);
  feedDetails.set(detail.item.id, {detail, task});
  feedExpanded = detail.item.id; expandedId = Number(id);
  const position = feedItems.findIndex(item => item.id === detail.item.id);
  if (position < 0) feedItems.unshift(detail.item);
  feedRender(feedItems, Math.max(feedTotal, feedItems.length));
}
function feedCreateWorkflow() {
  openWorkflows();
  if (!document.getElementById('wfCreateForm').classList.contains('open')) toggleWorkflowCreate();
}
function feedChangeSource(value) { feedSource = value; feedOffset = 0; loadTasks(); }
function feedChangeSearch(value) {
  feedSearch = value.slice(0, 200); feedOffset = 0;
  clearTimeout(feedSearchTimer); feedSearchTimer = setTimeout(() => loadTasks(), 250);
}
function feedShowMore() { feedOffset += feedLimit; loadTasks(); }
function feedPrevious() { feedOffset = Math.max(0, feedOffset - feedLimit); loadTasks(); }

function wfWizardExternalChanged() {
  const external = document.getElementById('wfNewExecutionMode')?.value === 'external';
  document.getElementById('wfExternalClientFields').hidden = !external;
  const planning = document.getElementById('wfNewPlanning');
  planning.disabled = external;
  planning.closest('label').style.display = external ? 'none' : '';
  const approval = document.getElementById('wfNewPlanApproval');
  approval.disabled = external;
  approval.closest('label').style.display = external ? 'none' : '';
  for (const suffix of ['Provider','Model','Effort','Timeout','Worktree','Skip']) {
    const field = document.getElementById('wfNewExecutor' + suffix);
    if (field) { field.disabled = external; field.closest('label').style.display = external ? 'none' : ''; }
  }
  wfWizardSkipChanged();
  wfWizardUpdateFinalLabel();
}
