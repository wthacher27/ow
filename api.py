import httpx

BASE_URL = "https://overfast-api.tekrop.fr"


async def search_players(client: httpx.AsyncClient, name: str) -> list[dict]:
    resp = await client.get(f"{BASE_URL}/players", params={"name": name})
    if resp.status_code == 200:
        return resp.json().get("results", [])
    return []


async def fetch_player_summary(client: httpx.AsyncClient, player_id: str) -> dict | None:
    resp = await client.get(f"{BASE_URL}/players/{player_id}/summary")
    if resp.status_code == 200:
        return resp.json()
    return None


async def fetch_stats_summary(client: httpx.AsyncClient, player_id: str) -> dict | None:
    resp = await client.get(f"{BASE_URL}/players/{player_id}/stats/summary")
    if resp.status_code == 200:
        return resp.json()
    return None


async def fetch_player_stats(client: httpx.AsyncClient, player_id: str, mode: str = "competitive") -> dict | None:
    resp = await client.get(
        f"{BASE_URL}/players/{player_id}/stats",
        params={"gamemode": mode},
    )
    if resp.status_code == 200:
        return resp.json()
    return None


def extract_ranks(summary: dict, platform: str = "pc") -> list[dict]:
    ranks = []
    competitive = summary.get("competitive") or {}
    platform_data = competitive.get(platform) or {}

    for role_key in ("tank", "damage", "support"):
        role_data = platform_data.get(role_key)
        if role_data:
            division = role_data.get("division")
            tier = role_data.get("tier")
            rank_text = f"{division} {tier}" if division and tier else None
            ranks.append({
                "role": role_key,
                "division": division,
                "tier": tier,
                "rank_text": rank_text,
            })
    return ranks


def _get_stat(categories: list[dict], category: str, key: str, default=0):
    for cat in categories:
        if cat.get("category") == category:
            for stat in cat.get("stats", []):
                if stat.get("key") == key:
                    return stat.get("value", default)
    return default


def extract_hero_stats(stats_data: dict) -> list[dict]:
    heroes = []
    for hero_name, categories in stats_data.items():
        if hero_name == "all-heroes" or not isinstance(categories, list):
            continue

        time_played = _get_stat(categories, "game", "time_played", 0)
        games_won = _get_stat(categories, "game", "games_won", 0)
        games_played = _get_stat(categories, "game", "games_played", 0)
        eliminations = _get_stat(categories, "combat", "eliminations", 0)
        deaths = _get_stat(categories, "combat", "deaths", 0)
        damage = _get_stat(categories, "combat", "all_damage_done", 0) or _get_stat(categories, "combat", "hero_damage_done", 0)
        healing = _get_stat(categories, "assists", "healing_done", 0)
        accuracy = _get_stat(categories, "combat", "weapon_accuracy", None)
        # prefer scoped accuracy for heroes that have it (Ana, Widow, etc.)
        scoped = _get_stat(categories, "hero_specific", "scoped_accuracy", None)
        if scoped is not None:
            accuracy = scoped

        extra = {}
        for cat in categories:
            cat_name = cat.get("category", "")
            extra[cat_name] = {s["key"]: s["value"] for s in cat.get("stats", [])}

        heroes.append({
            "hero": hero_name,
            "time_played_seconds": time_played,
            "games_won": games_won,
            "games_played": games_played,
            "eliminations": eliminations,
            "deaths": deaths,
            "damage": damage,
            "healing": healing,
            "accuracy": accuracy,
            "extra": extra,
        })
    return heroes
