"""Documentation sites as context: pages found through llms.txt or
sitemap.xml on one registered origin, split into sections at headings.

Standard library only. A page is fetched with its stored validators
(If-None-Match, If-Modified-Since); a 304, or text whose hash did not
change, writes nothing. A section that disappears, or a page that
returns 404 or 410, is recorded as deleted rather than erased. Other
origins, pages robots.txt disallows and non-text content are refused
and counted. See docs/context-sources.md.
"""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

from .sources import Batch, SourceError
from .store import Invalid

MISSING_CREDENTIALS = ''  # public documentation needs none
ENABLE = 'python -m bridge.sources add docs --repo owner/name --target https://docs.example.com --workspace-shared'
LIMITS = ('Only the registered origin is read, through llms.txt or sitemap.xml, else the page itself',
          'Content behind a login is not fetched')
USER_AGENT = 'RavenContext/1 (+documentation sync)'
MAX_BYTES = 2 * 1024 * 1024
DEFAULT_PAGES = 200
SECTION_CHARS = 6000
TEXT_TYPES = ('text/html', 'text/plain', 'text/markdown', 'text/x-markdown', 'application/xhtml+xml')


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only within the origin being read."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise urllib.error.HTTPError(req.full_url, code, 'redirect leaves the registered origin', headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Response:
    def __init__(self, status, body=b'', headers=None, url=''):
        self.status, self.body, self.headers, self.url = status, body, headers or {}, url

    def header(self, name):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return ''


class HTTPClient:
    """GET with conditional headers, a size bound and same-origin redirects."""

    def __init__(self, timeout=20):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(_SameOrigin)

    def get(self, url, headers=None):
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, **(headers or {})})
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(MAX_BYTES + 1)
                return Response(resp.status, body, dict(resp.headers.items()), resp.geturl())
        except urllib.error.HTTPError as error:
            if error.code in (304, 404, 410):
                return Response(error.code, b'', dict(error.headers.items()) if error.headers else {}, url)
            retry = error.headers.get('Retry-After') if error.headers else None
            raise SourceError(f'{url} returned HTTP {error.code}',
                              retry_after=float(retry) if retry and retry.isdigit() else None) from error
        except (urllib.error.URLError, OSError) as error:
            raise SourceError(f'{url} is unreachable: {getattr(error, "reason", error)}') from error


def client_from_env():
    return HTTPClient()


def identity(connection):
    return 'generic', 'docs:' + _origin(connection['target'])


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return f'{parts.scheme}://{parts.netloc}'.lower()


def normalize(target, options):
    parts = urllib.parse.urlsplit(target)
    if parts.scheme not in ('https', 'http') or not parts.netloc:
        raise Invalid('A documentation source is an http(s) URL')
    if parts.scheme == 'http' and parts.hostname not in ('localhost', '127.0.0.1'):
        raise Invalid('Use https for a documentation site; plain http is accepted only on this machine')
    if parts.username or parts.password:
        raise Invalid('Do not put credentials in a documentation URL')
    pages = options.get('max_pages', DEFAULT_PAGES)
    if not isinstance(pages, int) or isinstance(pages, bool) or not 1 <= pages <= 2000:
        raise Invalid('max_pages must be between 1 and 2000')
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', parts.query, '')), \
        {'max_pages': pages}


def probe(client, target, options):
    """The site is readable if the page, its llms.txt or its sitemap loads."""
    origin = _origin(target)
    for url in (target, origin + '/llms.txt', origin + '/sitemap.xml'):
        if client.get(url).status == 200:
            return origin
    raise Invalid(f'{target} did not load, and the site has no llms.txt or sitemap.xml; register a page that loads')


# ---------------- text ----------------

class _Text(HTMLParser):
    """HTML to text: headings become markdown headings, code stays whole,
    navigation and scripts are dropped."""
    SKIP = {'script', 'style', 'nav', 'footer', 'header', 'noscript', 'svg', 'form'}
    BLOCK = {'p', 'div', 'section', 'article', 'li', 'tr', 'br', 'table', 'ul', 'ol', 'blockquote', 'main'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.pre, self.title, self._in_title = [], 0, 0, '', False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag == 'title':
            self._in_title = True
        elif re.fullmatch(r'h[1-6]', tag):
            self.out.append('\n\n' + '#' * int(tag[1]) + ' ')
        elif tag == 'pre':
            self.pre += 1
            self.out.append('\n```\n')
        elif tag in self.BLOCK:
            self.out.append('\n')

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == 'title':
            self._in_title = False
        elif re.fullmatch(r'h[1-6]', tag):
            self.out.append('\n')
        elif tag == 'pre':
            self.pre = max(0, self.pre - 1)
            self.out.append('\n```\n')
        elif tag in self.BLOCK:
            self.out.append('\n')

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self.skip:
            self.out.append(data if self.pre else re.sub(r'\s+', ' ', data))


def to_text(body, content_type):
    """The page as text and its title."""
    raw = body.decode('utf-8', 'replace')
    if 'html' not in content_type:
        title = next((line.lstrip('# ').strip() for line in raw.splitlines() if line.startswith('# ')), '')
        return raw.strip(), title
    parser = _Text()
    parser.feed(raw)
    text = re.sub(r'[ \t]+\n', '\n', ''.join(parser.out))
    return re.sub(r'\n{3,}', '\n\n', text).strip(), parser.title.strip()


def sections(text, title):
    """Split at headings; a long section is split at paragraphs and never
    inside a code block. Each part is (heading, text)."""
    parts, heading, current = [], title, []
    fence = False
    for line in text.splitlines():
        if line.startswith('```'):
            fence = not fence
        if not fence and re.match(r'#{1,6} ', line) and current:
            parts.append((heading, '\n'.join(current).strip()))
            current = []
        if not fence and re.match(r'#{1,6} ', line):
            heading = line.lstrip('#').strip() or heading
        current.append(line)
    if current:
        parts.append((heading, '\n'.join(current).strip()))
    out = []
    for heading, body in parts:
        if not body:
            continue
        while len(body) > SECTION_CHARS:
            cut = body.rfind('\n\n', 0, SECTION_CHARS)
            if cut <= 0 or body[:cut].count('```') % 2:
                cut = SECTION_CHARS
            out.append((heading, body[:cut].strip()))
            body = body[cut:].strip()
        out.append((heading, body))
    return out


# ---------------- discovery ----------------

def _links_from_llms(text, base):
    return [urllib.parse.urljoin(base, m) for m in re.findall(r'\]\(([^)\s]+)\)', text)]


def _links_from_sitemap(client, url, origin, depth=0):
    response = client.get(url)
    if response.status != 200 or depth > 2:
        return []
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError:
        return []
    ns = '{http://www.sitemaps.org/schemas/sitemap/0.9}'
    if root.tag.endswith('sitemapindex'):
        out = []
        for loc in root.iter(ns + 'loc'):
            if _origin(loc.text or '') == origin:
                out.extend(_links_from_sitemap(client, loc.text.strip(), origin, depth + 1))
        return out
    return [loc.text.strip() for loc in root.iter(ns + 'loc') if loc.text]


def discover(client, target):
    """Pages to read: llms.txt links, else sitemap.xml, else the page."""
    origin = _origin(target)
    llms = client.get(origin + '/llms.txt')
    if llms.status == 200 and llms.body.strip():
        pages = _links_from_llms(llms.body.decode('utf-8', 'replace'), origin + '/')
        if pages:
            return pages, 'llms.txt'
    pages = _links_from_sitemap(client, origin + '/sitemap.xml', origin)
    if pages:
        return pages, 'sitemap.xml'
    return [target], 'page'


def _robots(client, origin):
    parser = urllib.robotparser.RobotFileParser()
    response = client.get(origin + '/robots.txt')
    parser.parse(response.body.decode('utf-8', 'replace').splitlines() if response.status == 200 else [])
    return parser


MAX_URL = 480


def _chunk_id(url, n):
    """The section's stable identity: the full URL in external_id, a
    fixed-length digest as the short ref so two long URLs never collide."""
    return f'{url}#section-{n}'


def _short_ref(chunk):
    return 'doc:' + hashlib.sha256(chunk.encode()).hexdigest()[:32]


def _scope(origin):
    return json.dumps({'docs_origin': origin, 'audience': 'workspace-shared'}, sort_keys=True)


def collect(connection, client, items):
    """One pass over the site. items maps page URL to its validators, hash
    and the section ids it produced last time."""
    target, origin = connection['target'], _origin(connection['target'])
    limit = connection['options'].get('max_pages', DEFAULT_PAGES)
    batch = Batch(label=origin)
    found, how = discover(client, target)
    robots = _robots(client, origin)
    pages = []
    for url in found:
        url = urllib.parse.urldefrag(url)[0]
        if _origin(url) != origin:
            batch.refuse('other_origin')
        elif len(url) > MAX_URL:
            batch.refuse('url_too_long')
        elif not robots.can_fetch(USER_AGENT, url):
            batch.refuse('robots_disallowed')
        elif url not in pages:
            pages.append(url)
    if len(pages) > limit:
        batch.refuse('over_page_limit', len(pages) - limit)
        pages = pages[:limit]
    for url in pages:
        state = items.get(url) or {}
        headers = {}
        if state.get('etag'):
            headers['If-None-Match'] = state['etag']
        if state.get('last_modified'):
            headers['If-Modified-Since'] = state['last_modified']
        response = client.get(url, headers)
        if response.status == 304:
            continue
        if response.status in (404, 410):
            _gone(batch, url, state, 'page removed')
            continue
        if len(response.body) > MAX_BYTES:
            batch.refuse('too_large')
            continue
        content_type = response.header('Content-Type').split(';')[0].strip().lower() or 'text/html'
        if content_type not in TEXT_TYPES:
            batch.refuse('not_text')
            continue
        text, title = to_text(response.body, content_type)
        digest = hashlib.sha256(text.encode()).hexdigest()
        detail = {'etag': response.header('ETag'), 'last_modified': response.header('Last-Modified'),
                  'hash': digest, 'sections': state.get('sections', []), 'title': title or url}
        if digest == state.get('hash'):
            batch.items[url] = detail
            continue
        parts = sections(text, title or url)
        ids = []
        for n, (heading, body) in enumerate(parts):
            ref = _chunk_id(url, n)
            ids.append(ref)
            name = (title or url) + (f' — {heading}' if heading and heading != title else '')
            batch.records.append({'kind': 'doc', 'ref': _short_ref(ref), 'provider': 'generic',
                                  'namespace': 'docs:' + origin, 'external_id': ref, 'title': name[:300],
                                  'body': body[:20000], 'url': url, 'updated_at': '', 'status': '',
                                  'resolved': False, 'source_version': hashlib.sha256(body.encode()).hexdigest(),
                                  'availability': 'available', 'paths': [],
                                  'access_scope': _scope(origin)})
        for ref in set(state.get('sections', [])) - set(ids):
            _section_gone(batch, ref, state.get('title') or url, url)
        detail['sections'] = ids
        batch.items[url] = detail
    for url in set(items) - set(pages):
        if not url.startswith(('http://', 'https://')):
            continue
        _gone(batch, url, items[url], 'no longer listed by the site')
    batch.cursor = how
    return batch


def _section_gone(batch, ref, title, url):
    batch.records.append({'kind': 'doc', 'ref': _short_ref(ref), 'provider': 'generic',
                          'namespace': 'docs:' + _origin(url), 'external_id': ref, 'title': title[:300],
                          'body': 'This section is no longer on the page.', 'url': url, 'status': 'deleted',
                          'resolved': False, 'availability': 'deleted', 'paths': [], 'access_scope': _scope(_origin(url))})


def _gone(batch, url, state, why):
    for ref in state.get('sections', []):
        _section_gone(batch, ref, state.get('title') or url, url)
    batch.items[url] = None
