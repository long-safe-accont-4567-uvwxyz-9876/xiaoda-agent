import json
import ssl
import urllib.request

ctx = ssl.create_default_context()

# Get the v0.5.15 workflow runs that are still in progress
url = 'https://api.github.com/repos/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent/actions/runs?per_page=10&status=in_progress'
req = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json'})
with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
    data = json.loads(resp.read().decode())

for run in data.get('workflow_runs', []):
    run_id = run['id']
    print(f"Run #{run_id}: {run['display_title']} ({run['status']}) created={run['created_at']}")
    # Get jobs for this run
    jobs_url = f'https://api.github.com/repos/long-safe-accont-4567-uvwxyz-9876/xiaoda-agent/actions/runs/{run_id}/jobs'
    jobs_req = urllib.request.Request(jobs_url, headers={'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(jobs_req, timeout=15, context=ctx) as jobs_resp:
        jobs_data = json.loads(jobs_resp.read().decode())
    for job in jobs_data.get('jobs', []):
        print(f"  Job: {job['name']:40} status={job['status']:12} conclusion={job.get('conclusion') or 'N/A'}")
        for step in job.get('steps', []):
            if step['status'] == 'in_progress':
                print(f"    -> STUCK: {step['name']}")
            elif step['status'] == 'queued':
                print(f"    -> QUEUED: {step['name']}")
    print()
