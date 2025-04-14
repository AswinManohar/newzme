#!/usr/bin/env python3
"""
Article Content Extractor

Article extraction with paywall bypassing and anti-detection measures.
SHhould to work with the RSS scraper to fetch full article content.
"""

import os
import random
import logging
import requests
import time
import json
from urllib.parse import urlparse
from typing import Dict, List, Optional, Union, Any, Tuple
from readability import Document
from bs4 import BeautifulSoup
from dataclasses import dataclass
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import re
from .config import USER_AGENTS, REFERRERS, paywall_selectors, selectors, content_selectors, paywall_phrases, cookies



logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('article_extractor')

#path to database
DB_FILE = os.path.join(os.path.dirname(__file__), 'articles.db')

@dataclass
class Article:
    id: Optional[int] = None
    source_id: Optional[int] = None
    title: Optional[str] = None
    url: Optional[str] = None
    published_date: Optional[str] = None
    content: Optional[str] = None
    html_content: Optional[str] = None
    extraction_date: Optional[str] = None
    status: str = "PENDING"  # PENDING, COMPLETE, FAILED, PAYWALL

class RateLimiter:
    """
    Domain-based rate limiter to prevent overloading sites.
    """
    def __init__(self, global_cooldown_ms: int = 500, domain_cooldown_ms: int = 3000):
        self.domain_timestamps: Dict[str, float] = {}
        self.global_last_request: float = 0
        self.global_cooldown_ms = global_cooldown_ms
        self.domain_cooldown_ms = domain_cooldown_ms
    
    def wait_if_needed(self, url: str) -> None:
        """
        Wait if necessary to respect rate limits for a domain.
        """
        domain = urlparse(url).netloc
        #convert to milliseconds
        now = time.time() * 1000  
        
   
        time_since_global = now - self.global_last_request
        if time_since_global < self.global_cooldown_ms:
            sleep_time = (self.global_cooldown_ms - time_since_global) / 1000
            logger.debug(f"Global rate limit: Sleeping for {sleep_time:.2f} seconds")
            time.sleep(sleep_time)
        
        # Domain specific rate limiting
        if domain in self.domain_timestamps:
            time_since_domain = now - self.domain_timestamps[domain]
            if time_since_domain < self.domain_cooldown_ms:
                sleep_time = (self.domain_cooldown_ms - time_since_domain) / 1000
                logger.debug(f"Domain rate limit for {domain}: Sleeping for {sleep_time:.2f} seconds")
                time.sleep(sleep_time)
        
        # Update timestamps
        self.global_last_request = time.time() * 1000
        self.domain_timestamps[domain] = time.time() * 1000

class ArticleExtractor:
    """
    Extracts full article content from URLs with advanced scraping techniques.
    """
    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self.rate_limiter = RateLimiter()
        self._init_db()
    
    def _init_db(self) -> None:
        """
        Initialize the database schema if it doesn't exist.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Make sure the article table has the needed columns
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            title TEXT,
            url TEXT UNIQUE,
            published_date TIMESTAMP,
            content TEXT,
            html_content TEXT,
            extraction_date TIMESTAMP,
            status TEXT DEFAULT 'PENDING',
            FOREIGN KEY (source_id) REFERENCES sources (id)
        )
        ''')
        
        # Check if columns exist and add them if they don't
        existing_columns = [row[1] for row in cursor.execute("PRAGMA table_info(articles)").fetchall()]
        
        if 'html_content' not in existing_columns:
            cursor.execute("ALTER TABLE articles ADD COLUMN html_content TEXT")
        
        if 'extraction_date' not in existing_columns:
            cursor.execute("ALTER TABLE articles ADD COLUMN extraction_date TIMESTAMP")
        
        if 'status' not in existing_columns:
            cursor.execute("ALTER TABLE articles ADD COLUMN status TEXT DEFAULT 'PENDING'")
        
        conn.commit()
        conn.close()
    
    def clean_html_content(self, html: str) -> str:
        """
        Clean the HTML content by removing ads, social widgets, etc.
        While preserving actual article content with proper paragraph handling.
        """
        # Parse the HTML content directly
        soup = BeautifulSoup(html, 'html.parser')
        
        # Remove scripts, iframes, and style tags
        for tag in soup.find_all(['script', 'style', 'iframe']):
            tag.decompose()
        
        for selector in selectors:
            #selectors to remove ads, social widgets, etc.
            for element in soup.select(selector):
                element.decompose()

        for selector in paywall_selectors:
            for element in soup.select(selector):
                element.decompose()
        
        # remove modals and overlays
        modal_elements = soup.select('.modal, .modal-backdrop, body > div[style*="position: fixed"]')
        for element in modal_elements:
            element.decompose()
        
        if soup.body:
            soup.body['style'] = 'overflow: auto;'
        
        # identify main content div/article
        main_content = None
        
        # Look for article tag first
        article_tag = soup.find('article')
        if article_tag:
            main_content = article_tag
        else:
            # Look for common content container selectors
            content_selectors = [
                'div[class*="content"]', 'div[class*="article"]', 
                'div[id*="content"]', 'div[id*="article"]',
                'div[class*="story"]', 'div[class*="post"]',
                'main', '.main', '#main',
                'div[class*="body"]', 'div[id*="body"]'
            ]
            
            for selector in content_selectors:
                elements = soup.select(selector)
                if elements:
                    # Choose the one with the most paragraph elements
                    max_paragraphs = 0
                    for element in elements:
                        p_count = len(element.find_all('p'))
                        if p_count > max_paragraphs:
                            max_paragraphs = p_count
                            main_content = element
                    if main_content:
                        break
        
       
        if main_content:
            # create a new soup with just the article content
            new_soup = BeautifulSoup('<html><body></body></html>', 'html.parser')
            new_soup.body.append(main_content)
            return str(new_soup)
        
        #return the cleaned original soup if no main content is found
        return str(soup)
    
    def is_paywall_detected(self, html: str) -> bool:
        """
        Check if a paywall is likely present in the content.
        """
        soup = BeautifulSoup(html, 'html.parser')
        
        text = soup.get_text().lower()
        for phrase in paywall_phrases:
            if phrase in text:
                return True
        
        for selector in paywall_selectors:
            if soup.select(selector):
                return True
        

        article_tag = soup.find('article')
        if article_tag:
            article_text = article_tag.get_text(strip=True)
            if len(article_text) < 500:  # Arbitrary threshold
                return True
        
        return False
    
    def fetch_with_direct_http(self, url: str) -> Tuple[str, bool]:
        """
        Fetch article with direct HTTP request.
        Returns (html_content, is_paywall)
        """
        self.rate_limiter.wait_if_needed(url)
        
        # Enhanced headers with more browser-like properties
        headers = {
            'User-Agent': random.choice(USER_AGENTS),
            'Referer': random.choice(REFERRERS),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.5',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Upgrade-Insecure-Requests': '1',
            'Cache-Control': 'max-age=0',
        }
        
        
        try:
            # Use session to handle cookies automatically
            session = requests.Session()
            
            # First, make a HEAD request to get cookies
            try:
                session.head(url, headers=headers, timeout=5)
            except:
                pass  # Ignore errors on HEAD request
            
            # Add our custom cookies to the session
            for key, value in cookies.items():
                session.cookies.set(key, value)
            
            # make request
            response = session.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            
            html = response.text
            
            #anti-bot detection
            if "Please enable JS and disable any ad blocker" in html or "Just a moment" in html:
                logger.warning(f"Anti-bot detection encountered for {url}. Attempting bypass...")
                
                # wait and then  retry with enhanced headers
                time.sleep(2)
                
                # Add some additional headers that might help
                enhanced_headers = headers.copy()
                enhanced_headers.update({
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
                    'Cookie': '; '.join([f'{k}={v}' for k, v in cookies.items()]),
                    'Sec-Ch-Ua': '"Not A(Brand";v="99", "Google Chrome";v="121", "Chromium";v="121"',
                    'Sec-Ch-Ua-Mobile': '?0',
                    'Sec-Ch-Ua-Platform': '"macOS"',
                })
                
                #  with a different session
                new_session = requests.Session()
                response = new_session.get(url, headers=enhanced_headers, timeout=30)
                html = response.text
                
                # try one more approach (simplified mobile user agent)
                if "Please enable JS and disable any ad blocker" in html or "Just a moment" in html:
                    logger.warning(f"First bypass attempt failed for {url}. Trying mobile approach...")
                    time.sleep(3)
                    
                    mobile_headers = {
                        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept-Encoding': 'gzip, deflate, br',
                    }
                    
                    mobile_session = requests.Session()
                    response = mobile_session.get(url, headers=mobile_headers, timeout=30)
                    html = response.text
            
            is_paywall = self.is_paywall_detected(html)
            
            # If paywall is detected, we still return the HTML for processing,
            # but we flag it so caller knows it's incomplete
            return html, is_paywall
            
        except requests.RequestException as e:
            logger.error(f"Error fetching article from {url}: {e}")
            raise
    
    def extract_with_readability(self, html: str, url: str) -> Dict[str, Any]:
        """
        Extract article content using Mozilla's Readability.
        Preserves paragraph structure and formatting for better readability.
        """
   
        cleaned_html = self.clean_html_content(html)
        
        # i use readability to extract the main content
        doc = Document(cleaned_html)
        article = doc.summary(html_partial=False)
        
    
        soup = BeautifulSoup(article, 'html.parser')
        
        #extract title from original HTML first
        original_soup = BeautifulSoup(html, 'html.parser')
        title = None
        title_source = None
        
        
        if not title:
            # che meta title first
            meta_title = original_soup.find('meta', property='og:title') or \
                        original_soup.find('meta', attrs={'name': 'title'})
            if meta_title:
                title = meta_title.get('content')
                title_source = 'meta'
                logger.debug(f"Found title in meta tags: {title}")
        
        if not title:
            # article headline
            headline = original_soup.find(class_=['headline', 'article-title', 'entry-title']) or \
                      original_soup.find(['h1', 'h2'], class_=['title', 'headline'])
            if headline:
                title = headline.get_text(strip=True)
                title_source = 'headline'
                logger.debug(f"Found title in headline: {title}")
        
        if not title:
            # main title tag
            title_tag = original_soup.find('title')
            if title_tag:
                title = title_tag.get_text(strip=True)
                # remove site name if present (usually after | or - or —)
                original_title = title
                title = re.split(r'[\|\-—]', title)[0].strip()
                title_source = 'title_tag'
                logger.debug(f"Found title in title tag: {original_title} -> {title}")
        
        if not title:
            # fallback to readability title
            title = doc.title()
            title_source = 'readability'
            logger.debug(f"Using readability title as fallback: {title}")
            
        if not title:
            logger.warning(f"Could not extract title for URL: {url}")
        else:
            logger.info(f"Extracted title from {title_source}: {title}")
        
        # Ensure paragraphs are properly formatted, look for p (check again in inspect element)
        for p in soup.find_all('p'):
            # remove empty paragraphs
            if not p.get_text(strip=True):
                p.decompose()
            # add proper spacing for paragraphs
            elif not p.get_text(strip=True).endswith(('.', '!', '?', ':', ';', '"', "'", ')', ']', '}')):
                p.string = p.get_text(strip=True) + "."
        
        #a clean version of the html
        processed_html = str(soup)
        
        # extract text from HTML
        paragraphs = []
        for p in soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            text = p.get_text(strip=True)
            if text: 
                paragraphs.append(text)
        
        # join paragraphs with double newlines for better readability
        text_content = '\n\n'.join(paragraphs)
        
        # get metadata
        metadata = {
            'title': title,
            'content': text_content,
            'html_content': processed_html,
            'url': url,
        }
 
        try:
            # watch out for common date meta tags in the original HTML
            meta_soup = BeautifulSoup(html, 'html.parser')
            date_meta = meta_soup.find('meta', attrs={'property': 'article:published_time'})
            if not date_meta:
                date_meta = meta_soup.find('meta', attrs={'name': 'pubdate'})
            if not date_meta:
                date_meta = meta_soup.find('meta', attrs={'name': 'publishdate'})
            if not date_meta:
                date_meta = meta_soup.find('meta', attrs={'name': 'date'})
            if not date_meta:
                date_meta = meta_soup.find('time')
                
            if date_meta and date_meta.get('content'):
                metadata['published_date'] = date_meta['content']
            elif date_meta and date_meta.get('datetime'):
                metadata['published_date'] = date_meta['datetime']
        except Exception as e:
            logger.debug(f"Error extracting publish date: {e}")
        
        return metadata
    
    def _is_empty_or_blocked_content(self, html: str) -> bool:
        """
        Check if the HTML content is empty, blocked, or shows an anti-bot message.
        """

        anti_bot_phrases = [
            "Please enable JS and disable any ad blocker",
            "Just a moment",
            "Please wait while we verify your browser",
            "Please turn JavaScript on",
            "Please enable cookies",
            "Access denied",
            "You need to enable JavaScript to run this app",
            "cmsg",  # Often used for cloudflare protection messages
            "captcha",
            "Please check the box below to proceed"
        ]
        
        
        if len(html) < 1000:
            for phrase in anti_bot_phrases:
                if phrase.lower() in html.lower():
                    return True
        
       
        soup = BeautifulSoup(html, 'html.parser')
        if soup.title and any(x in soup.title.text.lower() for x in ["cloudflare", "attention required", "captcha", "security check"]):
            return True
            
        # very bad logic, but I'll change later
        main_content = soup.find('main') or soup.find('article') or soup.find('body')
        if main_content and len(main_content.get_text(strip=True)) < 200:
            # If main content exists but is suspiciously small, check for anti-bot indicators
            for phrase in anti_bot_phrases:
                if phrase.lower() in main_content.get_text(strip=True).lower():
                    return True
                    
        return False
        
    def extract_article(self, article: Article) -> Article:
        """
        Extract the full content of an article and update the article object.
        """
        if not article.url:
            logger.error("Cannot extract article: URL is missing")
            article.status = "FAILED"
            return article
        
        logger.info(f"Extracting content from: {article.url}")
        
        try:
            # Check if this is a premium site that needs special handling
            try:
                from scrappers.bypass_script import is_premium_site, fetch_with_bypass
                if is_premium_site(article.url):
                    logger.info(f"Using specialized bypass for premium site: {article.url}")
                    html, status = fetch_with_bypass(article.url)
                    is_paywall = status == "BLOCKED" or self.is_paywall_detected(html)
                    
                    if status == "ERROR":
                        logger.warning(f"Bypass failed for {article.url}, falling back to standard method")
                        html, is_paywall = self.fetch_with_direct_http(article.url)
                else:
                    
                    html, is_paywall = self.fetch_with_direct_http(article.url)
            except ImportError:
                # fallback if bypass_script is not available
                logger.debug("bypass_script module not available, using standard method")
                html, is_paywall = self.fetch_with_direct_http(article.url)
            
            # Check if we got blocked or empty content
            if self._is_empty_or_blocked_content(html):
                logger.warning(f"Anti-bot protection detected for {article.url}")
                article.status = "BLOCKED"
                # still attempt to extract what we can, but mark it as blocked
            elif is_paywall:
                logger.warning(f"Paywall detected for {article.url}")
                article.status = "PAYWALL"
                # we still try to extract what we can
            
            # extract content using Readability
            extraction_result = self.extract_with_readability(html, article.url)
            
            # update article with extracted content
            article.title = extraction_result.get('title') or article.title
            article.content = extraction_result.get('content')
            article.html_content = extraction_result.get('html_content')
            article.extraction_date = time.strftime('%Y-%m-%d %H:%M:%S')
            
            # if we haven't already marked it as a paywall or blocked, mark as complete
            if article.status not in ["PAYWALL", "BLOCKED"]:
                article.status = "COMPLETE"
                
            # if content is too short, it might be a failed extraction or paywall
            if article.content and len(article.content) < 500 and article.status not in ["PAYWALL", "BLOCKED"]:
                logger.warning(f"Extracted content for {article.url} seems too short, might be incomplete")
                article.status = "PARTIAL"
            
            return article
            
        except Exception as e:
            logger.error(f"Error extracting article from {article.url}: {e}")
            article.status = "FAILED"
            return article
    
    def update_article_in_db(self, article: Article) -> None:
        """
        Update the article in the database with extracted content.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                UPDATE articles 
                SET 
                    title = COALESCE(?, title),
                    content = ?,
                    html_content = ?,
                    extraction_date = ?,
                    status = ?
                WHERE url = ?
                """,
                (
                    article.title,
                    article.content,
                    article.html_content,
                    article.extraction_date,
                    article.status,
                    article.url
                )
            )
            conn.commit()
            logger.info(f"Updated article {article.url} in database")
        except sqlite3.Error as e:
            logger.error(f"Database error updating article {article.url}: {e}")
        finally:
            conn.close()
    
    def get_pending_articles(self, limit: int = 50) -> List[Article]:
        """
        Get articles that need content extraction.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                """
                SELECT id, source_id, title, url, published_date, status
                FROM articles
                WHERE (status = 'PENDING' OR status IS NULL) AND url IS NOT NULL
                LIMIT ?
                """,
                (limit,)
            )
            
            articles = []
            for row in cursor.fetchall():
                article = Article(
                    id=row['id'],
                    source_id=row['source_id'],
                    title=row['title'],
                    url=row['url'],
                    published_date=row['published_date'],
                    status=row['status'] or "PENDING"
                )
                articles.append(article)
            
            return articles
        except sqlite3.Error as e:
            logger.error(f"Database error getting pending articles: {e}")
            return []
        finally:
            conn.close()
    
    def process_pending_articles(self, max_articles: int = 50, max_workers: int = 5) -> int:
        """
        Process all pending articles to extract their content.
        Returns the number of articles processed.
        """
        articles = self.get_pending_articles(max_articles)
        
        if not articles:
            logger.info("No pending articles to process")
            return 0
        
        logger.info(f"Found {len(articles)} articles to process")
        processed_count = 0
        
        # Process articles with a thread pool for parallel execution
        # but with rate limiting in place
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_article = {executor.submit(self.extract_article, article): article for article in articles}
            
            for future in future_to_article:
                article = future_to_article[future]
                try:
                    processed_article = future.result()
                    self.update_article_in_db(processed_article)
                    processed_count += 1
                    logger.info(f"Processed article {processed_count}/{len(articles)}: {article.url}")
                except Exception as e:
                    logger.error(f"Error processing article {article.url}: {e}")
                    # Update status to FAILED
                    article.status = "FAILED"
                    article.extraction_date = time.strftime('%Y-%m-%d %H:%M:%S')
                    self.update_article_in_db(article)
        
        return processed_count

def main():
    """
    Main function to run the article extractor.
    """
    import argparse
    
    parser = argparse.ArgumentParser(description="Extract full article content for RSS feed entries")
    parser.add_argument("-l", "--limit", type=int, default=50, help="Maximum number of articles to process")
    parser.add_argument("-w", "--workers", type=int, default=5, help="Maximum number of worker threads")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger('article_extractor').setLevel(logging.DEBUG)
    
    extractor = ArticleExtractor()
    processed = extractor.process_pending_articles(max_articles=args.limit, max_workers=args.workers)
    
    logger.info(f"Processed {processed} articles")

if __name__ == "__main__":
    main() 