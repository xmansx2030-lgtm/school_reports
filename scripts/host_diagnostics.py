#!/usr/bin/env python3
"""Read-only aggregate host evidence; no environment secrets or raw logs."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def run(*args, timeout=45):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=True)  # noqa: S603
    return result.stdout + result.stderr


def emit(section, value):
    print(json.dumps({"section": section, "data": value}, default=str), flush=True)


def cpu():
    values = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    return sum(values[:8]), values[3] + values[4], values[4], values[7]


def container_cpu(inspected):
    result = {}
    for item in inspected:
        pid = item['State'].get('Pid', 0)
        if not pid:
            continue
        try:
            group = next(line[3:] for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if line.startswith('0::'))
            path = Path('/sys/fs/cgroup', group.lstrip('/'), 'cpu.stat')
            values = dict(line.split() for line in path.read_text().splitlines())
            result[item['Name']] = int(values['usage_usec'])
        except (OSError, StopIteration, KeyError, ValueError):
            print('cgroup sample unavailable', flush=True)
    return result


def main():
    emit('captured_at', datetime.now(timezone.utc).isoformat())
    emit('host', {'cpus': os.cpu_count(), 'load': os.getloadavg(),
                  'memory': Path('/proc/meminfo').read_text(),
                  'disk': shutil.disk_usage('/')._asdict(),
                  'pressure': {name: Path('/proc/pressure', name).read_text() for name in ('cpu', 'memory', 'io')}})
    ids = run('docker', 'ps', '-aq').split()
    inspected = json.loads(run('docker', 'inspect', *ids)) if ids else []
    containers = []
    for item in inspected:
        config, state, host = item['Config'], item['State'], item['HostConfig']
        safe_env = {}
        for raw in config.get('Env', []):
            key, _, value = raw.partition('=')
            if key in {'WEB_CONCURRENCY', 'CELERY_CORE_CONCURRENCY', 'CELERY_MEDIA_CONCURRENCY',
                       'GUNICORN_MAX_REQUESTS', 'GUNICORN_MAX_REQUESTS_JITTER', 'CONN_MAX_AGE',
                       'OPERATIONS_ENABLED', 'RELEASE_SHA', 'MAX_CONCURRENT_REQUESTS'}:
                safe_env[key] = value
        containers.append({'name': item['Name'], 'image': config['Image'], 'state': state['Status'],
                           'health': state.get('Health', {}).get('Status'), 'oom': state['OOMKilled'],
                           'restarts': item['RestartCount'], 'started': state['StartedAt'],
                           'memory_limit': host.get('Memory'), 'cpu_limit': host.get('NanoCpus'),
                           'runtime': safe_env})
    emit('containers', containers)
    first = cpu()
    container_first = container_cpu(inspected)
    started = time.monotonic()
    emit('stats_start', run('docker', 'stats', '--no-stream', '--format', '{{json .}}'))
    emit('processes', run('ps', '-eo', 'pid,ppid,comm,pcpu,pmem,rss,etimes', '--sort=-pcpu').splitlines()[:45])
    for _ in range(2):
        time.sleep(30)
    last = cpu()
    container_last = container_cpu(inspected)
    elapsed = time.monotonic() - started
    emit('container_cpu_interval', {'duration_seconds': round(elapsed, 2), 'host_percent': {
        name: round((value - container_first[name]) / 1e6 / elapsed / os.cpu_count() * 100, 3)
        for name, value in container_last.items() if name in container_first
    }})
    total = last[0] - first[0]
    emit('cpu_interval', {'busy_percent': round(100 * (1 - (last[1] - first[1]) / total), 2),
                          'iowait_percent': round(100 * (last[2] - first[2]) / total, 2),
                          'steal_percent': round(100 * (last[3] - first[3]) / total, 2)})
    emit('stats_end', run('docker', 'stats', '--no-stream', '--format', '{{json .}}'))
    emit('failed_units', run('systemctl', '--failed', '--no-legend', '--no-pager'))
    emit('timers', run('systemctl', 'list-timers', '--all', '--no-pager'))
    for name in ('school-reports-web-1', 'school-reports-worker-core-1', 'school-reports-worker-media-1', 'school-reports-beat-1'):
        logs = run('docker', 'logs', '--since', '2h', '--tail', '20000', name)
        statuses, routes, tasks, exceptions = Counter(), Counter(), Counter(), Counter()
        for line in logs.splitlines():
            match = re.search(r'"(?:GET|POST|HEAD|OPTIONS) ([^ ?]+)[^"]*" (\d{3})', line)
            if match:
                route, status = match.groups()
                # Only fixed, known routes; no arbitrary URL/token output.
                route = route if route in {'/', '/healthz/', '/health/', '/readyz/', '/favicon.ico', '/login/'} else 'other'
                statuses[status] += 1
                routes[route] += 1
            match = re.search(r'Task ([a-zA-Z0-9_.]+)\[.*?\] (received|succeeded|failed)', line)
            if match:
                tasks[' '.join(match.groups())] += 1
            for exception in re.findall(r'\b([A-Za-z]+(?:Error|Exception))\b', line):
                exceptions[exception] += 1
        emit('logs_' + name, {'window': '2h, max 20000 lines', 'lines': len(logs.splitlines()),
                             'statuses': statuses, 'routes': routes, 'tasks': tasks, 'exceptions': exceptions,
                             'worker_boots': logs.count('Booting worker'), 'worker_exits': logs.count('Worker exiting'),
                             'shutdowns': logs.count('Shutting down')})
    # One Django startup for aggregate DB/runtime evidence. Read-only transaction.
    source = """
import os, json
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django
django.setup()
from datetime import timedelta
from django.db import connection, transaction
from django.db.models import Avg, Max, Count
from django.utils import timezone
from operations.models import ProjectMetricSnapshot, ServerMetricSnapshot, OperationAction, HostAgentHeartbeat, ManagedProject
from django.conf import settings
with transaction.atomic():
 with connection.cursor() as c:
  c.execute('SET TRANSACTION READ ONLY')
  c.execute("SELECT state, count(*) FROM pg_stat_activity GROUP BY state")
  print(json.dumps({'connections': c.fetchall()}))
  c.execute("SELECT datname, numbackends, xact_commit, xact_rollback, blks_read, blks_hit, temp_bytes, deadlocks FROM pg_stat_database WHERE datname=current_database()")
  print(json.dumps({'database': c.fetchall()}))
  c.execute("SELECT relname,n_live_tup,n_dead_tup,seq_scan,idx_scan FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 15")
  print(json.dumps({'tables': c.fetchall()}))
 cutoff=timezone.now()-timedelta(hours=24)
 for hours in (1,24):
  since=timezone.now()-timedelta(hours=hours)
  print(json.dumps({'hours':hours,'projects': list(ProjectMetricSnapshot.objects.filter(captured_at__gte=since).values('project__slug').annotate(samples=Count('id'),cpu_avg=Avg('cpu_percent'),cpu_max=Max('cpu_percent'),memory_mb_avg=Avg('memory_used_mb')))},default=str))
  print(json.dumps({'hours':hours,'host':list(ServerMetricSnapshot.objects.filter(captured_at__gte=since).values('source').annotate(samples=Count('id'),cpu_avg=Avg('cpu_percent'),cpu_max=Max('cpu_percent'),memory_avg=Avg('memory_percent')))},default=str))
 print(json.dumps({'actions':list(OperationAction.objects.filter(requested_at__gte=cutoff).values('action','status','error_code').annotate(count=Count('id')))},default=str))
 latest=[]
 for project in ManagedProject.objects.filter(is_active=True):
  metric=project.metric_snapshots.first()
  if metric:
   latest.append({'project':project.slug,'captured_at':metric.captured_at,'cpu_percent':metric.cpu_percent,
    'containers':[{'name':row.get('name'),'cpu_percent':row.get('cpu_percent'),
     'cpu_measurement':row.get('cpu_measurement'),'sample_duration_seconds':row.get('sample_duration_seconds')}
     for row in metric.container_states]})
 print(json.dumps({'latest_project_metrics':latest},default=str))
 heartbeat=HostAgentHeartbeat.objects.filter(name='primary').first()
 print(json.dumps({'host_agent_age_seconds':(timezone.now()-heartbeat.last_seen_at).total_seconds() if heartbeat else None}))
 print(json.dumps({'beat': {k: {'task':v['task'],'schedule':str(v['schedule'])} for k,v in settings.CELERY_BEAT_SCHEDULE.items()}}))
import redis
broker=redis.Redis.from_url(settings.CELERY_BROKER_URL,socket_connect_timeout=2,socket_timeout=2)
info=broker.info('memory')
stats=broker.info('stats')
print(json.dumps({'redis': {key: info.get(key) for key in ('used_memory','used_memory_peak','maxmemory','maxmemory_policy')},
 'redis_stats': {key:stats.get(key) for key in ('evicted_keys','rejected_connections','instantaneous_ops_per_sec')},
 'queue_lengths':{name:broker.llen(name) for name in ('default','notifications','images','periodic')}}))
"""
    emit('application', run('docker', 'exec', 'school-reports-web-1', 'python', '-c', source, timeout=90))


if __name__ == '__main__':
    main()

