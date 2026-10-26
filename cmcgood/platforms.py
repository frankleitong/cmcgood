"""GitHub / GitCode API 客户端。

只用到少量接口：列出组织/用户下的仓库、查询单个仓库、查询默认分支提交、创建仓库。
GitCode OpenAPI（v5）与 Gitee 风格兼容，鉴权使用 access_token 参数。
"""
from urllib.parse import quote

import requests

from .config import parse_namespace
from .util import parse_time, to_iso


class PlatformError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _commit_time(item):
    commit = item.get('commit') or {}
    for key in ('committer', 'author'):
        date = (commit.get(key) or {}).get('date')
        if date:
            return to_iso(parse_time(date))
    for key in ('committed_date', 'created_at', 'authored_date'):
        if item.get(key):
            return to_iso(parse_time(item[key]))
    return None


class BaseClient:
    platform = ''
    label = ''

    def __init__(self, api, web, token, timeout=20):
        self.api = api
        self.web = web
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()

    # --- 子类实现 ---
    def _headers(self):
        return {}

    def _params(self, params):
        return params

    def _normalize(self, item):
        raise NotImplementedError

    def list_repos(self, raw_namespace):
        raise NotImplementedError

    def create_repo(self, namespace, kind, name, description, private):
        raise NotImplementedError

    # --- 通用 ---
    def web_url(self, namespace, name):
        return '{}/{}/{}'.format(self.web, namespace, name)

    def _path(self, *parts):
        return '/' + '/'.join(quote(p, safe='') for p in parts)

    def _scrub(self, text):
        return text.replace(self.token, '***') if self.token else text

    def request(self, method, path, params=None, json=None):
        try:
            resp = self.session.request(method, self.api + path, params=self._params(dict(params or {})),
                                        json=json, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise PlatformError(self._scrub('{} 请求失败: {}'.format(self.label, e)))
        if resp.status_code >= 400:
            detail = resp.text[:300].replace('\n', ' ')
            raise PlatformError(self._scrub('{} {} {} -> HTTP {}: {}'.format(
                self.label, method, path, resp.status_code, detail)), resp.status_code)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            raise PlatformError('{} {} 返回的不是 JSON'.format(self.label, path))

    def paginate(self, path, params=None, per_page=100, max_pages=200):
        items = []
        for page in range(1, max_pages + 1):
            p = dict(params or {})
            p.update(page=page, per_page=per_page)
            data = self.request('GET', path, p)
            if not isinstance(data, list):
                raise PlatformError('{} {} 返回格式异常'.format(self.label, path))
            items.extend(data)
            if len(data) < per_page:
                break
        return items

    def get_repo(self, namespace, name):
        try:
            data = self.request('GET', self._path('repos', namespace, name))
        except PlatformError as e:
            if e.status == 404:
                return None
            raise
        return self._normalize(data) if isinstance(data, dict) else None

    def latest_activity(self, namespace, name, since_iso):
        """返回 (默认分支最近一次提交时间, since 以来的提交数[最多统计 100])。"""
        path = self._path('repos', namespace, name, 'commits')
        try:
            recent = self.request('GET', path, {'since': since_iso, 'per_page': 100}) or []
            if recent:
                return _commit_time(recent[0]), len(recent)
            last = self.request('GET', path, {'per_page': 1}) or []
            return (_commit_time(last[0]) if last else None), 0
        except PlatformError as e:
            # 空仓库在不同平台上会返回 409/404/400，先确认仓库本身是否存在
            if e.status in (400, 404, 409, 422):
                if self.get_repo(namespace, name) is not None:
                    return None, 0
                raise PlatformError('{} 上不存在仓库 {}/{}'.format(self.label, namespace, name), 404)
            raise


class GitHubClient(BaseClient):
    platform = 'github'
    label = 'GitHub'

    def _headers(self):
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                   'User-Agent': 'cmcgood'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        return headers

    def _normalize(self, item):
        return {
            'namespace': (item.get('owner') or {}).get('login') or '',
            'name': item.get('name') or '',
            'url': item.get('html_url') or '',
            'description': item.get('description') or '',
            'private': bool(item.get('private')),
            'archived': bool(item.get('archived')),
        }

    def list_repos(self, raw_namespace):
        kind, ns = parse_namespace(raw_namespace)
        if kind == 'user':
            items = None
            if self.token:
                me = self.request('GET', '/user') or {}
                if (me.get('login') or '').lower() == ns.lower():
                    # 令牌属于该用户时可以列出私有仓库
                    items = self.paginate('/user/repos', {'affiliation': 'owner', 'visibility': 'all'})
            if items is None:
                items = self.paginate(self._path('users', ns, 'repos'), {'type': 'owner'})
        else:
            items = self.paginate(self._path('orgs', ns, 'repos'), {'type': 'all'})
        return [self._normalize(i) for i in items]

    def create_repo(self, namespace, kind, name, description, private):
        path = '/user/repos' if kind == 'user' else self._path('orgs', namespace, 'repos')
        data = self.request('POST', path, json={'name': name, 'description': description, 'private': private})
        return self._normalize(data or {})


class GitCodeClient(BaseClient):
    platform = 'gitcode'
    label = 'GitCode'

    def _headers(self):
        return {'Accept': 'application/json', 'User-Agent': 'cmcgood'}

    def _params(self, params):
        if self.token:
            params['access_token'] = self.token
        return params

    def _normalize(self, item):
        full = item.get('path_with_namespace') or item.get('full_name') or ''
        namespace, name = '', item.get('path') or item.get('name') or ''
        if '/' in full:
            namespace, tail = full.rsplit('/', 1)
            name = name or tail
        if not namespace:
            ns_obj = item.get('namespace')
            if isinstance(ns_obj, dict):
                namespace = ns_obj.get('path') or ns_obj.get('name') or ''
        return {
            'namespace': namespace,
            'name': name,
            'url': item.get('html_url') or item.get('web_url') or '',
            'description': item.get('description') or '',
            'private': bool(item.get('private')),
            'archived': bool(item.get('archived')),
        }

    def list_repos(self, raw_namespace):
        kind, ns = parse_namespace(raw_namespace)
        seg = 'users' if kind == 'user' else 'orgs'
        return [self._normalize(i) for i in self.paginate(self._path(seg, ns, 'repos'))]

    def create_repo(self, namespace, kind, name, description, private):
        path = '/user/repos' if kind == 'user' else self._path('orgs', namespace, 'repos')
        body = {'name': name, 'path': name, 'description': description,
                'private': private, 'public': 0 if private else 1}
        data = self.request('POST', path, json=body)
        return self._normalize(data or {})


CLIENTS = {'github': GitHubClient, 'gitcode': GitCodeClient}


def get_client(cfg, platform):
    pcfg = cfg['PLATFORMS'].get(platform)
    if not pcfg:
        return None
    return CLIENTS[platform](pcfg['api'], pcfg['web'], pcfg['token'])


def web_url(cfg, platform, namespace, name):
    pcfg = cfg['PLATFORMS'].get(platform)
    return '{}/{}/{}'.format(pcfg['web'], namespace, name) if pcfg else ''
