#!/usr/bin/env python3
"""Stop obsolete runs of this pipeline, including jobs guarded by always()."""
import json
import os
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

repo = os.environ['GITHUB_REPOSITORY']
branch = os.environ['GITHUB_REF_NAME']
current = int(os.environ['GITHUB_RUN_ID'])
headers = {'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
           'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
base = 'https://api.github.com/repos/' + repo + '/actions/runs'
with urlopen(Request(base + '?branch=' + quote(branch, safe='') + '&per_page=30', headers=headers), timeout=30) as response:
    runs = json.load(response)['workflow_runs']
for run in runs:
    if (run['id'] >= current or run['status'] == 'completed'
            or run['head_branch'] != branch or run['name'] != 'OpenStack 2026.2 snapshot pipeline'):
        continue
    try:
        with urlopen(Request(base + '/' + str(run['id']) + '/force-cancel', headers=headers, data=b'', method='POST'), timeout=30):
            pass
        print('Cancelled superseded pipeline run', run['id'])
    except HTTPError as error:
        if error.code != 409:  # Run may have finished between list and cancel.
            raise
