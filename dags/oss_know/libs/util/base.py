import re
from datetime import datetime
from urllib.parse import urlparse

import geopy
import redis
import requests
import urllib3
from geopy.geocoders import GoogleV3
from multidict import CIMultiDict
from opensearchpy import OpenSearch
from tenacity import *

from oss_know.libs.base_dict.infer_file import CCTLD, COMPANY_COUNTRY
from oss_know.libs.base_dict.variable_key import LOCATIONGEO_TOKEN
from oss_know.libs.util.clickhouse_driver import CKServer
from oss_know.libs.util.proxy import GithubTokenProxyAccommodator
from ..util.log import logger


class HttpGetException(Exception):
    def __init__(self, message, status):
        super().__init__(message, status)
        self.message = message
        self.status = status


# retry 防止SSL解密错误，请正确处理是否忽略证书有效性
@retry(stop=stop_after_attempt(10),
       wait=wait_fixed(1),
       retry=(retry_if_exception_type(urllib3.exceptions.HTTPError) |
              retry_if_exception_type(urllib3.exceptions.MaxRetryError) |
              retry_if_exception_type(requests.exceptions.ProxyError) |
              retry_if_exception_type(requests.exceptions.SSLError)))
def do_get_result(req_session, url, headers, params):
    # 尝试处理网络请求错误
    # session.mount('http://', HTTPAdapter(
    #     max_retries=Retry(total=5, method_whitelist=frozenset(['GET', 'POST']))))  # 设置 post()方法进行重访问
    # session.mount('https://', HTTPAdapter(
    #     max_retries=Retry(total=5, method_whitelist=frozenset(['GET', 'POST']))))  # 设置 post()方法进行重访问
    # raise urllib3.exceptions.SSLError('获取github commits 失败！')

    res = req_session.get(url, headers=headers, params=params)
    if res.status_code >= 300:
        logger.warning(f"url:{url}")
        logger.warning(f"headers:{headers}")
        logger.warning(f"params:{params}")
        logger.warning(f"text:{res.text}")
        raise HttpGetException('http get 失败！', res.status_code)
    return res


# # retry 防止SSL解密错误，请正确处理是否忽略证书有效性
@retry(stop=stop_after_attempt(10),
       wait=wait_fixed(1),
       retry=(retry_if_exception_type(urllib3.exceptions.HTTPError) |
              retry_if_exception_type(urllib3.exceptions.MaxRetryError) |
              retry_if_exception_type(requests.exceptions.ProxyError) |
              retry_if_exception_type(requests.exceptions.ChunkedEncodingError) |
              retry_if_exception_type(urllib3.exceptions.ProtocolError) |
              retry_if_exception_type(HttpGetException) |
              retry_if_exception_type(requests.exceptions.SSLError)))
def do_get_github_result(req_session, url, headers, params, accommodator: GithubTokenProxyAccommodator):
    github_token, proxy_url = accommodator.next()
    logger.debug(f'GitHub request {url} with token {github_token}')
    req_session.headers.update({'Authorization': 'token %s' % github_token})

    url_scheme, proxy_scheme = urlparse(url), urlparse(proxy_url)
    if not proxy_scheme or not url_scheme:
        logger.error(f'At least one scheme not found in urls: {url}, {proxy_url}')
    # This elif branch is commented because http(s) proxy and request scheme don't have to be the same
    # elif url_scheme != proxy_scheme:
    #     logger.warning(f'URL scheme {url_scheme} does not match proxy scheme{proxy_scheme}, skipping')
    else:
        req_session.proxies[url_scheme] = proxy_url
        logger.debug(f'Request url {url} with proxy {proxy_url}')

    res = req_session.get(url, headers=headers, params=params, verify=False)
    if res.status_code >= 300:
        logger.warning(f"url:{url}")
        logger.warning(f"headers:{headers}")
        logger.warning(f"params:{params}")
        logger.warning(f"text:{res.text}")

        if res.status_code == 401:
            # Token no longer invalid
            logger.warning(f'Token {github_token} no longer available, remove it from token list')
            accommodator.report_invalid_token(github_token)
        elif res.status_code == 403:
            # Token runs out
            logger.warning(f'Token {github_token} has run out, cooling it down for recovery')
            accommodator.report_drain_token(github_token)
        # TODO The proxy service inside accommodator should provide a unified method to check if the proxy dies
        # elif some_proxy_condition:
        #     token_proxy_accommodator.report_invalid_proxy(proxy_url)

        raise HttpGetException('http get 失败！', res.status_code)
    return res


def get_opensearch_client(opensearch_conn_infos):
    client = OpenSearch(
        hosts=[{'host': opensearch_conn_infos["HOST"], 'port': opensearch_conn_infos["PORT"]}],
        http_compress=True,
        http_auth=(opensearch_conn_infos["USER"], opensearch_conn_infos["PASSWD"]),
        use_ssl=True,
        verify_certs=False,
        ssl_assert_hostname=False,
        ssl_show_warn=False
    )
    return client


def get_redis_client(redis_client_info):
    redis_client = redis.Redis(host=redis_client_info["HOST"], port=redis_client_info["PORT"], db=0,
                               decode_responses=True)
    return redis_client


def infer_country_from_emailcctld(email):
    """
    :param  email: the email address
    :return country_name  : the english name of a country
    """
    profile_domain = email.split(".")[-1].upper()
    if profile_domain in CCTLD:
        return CCTLD[profile_domain]
    return None


def infer_country_from_emaildomain(email):
    """
    :param  email: the email address
    :return country_name  : the english name of a country
    """
    emaildomain = str(re.findall(r"@(.+?)\.", email))
    if emaildomain in COMPANY_COUNTRY:
        return COMPANY_COUNTRY[emaildomain]
    return None


def infer_company_from_emaildomain(email):
    """
    :param  email: the email address
    :return company_name  : the english name of a company
    """
    emaildomain = str(re.findall(r"@(.+?)\.", email))
    if emaildomain in COMPANY_COUNTRY:
        return emaildomain
    return None


def infer_country_from_location(github_location):
    """
    :param  github_location: location from a GitHub profile
    :return country_name  : the english name of a country
    """
    from airflow.models import Variable
    api_token = Variable.get(LOCATIONGEO_TOKEN, deserialize_json=True)
    geolocator = GoogleV3(api_key=api_token)
    geo_res = geolocator.geocode(github_location, language='en')
    if geo_res:
        return geo_res.address.split(',')[-1].strip()
    return None


def infer_geo_info_from_location(github_location):
    """
    :param  github_location: the location given by github
    :return GoogleGeoInfo  : the information of GoogleGeo inferred by location
    """
    from airflow.models import Variable
    api_token = Variable.get(LOCATIONGEO_TOKEN, deserialize_json=True)
    geolocator = GoogleV3(api_key=api_token)
    geo_res = geolocator.geocode(github_location, language='en')
    if geo_res and geo_res.raw and ("address_components" in geo_res.raw) and geo_res.raw["address_components"]:
        address_components = geo_res.raw["address_components"]
        geo_info_from_location = {}
        for address_component in address_components:
            try:
                geo_info_from_location[address_component["types"][0]] = address_component["long_name"]
            except KeyError as e:
                logger.info(f"The key not exists in address_component :{e}")
        return geo_info_from_location
    return None


def infer_country_from_company(company):
    """
    :param  company: the company message
    :return country_name  : the english name of a country
    """
    company = company.replace("@", " ").lower().strip()
    company_country = CIMultiDict(COMPANY_COUNTRY)
    if company in company_country:
        return company_country[company][0]
    return None


def infer_final_company_from_company(company):
    """
    :param  company: the company message
    :return company_name  : the english name of a company
    """
    company = company.replace("@", " ").lower().strip()
    company_country = CIMultiDict(COMPANY_COUNTRY)
    if company in company_country:
        return company_country[company][1]
    return None


inferrers = [
    ("country_inferred_from_email_cctld", "email", infer_country_from_emailcctld),
    ("country_inferred_from_email_domain_company", "email", infer_country_from_emaildomain),
    ("country_inferred_from_location", "location", infer_country_from_location),
    ("country_inferred_from_company", "company", infer_country_from_company),
    ("company_inferred_from_email_domain_company", "email", infer_company_from_emaildomain),
    ("final_company_inferred_from_company", "company", infer_final_company_from_company),
    ("inferred_from_location", "location", infer_geo_info_from_location),
]


def infer_country_company_geo_insert_into_profile(latest_github_profile):
    try:
        for tup in inferrers:
            key, original_key, infer = tup
            original_property = latest_github_profile[original_key]
            latest_github_profile[key] = infer(original_property) if original_property else None
    except (urllib3.exceptions.MaxRetryError, requests.exceptions.ProxyError, geopy.exc.GeocoderQueryError) as e:
        logger.error(
            f"error occurs when inferring information by github profile, exception message: {e},the type of exception: {type(e)}")
        for inferrer in inferrers:
            latest_github_profile[inferrer[0]] = None


def get_clickhouse_client(clickhouse_server_info):
    ck = CKServer(host=clickhouse_server_info["HOST"],
                  port=clickhouse_server_info["PORT"],
                  user=clickhouse_server_info["USER"],
                  password=clickhouse_server_info["PASSWD"],
                  database=clickhouse_server_info["DATABASE"])

    return ck


def now_timestamp():
    return int(datetime.now().timestamp() * 1000)
