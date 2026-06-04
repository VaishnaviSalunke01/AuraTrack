import os
import logging
from typing import Dict, List, Optional, Any

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

# API Keys from settings (consistent with TMDB provider)
try:
    from django.conf import settings
    TMDB_API_KEY = settings.TMDB_API
    GOOGLE_BOOKS_API_KEY = settings.GOOGLE_BOOKS_API_KEY if hasattr(settings, 'GOOGLE_BOOKS_API_KEY') else None
    SPOTIFY_CLIENT_ID = settings.SPOTIFY_CLIENT_ID if hasattr(settings, 'SPOTIFY_CLIENT_ID') else None
    SPOTIFY_CLIENT_SECRET = settings.SPOTIFY_CLIENT_SECRET if hasattr(settings, 'SPOTIFY_CLIENT_SECRET') else None
except ImportError:
    # Fallback for when Django settings aren't available
    TMDB_API_KEY = os.environ.get('TMDB_API')
    GOOGLE_BOOKS_API_KEY = os.environ.get('GOOGLE_BOOKS_API_KEY')
    SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
    SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# DEBUG: Verify TMDB API Key is loaded at startup
print(f"DEBUG: TMDB API Key found: {TMDB_API_KEY[:4] if TMDB_API_KEY else 'NOT FOUND'}{'...' if TMDB_API_KEY else ''}")

# Cache timeout: 24 hours
CACHE_TIMEOUT = 86400

class StreamingServiceError(Exception):
    """Exception raised when a streaming service API fails."""
    pass

def get_spotify_access_token() -> Optional[str]:
    """Get Spotify access token using client credentials flow."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None

    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "client_credentials"
    }
    auth = (SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET)

    try:
        response = requests.post(url, data=data, auth=auth, timeout=10)
        response.raise_for_status()
        return response.json().get("access_token")
    except requests.RequestException as e:
        logger.error("Failed to get Spotify access token: %s", e)
        return None

def get_tmdb_watch_providers(media_type: str, media_id: str, region: str = "IN") -> List[Dict[str, Any]]:
    """Get watch providers from TMDB API with fallback search URLs."""
    if not TMDB_API_KEY:
        raise StreamingServiceError("TMDB API key not configured")

    cache_key = f"tmdb_watch_{media_type}_{media_id}_{region}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    url = f"https://api.themoviedb.org/3/{media_type}/{media_id}/watch/providers"
    params = {"api_key": TMDB_API_KEY}

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        providers = []
        if "results" in data and region in data["results"]:
            region_data = data["results"][region]

            def make_search_url(provider_name):
                query = requests.utils.quote(f"{provider_name}")
                return f"https://www.google.com/search?q={query}"

            if "flatrate" in region_data:
                for provider in region_data["flatrate"]:
                    providers.append({
                        "name": provider["provider_name"],
                        "logo": f"https://image.tmdb.org/t/p/original{provider['logo_path']}",
                        "type": "stream",
                        "url": make_search_url(provider["provider_name"])
                    })

            for provider_type in ["buy", "rent"]:
                if provider_type in region_data:
                    for provider in region_data[provider_type]:
                        providers.append({
                            "name": provider["provider_name"],
                            "logo": f"https://image.tmdb.org/t/p/original{provider['logo_path']}",
                            "type": "purchase",
                            "url": make_search_url(provider["provider_name"])
                        })

        cache.set(cache_key, providers, CACHE_TIMEOUT)
        return providers

    except requests.RequestException as e:
        logger.error("Failed to get TMDB watch providers: %s", e)
        return []

def get_google_books_links(isbn: str = None, title: str = None) -> List[Dict[str, Any]]:
    """Get purchase links from Google Books API."""
    if not GOOGLE_BOOKS_API_KEY:
        raise StreamingServiceError("Google Books API key not configured")

    cache_key = f"google_books_{isbn or title}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    query = f"isbn:{isbn}" if isbn else f"intitle:{title}"
    url = "https://www.googleapis.com/books/v1/volumes"
    params = {
        "q": query,
        "key": GOOGLE_BOOKS_API_KEY,
        "maxResults": 1
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        providers = []
        if "items" in data and data["items"]:
            volume = data["items"][0]["volumeInfo"]

            # Add purchase links
            if "saleInfo" in volume and volume["saleInfo"].get("buyLink"):
                providers.append({
                    "name": "Google Books",
                    "logo": "/static/img/providers/google-books.png",  # You'll need to add this image
                    "type": "purchase",
                    "url": volume["saleInfo"]["buyLink"]
                })

            # Add preview link if available
            if volume.get("previewLink"):
                providers.append({
                    "name": "Google Books Preview",
                    "logo": "/static/img/providers/google-books.png",
                    "type": "preview",
                    "url": volume["previewLink"]
                })

        cache.set(cache_key, providers, CACHE_TIMEOUT)
        return providers

    except requests.RequestException as e:
        logger.error("Failed to get Google Books links: %s", e)
        return []

def get_spotify_links(query: str) -> List[Dict[str, Any]]:
    """Get Spotify links for music."""
    access_token = get_spotify_access_token()
    if not access_token:
        return []

    cache_key = f"spotify_{query}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    url = "https://api.spotify.com/v1/search"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    params = {
        "q": query,
        "type": "album,track",
        "limit": 5
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        providers = []

        # Add albums
        if "albums" in data and "items" in data["albums"]:
            for album in data["albums"]["items"][:2]:  # Limit to 2
                providers.append({
                    "name": f"Spotify - {album['name']}",
                    "logo": "/static/img/providers/spotify.png",  # You'll need to add this image
                    "type": "stream",
                    "url": album["external_urls"]["spotify"]
                })

        # Add tracks
        if "tracks" in data and "items" in data["tracks"]:
            for track in data["tracks"]["items"][:2]:  # Limit to 2
                providers.append({
                    "name": f"Spotify - {track['name']} by {track['artists'][0]['name']}",
                    "logo": "/static/img/providers/spotify.png",
                    "type": "stream",
                    "url": track["external_urls"]["spotify"]
                })

        cache.set(cache_key, providers, CACHE_TIMEOUT)
        return providers

    except requests.RequestException as e:
        logger.error("Failed to get Spotify links: %s", e)
        return []

def get_smart_search_links(title: str, media_type: str) -> List[Dict[str, Any]]:
    """Generate smart search links for YouTube and Google when no direct links found."""
    encoded_title = requests.utils.quote(title)

    providers = [
        {
            "name": "YouTube Search",
            "logo": "/static/img/providers/youtube.png",  # You'll need to add this image
            "type": "search",
            "url": f"https://www.youtube.com/results?search_query={encoded_title}"
        },
        {
            "name": "Google Search",
            "logo": "/static/img/providers/google.png",  # You'll need to add this image
            "type": "search",
            "url": f"https://www.google.com/search?q={encoded_title}+{media_type}"
        }
    ]

    return providers

def get_media_links(category: str, item_id: str, title: str = None, isbn: str = None) -> Dict[str, Any]:
    """
    Get streaming/purchase links for a media item.

    Args:
        category: 'movie', 'tv', 'book', 'music', 'game', 'comic'
        item_id: The media ID (TMDB ID for movies/TV, ISBN for books, etc.)
        title: Media title for fallback searches
        isbn: ISBN for books

    Returns:
        Dict with 'providers' list and 'has_direct_links' boolean
    """
    providers = []

    try:
        if category in ['movie', 'tv']:
            providers = get_tmdb_watch_providers(category, item_id)
        elif category == 'book':
            providers = get_google_books_links(isbn=isbn, title=title)
        elif category == 'music':
            providers = get_spotify_links(title or item_id)
        else:
            # Games, comics, etc. - use smart search
            providers = get_smart_search_links(title or item_id, category)

        # If no direct links found, add smart search as fallback
        if not providers and title:
            providers = get_smart_search_links(title, category)

    except StreamingServiceError as e:
        logger.error("Streaming service error: %s", e)
        if title:
            providers = get_smart_search_links(title, category)

    return {
        "providers": providers,
        "has_direct_links": any(p["type"] in ["stream", "purchase", "preview"] for p in providers)
    }