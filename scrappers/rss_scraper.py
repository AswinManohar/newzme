#!/usr/bin/env python3
"""
RSS Feed Scraper - Extracts and processes articles from configured RSS sources
"""
import os
import json
import logging
import datetime
import time
import requests
import feedparser
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from urllib.parse import urlparse
import sqlite3
from scrappers.article_extractor import ArticleExtractor

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger('rss_scraper')


CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
DB_FILE = os.path.join(os.path.dirname(__file__), 'articles.db')
USER_AGENTS = [
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; SM-S908B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
]
REFERRERS = [
    'https://www.google.com/',
    'https://www.bing.com/search?q=news',
    'https://www.reddit.com/r/news',
]

@dataclass
class Source:
    id: int
    name: str
    url: str
    frequency: int  # 1: hourly, 2: every 4 hours, 3: every 6 hours, 4: daily
    last_checked: Optional[datetime.datetime] = None

@dataclass
class Article:
    source_id: int
    title: str
    url: str
    published_date: Optional[datetime.datetime]
    content: Optional[str] = None
    html_content: Optional[str] = None
    status: str = "PENDING"  # PENDING, COMPLETE, FAILED, PAYWALL

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize the database with required tables if they don't exist"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create sources table
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS sources (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            frequency INTEGER NOT NULL,
            last_checked TIMESTAMP
        )
        ''')
        
        # Create articles table with additional columns for content extraction
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            published_date TIMESTAMP,
            content TEXT,
            html_content TEXT,
            extraction_date TIMESTAMP,
            status TEXT DEFAULT 'PENDING',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
        
    def get_sources(self) -> List[Source]:
        """Get all sources from the database"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM sources")
        sources = [
            Source(
                id=row['id'],
                name=row['name'],
                url=row['url'],
                frequency=row['frequency'],
                last_checked=datetime.datetime.fromisoformat(row['last_checked']) if row['last_checked'] else None
            )
            for row in cursor.fetchall()
        ]
        
        conn.close()
        return sources
        
    def update_source_last_checked(self, source_id: int):
        """Update the last_checked timestamp for a source"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE sources SET last_checked = ? WHERE id = ?",
            (datetime.datetime.now().isoformat(), source_id)
        )
        
        conn.commit()
        conn.close()
        
    def insert_articles(self, articles: List[Article]):
        """Insert new articles into the database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for article in articles:
            try:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO articles
                    (source_id, title, url, published_date, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        article.source_id,
                        article.title,
                        article.url,
                        article.published_date.isoformat() if article.published_date else None,
                        article.status
                    )
                )
            except Exception as e:
                logger.error(f"Error inserting article {article.url}: {e}")
        
        conn.commit()
        conn.close()
        
    def add_source(self, name: str, url: str, frequency: int = 2):
        """Add a new source to the database"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute(
                "INSERT INTO sources (name, url, frequency) VALUES (?, ?, ?)",
                (name, url, frequency)
            )
            conn.commit()
            logger.info(f"Added new source: {name} ({url})")
        except sqlite3.IntegrityError:
            logger.warning(f"Source with URL {url} already exists")
        finally:
            conn.close()
    
    def get_pending_articles_count(self) -> int:
        """Get the count of articles waiting to be processed"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT COUNT(*) FROM articles WHERE status = 'PENDING' OR status IS NULL"
        )
        count = cursor.fetchone()[0]
        
        conn.close()
        return count

class RssScraper:
    def __init__(self):
        self.db = Database(DB_FILE)
        self._load_config()
    
    def _load_config(self):
        """Load configuration from config file or create default config"""
        if not os.path.exists(CONFIG_FILE):
            logger.info("Config file not found, creating default config")
            default_config = {
                "check_interval": 3600,  # 1 hour
                "tier_intervals": {
                    "1": 3600,           # 1 hour in seconds
                    "2": 14400,          # 4 hours
                    "3": 21600,          # 6 hours
                    "4": 86400           # 24 hours
                },
                "auto_extract_content": True,  # Automatically extract content after scraping
                "max_extraction_batch": 50,    # Maximum number of articles to extract in one batch
                "extraction_workers": 5        # Number of parallel extraction workers
            }
            with open(CONFIG_FILE, 'w') as f:
                json.dump(default_config, f, indent=2)
            self.config = default_config
        else:
            with open(CONFIG_FILE, 'r') as f:
                self.config = json.load(f)
    
    def _should_check_source(self, source: Source) -> bool:
        """Determine if a source should be checked based on its frequency"""
        if not source.last_checked:
            return True
            
        tier_intervals = self.config.get("tier_intervals", {})
        interval = tier_intervals.get(str(source.frequency), 14400)  # Default to 4 hours
        
        time_diff = (datetime.datetime.now() - source.last_checked).total_seconds()
        return time_diff >= interval

    
    def _parse_feed(self, source: Source) -> List[Article]:
        """Parse an RSS feed and extract articles"""
        headers = {
            'User-Agent': USER_AGENTS[hash(source.url) % len(USER_AGENTS)],
            'Referer': REFERRERS[hash(source.url) % len(REFERRERS)]
        }
        
        try:
            feed = feedparser.parse(source.url, request_headers=headers)
            
            if feed.bozo and feed.bozo_exception:
                logger.warning(f"Error parsing feed {source.url}: {feed.bozo_exception}")
            
            articles = []
            for entry in feed.entries:
                # Extract publication date
                published_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published_date = datetime.datetime(*entry.published_parsed[:6])
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    published_date = datetime.datetime(*entry.updated_parsed[:6])
                
                # Filter out articles older than a week
                if published_date and (datetime.datetime.now() - published_date).days > 7:
                    continue
                
                # Extract link
                link = entry.link if hasattr(entry, 'link') else None
                if not link:
                    continue
                
                # Create article
                article = Article(
                    source_id=source.id,
                    title=entry.title if hasattr(entry, 'title') else "Unknown Title",
                    url=link,
                    published_date=published_date,
                    status="PENDING"
                )
                articles.append(article)
            
            return articles
        except Exception as e:
            logger.error(f"Error fetching feed {source.url}: {e}")
            return []
    
    def scrape_feeds(self, force: bool = False):
        """Scrape all feeds that need updating"""
        sources = self.db.get_sources()
        
        if not sources:
            logger.warning("No sources configured. Add sources using add_source method.")
            return
        
        logger.info(f"Found {len(sources)} sources")
        total_new_articles = 0
        
        for source in sources:
            if force or self._should_check_source(source):
                logger.info(f"Scraping feed: {source.name} ({source.url})")
                
                articles = self._parse_feed(source)
                logger.info(f"Found {len(articles)} articles from {source.name}")
                
                if articles:
                    self.db.insert_articles(articles)
                    total_new_articles += len(articles)
                
                self.db.update_source_last_checked(source.id)
                
                # be nice to servers with a small delay
                time.sleep(3)
            else:
                logger.debug(f"Skipping {source.name}, not due for check yet")
        
        logger.info(f"Completed scraping, found {total_new_articles} new articles")
        
        # Automatically start content extraction if configured
        if total_new_articles > 0 and self.config.get("auto_extract_content", True):
            self._trigger_content_extraction()
    
    def _trigger_content_extraction(self):
        """Trigger content extraction for pending articles"""
        pending_count = self.db.get_pending_articles_count()
        
        if pending_count > 0:
            logger.info(f"Found {pending_count} articles pending content extraction")
            
            try:
                
                max_batch = self.config.get("max_extraction_batch", 50)
                workers = self.config.get("extraction_workers", 5)
                
                logger.info(f"Starting content extraction with {workers} workers, batch size: {max_batch}")
                extractor = ArticleExtractor(DB_FILE)
                processed = extractor.process_pending_articles(max_articles=max_batch, max_workers=workers)
                
                logger.info(f"Content extraction completed, processed {processed} articles")
            except ImportError:
                logger.error("Could not import ArticleExtractor. Make sure article_extractor.py is in the same directory.")
            except Exception as e:
                logger.error(f"Error during content extraction: {e}")
    
    def add_source(self, name: str, url: str, frequency: int = 2):
        """Add a new source to the database"""
        self.db.add_source(name, url, frequency)

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="RSS Feed Scraper")
    parser.add_argument("-f", "--force", action="store_true", help="Force scraping all feeds regardless of schedule")
    parser.add_argument("-n", "--no-extract", action="store_true", help="Skip automatic content extraction")
    parser.add_argument("-e", "--extract-only", action="store_true", help="Only run content extraction without scraping feeds")
    
    args = parser.parse_args()
    
    scraper = RssScraper()
    
   
    if args.no_extract:
        scraper.config["auto_extract_content"] = False
    
    if args.extract_only:
        scraper._trigger_content_extraction()
    else:
        # example of adding sources if needed
        scraper.add_source("ZDNET", "https://www.zdnet.com/news/rss.xml", 1)
        # Uncomment and modify these lines to add your own sources
        #craper.add_source("CNN", "http://rss.cnn.com/rss/cnn_topstories.rss", 1)
        # scraper.add_source("BBC", "http://feeds.bbci.co.uk/news/rss.xml", 1)
        # scraper.add_source("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", 2)
  
        # Scrape all feeds
        scraper.scrape_feeds(force=args.force)

if __name__ == "__main__":
    main() 