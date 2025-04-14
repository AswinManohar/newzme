"""
Configuration loader for scraper settings
"""
import json
import os
import logging

logger = logging.getLogger(__name__)

def load_config():
    """Load configuration from JSON file"""
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            return config
    except Exception as e:
        logger.error(f"Error loading config from {config_path}: {e}")
        raise

config = load_config()


USER_AGENTS = config['user_agents']
REFERRERS = config['referrers']
selectors = config['selectors']
paywall_selectors = config['paywall_selectors']
content_selectors = config['content_selectors']
paywall_phrases = config['paywall_phrases']
cookies = config['cookies'] 