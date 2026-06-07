# AuraTrack



AuraTrack is a self hosted media tracker for movies, tv shows, anime, manga, video games, books, comics, and board games.


![App Tests](https://github.com/VaishnaviSalunke01/AuraTrack/actions/workflows/app-tests.yml/badge.svg)
![Docker Image](https://github.com/VaishnaviSalunke01/AuraTrack/actions/workflows/docker-image.yml/badge.svg)
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)

## 🚀 Demo

You can try the app at [auratrack.fuzzygrim.com](https://auratrack.fuzzygrim.com) using the username `demo` and password `demo`.

## ✨ Features

- 🎬 Track movies, tv shows, anime, manga, games, books, comics, and board games.
- 📺 Track each season of a tv show individually and episodes watched.
- ⭐ Save score, status, progress, repeats (rewatches, rereads...), start and end dates, or write a note.
- 📈 Keep a tracking history with each action with a media, such as when you added it, when you started it, when you started watching it again, etc.
- ✏️ Create custom media entries, for niche media that cannot be found by the supported APIs.
- 📂 Create personal lists to organize your media for any purpose, add other members to collaborate on your lists.
- 📅 Keep up with your upcoming media with a calendar, which can be subscribed to in external applications using a iCalendar (.ics) URL.
- 🔔 Receive notifications of upcoming releases via Apprise (supports Discord, Telegram, ntfy, Slack, email, and many more).
- 🐳 Easy deployment with Docker via docker-compose with SQLite or PostgreSQL.
- 👥 Multi-users functionality allowing individual accounts with personalized tracking.
- 🔑 Flexible authentication options including OIDC and 100+ social providers (Google, GitHub, Discord, etc.) via django-allauth.
- 🦀 Integration with [Jellyfin](https://jellyfin.org/), [Plex](https://plex.tv/) and [Emby](https://emby.media/) to automatically track new media watched.
- 📥 Import from [Trakt](https://trakt.tv/), [Simkl](https://simkl.com/), [MyAnimeList](https://myanimelist.net/), [AniList](https://anilist.co/) and [Kitsu]   (https://kitsu.app/) with support for periodic automatic imports.
- 📊 Export all your tracked media to a CSV file and import it back.
  
- 🤖 Personalized AI recommendations — Home and media listing pages surface tailored "AI Picks" generated from your recent activity, tracked statuses, and custom       lists, hydrated with real metadata via Auratrack's provider APIs.
- 🔄 Refreshable spotlight picks — Regenerate recommendations on the fly (powered by HTMX) and get a highlighted "AI Pick" spotlight item when one stands out.
- 💬 In-app AI chat assistant — An intelligent chatbot answers both general questions and app-specific queries using Groq (cloud) or Ollama (local), depending on        your configuration.
- 📚 Library-aware chat — Ask about your watchlist, completed titles, progress, or custom lists and get answers drawn directly from your Yamtrack library data.
- 🔍 Fact-grounded media Q&A — For questions about cast, plot, or release details, the assistant searches the database before responding — no hallucinated facts,        only verified information.




| -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |


Screenshots:

<img width="1700" height="900" alt="image" src="https://github.com/user-attachments/assets/4c0c7aec-40db-42da-b9c6-25a340cbc0d9" />
<img width="1918" height="913" alt="image" src="https://github.com/user-attachments/assets/073ddb09-6d54-417d-bb96-001621956dc0" />
<img width="1919" height="908" alt="image" src="https://github.com/user-attachments/assets/1c8d1c44-22bd-405a-9296-853706ae90cc" />
<img width="1913" height="909" alt="image" src="https://github.com/user-attachments/assets/30e76cbc-7f86-471e-b3bd-d2a73db03c4e" />
<img width="1915" height="907" alt="image" src="https://github.com/user-attachments/assets/dfb62d67-68a9-4a32-83b2-8973dc89e00f" />





## 🐳 Installing with Docker

Copy the default `docker-compose.yml` file from the repository and set the environment variables. This would use a SQlite database, which is enough for most use cases.

To start the containers run:

```bash
docker-compose up -d
```

Alternatively, if you need a PostgreSQL database, you can use the `docker-compose.postgres.yml` file.

### 🌊 Reverse Proxy Setup

When using a reverse proxy, if you see a `403 - Forbidden` error, you need to set the `URLS` environment variable to the URL you are using for the app.

```bash
services:
  yamtrack:
    ...
    environment:
      - URLS=https://auratrack.mydomain.com
    ...
```

Note that the setting must include the correct protocol (`https` or `http`), and must not include the application `/` context path. Multiple origins can be specified by separating them with a comma (`,`).

### ⚙️ Environment variables

For detailed information on environment variables, please refer to the [Environment Variables wiki page](https://github.com/FuzzyGrim/AuraTrack/wiki/Environment-Variables).

## 💻 Local development

Clone the repository and change directory to it.

```bash
git clone https://github.com/FuzzyGrim/AuraTrack.git
cd AuraTrack
```

Install Redis or spin up a bare redis container:

```bash
docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:8-alpine
```

Create a `.env` file in the root directory and add the following variables.

```bash
TMDB_API=API_KEY
MAL_API=API_KEY
IGDB_ID=IGDB_ID
IGDB_SECRET=IGDB_SECRET
STEAM_API_KEY=STEAM_API_SECRET
BGG_API_TOKEN=BGG_API_TOKEN
SECRET=SECRET
DEBUG=True
```

Then run the following commands.

```bash
python -m pip install -U -r requirements-dev.txt
pre-commit install
cd src
python manage.py migrate
python manage.py runserver & celery -A config worker --beat --scheduler django --loglevel DEBUG & tailwindcss -i ./static/css/input.css -o ./static/css/tailwind.css --watch
```

Go to: http://localhost:8000

## 💪 Support the Project

There are many ways to make AuraTrack better:

- ⭐ Star the repository on GitHub to increase visibility.
- 🐛 Report bugs by opening an [issue](https://github.com/VaishnaviSalunke01/AuraTrack/issues).
- 💡 Suggest new features through [GitHub issues](https://github.com/VaishnaviSalunke01/AuraTrack/issues).
- 🧪 Send pull requests for documentation, bug fixes, or new features.

