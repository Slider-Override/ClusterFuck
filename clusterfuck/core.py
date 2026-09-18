"""Persistent node configuration, scheduling and validated iperf arguments."""
import copy
import datetime as dt
import json
import math
import os
import re
import secrets
from pathlib import Path
from zoneinfo import ZoneInfo


def save(path, value):
    path = Path(path)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    if os.name == 'posix':
        os.chmod(temp, 0o600)
    temp.replace(path)


def load(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if Path(path).exists() else copy.deepcopy(default)


def initial_config():
    return dict(name=os.getenv('NODE_NAME', 'ClusterFuck'), timezone='Europe/Berlin',
                public_url='', public_host='', public_iperf_port=5201,
                server_enabled=True, paused_until=0, schedules=[], peers=[], targets=[], profiles=[])


def integer(value, low, high, label):
    if isinstance(value, bool):
        raise ValueError(f'{label}: ungültiger Wert')
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label}: Zahl erforderlich') from None
    if str(result) != str(value) or not low <= result <= high:
        raise ValueError(f'{label}: erlaubt sind {low}–{high}')
    return result


def host(value):
    if not isinstance(value, str) or len(value) > 253 or not re.fullmatch(r'[a-zA-Z0-9_.:\-]+', value) or value.startswith('-'):
        raise ValueError('Host: Hostname oder IP ohne URL/Pfad erforderlich')
    return value


def options(raw):
    result = dict(duration=integer(raw.get('duration', 30), 1, 3600, 'Dauer'),
                  interval=integer(raw.get('interval', 1), 1, 10, 'Intervall'),
                  streams=integer(raw.get('streams', 1), 1, 32, 'Streams'),
                  omit=integer(raw.get('omit', 0), 0, 30, 'Anlaufphase'),
                  protocol=raw.get('protocol', 'tcp'), direction=raw.get('direction', 'upload'),
                  ip_version=str(raw.get('ip_version', 'auto')), bitrate=str(raw.get('bitrate', '100M')),
                  zerocopy=bool(raw.get('zerocopy', False)), no_delay=bool(raw.get('no_delay', False)))
    if result['protocol'] not in ('tcp', 'udp') or result['direction'] not in ('upload', 'download', 'bidir'):
        raise ValueError('Ungültiges Protokoll oder Richtung')
    if result['ip_version'] not in ('auto', '4', '6'):
        raise ValueError('Ungültige IP-Version')
    if not re.fullmatch(r'[1-9][0-9]{0,11}[KMG]?', result['bitrate']):
        raise ValueError('Bitrate: z.B. 100M, 1G oder 1000000')
    return result


def command(target, settings):
    settings = options(settings)
    cmd = [os.getenv('IPERF_BINARY', 'iperf3'), '-c', host(target['host']), '-p',
           str(integer(target['port'], 1, 65535, 'Zielport')), '--json-stream', '--forceflush',
           '--connect-timeout', '5000', '-t', str(settings['duration']), '-i', str(settings['interval']),
           '-P', str(settings['streams']), '-O', str(settings['omit'])]
    if settings['protocol'] == 'udp':
        cmd += ['-u', '-b', settings['bitrate']]
    if settings['direction'] == 'download':
        cmd += ['-R']
    elif settings['direction'] == 'bidir':
        cmd += ['--bidir']
    if settings['ip_version'] != 'auto':
        cmd += ['-' + settings['ip_version']]
    if settings['zerocopy']:
        cmd += ['-Z']
    if settings['no_delay']:
        cmd += ['-N']
    return cmd


def blocked(config, timestamp=None):
    timestamp = timestamp if timestamp is not None else dt.datetime.now().timestamp()
    pause = float(config.get('paused_until', 0))
    if pause == -1 or pause > timestamp:
        return 'Manuell pausiert'
    current = dt.datetime.fromtimestamp(timestamp, ZoneInfo(config['timezone']))
    minute = current.hour * 60 + current.minute
    for rule in config.get('schedules', []):
        if not rule.get('enabled', True):
            continue
        start, end = (int(x[:2]) * 60 + int(x[3:]) for x in (rule['start'], rule['end']))
        days = rule['days']
        if start < end:
            active = current.weekday() in days and start <= minute < end
        else:
            active = (current.weekday() in days and minute >= start) or ((current.weekday() - 1) % 7 in days and minute < end)
        if active:
            return rule.get('label', 'Sperrzeit')
    return ''


def ensure_window(config, start, seconds):
    # Evaluate the full window, including setup margin and boundaries.
    for timestamp in range(int(start), int(start + seconds) + 1):
        reason = blocked(config, timestamp)
        if reason:
            raise ValueError(f'Nicht bereit: {reason} (Test berührt eine Sperrzeit)')


def validate_config(raw, previous, insecure=False):
    from urllib.parse import urlsplit
    cfg = copy.deepcopy(raw)
    cfg['name'] = str(cfg.get('name', '')).strip()[:80]
    if not cfg['name']:
        raise ValueError('Node-Name erforderlich')
    ZoneInfo(cfg['timezone'])
    cfg['paused_until'] = float(cfg.get('paused_until', 0))
    if not math.isfinite(cfg['paused_until']) or cfg['paused_until'] < -1:
        raise ValueError('Ungültige Pausenzeit')
    if type(cfg.get('server_enabled')) is not bool:
        raise ValueError('Server-Einstellung muss true/false sein')
    cfg['public_iperf_port'] = integer(cfg.get('public_iperf_port', 5201), 1, 65535, 'Öffentlicher iperf-Port')
    if cfg.get('public_host'):
        host(cfg['public_host'])
    for key in ('peers', 'targets', 'profiles', 'schedules'):
        if not isinstance(cfg.get(key), list) or len(cfg[key]) > 100:
            raise ValueError(f'{key}: maximal 100 Einträge')
    for rule in cfg['schedules']:
        if not isinstance(rule['days'], list) or not rule['days'] or any(type(d) is not int or d not in range(7) for d in rule['days']):
            raise ValueError('Wochentage: 0 (Montag) bis 6 (Sonntag)')
        for key in ('start', 'end'):
            if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', rule[key]):
                raise ValueError('Uhrzeit muss HH:MM sein')
    for key in ('peers', 'targets', 'profiles'):
        ids = set()
        for item in cfg[key]:
            item['id'] = item.get('id') or secrets.token_hex(8)
            if item['id'] in ids or item['id'] == 'self' or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', item['id']):
                raise ValueError('IDs müssen eindeutig sein')
            ids.add(item['id'])
    known_peers = {p['id'] for p in cfg['peers']}
    old = {p['id']: p for p in previous['peers']}
    for peer in cfg['peers']:
        parsed = urlsplit(peer['url'])
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Peer-Adresse: vollständige Basis-URL ohne Pfad/Zugangsdaten')
        if parsed.scheme not in (('http', 'https') if insecure else ('https',)):
            raise ValueError('Peers benötigen HTTPS; HTTP nur im expliziten lokalen Testmodus')
        parsed.port
        peer['url'] = peer['url'].rstrip('/')
        peer['fingerprint'] = peer.get('fingerprint', '').replace(':', '').lower().strip()
        if parsed.scheme == 'https' and not re.fullmatch(r'[a-f0-9]{64}', peer['fingerprint']):
            raise ValueError('SHA256-Zertifikatfingerabdruck mit 64 Hex-Zeichen erforderlich')
        peer['token'] = peer.get('token') or old.get(peer['id'], {}).get('token', '')
        if len(peer['token']) < 20 or len(peer['token']) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in peer['token']):
            raise ValueError('Peer-Token fehlt oder ist ungültig')
    for target in cfg['targets']:
        host(target['host'])
        target['port'] = integer(target['port'], 1, 65535, 'Zielport')
        if target.get('node_id') and target['node_id'] not in known_peers | {'self'}:
            raise ValueError('Testziel verweist auf unbekannten Node')
    for profile in cfg['profiles']:
        profile['options'] = options(profile['options'])
    return cfg
