#!/usr/bin/env python3
"""School-display diagnostics and explicit scoped repair; default is read-only."""
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

import hashlib, os, shutil, sys, time
from pathlib import Path

def apply_school_display_fix():
    root=Path('/opt/school-display/app')
    config=root/'compose.production.yaml'
    release=root/'.release-commit'
    expected_release='728699811e035936834a00f1d03a7cd049d91e5c'
    expected_hash='d97f1a18293c5409310e52609c21d6106dd9e91942f8e184b722422cab5196ec'
    assert release.read_text().strip()==expected_release, 'Release changed; stop'
    original=config.read_bytes()
    original_stat=config.stat()
    assert hashlib.sha256(original).hexdigest()==expected_hash, 'Compose changed; stop'
    marker='  web:\n    <<: *django\n'
    text=original.decode()
    assert text.count(marker)==1, 'Unexpected web definition'
    replacement=marker+'    environment:\n      # ASGI requests must close DB connections instead of retaining one per thread.\n      CONN_MAX_AGE: "0"\n'
    candidate=config.with_name('compose.db-connections.candidate.yaml')
    candidate.write_text(text.replace(marker,replacement),encoding='utf-8')
    os.chmod(candidate,0o600)
    def compose(path,*args):
        p=subprocess.run(['docker','compose','-f',str(path),*args],cwd=root,capture_output=True,text=True,timeout=120)
        if p.returncode:
            raise RuntimeError('Compose command failed: '+' '.join(args[:2]))
        return p.stdout
    before=json.loads(compose(config,'config','--format','json'))
    after=json.loads(compose(candidate,'config','--format','json'))
    assert after['services']['web']['environment'].pop('CONN_MAX_AGE')=='0'
    before['services']['web']['environment'].pop('CONN_MAX_AGE',None)
    assert before==after, 'Unexpected resolved Compose difference'
    code,names=run('docker','ps','-aq')
    inventory=json.loads(run('docker','inspect',*names.split())[1])
    web=next(x for x in inventory if x['Name']=='/school-display-web-1')
    assert run('docker','image','inspect',web['Config']['Image'],'--format','{{.Id}}')[1].strip()==web['Image'], 'Image tag changed'
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=config.with_name(config.name+'.before-db-connections-'+stamp)
    shutil.copy2(config,backup)
    os.chmod(candidate,original_stat.st_mode & 0o777)
    os.chown(candidate,original_stat.st_uid,original_stat.st_gid)
    os.replace(candidate,config)
    emit('repair_started',{'backup':str(backup),'change':'services.web.environment.CONN_MAX_AGE=0'})
    try:
        compose(config,'up','-d','--no-deps','--no-build','--pull','never','--force-recreate','web')
        healthy=False
        for attempt in range(60):
            current=json.loads(run('docker','inspect','school-display-web-1')[1])[0]
            if current['State'].get('Health',{}).get('Status')=='healthy':
                healthy=True
                break
            time.sleep(2)
        assert healthy, 'Web health did not recover'
        assert current['Image']==web['Image'], 'Image changed unexpectedly'
        old_env=dict(e.split('=',1) for e in web['Config']['Env'])
        new_env=dict(e.split('=',1) for e in current['Config']['Env'])
        assert new_env.pop('CONN_MAX_AGE')=='0'
        old_env.pop('CONN_MAX_AGE',None)
        assert new_env==old_env, 'Unexpected runtime environment difference'
        assert release.read_text().strip()==expected_release
        others=[x for x in inventory if x['Name']!='/school-display-web-1']
        preserved=json.loads(run('docker','inspect',*[x['Id'] for x in others])[1])
        assert all(a['Id']==b['Id'] and a['State']['StartedAt']==b['State']['StartedAt'] for a,b in zip(others,preserved)), 'Another container changed'
        emit('repair_verified',{'web_id':current['Id'],'image':current['Image'],'health':'healthy','unchanged_other_containers':len(others),'release':expected_release})
    except Exception:
        shutil.copy2(backup,config)
        compose(config,'up','-d','--no-deps','--no-build','--pull','never','--force-recreate','web')
        emit('repair_rolled_back',{'backup':str(backup)})
        raise

if '--apply-school-display-db-fix' in sys.argv:
    apply_school_display_fix()
