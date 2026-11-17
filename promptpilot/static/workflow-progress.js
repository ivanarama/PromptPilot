/* Read-only presentation. This module never requests, approves or dispatches work. */
(function (global) {
  'use strict';
  const STATES = {
    draft: 'Черновик', planning: 'Формируется план', awaiting_plan_approval: 'План ждёт утверждения',
    queued: 'Ожидает запуска', executing: 'Выполняется', awaiting_external: 'Ждём внешний результат',
    gating: 'Проверяется', reviewing: 'На независимом ревью', revision_required: 'Возвращён на доработку',
    awaiting_human: 'Нужно решение владельца', completed: 'Завершён', failed: 'Ошибка', cancelled: 'Отменён',
    pending: 'Ожидает запуска', running: 'В работе', rate_limited: 'Ожидает лимита',
  };
  const STEP_STATES = {done: 'Завершено', current: 'Сейчас', waiting: 'Впереди', blocked: 'Не пройдено'};
  const esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
  const text = (value, limit = 1200) => typeof value === 'string' ? value.slice(0, limit) : '';
  const number = value => Number.isInteger(value) && value >= 0 ? value : null;
  const statusLabel = value => STATES[value] || 'Нет данных о состоянии';
  const roundStatusLabel = value => ({executing:'Попытка исполнения',awaiting_external:'Внешнее задание',gating:'Проверка результата',reviewing:'Ревью результата'})[value] || statusLabel(value);

  function correlated(workflow, progress) {
    return progress && progress.workflow_id === workflow.id && Number.isInteger(progress.state_version)
      && progress.state_version === workflow.state_version ? progress : null;
  }
  function fallback(workflow, stages) {
    const index = stages.findIndex(stage => stage.id === workflow.current_stage_id);
    const stage = index >= 0 ? stages[index] : {};
    let owner = {
      planning: 'Планировщик', awaiting_plan_approval: 'Владелец', executing: 'Исполнитель',
      awaiting_external: 'Внешний исполнитель', gating: 'Проверки', reviewing: 'Проверяющий',
      awaiting_human: 'Владелец', revision_required: 'Владелец',
    }[workflow.status] || 'PromptPilot';
    let headline = statusLabel(workflow.status);
    let next = workflow.status === 'awaiting_external' ? 'Дождаться нового результата внешнего исполнителя.' : '';
    if (workflow.status === 'gating') {
      const automatic = workflow.config?.automation?.enabled === true && workflow.config.automation.auto_gate !== false;
      owner = automatic ? 'PromptPilot' : 'Владелец';
      headline = automatic ? 'Ожидаем автоматическую проверку' : 'Нужно зафиксировать результат проверок';
      next = automatic ? 'Дождаться результата проверок.' : 'Выполнить проверки и выбрать итог в текущем раунде.';
    }
    return {current: {round_no: workflow.current_round, stage_index: index >= 0 ? index + 1 : null,
      stage_total: stages.length, stage_code: stage.code, stage_title: stage.title, status: workflow.status,
      owner, headline,
      detail: 'Подробное состояние сейчас недоступно. Показан сохранённый статус задачи.',
      next_action: next},
    steps: [], last_return: null, history: []};
  }
  function paths(values, label) {
    const all = Array.isArray(values) ? values.filter(value => typeof value === 'string') : [];
    const list = all.slice(0, 20);
    if (!list.length) return '';
    return `<div class="wf-progress-files"><strong>${esc(label)}</strong><ul>${list.map(path => `<li><code>${esc(text(path, 500))}</code></li>`).join('')}</ul>${all.length > list.length ? `<p>И ещё ${all.length - list.length} файлов.</p>` : ''}</div>`;
  }
  function returnReason(info, current) {
    // A later gate success/review must not look blocked by an older round.
    if (!info || !['recorded', 'current_artifact_policy'].includes(info.basis) || ['reviewing', 'completed'].includes(current.status)) return '';
    const inferred = info.basis === 'current_artifact_policy';
    const round = number(info.round_no);
    return `<aside class="wf-progress-return" aria-label="Почему результат возвращался">
      <h4>${inferred ? 'Сверка результата' : 'Возврат на доработку'}${round !== null ? ` · раунд ${round}` : ''}</h4>
      ${inferred ? '<p class="wf-progress-basis">Сверка файлов по текущим правилам.</p>' : '<p class="wf-progress-basis">Причина зафиксирована при проверке результата.</p>'}
      ${text(info.summary) ? `<p>${esc(text(info.summary))}</p>` : ''}
      <div class="wf-progress-file-grid">${paths(info.expected_paths, inferred ? 'Сейчас ожидаются' : 'Ожидались')}${paths(info.received_paths, 'Получены')}</div>
      ${text(info.action) ? `<p class="wf-progress-next"><strong>Что исправить:</strong> ${esc(text(info.action))}</p>` : ''}
    </aside>`;
  }
  function renderSummary({workflow, stages = [], progress = null}) {
    const data = correlated(workflow, progress) || fallback(workflow, stages);
    const current = data.current || {};
    const index = number(current.stage_index), total = number(current.stage_total), round = number(current.round_no);
    const stage = index !== null && index > 0 && total !== null && index <= total ? `Этап ${index} из ${total}` : 'Этап ещё не выбран';
    const stageTitle = [text(current.stage_code, 100), text(current.stage_title, 300)].filter(Boolean).join(' · ');
    const steps = Array.isArray(data.steps) ? data.steps.slice(0, 12) : [];
    return `<section class="wf-progress" aria-label="Где сейчас задача">
      <div class="wf-progress-location">${esc(stage)}${round !== null && round > 0 ? ` · Раунд ${round}` : ' · Раунд ещё не начат'}</div>
      ${stageTitle ? `<div class="wf-progress-stage">${esc(stageTitle)}</div>` : ''}
      <h3>${esc(text(current.headline, 500) || statusLabel(current.status))}</h3>
      <p class="wf-progress-owner"><strong>Сейчас отвечает:</strong> ${esc(text(current.owner, 160) || 'Не указано')}</p>
      ${text(current.detail) ? `<p>${esc(text(current.detail))}</p>` : ''}
      ${text(current.next_action) ? `<p class="wf-progress-next"><strong>Что дальше:</strong> ${esc(text(current.next_action))}</p>` : ''}
      ${steps.length ? `<ol class="wf-progress-steps" aria-label="Путь текущего раунда">${steps.map(step => {
        const state = Object.hasOwn(STEP_STATES, step.state) ? step.state : 'unknown';
        return `<li data-progress-state="${state}"><span class="wf-progress-step-label">${esc(text(step.label, 100))}</span><small>${esc(STEP_STATES[state] || 'Нет данных')}</small></li>`;
      }).join('')}</ol>` : ''}
      ${returnReason(data.last_return, current)}
    </section>`;
  }
  function renderHistory({workflow, rounds = [], progress = null}) {
    const data = correlated(workflow, progress);
    const history = data && Array.isArray(data.history) ? data.history : rounds.map(round => ({
      round_no: round.round_no, status: round.status, summary: round.summary?.summary,
    }));
    const rows = history.filter(row => number(row.round_no) !== null && row.round_no !== workflow.current_round);
    if (!rows.length) return '<p class="wf-meta">Предыдущих раундов пока нет.</p>';
    return [...rows].sort((a, b) => b.round_no - a.round_no).map(row => `<article class="wf-progress-history">
      <div><strong>Раунд ${row.round_no}</strong><span>${esc(roundStatusLabel(row.status))}</span></div>
      <p>${esc(text(row.summary, 1600) || 'Краткий итог не записан.')}</p>
    </article>`).join('');
  }
  const exported = {renderSummary, renderHistory, correlated, statusLabel};
  global.WorkflowProgress = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
