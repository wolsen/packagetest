"""Run inside a disposable chroot; exercise migrations and the real Glance API."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen

with tempfile.TemporaryDirectory(prefix='glance-smoke-') as directory:
    root = Path(directory)
    # Reserve a free loopback port; this VM may host another smoke session.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config = root / 'glance.conf'
    config.write_text(f'''[DEFAULT]
bind_host = 127.0.0.1
bind_port = {port}
workers = 1
show_image_direct_url = true
[database]
connection = sqlite:///{root}/glance.sqlite
[paste_deploy]
flavor = noauth
config_file = /etc/glance/glance-api-paste.ini
[glance_store]
stores = file
default_store = file
filesystem_store_datadir = {root}/images
''')
    (root / 'images').mkdir()
    migration = subprocess.run(['glance-manage', '--config-file', str(config), 'db_sync'],
                               capture_output=True, text=True, timeout=90)
    if migration.returncode:
        raise RuntimeError(migration.stdout + migration.stderr)
    report = {'database_migration': migration.stdout + migration.stderr}
    env = dict(os.environ, OS_GLANCE_DISABLE_EVENTLET_PATCHING='')
    with (root / 'api.log').open('w+') as log:
        process = subprocess.Popen(['glance-api', '--config-file', str(config)], stdout=log, stderr=log, env=env)
        try:
            url = f'http://127.0.0.1:{port}'
            for attempt in range(90):
                try:
                    with urlopen(url + '/versions', timeout=2) as response:
                        report['versions'] = json.load(response)
                    break
                except Exception:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError('Glance exited: ' + log.read())
                    time.sleep(0.5)
            else:
                raise RuntimeError('Glance API did not become ready')
            headers = {'Content-Type': 'application/json', 'X-User-Id': 'packagetest',
                       'X-Tenant-Id': 'packagetest', 'X-Roles': 'admin', 'X-Auth-Token': 'packagetest'}
            request = Request(url + '/v2/images', json.dumps({'name': 'packaging-smoke',
                              'disk_format': 'raw', 'container_format': 'bare'}).encode(), headers)
            with urlopen(request, timeout=15) as response:
                created = json.load(response)
            image_id = created['id']
            data = b'packagetest-glance-image\n'
            upload_headers = {**headers, 'Content-Type': 'application/octet-stream'}
            with urlopen(Request(url + f'/v2/images/{image_id}/file', data, upload_headers, method='PUT'), timeout=15) as response:
                assert response.status == 204
            with urlopen(Request(url + f'/v2/images/{image_id}/file', headers=headers), timeout=15) as response:
                assert response.read() == data
            with urlopen(Request(url + f'/v2/images/{image_id}', headers=headers, method='DELETE'), timeout=15) as response:
                assert response.status == 204
            report.update(image_id=image_id, image_round_trip='PASSED', result='SUCCEEDED')
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.seek(0)
            report['api_log'] = log.read()
    print(json.dumps(report))
