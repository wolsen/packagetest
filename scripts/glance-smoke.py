"""Run inside a disposable chroot; exercise migrations and the real Glance API."""
import json
import sys
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

with tempfile.TemporaryDirectory(prefix='glance-smoke-') as directory:
    root = Path(directory)
    # Reserve a free loopback port; this VM may host another smoke session.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    # Use the shipped request-context middleware with a fixed test identity.
    # The legacy unauthenticated context has no project scope and cannot pass
    # current policy checks. This probes policy/API/storage without Keystone.
    paste = root / 'paste.ini'
    shipped_paste = Path('/etc/glance/glance-api-paste.ini').read_text()
    if 'osprofiler unauthenticated-context' not in shipped_paste:
        raise RuntimeError('Unexpected installed Glance paste configuration')
    paste.write_text(shipped_paste.replace('osprofiler unauthenticated-context', 'osprofiler context'))
    config = root / 'glance.conf'
    config.write_text(f'''[DEFAULT]
bind_host = 127.0.0.1
bind_port = {port}
workers = 1
show_image_direct_url = true
[database]
connection = sqlite:///{root}/glance.sqlite
[paste_deploy]
flavor =
config_file = {paste}
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
    with (root / 'api.log').open('w+') as log:
        process = subprocess.Popen(['glance-api', '--config-file', str(config)], stdout=log, stderr=log)
        try:
            url = f'http://127.0.0.1:{port}'
            for attempt in range(90):
                try:
                    try:
                        response = urlopen(url + '/', timeout=2)
                    except HTTPError as exc:
                        if exc.code != 300:  # Glance's version discovery response.
                            raise
                        response = exc
                    with response:
                        report['versions'] = json.load(response)
                    if 'versions' not in report['versions']:
                        raise RuntimeError('Missing API versions document')
                    break
                except Exception:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError('Glance exited: ' + log.read())
                    time.sleep(0.5)
            else:
                raise RuntimeError('Glance API did not become ready')
            headers = {'Content-Type': 'application/json', 'X-Identity-Status': 'Confirmed',
                       'X-User-Id': '11111111111111111111111111111111',
                       'X-Project-Id': '22222222222222222222222222222222',
                       'X-Roles': 'admin,member,reader', 'X-Auth-Token': 'packagetest'}
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
        except Exception as exc:
            if isinstance(exc, HTTPError):
                print(exc.read().decode(errors='replace'), file=sys.stderr)
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
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
