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
