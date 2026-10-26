#!/usr/bin/env python3
"""Read-only redacted school-display diagnostics. No service or data changes."""
import json, re, subprocess
from collections import Counter
from datetime import datetime, timezone

def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, timeout=40)
    return p.returncode, p.stdout + p.stderr

def emit(section, data):
    print(json.dumps({'section':section,'data':data}), flush=True)

def scrub(value):
    value = re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[email]', value)
    value = re.sub(r'\b(?:https?|postgres(?:ql)?|redis)://\S+', '[url]', value)
    value = re.sub(r'(?i)(password|secret|token|authorization)(\s*[:=]\s*)\S+', r'\1\2[redacted]', value)
    value = re.sub(r'([\x22\x27])(.*?)\1', '[quoted]', value)
    return value[:500]

emit('captured_at', datetime.now(timezone.utc).isoformat())
code, ids = run('docker','ps','-aq','--filter','name=school-display')
items = json.loads(run('docker','inspect',*ids.split())[1]) if ids.strip() else []
emit('containers',[{'name':x['Name'],'image':x['Config']['Image'],'state':x['State']['Status'], 'health':x['State'].get('Health',{}).get('Status'),'oom':x['State']['OOMKilled'],'restarts':x['RestartCount'],'started':x['State']['StartedAt']} for x in items])
for item in items:
    name = item['Name'].lstrip('/')
    if not any(word in name for word in ('web','postgres','redis')):
        continue
    code, output = run('docker','logs','--timestamps','--since','90m','--tail','20000',name)
    lines = output.splitlines()
    errors, examples, frames = Counter(), [], []
    for line in lines:
        frame = re.search(r'File "([^"]+)", line (\d+), in ([A-Za-z0-9_<>]+)',line)
        if frame:
            frames.append(frame.groups())
        error = re.search(r'\b([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s*(.*)',line)
        if error:
            kind, message = error.groups()
            errors[kind] += 1
            examples.append({'type':kind,'message':scrub(message),'frames':frames[-12:]})
            frames = []
    emit('logs_'+name,{'lines':len(lines),'window':'90m; max 20000 lines','errors':errors,'examples':examples[-8:],'http_500_lines':sum(bool(re.search(r'\b500\b',line)) for line in lines),'worker_timeouts':sum('WORKER TIMEOUT' in line for line in lines),'categories':{term:sum(term in line.lower() for line in lines) for term in ('too many clients','remaining connection slots','deadlock detected','terminating connection','could not connect','connection refused','connection reset','out of memory','error:','fatal:')}})

for query in ('SHOW max_connections', "SELECT state,client_addr,application_name,count(*),max(now()-backend_start) AS oldest_connection FROM pg_stat_activity WHERE backend_type='client backend' GROUP BY state,client_addr,application_name ORDER BY count(*) DESC"):
    code, output = run('docker','exec','school-display-postgres-1','sh','-c','exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"','sh',query)
    emit('postgres_connections', {'code':code,'output':output})
emit('container_addresses', [{'name':x['Name'],'networks':{k:v.get('IPAddress') for k,v in x['NetworkSettings']['Networks'].items()},'connection_age':[e for e in x['Config'].get('Env',[]) if e.startswith('CONN_MAX_AGE=')]} for x in items])
code, output = run('docker','exec','school-display-web-1','python','-c',"import os,json; os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings'); from django.conf import settings; print(json.dumps({'CONN_MAX_AGE':settings.DATABASES['default'].get('CONN_MAX_AGE'),'CONN_HEALTH_CHECKS':settings.DATABASES['default'].get('CONN_HEALTH_CHECKS')}))")
emit('web_database_settings', {'code':code,'output':output})
