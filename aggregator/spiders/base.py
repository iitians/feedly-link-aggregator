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

from __future__ import annotations

import logging
import os
import re
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from pprint import pformat
from typing import Optional, Union
from urllib.parse import unquote

from scrapy import Spider
from scrapy.http import Request, TextResponse
from scrapy.signals import spider_opened
from twisted.python.failure import Failure

from ..config import Config
from ..feedly import FeedlyEntry, build_api_url, get_feed_uri
from ..requests import (FinishedRequest, ProbeRequest, ResumeRequest,
                        reconstruct_request)
from ..urlkit import build_urls, select_templates
from ..utils import JSONDict, SpiderOutput
from ..utils import colored as _
from ..utils import guard_json


class FeedlyRSSSpider(Spider, ABC):
    custom_settings = {
        'ROBOTSTXT_OBEY': False,
        'SPIDER_MIDDLEWARES': {
            'scrapy.spidermiddlewares.depth.DepthMiddleware': None,
            'aggregator.middlewares.ConditionalDepthSpiderMiddleware': 100,
            'aggregator.spiders.base.FetchSourceSpiderMiddleware': 500,
            'aggregator.spiders.base.CrawledItemSpiderMiddleware': 700,
        },
    }

    class SpiderConfig:
        OUTPUT = f'./crawl.{datetime.now().strftime("%Y%m%d%H%M%S")}'

        FEED = 'https://xkcd.com/atom.xml'
        FEED_TEMPLATES = {}

        DOWNLOAD_ORDER = 'oldest'
        DOWNLOAD_PER_BATCH = 1000

        ENABLE_SEARCH = False
        ACCESS_TOKEN = None

        STREAM_ID_PREFIX = 'feed/'

        DATABASE_CACHE_SIZE = 100000

    SELECTION_STRATS = {
        'dead': {None: 1, True: 1, False: 0},
        'alive': {None: 1, True: 0, False: 1},
        'dead+': {None: 1, True: 1, False: -128},
        'alive+': {None: 1, True: -128, False: 1},
        'all': {None: 1, True: 1, False: 1},
    }

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider: FeedlyRSSSpider = super().from_crawler(crawler, *args, **kwargs)
        spider.stats = crawler.stats
        crawler.signals.connect(spider.open_spider, spider_opened)
        return spider

    def __init__(self, name: Optional[str] = None, preset: Optional[str] = None, **kwargs):
        super().__init__(name=name, **kwargs)

        kwargs = {k.upper(): v for k, v in kwargs.items()}
        config = Config()
        config.from_object(self.SpiderConfig)
        if preset:
            config.from_pyfile(preset)
        config.merge(kwargs)

        output_dir = Path(config['OUTPUT'])
        config['OUTPUT'] = output_dir
        os.makedirs(output_dir, exist_ok=True)

        config.set('FEED', unquote(config.get('FEED')))

        templates = {re.compile(k): v for k, v in config['FEED_TEMPLATES'].items()}
        config['FEED_TEMPLATES'] = templates

        self.api_base_params = {
            'count': int(config['DOWNLOAD_PER_BATCH']),
            'ranked': config['DOWNLOAD_ORDER'],
            'similar': 'true',
            'unreadOnly': 'false',
        }
        self.config = config

        self.logstats_items = ['rss/page_count']

    def open_spider(self, spider):
        self.logger.info(f'Spider parameters:\n{pformat(self.config.copy_to_dict())}')

    @abstractmethod
    def start_requests(self):
        yield ResumeRequest(callback=self.resume_crawl)

    def resume_crawl(self, response):
        requests = response.meta.get('_requests', {}).values()
        if not requests:
            yield self.probe_feed(self.config['FEED'], meta={'reason': 'user_specified', 'depth': 1})
        else:
            self.logger.info(_(f'Resuming crawl with {len(requests)} request(s)', color='cyan'))
            for cls, kwargs in requests:
                yield reconstruct_request(cls, self, **kwargs)

    def get_streams_url(self, feed_id: str, **params) -> str:
        params = {**self.api_base_params, **params}
        return build_api_url('streams', streamId=feed_id, **params)

    def probe_feed(self, query: str, derive: bool = True, source: Optional[Request] = None, **kwargs):
        templates = self.config['FEED_TEMPLATES']
        if derive and templates:
            try:
                urls = build_urls(query, *select_templates(query, templates))
            except ValueError:
                self.logger.debug(f'No template for {query}')
                urls = [query]
        else:
            urls = [query]

        prefix = self.config['STREAM_ID_PREFIX']
        meta = kwargs.pop('meta', {})
        meta['try_feeds'] = {f'{prefix}{u}': None for u in urls}
        meta['search_query'] = query
        return ProbeRequest(url=query, callback=self.start_feeds, meta=meta, source=source, **kwargs)

    def start_feeds(self, response: TextResponse):
        meta = response.meta
        yield FinishedRequest(meta={**meta})

        feeds = meta.get('valid_feeds')
        if feeds is None:
            feeds = meta.get('try_feeds', {})
        if not len(feeds) and meta['reason'] == 'user_specified':
            self.logger.info(f'No valid RSS feed can be found using `{meta["search_query"]}` and available feed templates.')
            self.logger.critical('No feed to crawl!')

        yield from self.filter_feeds(feeds, meta)
        yield from self.get_feed_info(feeds, meta)

    def filter_feeds(self, feeds, meta):
        if meta['reason'] == 'user_specified':
            for feed in feeds:
                yield self.next_page({'id': feed}, meta=meta, initial=True)
            return

        select = self.config.get('FEED_STATE_SELECT', 'all')
        for feed, dead in feeds.items():
            prio = self.SELECTION_STRATS[select][dead]
            if not prio:
                self.logger.info(_(f'Dropped {"dead" if dead else "living"} feed {feed[5:]}', color='grey'))
            else:
                yield self.next_page({'id': feed}, meta=meta, initial=True, priority=prio)

    def get_feed_info(self, feeds, meta):
        feed_info = meta.get('feed_info', {})
        for feed, info in feed_info.items():
            yield {'source': info, 'dead': feeds.get(feed)}

    def next_page(self, data: JSONDict, response: Optional[TextResponse] = None, initial: bool = False, **kwargs) -> Union[JSONDict, Request]:
        feed = data['id']

        if response:
            meta = {**response.meta}
        else:
            meta = {}
        meta.update(kwargs.pop('meta', {}))
        if not initial:
            meta['no_filter'] = True
            meta.pop('inc_depth', None)

        feed_url = get_feed_uri(feed)
        meta['feed_url'] = feed_url

        meta['pkey'] = (feed_url, 'main')
        meta['_persist'] = 'add'

        params = {}
        cont = data.get('continuation')
        if cont:
            params['continuation'] = cont
            meta['reason'] = 'continuation'
        elif not initial:
            meta['_persist'] = 'remove'
            return FinishedRequest(meta=meta)

        depth = meta.get('depth')
        reason = meta.get('reason')
        self.logger.debug(f'initial={initial} depth={depth} reason={reason} {feed}')

        url = self.get_streams_url(feed, **params)
        if response:
            return response.request.replace(url=url, meta=meta, **kwargs)
        else:
            return Request(url, callback=self.parse_feed, meta=meta, **kwargs)

    def parse_feed(self, response: TextResponse):
        if not response:
            return

        data = guard_json(response.text)
        items = data.get('items')
        source = response.meta['feed_url']
        if items:
            response.meta['valid_feed'] = True
            if response.meta.get('reason') != 'continuation':
                self.logger.info(_(f'Got new feed: {source}', color='green'))

        for item in items:
            entry = FeedlyEntry.from_upstream(item)
            if not entry.source:
                entry.source = {'feed': source}
            if not entry:
                continue
            self.stats.inc_value('rss/page_count')

            depth = response.meta.get('depth', 0)

            yield {
                'item': entry,
                'depth': depth,
                'time_crawled': time.time(),
            }

        yield self.next_page(data, response=response)


class FetchSourceSpiderMiddleware:
    def __init__(self):
        self.logger = logging.getLogger('worker.fetchsource')
        self.initialized = False

    def init(self, spider: FeedlyRSSSpider):
        self.scrape_source = spider.config.getbool('SCRAPE_SOURCE_PAGE', False)
        self.initialized = True

    def process_spider_output(self, response: TextResponse, result: SpiderOutput, spider: FeedlyRSSSpider):
        if not self.initialized:
            self.init(spider)
        if not self.scrape_source:
            yield from result

        for data in result:
            if isinstance(data, Request) or 'item' not in data or 'source_fetched' in data:
                yield data
                continue
            item: FeedlyEntry = data['item']
            yield Request(
                item.url, callback=self.parse_source,
                errback=self.handle_source_failure,
                meta={'data': data},
            )

    def parse_source(self, response: TextResponse):
        meta = response.meta
        data = meta['data']
        data['source_fetched'] = True
        item: FeedlyEntry = data['item']
        with suppress(AttributeError):
            if response.status >= 400:
                self.logger.debug(f'Dropping {response}')
                raise AttributeError
            body = response.text
            item.add_markup('webpage', body)
        yield data

    def handle_source_failure(self, failure: Failure):
        self.logger.debug(failure)
        request = failure.request
        data = request.meta['data']
        data['source_fetched'] = True
        yield data


class CrawledItemSpiderMiddleware:
    def __init__(self):
        self.crawled_items = set()
        self.initialized = False

    def init(self, spider: FeedlyRSSSpider):
        path = spider.config['OUTPUT'] / 'crawled_items.txt'
        if path.exists():
            with open(path, 'r') as f:
                self.crawled_items |= set(f.read().split('\n'))
        self.initialized = True

    def process_spider_output(self, response: TextResponse, result: SpiderOutput, spider: FeedlyRSSSpider):
        for data in result:
            if isinstance(data, Request) or 'item' not in data:
                yield data
                continue
            item: FeedlyEntry = data['item']
            if item.url not in self.crawled_items:
                yield data