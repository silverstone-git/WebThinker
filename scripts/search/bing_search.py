import os
import json
import requests
from requests.exceptions import Timeout
from bs4 import BeautifulSoup
from tqdm import tqdm
import concurrent
from concurrent.futures import ThreadPoolExecutor
import pdfplumber
from io import BytesIO
import re
import string
from typing import Optional, Tuple
from nltk.tokenize import sent_tokenize
from typing import List, Dict, Union
import time
from urllib.parse import quote_plus, urlparse, parse_qs, unquote, urljoin
import aiohttp
import asyncio
import chardet
import random


# ----------------------- Set your WebParserClient URL -----------------------
WebParserClient_url = None


DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"


# ----------------------- Custom Headers -----------------------
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/58.0.3029.110 Safari/537.36',
    'Referer': 'https://www.google.com/',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1'
}

# Initialize session
session = requests.Session()
session.headers.update(headers)

error_indicators = [
    'limit exceeded',
    'Error fetching',
    'Account balance not enough',
    'Invalid bearer token',
    'HTTP error occurred',
    'Error: Connection error occurred',
    'Error: Request timed out',
    'Unexpected error',
    'Please turn on Javascript',
    'Enable JavaScript',
    'port=443',
    'Please enable cookies',
]

class WebParserClient:
    def __init__(self, base_url: str = "http://localhost:8000"):
        """
        初始化Web解析器客户端
        
        Args:
            base_url: API服务器的基础URL，默认为本地测试服务器
        """
        self.base_url = base_url.rstrip('/')
        
    def parse_urls(self, urls: List[str], timeout: int = 120) -> List[Dict[str, Union[str, bool]]]:
        """
        发送URL列表到解析服务器并获取解析结果
        
        Args:
            urls: 需要解析的URL列表
            timeout: 请求超时时间，默认20秒
            
        Returns:
            解析结果列表
            
        Raises:
            requests.exceptions.RequestException: 当API请求失败时
            requests.exceptions.Timeout: 当请求超时时
        """
        endpoint = urljoin(self.base_url, "/parse_urls")
        response = requests.post(endpoint, json={"urls": urls}, timeout=timeout)
        response.raise_for_status()  # 如果响应状态码不是200，抛出异常
        
        return response.json()["results"]


def remove_punctuation(text: str) -> str:
    """Remove punctuation from the text."""
    return text.translate(str.maketrans("", "", string.punctuation))

def f1_score(true_set: set, pred_set: set) -> float:
    """Calculate the F1 score between two sets of words."""
    intersection = len(true_set.intersection(pred_set))
    if not intersection:
        return 0.0
    precision = intersection / float(len(pred_set))
    recall = intersection / float(len(true_set))
    return 2 * (precision * recall) / (precision + recall)

def extract_snippet_with_context(full_text: str, snippet: str, context_chars: int = 3000) -> Tuple[bool, str]:
    """
    Extract the sentence that best matches the snippet and its context from the full text.

    Args:
        full_text (str): The full text extracted from the webpage.
        snippet (str): The snippet to match.
        context_chars (int): Number of characters to include before and after the snippet.

    Returns:
        Tuple[bool, str]: The first element indicates whether extraction was successful, the second element is the extracted context.
    """
    try:
        full_text = full_text[:100000]

        snippet = snippet.lower()
        snippet = remove_punctuation(snippet)
        snippet_words = set(snippet.split())

        best_sentence = None
        best_f1 = 0.2

        # sentences = re.split(r'(?<=[.!?]) +', full_text)  # Split sentences using regex, supporting ., !, ? endings
        sentences = sent_tokenize(full_text)  # Split sentences using nltk's sent_tokenize

        for sentence in sentences:
            key_sentence = sentence.lower()
            key_sentence = remove_punctuation(key_sentence)
            sentence_words = set(key_sentence.split())
            f1 = f1_score(snippet_words, sentence_words)
            if f1 > best_f1:
                best_f1 = f1
                best_sentence = sentence

        if best_sentence:
            para_start = full_text.find(best_sentence)
            para_end = para_start + len(best_sentence)
            start_index = max(0, para_start - context_chars)
            end_index = min(len(full_text), para_end + context_chars)
            # if end_index - start_index < 2 * context_chars:
            #     end_index = min(len(full_text), start_index + 2 * context_chars)
            context = full_text[start_index:end_index]
            return True, context
        else:
            # If no matching sentence is found, return the first context_chars*2 characters of the full text
            return False, full_text[:context_chars * 2]
    except Exception as e:
        return False, f"Failed to extract snippet context due to {str(e)}"

def extract_text_from_url(url, use_jina=False, jina_api_key=None, snippet: Optional[str] = None, keep_links=False):
    """
    Extract text from a URL. If a snippet is provided, extract the context related to it.

    Args:
        url (str): URL of a webpage or PDF.
        use_jina (bool): Whether to use Jina for extraction.
        jina_api_key (str): API key for Jina.
        snippet (Optional[str]): The snippet to search for.
        keep_links (bool): Whether to keep links in the extracted text.

    Returns:
        str: Extracted text or context.
    """
    try:
        if use_jina:
            jina_headers = {
                'Authorization': f'Bearer {jina_api_key}',
                'X-Return-Format': 'markdown',
            }
            response = requests.get(f'https://r.jina.ai/{url}', headers=jina_headers).text
            # Remove URLs
            pattern = r"\(https?:.*?\)|\[https?:.*?\]"
            text = re.sub(pattern, "", response).replace('---','-').replace('===','=').replace('   ',' ').replace('   ',' ')
        else:
            if 'pdf' in url:
                return extract_pdf_text(url)

            try:
                response = session.get(url, timeout=30)
                response.raise_for_status()
                
                # 添加编码检测和处理
                if response.encoding.lower() == 'iso-8859-1':
                    # 尝试从内容检测正确的编码
                    response.encoding = response.apparent_encoding
                
                try:
                    soup = BeautifulSoup(response.text, 'lxml')
                except Exception:
                    soup = BeautifulSoup(response.text, 'html.parser')

                # Check if content has error indicators
                has_error = (any(indicator.lower() in response.text.lower() for indicator in error_indicators) and len(response.text.split()) < 64) or response.text == ''
                if has_error:
                    if WebParserClient_url is None:
                        # If WebParserClient is not available, return error message
                        return f"Error extracting content: {str(e)}"
                    # If content has error, use WebParserClient as fallback
                    client = WebParserClient(WebParserClient_url)
                    results = client.parse_urls([url])
                    if results and results[0]["success"]:
                        text = results[0]["content"]
                    else:
                        error_msg = results[0].get("error", "Unknown error") if results else "No results returned"
                        return f"WebParserClient error: {error_msg}"
                else:
                    if keep_links:
                        # Clean and extract main content
                        # Remove script, style tags etc
                        for element in soup.find_all(['script', 'style', 'meta', 'link']):
                            element.decompose()

                        # Extract text and links
                        text_parts = []
                        for element in soup.body.descendants if soup.body else soup.descendants:
                            if isinstance(element, str) and element.strip():
                                # Clean extra whitespace
                                cleaned_text = ' '.join(element.strip().split())
                                if cleaned_text:
                                    text_parts.append(cleaned_text)
                            elif element.name == 'a' and element.get('href'):
                                href = element.get('href')
                                link_text = element.get_text(strip=True)
                                if href and link_text:  # Only process a tags with both text and href
                                    # Handle relative URLs
                                    if href.startswith('/'):
                                        base_url = '/'.join(url.split('/')[:3])
                                        href = base_url + href
                                    elif not href.startswith(('http://', 'https://')):
                                        href = url.rstrip('/') + '/' + href
                                    text_parts.append(f"[{link_text}]({href})")

                        # Merge text with reasonable spacing
                        text = ' '.join(text_parts)
                        # Clean extra spaces
                        text = ' '.join(text.split())
                    else:
                        text = soup.get_text(separator=' ', strip=True)
            except Exception as e:
                if WebParserClient_url is None:
                    # If WebParserClient is not available, return error message
                    return f"Error extracting content: {str(e)}"
                # If normal extraction fails, try using WebParserClient
                client = WebParserClient(WebParserClient_url)
                results = client.parse_urls([url])
                if results and results[0]["success"]:
                    text = results[0]["content"]
                else:
                    error_msg = results[0].get("error", "Unknown error") if results else "No results returned"
                    return f"WebParserClient error: {error_msg}"

        if snippet:
            success, context = extract_snippet_with_context(text, snippet)
            if success:
                return context
            else:
                return text
        else:
            # If no snippet is provided, return directly
            return text[:20000]
    except requests.exceptions.HTTPError as http_err:
        return f"HTTP error occurred: {http_err}"
    except requests.exceptions.ConnectionError:
        return "Error: Connection error occurred"
    except requests.exceptions.Timeout:
        return "Error: Request timed out after 20 seconds"
    except Exception as e:
        return f"Unexpected error: {str(e)}"

def fetch_page_content(urls, max_workers=32, use_jina=False, jina_api_key=None, snippets: Optional[dict] = None, show_progress=False, keep_links=False):
    """
    Concurrently fetch content from multiple URLs.

    Args:
        urls (list): List of URLs to scrape.
        max_workers (int): Maximum number of concurrent threads.
        use_jina (bool): Whether to use Jina for extraction.
        jina_api_key (str): API key for Jina.
        snippets (Optional[dict]): A dictionary mapping URLs to their respective snippets.
        show_progress (bool): Whether to show progress bar with tqdm.
        keep_links (bool): Whether to keep links in the extracted text.

    Returns:
        dict: A dictionary mapping URLs to the extracted content or context.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(extract_text_from_url, url, use_jina, jina_api_key, snippets.get(url) if snippets else None, keep_links): url
            for url in urls
        }
        completed_futures = concurrent.futures.as_completed(futures)
        if show_progress:
            completed_futures = tqdm(completed_futures, desc="Fetching URLs", total=len(urls))
            
        for future in completed_futures:
            url = futures[future]
            try:
                data = future.result()
                results[url] = data
            except Exception as exc:
                results[url] = f"Error fetching {url}: {exc}"
            # time.sleep(0.1)  # Simple rate limiting
    return results

def bing_web_search(query, subscription_key, endpoint, market='en-US', language='en', timeout=20):
    """
    Perform a search using the Bing Web Search API with a set timeout.

    Args:
        query (str): Search query.
        subscription_key (str): Subscription key for the Bing Search API.
        endpoint (str): Endpoint for the Bing Search API.
        market (str): Market, e.g., "en-US" or "zh-CN".
        language (str): Language of the results, e.g., "en".
        timeout (int or float or tuple): Request timeout in seconds.
                                         Can be a float representing the total timeout,
                                         or a tuple (connect timeout, read timeout).

    Returns:
        dict: JSON response of the search results. Returns empty dict if all retries fail.
    """
    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key
    }
    params = {
        "q": query,
        "mkt": market,
        "setLang": language,
        "textDecorations": True,
        "textFormat": "HTML"
    }

    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            response = requests.get(endpoint, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()  # Raise exception if the request failed
            search_results = response.json()
            return search_results
        except Timeout:
            retry_count += 1
            if retry_count == max_retries:
                print(f"Bing Web Search request timed out ({timeout} seconds) for query: {query} after {max_retries} retries")
                return {}
            print(f"Bing Web Search Timeout occurred, retrying ({retry_count}/{max_retries})...")
        except requests.exceptions.RequestException as e:
            retry_count += 1
            if retry_count == max_retries:
                print(f"Bing Web Search Request Error occurred: {e} after {max_retries} retries")
                return {}
            print(f"Bing Web Search Request Error occurred, retrying ({retry_count}/{max_retries})...")
        time.sleep(1)  # Wait 1 second between retries
    
    return {}  # Should never reach here but added for completeness


def extract_pdf_text(url):
    """
    Extract text from a PDF.

    Args:
        url (str): URL of the PDF file.

    Returns:
        str: Extracted text content or error message.
    """
    try:
        response = session.get(url, timeout=20)  # Set timeout to 20 seconds
        if response.status_code != 200:
            return f"Error: Unable to retrieve the PDF (status code {response.status_code})"
        
        # Open the PDF file using pdfplumber
        with pdfplumber.open(BytesIO(response.content)) as pdf:
            full_text = ""
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    full_text += text
        
        # Limit the text length
        cleaned_text = full_text
        return cleaned_text
    except requests.exceptions.Timeout:
        return "Error: Request timed out after 20 seconds"
    except Exception as e:
        return f"Error: {str(e)}"

def extract_relevant_info(search_results):
    """
    Extract relevant information from Bing search results.

    Args:
        search_results (dict): JSON response from the Bing Web Search API.

    Returns:
        list: A list of dictionaries containing the extracted information.
    """
    useful_info = []

    print("search results are: ")
    print(search_results)
    
    if 'webPages' in search_results and 'value' in search_results['webPages']:
        for id, result in enumerate(search_results['webPages']['value']):
            info = {
                'id': id + 1,  # Increment id for easier subsequent operations
                'title': result.get('name', ''),
                'url': result.get('url', ''),
                'site_name': result.get('siteName', ''),
                'date': result.get('datePublished', '').split('T')[0],
                'snippet': result.get('snippet', ''),  # Remove HTML tags
                # Add context content to the information
                'context': ''  # Reserved field to be filled later
            }
            useful_info.append(info)
    
    return useful_info




def extract_relevant_info_ddg(search_results):
    """
    Extract relevant information from a list of search result dictionaries.

    Args:
        search_results (list): A list of dictionaries, where each dictionary
                               represents a single search result.

    Returns:
        list: A list of dictionaries containing the extracted information.
              Each dictionary will have the following keys:
                'id': An integer ID for the search result.
                'title': The title of the search result.
                'url': The URL of the search result.
                'site_name': The display URL of the website.
                'date': The date of the search result (if available, otherwise '').
                'snippet': A short summary or snippet of the search result.
                'context': An empty string, reserved for potential future use.
    """
    useful_info = []

    print("search results are: ")
    print(search_results)

    for id, result in enumerate(search_results):
        info = {
            'id': id + 1,  # Increment id for easier subsequent operations
            'title': result.get('title', ''),
            'url': result.get('url', ''),
            'site_name': result.get('displayUrl', ''),
            'date': '',  # Date is not directly available in this input format
            'snippet': result.get('snippet', ''),  # Remove HTML tags
            # Add context content to the information
            'context': ''  # Reserved field to be filled later
        }
        useful_info.append(info)

    return useful_info



def duckduckgo_lite_search(query, timeout=10):
    """
    Perform a synchronous search using DuckDuckGo Lite and parse results.

    Args:
        query (str): Search query.
        timeout (int): Request timeout in seconds.

    Returns:
        list: A list of dictionaries, where each dictionary represents
              a search result with keys 'title', 'url', 'snippet', 'displayUrl'.
              Returns an empty list if no results are found or if the
              request fails after retries.
    """

    # Encode the query for use in the URL
    encoded_query = quote_plus(query)
    url = f"{DDG_LITE_URL}?q={encoded_query}"

    # Basic headers to mimic a browser request (optional but can sometimes help)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36'
    }

    max_retries = 5
    retry_count = 0

    while retry_count < max_retries:
        try:
            # Make the HTTP GET request
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

            # Parse the HTML content using BeautifulSoup
            soup = BeautifulSoup(response.content, 'html.parser')

            results = []

            # Find all the main search result links. Based on the HTML, these are <a>
            # tags with class 'result-link'.
            # Each result block seems to be structured like:
            # <tr><td>num.</td><td><a class='result-link'>...</a></td></tr>
            # <tr><td>...</td><td class='result-snippet'>...</td></td></tr>
            # <tr><td>...</td><td><span class='link-text'>...</span></td></tr>
            # followed by a spacer <tr>

            # Find all link elements that signify the start of a result block
            result_links = soup.find_all('a', class_='result-link')

            for link_tag in result_links:
                try:
                    title = link_tag.get_text(strip=True)
                    raw_url = link_tag['href']

                    # DuckDuckGo Lite uses redirect URLs like //duckduckgo.com/l/?uddg=...
                    # We need to extract the actual URL from the 'uddg' parameter.
                    actual_url = None
                    if raw_url.startswith('//duckduckgo.com/l/'):
                        # Prepend scheme if missing for urlparse
                        if raw_url.startswith('//'):
                            raw_url = 'https:' + raw_url

                        parsed_ddg_url = urlparse(raw_url)
                        query_params = parse_qs(parsed_ddg_url.query)
                        uddg_urls = query_params.get('uddg')

                        if uddg_urls and uddg_urls[0]:
                            actual_url = unquote(uddg_urls[0]) # Decode the URL
                        else:
                            # Fallback: use the DDG redirect link if uddg not found
                            actual_url = raw_url
                    else:
                         # Fallback: use the link as is if it's not the standard DDG redirect format
                         actual_url = raw_url


                    # Navigate the HTML structure relative to the link_tag
                    # Go up to the <tr> parent of the link
                    link_tr = link_tag.find_parent('tr')

                    snippet = ""
                    display_url = ""

                    if link_tr:
                        # Find the next <tr> sibling (should contain the snippet)
                        snippet_tr = link_tr.find_next_sibling('tr')
                        if snippet_tr:
                            snippet_td = snippet_tr.find('td', class_='result-snippet')
                            if snippet_td:
                                snippet = snippet_td.get_text(strip=True)

                            # Find the next <tr> sibling after the snippet_tr (should contain the display URL)
                            display_tr = snippet_tr.find_next_sibling('tr')
                            if display_tr:
                                display_span = display_tr.find('span', class_='link-text')
                                if display_span:
                                    display_url = display_span.get_text(strip=True)


                    results.append({
                        'title': title,
                        'url': actual_url,
                        'snippet': snippet,
                        'displayUrl': display_url
                    })

                except Exception as parse_error:
                    # Print error for a specific result but continue processing others
                    print(f"Error parsing individual result: {parse_error}")
                    continue # Skip this potentially malformed result

            # If we successfully parsed the page, return the results
            return results

        except requests.exceptions.RequestException as e:
            # Handle request errors (network issues, timeouts, HTTP errors)
            retry_count += 1
            if retry_count == max_retries:
                print(f"DuckDuckGo Lite Search Request Error occurred: {e} after {max_retries} retries")
                return [] # Return empty list after max retries
            print(f"DuckDuckGo Lite Search Request Error occurred, retrying ({retry_count}/{max_retries})...")
            time.sleep(1)  # Wait before retrying

        except Exception as e:
            # Catch any other unexpected errors during parsing
            print(f"An unexpected error occurred during parsing: {e}")
            return [] # Return empty list for unexpected errors

    # This part should technically not be reached if retries are handled,
    # but serves as a final fallback.
    return []


async def bing_web_search_async(query, subscription_key, endpoint, market='en-US', language='en', timeout=20):
    """
    Perform an asynchronous search using the Bing Web Search API.

    Args:
        query (str): Search query.
        subscription_key (str): Subscription key for the Bing Search API.
        endpoint (str): Endpoint for the Bing Search API.
        market (str): Market, e.g., "en-US" or "zh-CN".
        language (str): Language of the results, e.g., "en".
        timeout (int): Request timeout in seconds.

    Returns:
        dict: JSON response of the search results. Returns empty dict if all retries fail.
    """
    headers = {
        "Ocp-Apim-Subscription-Key": subscription_key
    }
    params = {
        "q": query,
        "mkt": market,
        "setLang": language,
        "textDecorations": True,
        "textFormat": "HTML"
    }

    max_retries = 5
    retry_count = 0

    while retry_count < max_retries:
        try:
            response = session.get(endpoint, headers=headers, params=params, timeout=timeout)
            response.raise_for_status()
            search_results = response.json()
            return search_results
        except Exception as e:
            retry_count += 1
            if retry_count == max_retries:
                print(f"Bing Web Search Request Error occurred: {e} after {max_retries} retries")
                return {}
            print(f"Bing Web Search Request Error occurred, retrying ({retry_count}/{max_retries})...")
            time.sleep(1)  # Wait 1 second between retries

    return {}

class RateLimiter:
    def __init__(self, rate_limit: int, time_window: int = 60):
        """
        初始化速率限制器
        
        Args:
            rate_limit: 在时间窗口内允许的最大请求数
            time_window: 时间窗口大小(秒)，默认60秒
        """
        self.rate_limit = rate_limit
        self.time_window = time_window
        self.tokens = rate_limit
        self.last_update = time.time()
        self.lock = asyncio.Lock()

    async def acquire(self):
        """获取一个令牌，如果没有可用令牌则等待"""
        async with self.lock:
            while self.tokens <= 0:
                now = time.time()
                time_passed = now - self.last_update
                self.tokens = min(
                    self.rate_limit,
                    self.tokens + (time_passed * self.rate_limit / self.time_window)
                )
                self.last_update = now
                if self.tokens <= 0:
                    await asyncio.sleep(random.randint(5, 30))  # 等待xxx秒后重试
            
            self.tokens -= 1
            return True

# 创建全局速率限制器实例
jina_rate_limiter = RateLimiter(rate_limit=130)  # 每分钟xxx次，避免报错

async def extract_text_from_url_async(url: str, session: aiohttp.ClientSession, use_jina: bool = False, 
                                    jina_api_key: Optional[str] = None, snippet: Optional[str] = None, 
                                    keep_links: bool = False) -> str:
    """Async version of extract_text_from_url"""
    try:
        if use_jina:
            # 在调用jina之前获取令牌
            await jina_rate_limiter.acquire()
            
            jina_headers = {
                'Authorization': f'Bearer {jina_api_key}',
                'X-Return-Format': 'markdown',
            }
            async with session.get(f'https://r.jina.ai/{url}', headers=jina_headers) as response:
                text = await response.text()
                if not keep_links:
                    pattern = r"\(https?:.*?\)|\[https?:.*?\]"
                    text = re.sub(pattern, "", text)
                text = text.replace('---','-').replace('===','=').replace('   ',' ').replace('   ',' ')
        else:
            if 'pdf' in url:
                # Use async PDF handling
                text = await extract_pdf_text_async(url, session)
                return text[:10000]

            async with session.get(url) as response:
                # 检测和处理编码
                content_type = response.headers.get('content-type', '').lower()
                if 'charset' in content_type:
                    charset = content_type.split('charset=')[-1]
                    html = await response.text(encoding=charset)
                else:
                    # 如果没有指定编码，先用bytes读取内容
                    content = await response.read()
                    # 使用chardet检测编码
                    detected = chardet.detect(content)
                    encoding = detected['encoding'] if detected['encoding'] else 'utf-8'
                    html = content.decode(encoding, errors='replace')
                
                # 检查是否有错误指示
                has_error = (any(indicator.lower() in html.lower() for indicator in error_indicators) and len(html.split()) < 64) or len(html) < 50 or len(html.split()) < 20
                # has_error = len(html.split()) < 64
                if has_error:
                    if WebParserClient_url is None:
                        # If WebParserClient is not available, return error message
                        return f"Error extracting content: {str(e)}"
                    # If content has error, use WebParserClient as fallback
                    client = WebParserClient(WebParserClient_url)
                    results = client.parse_urls([url])
                    if results and results[0]["success"]:
                        text = results[0]["content"]
                    else:
                        error_msg = results[0].get("error", "Unknown error") if results else "No results returned"
                        return f"WebParserClient error: {error_msg}"
                else:
                    try:
                        soup = BeautifulSoup(html, 'lxml')
                    except Exception:
                        soup = BeautifulSoup(html, 'html.parser')

                    if keep_links:
                        # Similar link handling logic as in synchronous version
                        for element in soup.find_all(['script', 'style', 'meta', 'link']):
                            element.decompose()

                        text_parts = []
                        for element in soup.body.descendants if soup.body else soup.descendants:
                            if isinstance(element, str) and element.strip():
                                cleaned_text = ' '.join(element.strip().split())
                                if cleaned_text:
                                    text_parts.append(cleaned_text)
                            elif element.name == 'a' and element.get('href'):
                                href = element.get('href')
                                link_text = element.get_text(strip=True)
                                if href and link_text:
                                    if href.startswith('/'):
                                        base_url = '/'.join(url.split('/')[:3])
                                        href = base_url + href
                                    elif not href.startswith(('http://', 'https://')):
                                        href = url.rstrip('/') + '/' + href
                                    text_parts.append(f"[{link_text}]({href})")

                        text = ' '.join(text_parts)
                        text = ' '.join(text.split())
                    else:
                        text = soup.get_text(separator=' ', strip=True)

        # print('---\n', text[:1000])
        if snippet:
            success, context = extract_snippet_with_context(text, snippet)
            return context if success else text
        else:
            return text[:50000]

    except Exception as e:
        return f"Error fetching {url}: {str(e)}"

async def fetch_page_content_async(urls: List[str], use_jina: bool = False, jina_api_key: Optional[str] = None, 
                                 snippets: Optional[Dict[str, str]] = None, show_progress: bool = False,
                                 keep_links: bool = False, max_concurrent: int = 32) -> Dict[str, str]:
    """Asynchronously fetch content from multiple URLs."""
    async def process_urls():
        connector = aiohttp.TCPConnector(limit=max_concurrent)
        timeout = aiohttp.ClientTimeout(total=240)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers) as session:
            tasks = []
            for url in urls:
                task = extract_text_from_url_async(
                    url, 
                    session, 
                    use_jina, 
                    jina_api_key,
                    snippets.get(url) if snippets else None,
                    keep_links
                )
                tasks.append(task)
            
            if show_progress:
                results = []
                for task in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Fetching URLs"):
                    result = await task
                    results.append(result)
            else:
                results = await asyncio.gather(*tasks)
            
            return {url: result for url, result in zip(urls, results)}  # 返回字典而不是协程对象

    return await process_urls()  # 确保等待异步操作完成

async def extract_pdf_text_async(url: str, session: aiohttp.ClientSession) -> str:
    """
    Asynchronously extract text from a PDF.

    Args:
        url (str): URL of the PDF file.
        session (aiohttp.ClientSession): Aiohttp client session.

    Returns:
        str: Extracted text content or error message.
    """
    try:
        async with session.get(url, timeout=30) as response:  # Set timeout to 20 seconds
            if response.status != 200:
                return f"Error: Unable to retrieve the PDF (status code {response.status})"
            
            content = await response.read()
            
            # Open the PDF file using pdfplumber
            with pdfplumber.open(BytesIO(content)) as pdf:
                full_text = ""
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        full_text += text
            
            # Limit the text length
            cleaned_text = full_text
            return cleaned_text
    except asyncio.TimeoutError:
        return "Error: Request timed out after 20 seconds"
    except Exception as e:
        return f"Error: {str(e)}"




# ------------------------------------------------------------

if __name__ == "__main__":
    # Example usage
    # Define the query to search
    query = "Structure of dimethyl fumarate"
    
    # Subscription key and endpoint for Bing Search API
    BING_SUBSCRIPTION_KEY = "YOUR_BING_SUBSCRIPTION_KEY"
    if not BING_SUBSCRIPTION_KEY:
        raise ValueError("Please set the BING_SEARCH_V7_SUBSCRIPTION_KEY environment variable.")
    
    bing_endpoint = "https://api.bing.microsoft.com/v7.0/search"
    
    # Perform the search
    print("Performing Bing Web Search...")
    search_results = bing_web_search(query, BING_SUBSCRIPTION_KEY, bing_endpoint)
    
    print("Extracting relevant information from search results...")
    extracted_info = extract_relevant_info(search_results)

    print("Fetching and extracting context for each snippet...")
    for info in tqdm(extracted_info, desc="Processing Snippets"):
        full_text = extract_text_from_url(info['url'], use_jina=True)  # Get full webpage text
        if full_text and not full_text.startswith("Error"):
            success, context = extract_snippet_with_context(full_text, info['snippet'])
            if success:
                info['context'] = context
            else:
                info['context'] = f"Could not extract context. Returning first 8000 chars: {full_text[:8000]}"
        else:
            info['context'] = f"Failed to fetch full text: {full_text}"

    # print("Your Search Query:", query)
    # print("Final extracted information with context:")
    # print(json.dumps(extracted_info, indent=2, ensure_ascii=False))
