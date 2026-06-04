import logging
import time
import urllib3
import requests
from defusedxml import ElementTree
from django.conf import settings
from pyrate_limiter import RedisBucket
from redis import ConnectionPool
from requests.adapters import HTTPAdapter
from requests_ratelimiter import LimiterSession

from app.models import BasicMedia, Status
from lists.models import CustomList

logger = logging.getLogger(__name__)

def get_redis_connection():
    if settings.TESTING:
        import fakeredis
        return fakeredis.FakeStrictRedis().connection_pool
    return ConnectionPool.from_url(settings.REDIS_URL)

try:
    redis_pool = get_redis_connection()
    bucket_name = f"{settings.REDIS_PREFIX}_api" if settings.REDIS_PREFIX else "api"
    session = LimiterSession(per_second=5, bucket_class=RedisBucket, bucket_kwargs={"redis_pool": redis_pool, "bucket_name": bucket_name})
except Exception:
    logger.warning("Redis unavailable, using plain session (BUG 5 FIX)")
    session = requests.Session()
session.mount("http://", HTTPAdapter(max_retries=3))
session.mount("https://", HTTPAdapter(max_retries=3))  # BUG 5 FIX: Fallback no rate limit

class ProviderAPIError(Exception):
    def __init__(self, provider, error, details=None):
        self.provider = provider
        self.status_code = error.response.status_code
        super().__init__(f"Error contacting {provider} API (HTTP {self.status_code})")

def api_request(provider, method, url, params=None, data=None, headers=None, response_format="json"):
    try:
        request_kwargs = {"url": url, "headers": headers, "timeout": settings.REQUEST_TIMEOUT}
        if method == "GET":
            request_kwargs["params"] = params
            response = session.get(**request_kwargs)
        else:
            request_kwargs["json"] = params
            response = session.post(**request_kwargs)
        response.raise_for_status()
        return ElementTree.fromstring(response.text) if response_format == "xml" else response.json()
    except requests.exceptions.ConnectionError as e:
        # Safely wrap connection error to mimic a 503 response so providers handle it natively
        mock_response = type("Response", (), {"status_code": 503})()
        mock_error = type("Error", (), {"response": mock_response})()
        raise ProviderAPIError(provider, error=mock_error, details="Connection error - API temporarily unavailable") from e
    except Exception as error:
        raise error

def get_media_metadata(media_type, media_id, source, season_numbers=None, episode_number=None):
    from app.models import MediaTypes, Sources
    from app.providers import manual, mal, mangaupdates, tmdb, igdb, hardcover, openlibrary, bgg, comicvine
    if source == Sources.MANUAL.value:
        return manual.metadata(media_id, media_type)
    
    if media_type == MediaTypes.MOVIE.value:
        return tmdb.movie(media_id)
    elif media_type == MediaTypes.TV.value:
        return tmdb.tv(media_id)
    elif media_type == "tv_with_seasons":
        return tmdb.tv_with_seasons(media_id, season_numbers)
    elif media_type == MediaTypes.GAME.value:
        return igdb.game(media_id)
    elif media_type == MediaTypes.ANIME.value:
        return mal.anime(media_id)
    elif media_type == MediaTypes.MANGA.value:
        return mal.manga(media_id) if source == Sources.MAL.value else mangaupdates.manga(media_id)
    elif media_type == MediaTypes.BOOK.value:
        return hardcover.book(media_id) if source == Sources.HARDCOVER.value else openlibrary.book(media_id)
    elif media_type == MediaTypes.COMIC.value:
        return comicvine.comic(media_id)
    elif media_type == MediaTypes.BOARDGAME.value:
        return bgg.boardgame(media_id)
        
    return tmdb.movie(media_id)

def search(media_type, query, page, source=None):
    """
    Search using the provider selected by `source`.

    Always returns the standard Yamtrack search shape:
    {"page": int, "total_results": int, "total_pages": int, "results": [ ... ]}
    """
    from app import config
    from app.models import MediaTypes, Sources
    from app.providers import (
        bgg,
        comicvine,
        hardcover,
        igdb,
        mal,
        mangaupdates,
        openlibrary,
        tmdb,
    )

    # Normalize/validate source. If missing or invalid, fall back to configured default.
    try:
        selected_source = Sources(source).value if source else None
    except Exception:
        selected_source = None
    if not selected_source:
        selected_source = config.get_default_source_name(media_type).value

    # Route by selected source, with safe fallbacks if a mismatched source is chosen.
    if selected_source == Sources.TMDB.value:
        # TMDB supports only movie/tv style search in this app.
        tmdb_type = media_type
        if media_type in (MediaTypes.SEASON.value, MediaTypes.EPISODE.value):
            tmdb_type = MediaTypes.TV.value
        return tmdb.search(tmdb_type, query, page)

    if selected_source == Sources.MAL.value:
        # MAL supports anime/manga endpoints; if other media_type is passed, fall back to default.
        if media_type not in (MediaTypes.ANIME.value, MediaTypes.MANGA.value):
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return mal.search(media_type, query, page)

    if selected_source == Sources.MANGAUPDATES.value:
        if media_type != MediaTypes.MANGA.value:
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return mangaupdates.search(query, page)

    if selected_source == Sources.IGDB.value:
        if media_type != MediaTypes.GAME.value:
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return igdb.search(query, page)

    if selected_source == Sources.OPENLIBRARY.value:
        if media_type != MediaTypes.BOOK.value:
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return openlibrary.search(query, page)

    if selected_source == Sources.HARDCOVER.value:
        if media_type != MediaTypes.BOOK.value:
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return hardcover.search(query, page)

    if selected_source == Sources.COMICVINE.value:
        if media_type != MediaTypes.COMIC.value:
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return comicvine.search(query, page)

    if selected_source == Sources.BGG.value:
        if media_type != MediaTypes.BOARDGAME.value:
            selected_source = config.get_default_source_name(media_type).value
            return search(media_type, query, page, selected_source)
        return bgg.search(query, page)

    # Final fallback (should be unreachable unless new source is added without routing).
    selected_source = config.get_default_source_name(media_type).value
    return search(media_type, query, page, selected_source)

def get_smart_recommendations(user):
    """Gemini-powered recommendations grouped by media type."""
    from app.models import Item, BasicMedia, Status
    from lists.models import CustomList
    import os
    import json
    import google.genai as genai
    from app.templatetags.app_tags import slug
    from django.core.cache import cache

    # Try to fetch from cache first to avoid slow API/Gemini calls on every page load
    cache_key = f"smart_recs_{user.id}"
    cached_recs = cache.get(cache_key)
    # FIX: Only return the cache if it has actual recommendations
    if cached_recs and cached_recs.get("recommendations"):
        return cached_recs

    try:
        items_by_type = {}

        # 1. Recently tracked items (existing logic)
        qs_recent = Item.objects.filter(
            basicmedia__user=user
        ).order_by('-id')[:10]
        for item in qs_recent:
            mt = getattr(item, 'media_type', 'movie')
            items_by_type.setdefault(mt, []).append(item.title)

        # 2. Items from custom lists
        user_lists = CustomList.objects.get_user_lists(user)
        for custom_list in user_lists:
            list_name = custom_list.name
            # Limit to a reasonable number of items per list to avoid overly long prompts
            for list_item in custom_list.items.all()[:5]:
                mt = getattr(list_item, 'media_type', 'movie')
                # Use a unique key for list items to distinguish them in the prompt
                key = f"list_{slug(list_name)}_{mt}"
                items_by_type.setdefault(key, []).append(list_item.title)

        # 3. Items by status (Drafted, Planning, Completing, etc.)
        status_qs = BasicMedia.objects.filter(
            user=user,
            status__in=[
                Status.PLANNING, Status.IN_PROGRESS, Status.COMPLETED,
                Status.DROPPED, Status.PAUSED, 
            ]
        ).select_related('item').order_by('-created_at')[:20] # Limit overall items by status

        for media in status_qs:
            mt = getattr(media.item, 'media_type', 'movie')
            # Use a unique key for status items
            key = f"status_{media.status.lower()}_{mt}"
            items_by_type.setdefault(key, []).append(media.item.title)

        if not items_by_type:
            return {"recommendations": [], "spotlight": {}}

        prompt_parts = [
            "Recommend 3-5 similar/popular titles per category based on these items. "
            "Return ONLY valid JSON: {\"movie\": [\"Title1\", \"Title2\"], \"tv\": [\"Title3\"], \"anime\": [\"Title4\"]} no other text."
        ]

        for key, titles in items_by_type.items():
            display_name = key
            if key.startswith("list_"):
                # Extract original list name from slugged key for display
                parts = key.split('_')
                list_name_slug = parts[1]
                media_type = parts[2]
                original_list_name = next((cl.name for cl in user_lists if slug(cl.name) == list_name_slug), list_name_slug)
                display_name = f"from custom list '{original_list_name}' ({media_type})"
            elif key.startswith("status_"):
                _, status_name, media_type = key.split('_')
                display_name = f"with status '{status_name}' ({media_type})"
            else: # Default for recently tracked items, assuming key is media_type
                display_name = f"recently tracked ({key})"

            prompt_parts.append(f"Items {display_name}: {', '.join(titles[:5])}")

        prompt = "\n".join(prompt_parts) + "\nJSON:"

        # Use a mock response if testing to avoid external API calls
        if settings.TESTING:
             recs = {"movie": ["Test Movie 1", "Test Movie 2"], "tv": ["Test TV Show 1"]}
        else:
            api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            if not api_key:
                logger.error("GEMINI_API_KEY is missing! Please set it in your .env file.")
                return {"recommendations": [], "spotlight": {}}
                
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            import re
            json_match = re.search(r'\{.*\}', response.text, re.DOTALL)
            if json_match:
                recs = json.loads(json_match.group(0))
            else:
                logger.error(f"Gemini returned invalid format: {response.text}")
                recs = {}

        recommendations = []
        for mtype, titles in recs.items():
            for title in titles[:4]:
                try:
                    # HYDRATION STEP: Search API to get real metadata
                    search_results = search(mtype, title, 1)
                    if search_results and search_results.get("results"):
                        top_result = search_results["results"][0]
                        
                        # FIX: Safely map the item depending on the provider's return shape
                        item_data = top_result.get("item") if isinstance(top_result.get("item"), dict) else top_result
                        
                        if isinstance(item_data, dict) and item_data.get("media_id"):
                            item_data["media_type"] = item_data.get("media_type") or mtype
                            item_data["source"] = item_data.get("source") or 'tmdb'
                            
                            recommendations.append({
                                'item': item_data,
                                'title': item_data.get('title') or title
                            })
                            continue
                except Exception as e:
                    logger.error(f"Failed to fetch metadata for recommended {mtype} '{title}': {e}")

                # Fallback if API search fails
                recommendations.append({
                    'item': {
                        'title': title,
                        'image': '',
                        'media_id': slug(title),
                        'media_type': mtype,
                        'source': 'tmdb',
                    },
                    'title': title
                })

        movie_recs = recs.get('movie', [])
        spotlight_item = movie_recs if movie_recs else {}
        result = {"recommendations": recommendations[:20], "spotlight": spotlight_item}
        
        # FIX: Only cache recommendations if the API actually gave us titles
        if recommendations:
            cache.set(cache_key, result, 86400)
            
        return result
    except Exception as e:
        logger.error(f"Gemini recs failed: {e}")
        return {"recommendations": [], "spotlight": {}}

def generate_summary(text):
    import os
    import google.genai as genai  # FIXED Bug 2: New SDK
    try:
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-pro",
            contents=f"Summarize in 2-3 sentences: {text}"
        )  # FIXED Bug 2: Correct syntax
        return response.text.strip()
    except Exception:
        return "Summary not available."
