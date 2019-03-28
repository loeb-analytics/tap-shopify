import math
import functools
import datetime
import sys
import backoff
import pyactiveresource
import pyactiveresource.formats
import simplejson
import singer
from singer import utils
from tap_shopify.context import Context
import asyncio
import aiohttp
import shopify
from urllib.parse import urlencode

LOGGER = singer.get_logger()

RESULTS_PER_PAGE = 250

# We've observed 500 errors returned if this is too large (30 days was too
# large for a customer)
DATE_WINDOW_SIZE = 1

# We will retry a 500 error a maximum of 5 times before giving up
MAX_RETRIES = 5

# Streams that can run Async
ASYNC_AVAILABLE_STREAMS = ['orders', 'products', 'customers', 'abandoned_checkouts']

def is_not_status_code_fn(status_code):
    def gen_fn(exc):
        if getattr(exc, 'code', None) and exc.code not in status_code:
            return True
        # Retry other errors up to the max
        return False
    return gen_fn

def leaky_bucket_handler(details):
    LOGGER.info("Received 429 -- sleeping for %s seconds",
                details['wait'])

def retry_handler(details):
    LOGGER.info("Received 500 or retryable error -- Retry %s/%s",
                details['tries'], MAX_RETRIES)

#pylint: disable=unused-argument
def retry_after_wait_gen(**kwargs):
    # This is called in an except block so we can retrieve the exception
    # and check it.
    exc_info = sys.exc_info()
    resp = exc_info[1].response
    # Retry-After is an undocumented header. But honoring
    # it was proven to work in our spikes.
    sleep_time_str = resp.headers.get('Retry-After')
    yield math.floor(float(sleep_time_str))

def shopify_error_handling(fnc):
    @backoff.on_exception(backoff.expo,
                          (pyactiveresource.connection.ServerError,
                           pyactiveresource.formats.Error,
                           simplejson.scanner.JSONDecodeError),
                          giveup=is_not_status_code_fn(range(500, 599)),
                          on_backoff=retry_handler,
                          max_tries=MAX_RETRIES)
    @backoff.on_exception(retry_after_wait_gen,
                          pyactiveresource.connection.ClientError,
                          giveup=is_not_status_code_fn([429]),
                          on_backoff=leaky_bucket_handler,
                          # No jitter as we want a constant value
                          jitter=None)
    @functools.wraps(fnc)
    def wrapper(*args, **kwargs):
        return fnc(*args, **kwargs)
    return wrapper

class Error(Exception):
    """Base exception for the API interaction module"""

class OutOfOrderIdsError(Error):
    """Raised if our expectation of ordering by ID is violated"""

class Stream():
    # Used for bookmarking and stream identification. Is overridden by
    # subclasses to change the bookmark key.
    name = None
    replication_method = 'INCREMENTAL'
    replication_key = 'updated_at'
    key_properties = ['id']
    # Controls which SDK object we use to call the API by default.
    replication_object = None
    replication_object_async = None
    endpoint = None
    result_key = None

    def get_bookmark(self):
        bookmark = (singer.get_bookmark(Context.state,
                                        # name is overridden by some substreams
                                        self.name,
                                        self.replication_key)
                    or Context.config["start_date"])
        return utils.strptime_with_tz(bookmark)

    def get_since_id(self):
        return singer.get_bookmark(Context.state,
                                   # name is overridden by some substreams
                                   self.name,
                                   'since_id')

    def update_bookmark(self, bookmark_value, bookmark_key=None):
        # NOTE: Bookmarking can never be updated to not get the most
        # recent thing it saw the next time you run, because the querying
        # only allows greater than or equal semantics.
        singer.write_bookmark(
            Context.state,
            # name is overridden by some substreams
            self.name,
            bookmark_key or self.replication_key,
            bookmark_value
        )
        singer.write_state(Context.state)


    # This function can be overridden by subclasses for specialized API
    # interactions. If you override it you need to remember to decorate it
    # with shopify_error_handling to get 429 and 500 handling.
    @shopify_error_handling
    def call_api(self, query_params):
        return self.replication_object.find(**query_params)

    def get_objects(self):
        updated_at_min = self.get_bookmark()
        end_date = Context.config.get("end_date", None)
        stop_time = utils.strptime_with_tz(end_date) if end_date is not None else singer.utils.now().replace(microsecond=0)
        date_window_size = float(Context.config.get("date_window_size", DATE_WINDOW_SIZE))
        results_per_page = int(Context.config.get("results_per_page", RESULTS_PER_PAGE))

        # Page through till the end of the resultset
        while updated_at_min < stop_time:
            # Bookmarking can also occur on the since_id
            since_id = self.get_since_id() or 1

            if since_id != 1:
                LOGGER.info("Resuming sync from since_id %d", since_id)

            # It's important that `updated_at_min` has microseconds
            # truncated. Why has been lost to the mists of time but we
            # think it has something to do with how the API treats
            # microseconds on its date windows. Maybe it's possible to
            # drop data due to rounding errors or something like that?
            updated_at_max = updated_at_min + datetime.timedelta(days=date_window_size)
            if updated_at_max > stop_time:
                updated_at_max = stop_time
            while True:
                query_params = {
                    "since_id": since_id,
                    "updated_at_min": updated_at_min,
                    "updated_at_max": updated_at_max,
                    "limit": results_per_page,
                    "status": "any"
                }
                objects = self.call_api(query_params)
                for obj in objects:
                    if obj.id < since_id:
                        # This verifies the api behavior expectation we
                        # have that all results actually honor the
                        # since_id parameter.
                        raise OutOfOrderIdsError("obj.id < since_id: {} < {}".format(
                            obj.id, since_id))
                    yield obj

                # You know you're at the end when the current page has
                # less than the request size limits you set.
                if len(objects) < results_per_page:
                    # Save the updated_at_max as our bookmark as we've synced all rows up in our
                    # window and can move forward. Also remove the since_id because we want to
                    # restart at 1.
                    Context.state.get('bookmarks', {}).get(self.name, {}).pop('since_id', None)
                    self.update_bookmark(utils.strftime(updated_at_max))
                    break

                if objects[-1].id != max([o.id for o in objects]):
                    # This verifies the api behavior expectation we have
                    # that all pages are internally ordered by the
                    # `since_id`.
                    raise OutOfOrderIdsError("{} is not the max id in objects ({})".format(
                        objects[-1].id, max([o.id for o in objects])))
                since_id = objects[-1].id

                # Put since_id into the state.
                self.update_bookmark(since_id, bookmark_key='since_id')

            updated_at_min = updated_at_max

    def get_objects_async(self):
        updated_at_min = self.get_bookmark()
        end_date = Context.config.get("end_date", None)
        updated_at_max = utils.strptime_with_tz(end_date) if end_date is not None else singer.utils.now().replace(microsecond=0)
        results_per_page = int(Context.config.get("results_per_page", RESULTS_PER_PAGE))
        
        DT_FMT = '%Y-%m-%dT%H:%M:%S'
        query_params = {
            "updated_at_min": utils.strftime(updated_at_min, format_str=DT_FMT),
            "updated_at_max": utils.strftime(updated_at_max, format_str=DT_FMT),
            "status": "any"
        }

        objects = self.replication_object_async.find(
            endpoint = self.endpoint,
            result_key = self.result_key,
            retry_limit = MAX_RETRIES, 
            results_per_page = results_per_page,
            **query_params
        )

        for obj in objects:
            yield obj
        
        self.update_bookmark(utils.strftime(updated_at_max))

    def sync(self):
        """Yield's processed SDK object dicts to the caller.

        This is the default implementation. Get's all of self's objects
        and calls to_dict on them with no further processing.
        """
        if Context.config.get("use_async", False) and self.name in ASYNC_AVAILABLE_STREAMS:
            for obj in self.get_objects_async():
                yield obj
        else:
            for obj in self.get_objects():
                yield obj.to_dict()


class RunAsync():
    def __init__(self, endpoint, result_key, retry_limit, results_per_page, params):
        super()
        self.retry_limit = retry_limit
        self.results_per_page = results_per_page
        self.params = params
        self.params['limit'] = str(self.results_per_page)
        self.base_url = shopify.ShopifyResource.get_site()
        self.current_shop = shopify.Shop.current()
        self.bucket_size = 80 if self.current_shop.plan_name == "shopify_plus" else 40
        self.shop_display_url = "https://{}".format(self.current_shop.myshopify_domain)
        self.endpoint = endpoint
        self.result_key = result_key

    async def _get_async(self, url, headers=None, params=None, retry_attempt=0):
        async with aiohttp.ClientSession() as session:
            async with session.get(url=url, headers=headers, params=params) as response:
                if response.status == 429:
                    sleep_time_str = math.floor(float(response.headers.get('Retry-After')))
                    LOGGER.info("Received 429 -- sleeping for {} seconds".format(sleep_time_str))
                    await asyncio.sleep(sleep_time_str)
                    return await self._get_async(url, headers, params, retry_attempt=retry_attempt+1)
                if response.status in range(500, 599):
                    return await self._get_async(url, headers, params, retry_attempt=retry_attempt+1)
                else:
                    resp = await response.json()
                    if 'page' in params:
                        resp['page'] = params['page']
                    return resp

    async def _request_count(self):
        endpoint = '{}/count.json'.format(self.endpoint)
        url = self.base_url + endpoint
        resp = await self._get_async(url, params=self.params)
        LOGGER.info("GET {}{}?{}".format(self.shop_display_url, endpoint, urlencode(self.params)))
        return resp['count']

    async def _request(self, job):
        endpoint = '{}.json'.format(self.endpoint)
        url = self.base_url + endpoint
        resp = await self._get_async(url, params=job['params'])
        LOGGER.info("GET {}{}?{}".format(self.shop_display_url, endpoint, urlencode(job['params'])))
        return resp

    async def _runner(self):
        result_set_size = await self._request_count()
        num_pages = math.ceil(result_set_size/self.results_per_page)
        jobs = [{'params': { **self.params, 'page': p+1 }} for p in range(num_pages)]
        chunked_jobs = utils.chunk(jobs, self.bucket_size)

        all_jobs_results_dict = {}
        for chunk_of_jobs in chunked_jobs:
            futures = [self._request(i) for i in chunk_of_jobs]
            resp_dict = {}
            for i, future in enumerate(asyncio.as_completed(futures)):
                result = await future
                resp_dict[result['page']] = result[self.result_key] if self.result_key in result else []
            all_jobs_results_dict = {**all_jobs_results_dict, **resp_dict}
        ordered_results = []
        for k,v in sorted(all_jobs_results_dict.items()):
            if len(v) > 0:
                ordered_results += v
        return ordered_results

    def Run(self):
        result = asyncio.run(self._runner())
        return result

    @classmethod
    def find(cls, endpoint, result_key, retry_limit=5, results_per_page=250, **kwargs):
        return RunAsync(endpoint, result_key, retry_limit, results_per_page, params=kwargs).Run()