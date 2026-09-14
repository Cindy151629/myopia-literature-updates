"""Dedicated Git branch storage; no caches or expiring artifacts as database."""
import argparse, subprocess
from pathlib import Path

def git(*args, cwd=None, check=True):
    return subprocess.run(['git',*args],cwd=cwd,check=check,capture_output=True,text=True)

def restore(path):
    path=Path(path).resolve()
    found=git('ls-remote','--exit-code','--heads','origin','data',check=False)
    if found.returncode not in (0,2):raise RuntimeError('Cannot inspect persistent branch')
    if found.returncode==0:
        git('fetch','--no-tags','origin','data');git('worktree','add','--detach',str(path),'FETCH_HEAD')
    else:
        # Share Git object storage, but start data history with no source files.
        # All network operations stay in the original checkout: checkout@v6
        # scopes its temporary credentials there. Never copy credential values.
        git('worktree','add','--detach',str(path),'HEAD')
        git('switch','--orphan','data',cwd=path)
    git('config','user.name','github-actions[bot]',cwd=path)
    git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com',cwd=path)

def persist(path,message):
    path=Path(path)
    for name in ('state','public','publication-status.json'):
        if (path/name).exists():git('add','--',name,cwd=path)
    if git('diff','--cached','--quiet',cwd=path,check=False).returncode:
        git('commit','-m',message,cwd=path)
    commit=git('rev-parse','HEAD',cwd=path).stdout.strip()
    git('push','origin',commit+':refs/heads/data')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['restore','save']);p.add_argument('directory');p.add_argument('--message',default='Persist literature metadata and update status');a=p.parse_args()
    restore(a.directory) if a.command=='restore' else persist(a.directory,a.message)
