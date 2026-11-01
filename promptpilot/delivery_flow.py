"""Quota-free explanation of observed work; never a scheduling/mutation gate."""
from datetime import datetime, timezone


def summarize(data, series, now, *, paused=False):
    queues = {q['id']: q for q in data.get('queues', [])}
    stages = {replica['series_id']: q['id'] for q in queues.values()
              for replica in q.get('series_replicas', [])}
    active = []
    for item in series:
        if item.get('next_status') != 'running':
            continue
        try:
            started = datetime.fromisoformat(str(item['next_started_at']).replace('Z', '+00:00'))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            started = None
        active.append({'task_id': item.get('next_task_id'), 'series_id': item['id'],
                       'stage': stages.get(item['id']), 'title': item.get('title', ''),
                       'started_at': started.isoformat() if started else None,
                       'elapsed_seconds': max(0, round((now - started).total_seconds())) if started else None})
    cache = data.get('cache') or {}
    fresh = bool(cache.get('complete') and not cache.get('stale') and not cache.get('invalidated'))
    merge = queues.get('merge', {}).get('backlog')
    review = queues.get('review', {}).get('backlog')
    fix = queues.get('fix', {}).get('backlog')
    if paused:
        code, reason = 'paused', 'Конвейер на паузе.'
    elif data.get('runtime', {}).get('state') == 'offline':
        code, reason = 'offline', 'Worker недоступен.'
    elif not fresh:
        code, reason = 'unknown', 'Снимок очереди устарел или неполон; причина ожидания требует обновления.'
    elif isinstance(merge, int) and merge > 0:
        code, reason = 'merge', 'Есть кандидаты на слияние; действуют обычные проверки допуска.'
    elif merge == 0 and review == 0 and isinstance(fix, int) and fix > 0:
        code, reason = 'fix', 'Готовых кандидатов на слияние нет; работа находится на доработке.'
    elif merge == 0 and isinstance(review, int) and review > 0:
        code, reason = 'review', 'Кандидаты ожидают независимого ревью.'
    else:
        code, reason = 'waiting', 'Готовых кандидатов на слияние нет; проверьте решения и причины ожидания этапов.'
    rounds = {}
    diagnostics = data.get('diagnostics') or {}
    for field in ('fix_candidates', 'human_waiting', 'review_backlog'):
        for item in diagnostics.get(field, []):
            number, depth = item.get('number'), item.get('review_depth')
            if type(number) is int and type(depth) is int and number > 0 and depth > 1:
                rounds[number] = max(depth, rounds.get(number, 0))
    return {'reason_code': code, 'reason': reason, 'queue_observation_fresh': fresh,
            'observed_at': data.get('generated_at'), 'active': active,
            'review_rounds': [{'number': number, 'rounds': depth}
                              for number, depth in sorted(rounds.items(), key=lambda x: (-x[1], x[0]))[:10]],
            'fix_runs_5h': queues.get('fix', {}).get('runs_5h', {})}
