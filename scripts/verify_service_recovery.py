"""独立数据库/网络/容器故障验收，不停正式数据库，不保存账号或访问令牌。"""
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def docker(*args):
    result = subprocess.run(['docker', *args], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('Docker测试操作失败：' + args[0] + '; ' + result.stderr[-600:])
    return (result.stdout + result.stderr).strip() if args[0] == 'logs' else result.stdout.strip()


def wait_until(check, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check(): return
        time.sleep(1)
    raise AssertionError('等待隔离测试条件超时')


def status(url):
    def parse(data):
        try: return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError): return {'format': 'non_json'}
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, parse(response.read())
    except urllib.error.HTTPError as error:
        return error.code, parse(error.read())
    except (OSError, TimeoutError):
        return 0, {}


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    suffix = uuid.uuid4().hex[:10]
    prefix = 'poc-recovery-test-' + suffix
    api, db, web, blocker = (prefix + '-' + name for name in ['api', 'db', 'web', 'ip-holder'])
    network, volume = prefix + '-network', prefix + '-pg'
    api_port, web_port = free_port(), free_port()
    base, direct = f'http://127.0.0.1:{web_port}', f'http://127.0.0.1:{api_port}'
    containers = []
    report = {'scope': '独立测试数据库、网络、卷和端口；只读使用平台Redis/MinIO健康服务', 'checks': [], 'status': 'RUNNING'}
    created_volume = created_network = False
    def passed(name):
        report['checks'].append(name)
        print('PASS', name, flush=True)
    def launch_api():
        docker('run', '-d', '--name', api, '--label', 'poc.recovery-test=' + suffix,
               '--network', network, '--network-alias', 'api', '-p', f'127.0.0.1:{api_port}:8000',
               '-e', 'DATABASE_URL=postgresql+psycopg://recovery:RecoveryTestOnly@recovery-db:5432/recovery',
               '-e', 'REDIS_URL=redis://poc-redis:6379/0', '-e', 'MINIO_ENDPOINT=poc-minio:9000',
               '-e', 'API_STARTUP_TIMEOUT=60', 'poc-api')
        containers.append(api)
        docker('network', 'connect', 'poc_default', api)
    try:
        docker('network', 'create', '--label', 'poc.recovery-test=' + suffix, network)
        created_network = True
        docker('volume', 'create', '--label', 'poc.recovery-test=' + suffix, volume)
        created_volume = True
        launch_api()
        wait_until(lambda: '数据库暂不可用' in docker('logs', api) or 'OperationalError' in docker('logs', api))
        # 检查存活且尚未ready，确认这是等待数据库而不是容器重启。
        assert json.loads(docker('inspect', api))[0]['State']['Running']
        assert status(direct + '/health/ready')[0] != 200
        passed('数据库尚未启动时API保持启动等待')
        docker('run', '-d', '--name', db, '--label', 'poc.recovery-test=' + suffix,
               '--network', network, '--network-alias', 'recovery-db',
               '--mount', f'type=volume,source={volume},target=/var/lib/postgresql/data',
               '-e', 'POSTGRES_USER=recovery', '-e', 'POSTGRES_PASSWORD=RecoveryTestOnly',
               '-e', 'POSTGRES_DB=recovery', 'postgres:16-alpine')
        containers.append(db)
        wait_until(lambda: status(direct + '/health/ready')[0] == 200)
        assert json.loads(docker('inspect', api))[0]['RestartCount'] == 0
        passed('数据库迟到后API自行初始化，未退出或重启')

        docker('run', '-d', '--name', web, '--label', 'poc.recovery-test=' + suffix,
               '--network', network, '-p', f'127.0.0.1:{web_port}:3000', 'poc-web-console')
        containers.append(web)
        wait_until(lambda: status(base + '/health/ready')[0] == 200)
        assert status(base + '/api/auth/status')[0] == 200
        passed('前端代理与后端就绪探针可访问')

        docker('stop', db)
        wait_until(lambda: status(direct + '/health/ready')[0] == 503)
        assert status(direct + '/health/live')[0] == 200
        code, body = status(base + '/api/auth/status')
        assert code == 503 and body['detail'] == '数据库暂不可用，请稍后重试', (code, body)
        docker('start', db)
        wait_until(lambda: status(base + '/health/ready')[0] == 200)
        assert status(base + '/api/auth/status')[0] == 200
        assert json.loads(docker('inspect', api))[0]['RestartCount'] == 0
        passed('运行中数据库断开返回503，恢复后API无需重启')

        old_ip = json.loads(docker('inspect', api))[0]['NetworkSettings']['Networks'][network]['IPAddress']
        docker('rm', '-f', api)
        containers.remove(api)
        # 占住旧地址，保证新API实际获得不同IP，不能只测试同地址重启。
        docker('run', '-d', '--name', blocker, '--label', 'poc.recovery-test=' + suffix,
               '--network', network, '--ip', old_ip, 'nginx:latest')
        containers.append(blocker)
        launch_api()
        new_ip = json.loads(docker('inspect', api))[0]['NetworkSettings']['Networks'][network]['IPAddress']
        assert new_ip != old_ip
        wait_until(lambda: status(base + '/health/ready')[0] == 200)
        assert status(base + '/api/auth/status?test=ip-change')[0] == 200
        passed('后端地址变化后前端DNS自动恢复，无需重启前端')
        report['status'] = 'PASS'
    except Exception:
        report['status'] = 'FAIL'
        raise
    finally:
        for name in reversed(containers):
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        if created_network: subprocess.run(['docker', 'network', 'rm', network], capture_output=True)
        if created_volume: subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
        output = ROOT / 'tmp/reports/service-recovery-result.json'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__': main()
