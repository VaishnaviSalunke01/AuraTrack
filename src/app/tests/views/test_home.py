from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from app.models import (
    Anime,
    Episode,
    Item,
    MediaTypes,
    Season,
    Sources,
    Status,
    BasicMedia,
)
from lists.models import CustomList, CustomListItem
from users.models import HomeSortChoices


class HomeViewTests(TestCase):
    """Test the home view."""

    def setUp(self):
        """Create a user and log in."""
        self.credentials = {"username": "test", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.client.login(**self.credentials)

        season_item = Item.objects.create(
            media_id="1668",
            source=Sources.TMDB.value,
            media_type=MediaTypes.SEASON.value,
            title="Test TV Show",
            image="http://example.com/image.jpg",
            season_number=1,
        )
        season = Season.objects.create(
            item=season_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
        )

        for i in range(1, 6):  # Create 5 episodes
            episode_item = Item.objects.create(
                media_id="1668",
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title="Test TV Show",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=i,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now() - timezone.timedelta(days=i),
            )

        anime_item = Item.objects.create(
            media_id="1",
            source=Sources.MAL.value,
            media_type=MediaTypes.ANIME.value,
            title="Test Anime",
            image="http://example.com/image.jpg",
        )
        Anime.objects.create(
            item=anime_item,
            user=self.user,
            status=Status.IN_PROGRESS.value,
            progress=10,
        )

        self.movie_item = Item.objects.create(
            media_id="2",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Recommended Movie",
            image="http://example.com/movie.jpg",
        )
        self.movie_media = BasicMedia.objects.create(
            item=self.movie_item,
            user=self.user,
            status=Status.COMPLETED.value,
        )

        self.tv_item = Item.objects.create(
            media_id="3",
            source=Sources.TMDB.value,
            media_type=MediaTypes.TV.value,
            title="Recommended TV Show",
            image="http://example.com/tv.jpg",
        )
        self.tv_media = BasicMedia.objects.create(
            item=self.tv_item,
            user=self.user,
            status=Status.PLANNING.value,
        )

        self.other_movie_item = Item.objects.create(
            media_id="4",
            source=Sources.TMDB.value,
            media_type=MediaTypes.MOVIE.value,
            title="Another Recommended Movie",
            image="http://example.com/another_movie.jpg",
        )

    def test_home_view(self):
        """Test the home view displays in-progress media."""
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/home.html")

        self.assertIn("list_by_type", response.context)
        self.assertIn(MediaTypes.SEASON.value, response.context["list_by_type"])
        self.assertIn(MediaTypes.ANIME.value, response.context["list_by_type"])

        self.assertIn("sort_choices", response.context)
        self.assertEqual(response.context["sort_choices"], HomeSortChoices.choices)

        season = response.context["list_by_type"][MediaTypes.SEASON.value]
        self.assertEqual(len(season["items"]), 1)
        self.assertEqual(season["items"][0].progress, 5)

    def test_home_view_with_sort(self):
        """Test the home view with sorting parameter."""
        response = self.client.get(reverse("home") + "?sort=completion")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_sort"], "completion")

        self.user.refresh_from_db()
        self.assertEqual(self.user.home_sort, "completion")

    @patch("app.providers.services.get_media_metadata")
    def test_home_view_htmx_load_more(self, mock_get_media_metadata):
        """Test the HTMX load more functionality."""
        mock_get_media_metadata.return_value = {
            "title": "Test TV Show",
            "image": "http://example.com/image.jpg",
            "season/1": {
                "episodes": [{"id": 1}, {"id": 2}, {"id": 3}],  # 3 episodes
            },
            "related": {
                "seasons": [
                    {"season_number": 1, "image": "http://example.com/image.jpg"},
                ],  # Only one season
            },
        }

        for i in range(6, 20):  # Create 14 more TV shows (we already have 1)
            season_item = Item.objects.create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.SEASON.value,
                title=f"Test TV Show {i}",
                image="http://example.com/image.jpg",
                season_number=1,
            )
            season = Season.objects.create(
                item=season_item,
                user=self.user,
                status=Status.IN_PROGRESS.value,
            )

            episode_item = Item.objects.create(
                media_id=str(i),
                source=Sources.TMDB.value,
                media_type=MediaTypes.EPISODE.value,
                title=f"Test TV Show {i}",
                image="http://example.com/image.jpg",
                season_number=1,
                episode_number=1,
            )
            Episode.objects.create(
                item=episode_item,
                related_season=season,
                end_date=timezone.now(),
            )

        # Now test the load more functionality
        response = self.client.get(
            reverse("home") + "?load_media_type=season", headers={"hx-request": "true"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "app/components/home_grid.html")

        self.assertIn("media_list", response.context)

        self.assertIn("items", response.context["media_list"])
        self.assertIn("total", response.context["media_list"])

        # Since we're loading more (items after the first 14),
        # we should have at least 1 item in the response
        self.assertEqual(len(response.context["media_list"]["items"]), 1)
        self.assertEqual(
            response.context["media_list"]["total"],
            15,
        )  # 15 TV shows total

    @patch("app.providers.services.get_smart_recommendations")
    def test_home_view_displays_recommendations_for_authenticated_user(self, mock_get_smart_recommendations):
        """Test that the home view displays recommendations for an authenticated user."""
        mock_get_smart_recommendations.return_value = {
            "recommendations": [
                {
                    'item': {
                        'title': "Recommended Movie",
                        'image': self.movie_item.image,
                        'media_id': self.movie_item.media_id,
                        'media_type': MediaTypes.MOVIE.value,
                        'source': Sources.TMDB.value,
                    },
                    'title': "Recommended Movie",
                },
                {
                    'item': {
                        'title': "Recommended TV Show",
                        'image': self.tv_item.image,
                        'media_id': self.tv_item.media_id,
                        'media_type': MediaTypes.TV.value,
                        'source': Sources.TMDB.value,
                    },
                    'title': "Recommended TV Show",
                },
            ],
            "spotlight": {},
        }

        response = self.client.get(reverse("home") + "?load_recommendations=true", headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Smart Recommendations")
        self.assertEqual(len(response.context["recommendations"]), 2)
        self.assertContains(response, "Recommended Movie")
        self.assertContains(response, "Recommended TV Show")


    @patch("app.providers.services.get_smart_recommendations")
    def test_home_view_no_recommendations_for_authenticated_user(self, mock_get_smart_recommendations):
        """Test that no recommendations are displayed if the service returns an empty list."""
        mock_get_smart_recommendations.return_value = {
            "recommendations": [],
            "spotlight": {},
        }

        response = self.client.get(reverse("home") + "?load_recommendations=true", headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        # Verify the context is indeed empty
        self.assertEqual(len(response.context["recommendations"]), 0)
        # Check if the header specifically is missing
        self.assertNotContains(response, "AI Smart Recommendations")

    def test_home_view_no_recommendations_for_anonymous_user(self):
        """Test that no recommendations are displayed for an anonymous user."""
        self.client.logout()
        response = self.client.get(reverse("home"))

        # The view should redirect unauthenticated users to the login page.
        # Check for the expected redirect status code (302).
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("account_login")))

    @patch("app.providers.services.get_smart_recommendations")
    @patch("app.helpers.enrich_items_with_user_data")
    def test_home_view_recommendation_enrichment_with_existing_media(self, mock_enrich, mock_get_smart_recommendations):
        """Test that recommendations are enriched with existing user media data."""
        mock_get_smart_recommendations.return_value = {
            "recommendations": [
                {
                    'item': {
                        'title': "Recommended Movie",
                        'image': self.movie_item.image,
                        'media_id': self.movie_item.media_id,
                        'media_type': MediaTypes.MOVIE.value,
                        'source': Sources.TMDB.value,
                    },
                    'title': "Recommended Movie",
                },
            ],
            "spotlight": {},
        }

        # Mock enrich_items_with_user_data to return a processed list where 'media' is not None
        mock_enrich.return_value = [
            {
                'item': mock_get_smart_recommendations.return_value["recommendations"][0]['item'],
                'media': self.movie_media, # This is the key part - linking to an existing BasicMedia
            },
        ]

        response = self.client.get(reverse("home") + "?load_recommendations=true", headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Smart Recommendations")
        self.assertEqual(len(response.context["recommendations"]), 1)
        self.assertIsNotNone(response.context["recommendations"][0]['media'])
        self.assertEqual(response.context["recommendations"][0]['media'].id, self.movie_media.id)
        mock_enrich.assert_called_once()

    @patch("app.providers.services.get_smart_recommendations")
    @patch("app.helpers.enrich_items_with_user_data")
    def test_home_view_recommendation_enrichment_without_existing_media(self, mock_enrich, mock_get_smart_recommendations):
        """Test that recommendations are handled when no existing user media data matches."""
        mock_get_smart_recommendations.return_value = {
            "recommendations": [
                {
                    'item': {
                        'title': "Another Recommended Movie",
                        'image': self.other_movie_item.image,
                        'media_id': self.other_movie_item.media_id,
                        'media_type': MediaTypes.MOVIE.value,
                        'source': Sources.TMDB.value,
                    },
                    'title': "Another Recommended Movie",
                },
            ],
            "spotlight": {},
        }

        # Mock enrich_items_with_user_data to return a processed list where 'media' is None
        mock_enrich.return_value = [
            {
                'item': mock_get_smart_recommendations.return_value["recommendations"][0]['item'],
                'media': None, # No matching BasicMedia found
            },
        ]

        response = self.client.get(reverse("home") + "?load_recommendations=true", headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Smart Recommendations")
        self.assertEqual(len(response.context["recommendations"]), 1)
        self.assertIsNone(response.context["recommendations"][0]['media'])
        mock_enrich.assert_called_once()

    @patch("app.providers.services.get_smart_recommendations")
    def test_home_view_recommendation_error_handling(self, mock_get_smart_recommendations):
        """Test that the home view handles errors gracefully when fetching recommendations."""
        mock_get_smart_recommendations.side_effect = Exception("Gemini API error")

        response = self.client.get(reverse("home") + "?load_recommendations=true", headers={"hx-request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "AI Smart Recommendations")
        self.assertEqual(len(response.context["recommendations"]), 0)
        mock_get_smart_recommendations.assert_called_once()
