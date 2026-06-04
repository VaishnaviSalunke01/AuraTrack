import logging
import json
import os
import requests
from pathlib import Path
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import prefetch_related_objects
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.timezone import datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.contrib.auth.decorators import login_required

from app import config, helpers, history_processor
from app import statistics as stats
from app.forms import EpisodeForm, ManualItemForm, get_form_class
from app.models import TV, BasicMedia, Item, MediaTypes, Season, Sources, Status
from app.providers import manual, services, streaming_services, tmdb
from app.templatetags import app_tags
from lists.models import CustomList
from users.models import HomeSortChoices, MediaSortChoices, MediaStatusChoices

logger = logging.getLogger(__name__)

def get_cached_recommendations(user):
    """Fetch smart recommendations, using cache to prevent API spam."""
    if not user.is_authenticated:
        return {"recommendations": [], "spotlight": {}}
        
    cache_key = f"user_{user.id}_smart_recs"
    recs_data = cache.get(cache_key)
    
    # FIX: Ignore the cache if it's an empty list trap
    if not recs_data or not recs_data.get("recommendations"):
        try:
            recs_data = services.get_smart_recommendations(user)
            if recs_data and recs_data.get("recommendations"):
                cache.set(cache_key, recs_data, timeout=60 * 60 * 12)  # Cache for 12 hours
        except Exception as e:
            logger.error(f"Error fetching recommendations: {e}")
            recs_data = {"recommendations": [], "spotlight": {}}
            
    return recs_data


def build_user_library_summary(user):
    """Build a concise summary of the authenticated user's Yamtrack library."""
    if not user.is_authenticated:
        return "The user is not authenticated. Personal Yamtrack library details are unavailable."

    from django.db.models import Count

    media_qs = BasicMedia.objects.filter(user=user.id).select_related("item")
    total_items = media_qs.count()

    status_counts = {status: 0 for status in Status.values}
    for row in media_qs.values("status").annotate(count=Count("id")):
        status_counts[row["status"]] = row["count"]

    media_type_counts = {}
    for row in media_qs.values("item__media_type").annotate(count=Count("id")):
        media_type_counts[row["item__media_type"]] = row["count"]

    recently_added = list(media_qs.order_by("-created_at")[:5])
    recently_completed = list(media_qs.filter(status=Status.COMPLETED.value).order_by("-end_date")[:5])
    in_progress_items = list(media_qs.filter(status=Status.IN_PROGRESS.value).order_by("-created_at")[:5])
    custom_lists = CustomList.objects.get_user_lists(user)

    list_details = [f"{custom_list.name} ({custom_list.items.count()} items)" for custom_list in custom_lists]

    lines = [
        "Yamtrack library summary for the authenticated user:",
        f"Total tracked items: {total_items}.",
    ]

    if total_items:
        status_summary = ", ".join(
            f"{Status(status).label if status in Status.values else status}: {count}"
            for status, count in status_counts.items()
            if count
        )
        if status_summary:
            lines.append(f"Status counts: {status_summary}.")

    if media_type_counts:
        media_type_summary = ", ".join(
            f"{MediaTypes(mt).label if mt in MediaTypes.values else mt}: {count}"
            for mt, count in media_type_counts.items()
        )
        lines.append(f"Tracked media by type: {media_type_summary}.")

    if list_details:
        lines.append("Custom lists: " + "; ".join(list_details) + ".")

    if in_progress_items:
        lines.append(
            "Current in-progress items: "
            + ", ".join(item.item.title for item in in_progress_items[:3])
            + "."
        )

    if recently_added:
        lines.append(
            "Recently added: "
            + ", ".join(item.item.title for item in recently_added[:3])
            + "."
        )

    if recently_completed:
        lines.append(
            "Recently completed: "
            + ", ".join(item.item.title for item in recently_completed[:3])
            + "."
        )

    lines.append(
        "Use this library data when the user asks about their own tracked items, watchlist, recent activity, or custom lists."
    )
    return "\n".join(lines)


def build_user_library_response(user, query=""):
    """
    **ENHANCED** Build intelligent response to user's specific library query.
    This function actually PROCESSES the query and returns relevant filtered data.
    
    Args:
        user: Django user object
        query (str): User's actual question (e.g., "What's in my completed list?", "Show my watchlist")
        
    Returns:
        str: Specific answer to user's query
    """
    if not user.is_authenticated:
        return "You're not logged in. I can't access your personal library."
    
    from django.db.models import Count
    
    query_lower = query.lower() if query else ""
    media_qs = BasicMedia.objects.filter(user=user.id).select_related("item")
    
    # 1. Check for COMPLETED/FINISHED items
    if any(word in query_lower for word in ["completed", "finished", "done", "watched", "read", "played"]):
        completed_items = media_qs.filter(status=Status.COMPLETED.value).order_by("-end_date")
        if completed_items.exists():
            items_list = [f"• {item.item.title}" for item in completed_items[:20]]
            return f"Your completed items ({completed_items.count()} total):\n" + "\n".join(items_list)
        else:
            return "You haven't completed anything yet. Start tracking items to see them here!"
    
    # 2. Check for WATCHLIST/WANT TO WATCH
    if any(word in query_lower for word in ["watchlist", "want to watch", "planning", "backlog", "todo", "to-do"]):
        planning_items = media_qs.filter(status=Status.PLANNING.value).order_by("-created_at")
        if planning_items.exists():
            items_list = [f"• {item.item.title}" for item in planning_items[:20]]
            return f"Your watchlist/planning items ({planning_items.count()} total):\n" + "\n".join(items_list)
        else:
            return "Your watchlist is empty. Add items to see them here!"
    
    # 3. Check for IN PROGRESS items
    if any(word in query_lower for word in ["progress", "watching", "reading", "playing", "currently", "in progress"]):
        in_progress = media_qs.filter(status=Status.IN_PROGRESS.value).order_by("-created_at")
        if in_progress.exists():
            items_list = [f"• {item.item.title}" for item in in_progress[:20]]
            return f"Items you're currently watching/reading ({in_progress.count()} total):\n" + "\n".join(items_list)
        else:
            return "You're not currently watching anything. Start something!"
    
    # 4. Check for custom lists by name
    if any(word in query_lower for word in ["list", "custom"]):
        custom_lists = CustomList.objects.get_user_lists(user)
        for custom_list in custom_lists:
            if custom_list.name.lower() in query_lower:
                items = custom_list.items.all()
                if items.exists():
                    items_list = [f"• {item.item.title}" for item in items[:20]]
                    return f"Items in '{custom_list.name}' ({items.count()} total):\n" + "\n".join(items_list)
                else:
                    return f"The list '{custom_list.name}' is empty."
    
    # 5. Check for TIME SPENT queries (this data might not exist yet)
    if any(word in query_lower for word in ["time", "spent", "hours", "today", "this week", "this month"]):
        total_items = media_qs.count()
        return f"Time tracking feature coming soon! You currently have {total_items} items tracked."
    
    # 6. Default: return general summary
    total_items = media_qs.count()
    status_counts = {}
    for row in media_qs.values("status").annotate(count=Count("id")):
        status_name = Status(row["status"]).label if row["status"] in Status.values else row["status"]
        status_counts[status_name] = row["count"]
    
    summary_lines = [f"Your library has {total_items} items:"]
    for status_name, count in status_counts.items():
        summary_lines.append(f"  • {status_name}: {count}")
    
    return "\n".join(summary_lines)


# ============================================================================
# EXTERNAL API HELPERS FOR ANTI-HALLUCINATION (OMDb + Wikipedia Fallback)
# ============================================================================

def fetch_from_omdb(title, year=None):
    """
    Fetch movie/show data from OMDb API when local DB is incomplete.
    
    Args:
        title (str): Movie or show title
        year (str): Optional year to narrow search
        
    Returns:
        dict: Structured data with cast, plot, etc. or None if not found
    """
    try:
        omdb_key = "89d71b4d"  # Your OMDb API key
        url = "https://www.omdbapi.com/"
        
        params = {
            "apikey": omdb_key,
            "t": title,
            "type": "movie",
            "plot": "full"
        }
        if year:
            params["y"] = year
            
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data.get("Response") == "True":
            # Parse OMDb response into our format
            actors = []
            if data.get("Actors") and data.get("Actors") != "N/A":
                actor_list = data.get("Actors", "").split(", ")
                for idx, actor_name in enumerate(actor_list[:15], start=1):
                    actors.append({
                        "actor": actor_name.strip(),
                        "character": "",  # OMDb doesn't provide character names in basic query
                        "billing": idx
                    })
            
            directors = []
            if data.get("Director") and data.get("Director") != "N/A":
                directors = [d.strip() for d in data.get("Director", "").split(",")]
            
            return {
                "title": data.get("Title"),
                "release_date": data.get("Released"),
                "synopsis": data.get("Plot"),
                "directors": directors,
                "cast": actors,
                "imdb_id": data.get("imdbID"),
                "source": "OMDb",
                "rating": data.get("imdbRating"),
            }
    except Exception as e:
        logger.error(f"OMDb API error for '{title}': {str(e)}")
    
    return None


def fetch_from_wikipedia(title):
    """
    Fetch movie/show data from Wikipedia API when OMDb fails.
    
    Args:
        title (str): Movie or show title
        
    Returns:
        dict: Structured data with plot summary or None if not found
    """
    try:
        # Wikipedia API endpoint
        url = "https://en.wikipedia.org/w/api.php"
        
        params = {
            "action": "query",
            "titles": title,
            "prop": "extracts|pageimages",
            "explaintext": True,
            "format": "json",
            "redirects": True
        }
        
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        pages = data.get("query", {}).get("pages", {})
        if pages:
            page = list(pages.values())[0]
            
            # Check if page exists
            if "missing" not in page:
                extract = page.get("extract", "")
                
                # Try to extract cast info from the extract text
                # This is a simple heuristic - Wikipedia format varies
                cast = []
                lines = extract.split("\n")
                
                if extract:
                    return {
                        "title": page.get("title"),
                        "synopsis": extract[:500] if extract else "",  # First 500 chars
                        "cast": cast,
                        "directors": [],
                        "source": "Wikipedia",
                    }
    except Exception as e:
        logger.error(f"Wikipedia API error for '{title}': {str(e)}")
    
    return None


@require_GET
def home(request):
    """Home page with media items in progress."""
    sort_by = request.GET.get("sort")
    if request.user.is_authenticated:
        sort_by = request.user.update_preference("home_sort", sort_by)
    else:
        sort_by = sort_by or HomeSortChoices.RECENT
        
    media_type_to_load = request.GET.get("load_media_type")
    items_limit = 14

    list_by_type = BasicMedia.objects.get_in_progress(
        request.user,
        sort_by,
        items_limit,
        media_type_to_load,
    )

    if request.headers.get("HX-Request") and media_type_to_load:
        context = {"media_list": list_by_type.get(media_type_to_load, [])}
        return render(request, "app/components/home_grid.html", context)

    # HTMX Endpoint for Lazy Loading Recommendations
    if request.headers.get("HX-Request") and request.GET.get("load_recommendations"):
        recommendations_list = []
        spotlight_item = {}
        if request.user.is_authenticated:
            if request.GET.get("refresh") == "true":
                cache.delete(f"user_{request.user.id}_smart_recs")
                cache.delete(f"smart_recs_{request.user.id}")
            recs_data = get_cached_recommendations(request.user)
            raw_recommendations = recs_data.get("recommendations", [])
            spotlight_item = recs_data.get("spotlight", {})

            if raw_recommendations:
                try:
                    # Extract just the raw items to send to the enricher
                    items_to_enrich = [rec["item"] for rec in raw_recommendations]
                    enriched_results = helpers.enrich_items_with_user_data(
                        request, items_to_enrich, "recommendations"
                    )
                    # Re-attach the customized title and structure for the template
                    for i, enriched in enumerate(enriched_results):
                        recommendations_list.append({
                            "item": enriched["item"],
                            "media": enriched.get("media"),
                            "title": raw_recommendations[i].get("title", enriched["item"].get("title"))
                        })
                except Exception as e:
                    logger.error(f"Error enriching recommendations: {e}")
                    # Fallback to raw items if database enrichment fails
                    recommendations_list = raw_recommendations
        
        return render(request, "app/components/home_recommendations.html", {
            "recommendations": recommendations_list,
            "spotlight": spotlight_item,
        })
    
    context = {
        "list_by_type": list_by_type,
        "current_sort": sort_by,
        "sort_choices": HomeSortChoices.choices,
        "items_limit": items_limit,
        "recommendations": [],
        "spotlight": {},
    }
    return render(request, "app/home.html", context)


@login_required
@require_POST
def progress_edit(request, media_type, instance_id):
    """Increase or decrease the progress of a media item from home page."""
    operation = request.POST["operation"]

    media = BasicMedia.objects.get_media_prefetch(
        request.user,
        media_type,
        instance_id,
    )

    if operation == "increase":
        media.increase_progress()
    elif operation == "decrease":
        media.decrease_progress()

    if media_type == MediaTypes.SEASON.value:
        # clear prefetch cache to get the updated episodes
        media.refresh_from_db()
        prefetch_related_objects([media], "episodes")

    context = {
        "media": media,
    }
    return render(
        request,
        "app/components/progress_changer.html",
        context,
    )


@login_required
@require_GET
def media_list(request, media_type):
    """Return the media list page."""
    layout = request.GET.get("layout", "grid")
    sort_filter = request.GET.get("sort")
    status_filter = request.GET.get("status")
    
    if request.user.is_authenticated:
        layout = request.user.update_preference(f"{media_type}_layout", layout) or "grid"
        sort_filter = request.user.update_preference(f"{media_type}_sort", sort_filter)
        status_filter = request.user.update_preference(f"{media_type}_status", status_filter)
        
    search_query = request.GET.get("search", "")
    page = request.GET.get("page", 1)

    if not status_filter:
        status_filter = MediaStatusChoices.ALL

    media_queryset = BasicMedia.objects.get_media_list(
        user=request.user,
        media_type=media_type,
        status_filter=status_filter,
        sort_filter=sort_filter,
        search=search_query,
    )

    paginator = Paginator(media_queryset, 32)
    media_page = paginator.get_page(page)

    BasicMedia.objects.annotate_max_progress(media_page.object_list, media_type)

    # Use cached recommendations to prevent API spam and guest user crashes
    recs = get_cached_recommendations(request.user)
    context = {
        "media_type": media_type,
        "media_type_plural": app_tags.media_type_readable_plural(media_type).lower(),
        "media_list": media_page,
        "current_layout": layout,
        "layout_class": ".media-grid" if layout == "grid" else "tbody",
        "current_sort": sort_filter,
        "current_status": status_filter,
        "sort_choices": MediaSortChoices.choices,
        "status_choices": MediaStatusChoices.choices,
        "recommendations": recs.get("recommendations", []),
        "spotlight": recs.get("spotlight", {}),
    }

    if request.headers.get("HX-Request"):
        if request.headers.get("HX-Target") == "empty_list":
            if not media_page.object_list:
                return HttpResponse(status=204)
            response = HttpResponse()
            response["HX-Redirect"] = reverse("medialist", args=[media_type])
            return response
        template_name = "app/components/media_grid_items.html" if layout == "grid" else "app/components/media_table_items.html"
    else:
        template_name = "app/media_list.html"

    return render(request, template_name, context)


@require_GET
def media_search(request):
    """Return the media search page with guaranteed data shape."""
    media_type = request.GET.get("media_type", "movie")
    if request.user.is_authenticated:
        media_type = request.user.update_preference("last_search_type", media_type)
        
    query = request.GET.get("q", "")
    page = int(request.GET.get("page", 1))
    layout = request.GET.get("layout", "grid")
    source = request.GET.get("source", config.get_default_source_name(media_type).value)

    # 1. Get raw search data - BUG 3 FIX: Never crash, always return data
    try:
        data = services.search(media_type, query, page, source)
    except Exception:
        data = helpers.format_search_response(page, 20, 0, [])

    # 2. Normalize results into a consistent shape for templates.
    # Templates `media_card*.html` expect:
    # - `item` to be the media dict (with media_id/title/image/media_type/source)
    # - `media` to be the user's tracked DB object (or None)
    normalized_items = []
    for r in (data.get("results") or []):
        if not r or not isinstance(r, dict):
            continue
        media_dict = r.get("item") if isinstance(r.get("item"), dict) else r
        if not isinstance(media_dict, dict) or not media_dict.get("media_id"):
            continue
        media_dict["media_type"] = media_dict.get("media_type") or media_type
        media_dict["source"] = media_dict.get("source") or source
        normalized_items.append(media_dict)

    # Try to enrich with user tracking data; never break the page if it fails.
    try:
        wrapped_results = helpers.enrich_items_with_user_data(
            request,
            normalized_items,
            "search",
        ) if normalized_items else []
    except Exception:
        wrapped_results = []

    # If enrichment failed, still return wrappers with `media=None`
    if normalized_items and not wrapped_results:
        wrapped_results = [{"item": item, "media": None} for item in normalized_items]

    data["results"] = wrapped_results




    context = {
        "data": data,
        "source": source,
        "media_type": media_type,
        "layout": layout,
        "recommendations": get_cached_recommendations(request.user).get("recommendations", []),
        "spotlight": {},
    }
    return render(request, "app/search.html", context)

@require_GET
def media_details(request, source, media_type, media_id, title): 
    
    """Return the details page for a media item."""
    media_metadata = services.get_media_metadata(media_type, media_id, source)

    if request.user.is_authenticated:
        user_medias = BasicMedia.objects.filter_media_prefetch(
            request.user,
            media_id,
            media_type,
            source,
        )
        current_instance = user_medias[0] if user_medias else None
    else:
        user_medias = []
        current_instance = None

    # Enrich related items with user tracking data
    if media_metadata.get("related"):
        for section_name, related_items in media_metadata["related"].items():
            if related_items:
                media_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request, related_items, section_name
                    )
                )

    # Fix: Guests don't have a watch_provider_region attribute
    region = getattr(request.user, "watch_provider_region", "") if request.user.is_authenticated else ""

    if media_type in ["tv", "movie"]:
        watch_providers = tmdb.filter_providers(
            media_metadata.get("providers"), region
        )
    else:
        watch_providers = None

    context = {
        "media": media_metadata,
        "media_type": media_type,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "watch_providers": watch_providers,
        "watch_provider_region": region,
    }
    return render(request, "app/media_details.html", context)


@require_GET
def season_details(request, source, media_id, title, season_number):  # noqa: ARG001 For URL
    """Return the details page for a season."""
    tv_with_seasons_metadata = services.get_media_metadata(
        "tv_with_seasons",
        media_id,
        source,
        [season_number],
    )
    season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

    if request.user.is_authenticated:
        user_medias = BasicMedia.objects.filter_media_prefetch(
            request.user,
            media_id,
            MediaTypes.SEASON.value,
            source,
            season_number=season_number,
        )
        current_instance = user_medias[0] if user_medias else None
        episodes_in_db = current_instance.episodes.all() if current_instance else []
    else:
        user_medias = []
        current_instance = None
        episodes_in_db = []

    if source == Sources.MANUAL.value:
        season_metadata["episodes"] = manual.process_episodes(
            season_metadata,
            episodes_in_db,
        )
    else:
        season_metadata["episodes"] = tmdb.process_episodes(
            season_metadata,
            episodes_in_db,
        )

    # Enrich related items with user tracking data
    if season_metadata.get("related"):
        for section_name, related_items in season_metadata["related"].items():
            if related_items:
                season_metadata["related"][section_name] = (
                    helpers.enrich_items_with_user_data(
                        request,
                        related_items,
                        section_name,
                    )
                )

    # Fix: Guests don't have a watch_provider_region attribute
    region = getattr(request.user, "watch_provider_region", "") if request.user.is_authenticated else ""

    context = {
        "media": season_metadata,
        "tv": tv_with_seasons_metadata,
        "media_type": MediaTypes.SEASON.value,
        "user_medias": user_medias,
        "current_instance": current_instance,
        "watch_providers": tmdb.filter_providers(
            season_metadata.get("providers"), region
        ),
        "watch_provider_region": region,
    }
    return render(request, "app/media_details.html", context)


@login_required
@require_POST
def update_media_score(request, media_type, instance_id):
    """Update the user's score for a media item."""
    media = BasicMedia.objects.get_media(
        request.user,
        media_type,
        instance_id,
    )

    try:
        score = float(request.POST.get("score"))
        media.score = score
        media.save()
        logger.info("%s score updated to %s", media, score)
    except (ValueError, TypeError):
        return JsonResponse(
            {"success": False, "error": "Invalid score format provided."},
            status=400,
        )

    return JsonResponse(
        {
            "success": True,
            "score": score,
        },
    )


@login_required
@require_POST
def sync_metadata(request, source, media_type, media_id, season_number=None):
    """Refresh the metadata for a media item."""
    if source == Sources.MANUAL.value:
        msg = "Manual items cannot be synced."
        messages.error(request, msg)
        return HttpResponse(
            msg,
            status=400,
            headers={"HX-Redirect": request.POST.get("next", "/")},
        )

    cache_key = f"{source}_{media_type}_{media_id}"
    if media_type == MediaTypes.SEASON.value:
        cache_key += f"_{season_number}"

    ttl = cache.ttl(cache_key)
    logger.debug("%s - Cache TTL for: %s", cache_key, ttl)

    if ttl is not None and ttl > (settings.CACHE_TIMEOUT - 3):
        msg = "The data was recently synced, please wait a few seconds."
        messages.error(request, msg)
        logger.error(msg)
    else:
        deleted = cache.delete(cache_key)
        logger.debug("%s - Old cache deleted: %s", cache_key, deleted)

        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.update_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        title = metadata["title"]
        if season_number:
            title += f" - Season {season_number}"

        if media_type == MediaTypes.SEASON.value:
            metadata["episodes"] = tmdb.process_episodes(
                metadata,
                [],
            )

            # Create a dictionary of existing episodes keyed by episode number
            existing_episodes = {
                ep.episode_number: ep
                for ep in Item.objects.filter(
                    source=source,
                    media_type=MediaTypes.EPISODE.value,
                    media_id=media_id,
                    season_number=season_number,
                )
            }

            episodes_to_update = []
            episode_count = 0

            for episode_data in metadata["episodes"]:
                episode_number = episode_data["episode_number"]
                if episode_number in existing_episodes:
                    episode_item = existing_episodes[episode_number]
                    episode_item.title = metadata["title"]
                    episode_item.image = episode_data["image"]
                    episodes_to_update.append(episode_item)
                    episode_count += 1

            logger.info(
                "Found %s existing episodes to update for %s",
                episode_count,
                title,
            )

            if episodes_to_update:
                updated_count = Item.objects.bulk_update(
                    episodes_to_update,
                    ["title", "image"],
                    batch_size=100,
                )
                logger.info(
                    "Successfully updated %s episodes for %s",
                    updated_count,
                    title,
                )

        item.fetch_releases(delay=False)

        msg = f"{title} was synced to {Sources(source).label} successfully."
        messages.success(request, msg)

    if request.headers.get("HX-Request"):
        return HttpResponse(
            status=204,
            headers={
                "HX-Redirect": request.POST["next"],
            },
        )
    return helpers.redirect_back(request)


@login_required
@require_GET
def track_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
):
    """Return the tracking form for a media item."""
    instance_id = request.GET.get("instance_id")
    if instance_id:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    elif request.GET.get("is_create"):
        media = None
    else:
        # no specific instance, try to find the first one
        user_medias = BasicMedia.objects.filter_media(
            request.user,
            media_id,
            media_type,
            source,
            season_number=season_number,
        )
        media = user_medias.first()
        if media:
            instance_id = media.id

    initial_data = {
        "media_id": media_id,
        "source": source,
        "media_type": media_type,
        "season_number": season_number,
        "instance_id": instance_id,
    }

    if media:
        title = media.item
        if media_type == MediaTypes.GAME.value:
            initial_data["progress"] = helpers.minutes_to_hhmm(media.progress)
    else:
        title = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )["title"]
        if media_type == MediaTypes.SEASON.value:
            title += f" S{season_number}"

    form = get_form_class(media_type)(instance=media, initial=initial_data)

    return render(
        request,
        "app/components/fill_track.html",
        {
            "title": title,
            "form": form,
            "media": media,
            "return_url": request.GET["return_url"],
        },
    )


@login_required
@require_POST
def media_save(request):
    """Save or update media data to the database."""
    media_id = request.POST["media_id"]
    source = request.POST["source"]
    media_type = request.POST["media_type"]
    season_number = request.POST.get("season_number")
    instance_id = request.POST.get("instance_id")

    if instance_id:
        instance = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
    else:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
        )
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            defaults={
                "title": metadata["title"],
                "image": metadata["image"],
            },
        )
        model = apps.get_model(app_label="app", model_name=media_type)
        instance = model(item=item, user=request.user)

    # Validate the form and save the instance if it's valid
    form_class = get_form_class(media_type)
    form = form_class(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        logger.info("%s saved successfully.", form.instance)
    else:
        logger.error(form.errors.as_json())
        for field, errors in form.errors.items():
            for error in errors:
                messages.error(
                    request,
                    f"{field.replace('_', ' ').title()}: {error}",
                )

    return helpers.redirect_back(request)


@login_required
@require_POST
def media_delete(request):
    """Delete media data from the database."""
    instance_id = request.POST["instance_id"]
    media_type = request.POST["media_type"]
    model = apps.get_model(app_label="app", model_name=media_type)

    try:
        media = BasicMedia.objects.get_media(
            request.user,
            media_type,
            instance_id,
        )
        media.delete()
        logger.info("%s deleted successfully.", media)

    except model.DoesNotExist:
        logger.warning("The %s was already deleted before.", media_type)

    return helpers.redirect_back(request)


@login_required
@require_POST
def episode_save(request):
    """Handle the creation, deletion, and updating of episodes for a season."""
    media_id = request.POST["media_id"]
    season_number = int(request.POST["season_number"])
    episode_number = int(request.POST["episode_number"])
    source = request.POST["source"]

    form = EpisodeForm(request.POST)
    if not form.is_valid():
        logger.error("Form validation failed: %s", form.errors)
        return HttpResponseBadRequest("Invalid form data")

    try:
        related_season = Season.objects.get(
            item__media_id=media_id,
            item__source=source,
            item__season_number=season_number,
            item__episode_number=None,
            user=request.user,
        )
    except Season.DoesNotExist:
        tv_with_seasons_metadata = services.get_media_metadata(
            "tv_with_seasons",
            media_id,
            source,
            [season_number],
        )
        season_metadata = tv_with_seasons_metadata[f"season/{season_number}"]

        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_with_seasons_metadata["title"],
                "image": season_metadata["image"],
            },
        )
        related_season = Season.objects.create(
            item=item,
            user=request.user,
            score=None,
            status=Status.IN_PROGRESS.value,
            notes="",
        )

        logger.info("%s did not exist, it was created successfully.", related_season)

    related_season.watch(episode_number, form.cleaned_data["end_date"])

    return helpers.redirect_back(request)


@login_required
@require_http_methods(["GET", "POST"])
def create_entry(request):
    """Return the form for manually adding media items."""
    if request.method == "GET":
        media_types = MediaTypes.values
        return render(request, "app/create_entry.html", {"media_types": media_types})

    # Process the form submission
    form = ManualItemForm(request.POST, user=request.user)
    if not form.is_valid():
        # Handle form validation errors
        logger.error(form.errors.as_json())
        helpers.form_error_messages(form, request)
        return redirect("create_entry")

    # Try to save the item
    try:
        item = form.save()
    except IntegrityError:
        # Handle duplicate item
        media_name = form.cleaned_data["title"]
        if form.cleaned_data.get("season_number"):
            media_name += f" - Season {form.cleaned_data['season_number']}"
        if form.cleaned_data.get("episode_number"):
            media_name += f" - Episode {form.cleaned_data['episode_number']}"

        logger.exception("%s already exists in the database.", media_name)
        messages.error(request, f"{media_name} already exists in the database.")
        return redirect("create_entry")

    # Prepare and validate the media form
    updated_request = request.POST.copy()
    updated_request.update({"source": item.source, "media_id": item.media_id})
    media_form = get_form_class(item.media_type)(updated_request)

    if not media_form.is_valid():
        # Handle media form validation errors
        logger.error(media_form.errors.as_json())
        helpers.form_error_messages(media_form, request)

        # Delete the item since the media creation failed
        item.delete()
        logger.info("%s was deleted due to media form validation failure", item)
        return redirect("create_entry")

    # Save the media instance
    media_form.instance.user = request.user
    media_form.instance.item = item

    # Handle relationships based on media type
    if item.media_type == MediaTypes.SEASON.value:
        media_form.instance.related_tv = form.cleaned_data["parent_tv"]
    elif item.media_type == MediaTypes.EPISODE.value:
        media_form.instance.related_season = form.cleaned_data["parent_season"]

    media_form.save()

    # Success message
    msg = f"{item} added successfully."
    messages.success(request, msg)
    logger.info(msg)

    return redirect("create_entry")


@login_required
@require_GET
def search_parent_tv(request):
    """Return the search results for parent TV shows."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for TV shows with query: %s",
        request.user.username,
        query,
    )

    parent_tvs = TV.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.TV.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_tv.html",
        {"results": parent_tvs, "query": query},
    )


@login_required
@require_GET
def search_parent_season(request):
    """Return the search results for parent seasons."""
    query = request.GET.get("q", "").strip()

    if len(query) <= 1:
        return render(request, "app/components/search_parent_tv.html")

    logger.debug(
        "%s - Searching for seasons with query: %s",
        request.user.username,
        query,
    )

    parent_seasons = Season.objects.filter(
        user=request.user,
        item__source=Sources.MANUAL.value,
        item__media_type=MediaTypes.SEASON.value,
        item__title__icontains=query,
    )[:5]

    return render(
        request,
        "app/components/search_parent_season.html",
        {"results": parent_seasons, "query": query},
    )


@login_required
@require_GET
def history_modal(
    request,
    source,
    media_type,
    media_id,
    season_number=None,
    episode_number=None,
):
    """Return the history page for a media item."""
    user_medias = BasicMedia.objects.filter_media(
        request.user,
        media_id,
        media_type,
        source,
        season_number=season_number,
        episode_number=episode_number,
    )

    total_medias = user_medias.count()
    timeline_entries = []
    for index, media in enumerate(user_medias, start=1):
        if history := media.history.all():
            media_entry_number = total_medias - index + 1
            timeline_entries.extend(
                history_processor.process_history_entries(
                    history,
                    media_type,
                    media_entry_number,
                    request.user,
                ),
            )
    return render(
        request,
        "app/components/fill_history.html",
        {
            "media_type": media_type,
            "timeline": timeline_entries,
            "total_medias": total_medias,
            "return_url": request.GET["return_url"],
        },
    )


@login_required
@require_http_methods(["DELETE"])
def delete_history_record(request, media_type, history_id):
    """Delete a specific history record."""
    try:
        historical_model = apps.get_model(
            app_label="app",
            model_name=f"historical{media_type.lower()}",
        )

        historical_model.objects.get(
            history_id=history_id,
            history_user=request.user,
        ).delete()

        logger.info(
            "Deleted history record %s",
            str(history_id),
        )

        # Return empty 200 response - the element will be removed by HTMX
        return HttpResponse()

    except historical_model.DoesNotExist:
        logger.exception(
            "History record %s not found for user %s",
            str(history_id),
            str(request.user),
        )
        return HttpResponse("Record not found", status=404)


@login_required
@require_GET
def statistics(request):
    """Return the statistics page."""
    # Set default date range to last year
    timeformat = "%Y-%m-%d"
    today = timezone.localdate()
    one_year_ago = today.replace(year=today.year - 1)

    # Get date parameters with defaults
    start_date_str = request.GET.get("start-date") or one_year_ago.strftime(timeformat)
    end_date_str = request.GET.get("end-date") or today.strftime(timeformat)

    if start_date_str == "all" and end_date_str == "all":
        start_date = None
        end_date = None
    else:
        start_date = parse_date(start_date_str)
        end_date = parse_date(end_date_str)

        if start_date and end_date:
            # Convert to datetime with timezone awareness
            start_date = timezone.make_aware(
                datetime.combine(start_date, datetime.min.time()),
            )

            # End date should be end of day
            end_date = timezone.make_aware(
                datetime.combine(end_date, datetime.max.time()),
            )

    # Get all user media data in a single operation
    user_media, media_count = stats.get_user_media(
        request.user,
        start_date,
        end_date,
    )

    # Calculate all statistics from the retrieved data
    media_type_distribution = stats.get_media_type_distribution(
        media_count,
    )
    score_distribution, top_rated = stats.get_score_distribution(user_media)
    status_distribution = stats.get_status_distribution(user_media)
    status_pie_chart_data = stats.get_status_pie_chart_data(
        status_distribution,
    )
    timeline = stats.get_timeline(user_media)

    activity_data = stats.get_activity_data(request.user, start_date, end_date)

    context = {
        "start_date": start_date,
        "end_date": end_date,
        "media_count": media_count,
        "activity_data": activity_data,
        "media_type_distribution": media_type_distribution,
        "score_distribution": score_distribution,
        "top_rated": top_rated,
        "status_distribution": status_distribution,
        "status_pie_chart_data": status_pie_chart_data,
        "timeline": timeline,
    }

    return render(request, "app/statistics.html", context)


@require_GET
def get_media_links(request, category, item_id):
    """Get streaming/purchase links for a media item."""
    title = request.GET.get('title', '')
    isbn = request.GET.get('isbn', '')

    try:
        result = streaming_services.get_media_links(category, item_id, title=title, isbn=isbn)
        return render(request, "app/components/media_watch_modal.html", {
            "result": result,
            "title": title,
            "category": category
        })
    except Exception as e:
        logger.error("Error getting media links: %s", e)
        fallback = {
            "providers": streaming_services.get_smart_search_links(title or item_id, category),
            "has_direct_links": False
        }
        return render(request, "app/components/media_watch_modal.html", {
            "result": fallback,
            "title": title,
            "category": category
        })


@require_GET
def service_worker(request):
    """Serve the service worker file."""
    sw_path = Path(settings.STATICFILES_DIRS[0]) / "js" / "serviceworker.js"
    with sw_path.open() as f:
        response = HttpResponse(f.read(), content_type="application/javascript")
        response["Service-Worker-Allowed"] = "/"
        return response


@require_POST
def chatbot_message(request):
    """Handle HTMX requests from the AI Chatbot."""
    user_message = request.POST.get("message", "").strip()
    current_path = request.POST.get("current_path", "")

    import requests
    import json
    import os
    from app.providers import services
    from django.db.models import Count

    def build_page_context(path):
        page_context = [f"The user is currently on this page: {path}."]
        if path.startswith("/details/"):
            try:
                parts = path.strip("/").split("/")
                if len(parts) >= 4:
                    source, media_type, media_id = parts[1], parts[2], parts[3]
                    metadata = services.get_media_metadata(media_type, media_id, source)
                    title = metadata.get("title", "Unknown")
                    description = metadata.get("description", "No description available.")
                    cast = [p.get("name") for p in metadata.get("credits", {}).get("cast", [])[:5]]
                    cast_str = ", ".join(cast) if cast else "Unknown"
                    page_context.append(
                        f"The current page is about the {media_type} '{title}'. Synopsis: {description}. Main Cast: {cast_str}."
                    )
            except Exception as e:
                logger.error(f"AI Context Error: {e}")
                page_context.append("Unable to fetch the page metadata for the current page.")
        return " ".join(page_context)

    def build_user_library_tool_summary(user):
        if not user.is_authenticated:
            return "The user is not authenticated. Personal Yamtrack library details are unavailable."

        media_qs = BasicMedia.objects.filter(user=user.id).select_related("item")
        total_items = media_qs.count()

        status_counts = {status: 0 for status in Status.values}
        for row in media_qs.values("status").annotate(count=Count("id")):
            status_counts[row["status"]] = row["count"]

        media_type_counts = {}
        for row in media_qs.values("item__media_type").annotate(count=Count("id")):
            media_type_counts[row["item__media_type"]] = row["count"]

        recently_added = list(media_qs.order_by("-created_at")[:5])
        recently_completed = list(media_qs.filter(status=Status.COMPLETED.value).order_by("-end_date")[:5])
        in_progress_items = list(media_qs.filter(status=Status.IN_PROGRESS.value).order_by("-created_at")[:5])
        custom_lists = CustomList.objects.get_user_lists(user)

        list_details = [f"{custom_list.name} ({custom_list.items.count()} items)" for custom_list in custom_lists]

        lines = [
            "Yamtrack library summary for the authenticated user:",
            f"Total tracked items: {total_items}.",
        ]

        if total_items:
            status_summary = ", ".join(
                f"{Status(status).label if status in Status.values else status}: {count}"
                for status, count in status_counts.items()
                if count
            )
            if status_summary:
                lines.append(f"Status counts: {status_summary}.")

        if media_type_counts:
            media_type_summary = ", ".join(
                f"{MediaTypes(mt).label if mt in MediaTypes.values else mt}: {count}"
                for mt, count in media_type_counts.items()
            )
            lines.append(f"Tracked media by type: {media_type_summary}.")

        if list_details:
            lines.append("Custom lists: " + "; ".join(list_details) + ".")

        if in_progress_items:
            lines.append(
                "Current in-progress items: "
                + ", ".join(item.item.title for item in in_progress_items[:3])
                + "."
            )

        if recently_added:
            lines.append(
                "Recently added: "
                + ", ".join(item.item.title for item in recently_added[:3])
                + "."
            )

        if recently_completed:
            lines.append(
                "Recently completed: "
                + ", ".join(item.item.title for item in recently_completed[:3])
                + "."
            )

        lines.append(
            "Use this library data when the user asks about their own tracked items, watchlist, recent activity, or custom lists."
        )
        return "\n".join(lines)

    # 1. Base System Prompt (Conversational but STRICT on facts - ANTI-HALLUCINATION FOCUSED)
    system_prompt = (
        "You are 'Aura AI', a friendly and knowledgeable assistant built into the AuraTrack app. "
        "CRITICAL ANTI-HALLUCINATION RULES:\n"
        "1. Whenever a user asks ANY question about a specific movie, TV show, anime, manga, game, book, comic, or board game, you MUST use the 'search_database' tool FIRST to fetch the facts.\n"
        "2. If a user asks about their own tracked items, custom lists, watchlist, or recent activity, you MUST use the 'query_user_library' tool.\n"
        "3. ALWAYS trigger the 'search_database' tool FIRST. Do not guess without searching!\n"
        "4. **IMPORTANT - ANSWER STYLE:**\n"
        "   - Give DIRECT, CONCISE answers to what the user asked\n"
        "   - DO NOT mention: confidence levels, data_confidence, fallback flags, probability, or uncertainty qualifiers\n"
        "   - DO NOT say: 'Based on search results', 'external sources', 'level of data confidence', etc.\n"
        "   - Just answer the question naturally and directly\n"
        "   - Example: User asks 'Who is the villain?' → Just say 'Sanjay Dutt is the villain.' NOT 'According to external sources with 0.4 confidence...'\n"
        "5. **DATA INTEGRITY - Internal Use Only:**\n"
        "   - Use data_confidence to internally verify you have good data\n"
        "   - If data_confidence is 'none' or 'minimal' and you have no reliable info → Just say: 'I don't have information about that.'\n"
        "   - If you have the data (confidence is 'complete' or 'partial' or 'external_data_used' is true) → Answer confidently without disclaimers\n"
        "6. **NEVER invent:** actor names, character names, plot details, release dates, or facts not in the tool result.\n"
        "7. Use only app-provided data for user-specific questions; do not invent tracked items, list names, or statuses.\n"
        "8. Be conversational and natural. Keep responses short and relevant.\n"
        "9. For general knowledge questions unrelated to specific media titles, answer without calling the tool."
    )

    context_info = build_page_context(current_path)
    if request.user.is_authenticated:
        context_info += "\n\n" + build_user_library_tool_summary(request.user)
    else:
        context_info += "\n\nThe user is not authenticated. Personal library context is unavailable."

    system_prompt += "\n\n" + context_info

    # 2. Retrieve chat history from session (keep last 10 messages to save tokens)
    chat_history = request.session.get("chat_history", [])
    chat_history.append({"role": "user", "content": user_message})
    chat_history = chat_history[-10:]

    # 3. Prepare the payload for the AI Provider
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            *chat_history
        ],
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search_database",
                    "description": "Search Yamtrack provider data for a specific media title, returning facts, release dates, cast, credits, directors, and synopsis.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The title or exact media name to search for."
                            },
                            "media_type": {
                                "type": "string",
                                "description": "The type of media: 'movie', 'tv', 'anime', 'manga', 'game', 'book', 'comic', or 'boardgame'."
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "query_user_library",
                    "description": "Fetch the authenticated user's Yamtrack library summary, including status counts, recent activity, and custom lists.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The user's personal library question, such as 'what is in my watchlist' or 'what did I complete this week'."
                            }
                        },
                        "required": ["query"]
                    }
                }
            }
        ]
    }

    # 4. Choose the AI Provider (Cloud vs Local)
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

    if GROQ_API_KEY:
        api_url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        payload["model"] = "llama-3.1-8b-instant"
    else:
        api_url = "http://host.docker.internal:11434/api/chat"
        headers = {"Content-Type": "application/json"}
        payload["model"] = "llama3.1"

    try:
        response = requests.post(api_url, json=payload, headers=headers, timeout=45)
        response.raise_for_status()
        response_data = response.json()

        if "choices" in response_data:
            message = response_data["choices"][0]["message"]
        else:
            message = response_data.get("message", {})

        if "tool_calls" in message:
            payload["messages"].append(message)
            for tool in message["tool_calls"]:
                args = tool["function"]["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}

                tool_name = tool["function"]["name"]
                tool_response_text = ""

                if tool_name == "search_database":
                    search_query = args.get("query", "").strip()
                    m_type = args.get("media_type", MediaTypes.MOVIE.value).strip().lower() if args.get("media_type") else MediaTypes.MOVIE.value

                    try:
                        allowed_types = [m.value for m in MediaTypes]
                        if m_type not in allowed_types:
                            m_type = MediaTypes.MOVIE.value

                        source = config.get_default_source_name(m_type).value
                        search_results = services.search(m_type, search_query, 1, source)
                        results_list = search_results.get("results", [])[:3]

                        structured_results = []
                        for res in results_list:
                            item = res.get("item") if isinstance(res, dict) and isinstance(res.get("item"), dict) else res
                            media_id = item.get("media_id") or item.get("id")
                            title = item.get("title") or item.get("name") or "Unknown"
                            synopsis = item.get("description") or item.get("overview") or item.get("synopsis") or ""
                            release_date = item.get("release_date") or item.get("first_air_date") or item.get("publish_date") or item.get("year") or ""

                            # Default empty structures
                            cast_list = []
                            directors = []

                            if media_id:
                                try:
                                    meta = services.get_media_metadata(m_type, media_id, source)
                                    credits = meta.get("credits", {})
                                    cast_raw = credits.get("cast", []) if isinstance(credits, dict) else []
                                    for idx, p in enumerate(cast_raw[:15], start=1):
                                        cast_list.append({
                                            "actor": p.get("name"),
                                            "character": p.get("character") or "",
                                            "billing": idx,
                                        })

                                    crew_raw = credits.get("crew", []) if isinstance(credits, dict) else []
                                    directors = [p.get("name") for p in crew_raw if p.get("job") == "Director"]

                                except Exception as e:
                                    logger.error(f"Metadata fetch error: {e}")

                            # Heuristic role inference
                            def infer_roles(cast_list, synopsis, title):
                                synopsis_lower = (synopsis or "").lower()
                                likely_protagonist = None
                                likely_antagonist = None

                                # Look for character names mentioned in synopsis
                                for c in cast_list:
                                    char = (c.get("character") or "").strip()
                                    actor = c.get("actor")
                                    if char and char.lower() in synopsis_lower:
                                        likely_protagonist = {
                                            "actor": actor,
                                            "character": char,
                                            "confidence": 0.9,
                                            "reason": f"Character name '{char}' appears in synopsis."
                                        }
                                        break

                                # If not found, choose top-billed as protagonist
                                if not likely_protagonist and cast_list:
                                    top = cast_list[0]
                                    likely_protagonist = {
                                        "actor": top.get("actor"),
                                        "character": top.get("character"),
                                        "confidence": 0.6,
                                        "reason": "Top-billed cast member; no explicit protagonist mention in synopsis."
                                    }

                                # Antagonist heuristic: look for villain keywords in character or synopsis
                                villain_keywords = ["villain", "antagonist", "enemy", "villainous", "archenemy"]
                                for c in cast_list:
                                    char = (c.get("character") or "").lower()
                                    actor = c.get("actor")
                                    if any(k in char for k in villain_keywords) or any(k in (synopsis or "").lower() for k in villain_keywords if k in (c.get("character") or "")):
                                        likely_antagonist = {
                                            "actor": actor,
                                            "character": c.get("character"),
                                            "confidence": 0.8,
                                            "reason": "Found villain/antagonist keyword in character or synopsis."
                                        }
                                        break

                                # Fallback to second billed
                                if not likely_antagonist and len(cast_list) > 1:
                                    second = cast_list[1]
                                    likely_antagonist = {
                                        "actor": second.get("actor"),
                                        "character": second.get("character"),
                                        "confidence": 0.4,
                                        "reason": "Second top-billed cast; no explicit antagonist found."
                                    }

                                # If only one cast member exists
                                if not likely_antagonist and cast_list:
                                    likely_antagonist = {
                                        "actor": None,
                                        "character": None,
                                        "confidence": 0.0,
                                        "reason": "No antagonist information available."
                                    }

                                return likely_protagonist, likely_antagonist

                            protagonist, antagonist = infer_roles(cast_list, synopsis, title)

                            # **NEW: ANTI-HALLUCINATION FALLBACK - Fetch from OMDb/Wikipedia if local DB incomplete**
                            original_data_source = source  # Track original source (TMDB, MAL, etc)
                            external_cast_found = False
                            
                            # If cast list is empty, try OMDb first, then Wikipedia
                            if not cast_list:
                                # Try OMDb API
                                omdb_data = fetch_from_omdb(title, release_date.split("-")[0] if release_date else None)
                                if omdb_data and omdb_data.get("cast"):
                                    cast_list = omdb_data.get("cast", [])
                                    if not directors and omdb_data.get("directors"):
                                        directors = omdb_data.get("directors", [])
                                    if not synopsis and omdb_data.get("synopsis"):
                                        synopsis = omdb_data.get("synopsis", "")
                                    external_cast_found = True
                                    source = "OMDb (Fallback)"
                                    logger.info(f"✓ Found cast data from OMDb for '{title}'")
                                
                                # If OMDb failed, try Wikipedia
                                if not external_cast_found:
                                    wiki_data = fetch_from_wikipedia(title)
                                    if wiki_data and wiki_data.get("synopsis"):
                                        if not synopsis:
                                            synopsis = wiki_data.get("synopsis", "")
                                        # Wikipedia doesn't provide cast in easy format, but we got synopsis
                                        external_cast_found = True
                                        source = "Wikipedia (Fallback)"
                                        logger.info(f"✓ Found synopsis from Wikipedia for '{title}'")
                            
                            # Determine if fallback to general knowledge is needed
                            # Fallback is true when cast data is empty or confidence is too low
                            needs_fallback = (
                                not cast_list or  # No cast data from any source
                                (protagonist and protagonist.get("confidence", 0) < 0.5) or  # Low protagonist confidence
                                (not protagonist and not antagonist)  # No roles found at all
                            )

                            # **NEW: Re-infer roles after fetching external data**
                            if external_cast_found:
                                protagonist, antagonist = infer_roles(cast_list, synopsis, title)

                            # **NEW: Calculate data_confidence level to PREVENT HALLUCINATION**
                            # This tells the AI what level of data certainty we have
                            if title and release_date and synopsis and cast_list:
                                data_confidence = "complete"  # Rich data available
                            elif title and release_date and (synopsis or cast_list):
                                data_confidence = "partial"  # Basic info but missing cast or synopsis
                            elif title:
                                data_confidence = "minimal"  # Only title, almost no metadata
                            else:
                                data_confidence = "minimal"  # No data at all

                            structured_results.append({
                                "title": title,
                                "release_date": release_date,
                                "synopsis": synopsis,
                                "directors": directors,
                                "cast": cast_list,
                                "likely_protagonist": protagonist,
                                "likely_antagonist": antagonist,
                                "source": source,
                                "data_confidence": data_confidence,
                                "external_data_used": external_cast_found,
                                "fallback": needs_fallback,
                                "fallback_reason": "Database has incomplete metadata for this title; you may supplement with general knowledge." if needs_fallback else None,
                            })

                        if structured_results:
                            tool_response_text = json.dumps({"results": structured_results}, ensure_ascii=False)
                        else:
                            tool_response_text = json.dumps({
                                "results": [], 
                                "data_confidence": "none",
                                "note": f"Movie/show '{search_query}' not found in the database. No reliable information available."
                            })
                    except Exception as e:
                        tool_response_text = json.dumps({"error": str(e)})
                elif tool_name == "query_user_library":
                    tool_query = args.get("query", "").strip()
                    # **NEW: Use smart query processor that actually answers user questions!**
                    tool_response_text = build_user_library_response(request.user, tool_query)
                else:
                    tool_response_text = f"Unknown tool requested: {tool_name}."

                tool_msg = {
                    "role": "tool",
                    "content": tool_response_text,
                }
                if "id" in tool:
                    tool_msg["tool_call_id"] = tool["id"]
                    tool_msg["name"] = tool_name

                payload["messages"].append(tool_msg)

            payload["tool_choice"] = "none"

            final_response = requests.post(api_url, json=payload, headers=headers, timeout=45)
            final_response.raise_for_status()
            final_data = final_response.json()

            if "choices" in final_data:
                bot_response = final_data["choices"][0]["message"].get("content")
            else:
                bot_response = final_data.get("message", {}).get("content")

            if not bot_response:
                bot_response = "I successfully retrieved the info, but had trouble formatting the final response."
        else:
            bot_response = message.get("content")
            if not bot_response:
                bot_response = "I couldn't generate a proper response. Please try asking again."

    except Exception as e:
        provider = "Groq Cloud" if GROQ_API_KEY else "Ollama Local"
        bot_response = f"AI Connection Error ({provider}): {str(e)}"

    if not bot_response.startswith("AI Connection Error"):
        chat_history.append({"role": "assistant", "content": bot_response})
        request.session["chat_history"] = chat_history

    context = {
        "user_message": user_message,
        "bot_response": bot_response,
    }
    return render(request, "app/components/chat_message_snippet.html", context)
