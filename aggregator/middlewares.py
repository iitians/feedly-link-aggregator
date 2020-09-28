# MIT License
#
# Copyright (c) 2020 Tony Wu <tony[dot]wu(at)nyu[dot]edu>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import gzip
import logging
import pickle
import time
from collections import deque
from contextlib import suppress
from threading import Event, Thread
from typing import Callable, List
from urllib.parse import urlsplit

import simplejson as json
from scrapy import Spider
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response
from scrapy.signals import request_dropped, request_scheduled, spider_closed
from scrapy.spidermiddlewares.depth import DepthMiddleware
from scrapy.utils.url import url_is_from_any_domain
from twisted.internet.defer import DeferredList
from twisted.python.failure import Failure

from .feedly import build_api_url, get_feed_uri
from .requests import FinishedRequest, ProbeRequest
from .utils import LOG_LISTENER
from .utils import colored as _
from .utils import guard_json, is_rss_xml, wait


def filter_domains(request: Request, spider: Spider):
    domains = spider.config['FOLLOW_DOMAINS']
    feed_url = request.meta.get('feed_url') or request.meta.get('search_query')
    if not feed_url or domains is None:
        return True
    return url_is_from_any_domain(feed_url, domains)


class ConditionalDepthSpiderMiddleware(DepthMiddleware):
    def __init__(self, maxdepth, stats, verbose_stats=False, prio=1):
        super().__init__(maxdepth, stats, verbose_stats=verbose_stats, prio=prio)

    def process_spider_output(self, response, result, spider):
        if not self.maxdepth:
            self.maxdepth = spider.config.getint('DEPTH_LIMIT')
        should_increase = []
        other_items = []
        for r in result:
            if not isinstance(r, Request):
                other_items.append(r)
                continue
            increase_in = r.meta.get('inc_depth', 0)
            if increase_in == 1:
                should_increase.append(r)
                continue
            elif increase_in > 1:
                r.meta['inc_depth'] = increase_in - 1
            other_items.append(r)
        other_items.extend(super().process_spider_output(response, should_increase, spider))
        return other_items


class FeedProbingDownloaderMiddleware:
    def __init__(self):
        self.logger = logging.getLogger('worker.prober')
        self.initialized = False

    def init(self, spider):
        self.test_status = spider.config.get('FEED_STATE_SELECT', 'all') in {'dead', 'dead+', 'alive', 'alive+'}
        self.initialized = True

    async def process_request(self, request: ProbeRequest, spider):
        if not self.initialized:
            self.init(spider)
        if not isinstance(request, ProbeRequest):
            return

        download = spider.crawler.engine.download
        meta = request.meta
        feeds = meta['try_feeds']
        query = meta['search_query']
        valid_feeds = []

        self.logger.info(_(f'Probing {query}', color='grey'))

        queries = []
        for feed_id in feeds:
            url = spider.get_streams_url(feed_id, count=1)
            queries.append(download(Request(url, meta={'feed': feed_id}), spider))
        results = await DeferredList(queries, consumeErrors=True)

        valid_feeds = {}
        feed_info = {}
        for successful, response in results:
            if not successful:
                continue
            data = guard_json(response.text)
            if data.get('items'):
                feed = response.meta['feed']
                valid_feeds[feed] = None
                feed_info[feed] = self.feed_info(data)

        if not valid_feeds and spider.config.getbool('ENABLE_SEARCH'):
            response = await spider.crawler.engine.download(Request(build_api_url('search', query=query)), spider)
            data = guard_json(response.text)
            if data.get('results'):
                for feed in data['results']:
                    feed_id = feed['feedId']
                    valid_feeds[feed_id] = None
                    feed_info[feed_id] = self.feed_info(data)

        if self.test_status:
            await self.probe_feed_status(valid_feeds, download, spider)

        meta['valid_feeds'] = valid_feeds
        meta['feed_info'] = feed_info
        del meta['try_feeds']
        if meta.get('inc_depth'):
            meta['depth'] -= 1
        return Response(url=request.url, request=request)

    def feed_info(self, data):
        return {
            'url': get_feed_uri(data['id']),
            'title': data.get('title', ''),
        }

    async def probe_feed_status(self, feeds, download, spider):
        requests = []
        for feed in feeds:
            feed = feed
            requests.append(download(Request(
                get_feed_uri(feed), method='HEAD', meta={
                    'url': feed,
                    'max_retry_times': 2,
                    'download_timeout': 10,
                }), spider))

        results = await DeferredList(requests, consumeErrors=True)

        for successful, response in results:
            if isinstance(response, Failure):
                response = response.value.response
            if not successful:
                dead = True
            elif response.status not in {200, 206, 405}:
                dead = True
            elif not is_rss_xml(response):
                dead = True
            else:
                dead = False
            feeds[response.meta['url']] = dead


class RequestPersistenceDownloaderMiddleware:
    @classmethod
    def from_crawler(cls, crawler):
        instance = cls()
        crawler.signals.connect(instance.eager_process, request_scheduled)
        crawler.signals.connect(instance._remove_request, request_dropped)
        crawler.signals.connect(instance._close, spider_closed)
        return instance

    def __init__(self):
        self.feed = None
        self.instructions = deque()
        self.logger = logging.getLogger('worker.request')
        self.closing = Event()
        self.thread = Thread(None, target=self.worker, name='RequestPersistenceThread',
                             args=(self.closing,), daemon=False)
        self.future = None
        self.initialized = False

    def worker(self, closing: Event):
        while closing.wait(20):
            try:
                self.archive()
            except Exception as e:
                self.logger.error(e, exc_info=True)
        self.archive()

    def archive(self):
        archive = self.load_archive()
        for action, key, data in self.instructions:
            if action == 'add':
                archive[key] = data
            if action == 'remove':
                archive.pop(key, None)
        self.instructions.clear()
        self.dump_archive(archive)
        self.dump_info({'crawling': self.feed})
        return archive

    def load_archive(self):
        requests = {}
        with suppress(EOFError, FileNotFoundError):
            with gzip.open(self.archive_path, 'rb') as f:
                requests = pickle.load(f)
        return requests

    def dump_archive(self, archive):
        with gzip.open(self.archive_path, 'wb') as f:
            pickle.dump(archive, f)

    def load_info(self):
        info = {}
        with suppress(EOFError, FileNotFoundError, json.JSONDecodeError):
            with open(self.info_path) as f:
                info = json.load(f)
        return info

    def dump_info(self, info):
        s = json.dumps(info)
        with open(self.info_path, 'wb+', buffering=0) as f:
            f.write(s.encode())

    def init(self, spider):
        self.feed = spider.config['FEED']
        output = spider.config['OUTPUT']
        self.info_path = output / 'info.json'
        self.archive_path = output / 'requests.pickle.gz'
        self.thread.start()
        self.initialized = True

    def eager_process(self, request, spider):
        with suppress(IgnoreRequest):
            self.process_request(request, spider, True)

    def process_request(self, request: Request, spider, idempotent=False):
        if not self.initialized:
            self.init(spider)

        meta = request.meta
        action = meta.get('_persist', None)
        if not action:
            if request.url == 'https://httpbin.org/status/204':
                raise ValueError
            return

        if action == 'release' and not idempotent:
            return self._continue(meta, request, spider)
        if action == 'add':
            self._add_request(request)
            if not idempotent:
                del meta['_persist']
            return
        if action == 'remove':
            self._remove_request(request)
            raise IgnoreRequest()

    def process_exception(self, request: Request, exception, spider):
        if isinstance(request, FinishedRequest):
            return
        if isinstance(exception, IgnoreRequest):
            return FinishedRequest(meta=request.meta)

    def _continue(self, meta, request, spider):
        if self.archive_path.exists():
            self.logger.info('Restoring persisted requests...')
        requests = {**self.load_archive()}
        feed = spider.config['FEED']
        resume_feed = self.load_info().get('crawling', '')

        if requests and feed != resume_feed:
            self.logger.info(_(f'Found unfinished crawl with {len(requests)} pending request(s)', color='cyan'))
            self.logger.info(_(f"Continue crawling '{resume_feed}'?", color='cyan'))
            self.logger.info(_(f"Start new crawl with '{feed}'?", color='cyan'))
            self.logger.info(_('Or exit?', color='cyan'))
            action = 'x'
        elif requests:
            action = 'c'
        else:
            action = 's'

        self.feed = resume_feed
        LOG_LISTENER.stop()
        while action not in 'cse':
            action = input('(continue/start/exit) [c]: ')[:1]
        LOG_LISTENER.start()

        if action == 'e':
            raise IgnoreRequest()
        elif action == 's':
            for k in requests:
                self.instructions.append(('remove', k, None))
            requests = {}
            self.feed = feed

        meta['_requests'] = requests
        del meta['_persist']
        return Response(url=request.url, request=request)

    def _add_request(self, request: Request):
        for_pickle = {
            'url': request.url,
            'method': request.method,
            'callback': request.callback.__name__,
            'meta': {**request.meta, '_time_pickled': time.perf_counter()},
            'priority': request.priority,
        }
        self.instructions.append(('add', request.meta['pkey'],
                                  (request.__class__, for_pickle)))

    def _remove_request(self, request: Request, spider=None):
        if 'pkey' not in request.meta:
            return
        self.instructions.append(('remove', request.meta['pkey'], None))

    def _close(self, spider=None, reason=None):
        if self.closing.is_set():
            return
        self.closing.set()
        self.thread.join(2)
        archive = self.archive()
        if len(archive):
            self.logger.info(_(f'# of requests persisted to filesystem: {len(archive)}', color='cyan'))


class RequestFilterDownloaderMiddleware:
    DEFAULT_FILTERS = {
        filter_domains: 300,
    }

    def __init__(self):
        self.tests: List[Callable[[Request, Spider], bool]] = []
        self._initialized = False

    def init(self, spider: Spider):
        tests = {**self.DEFAULT_FILTERS, **spider.config.get('REQUEST_FILTERS', {})}
        self.tests = [t[0] for t in sorted(tests.items(), key=lambda t: t[1])]
        self._initialized = True

    def process_request(self, request: Request, spider):
        if not self._initialized:
            self.init(spider)
        if request.meta.get('no_filter'):
            return
        for t in self.tests:
            result = t(request, spider)
            if not result:
                ignore = request.meta.get('if_ignore')
                if ignore:
                    ignore()
                raise IgnoreRequest()
            if isinstance(result, Request):
                result.meta['no_filter'] = True
                return result
        proceed = request.meta.get('if_proceed')
        if proceed:
            proceed()


class AuthorizationDownloaderMiddleware:
    def process_request(self, request: Request, spider):
        if request.headers.get('Authorization'):
            return
        auth = request.meta.get('auth')
        if auth:
            request = request.copy()
            request.headers['Authorization'] = f'OAuth {auth}'
            return request


class HTTPErrorDownloaderMiddleware:
    def __init__(self, crawler):
        self.crawler = crawler
        self.log = logging.getLogger('worker.ratelimiting')

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_response(self, request, response: Response, spider):
        if response.status == 401:
            self.log.warn('Server returned HTTP 401 Unauthorized.')
            self.log.warn('This is because you are accessing an API that requires authorization, and')
            self.log.warn('your either did not provide, or provided a wrong access token.')
            self.log.warn(f'URL: {request.url}')
            raise IgnoreRequest()
        if response.status == 429 and urlsplit(request.url) == 'cloud.feedly.com':
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                retry_after = int(retry_after)
                self.log.warn('Server returned HTTP 429 Too Many Requests.')
                self.log.warn('Either your IP address or your developer account is being rate-limited.')
                self.log.warn(f'Retry-After = {retry_after}s')
                self.log.warn(f'Scrapy will now pause for {retry_after}s')
                spider.crawler.engine.pause()
                to_sleep = retry_after * 1.2
                try:
                    wait(to_sleep)
                except KeyboardInterrupt:
                    self.crawler.engine.unpause()
                    raise
                spider.crawler.engine.unpause()
                self.log.info('Resuming crawl.')
                return request.copy()
            else:
                self.log.critical('Server returned HTTP 429 Too Many Requests.')
                self.log.critical('Either your IP address or your developer account is being rate-limited.')
                self.log.critical('Crawler will now stop.')
                self.crawler.engine.close_spider(spider, 'rate_limited')
                raise IgnoreRequest()
        return response