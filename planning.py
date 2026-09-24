"""Game availability and Bangkok reporting periods; no network calls."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

GAMES = (
    "Any game",
    "League of Legends",
    "Valorant",
    "Teamfight Tactics",
    "Minecraft",
    "Counter-Strike 2",
    "Dota 2",
    "Other / decide together",
)
BANGKOK = ZoneInfo("Asia/Bangkok")


def minutes(value):
    try:
        parsed = time.fromisoformat(str(value))
        if len(str(value)) != 5:
            raise ValueError()
        return parsed.hour * 60 + parsed.minute
    except (TypeError, ValueError):
        raise ValueError("Use a time in HH:MM format.") from None


def validate_plan(plan, start):
    games = plan.get("games", [])
    if (
        not isinstance(games, list)
        or not 1 <= len(games) <= 4
        or any(g not in GAMES for g in games)
    ):
        raise ValueError("Choose one to four games.")
    if "Any game" in games and len(games) > 1:
        raise ValueError("Choose Any game on its own, or select specific games.")
    end = str(plan.get("until", "23:59"))
    beginning = 18 * 60 if start == "Flexible" else minutes(start)
    if not 18 * 60 <= beginning < minutes(end) <= 1439:
        raise ValueError(
            "Available until must be later than your start, on the same evening."
        )
    if type(plan.get("reminder", False)) is not bool:
        raise ValueError("Choose whether to receive a reminder.")
    return {
        "games": list(dict.fromkeys(games)),
        "from": start,
        "until": end,
        "reminder": plan.get("reminder", False),
    }


def suggest(responses):
    """Best game + half-hour start with >=30 minutes overlap, minimum two people.

    Legacy Yes votes without a saved availability window do not imply availability.
    Flexible means 18:00 onwards. Ties prefer the earliest time, then game name.
    """
    candidates = []
    for response in responses:
        if response.get("choice") != "yes" or not response.get("plan"):
            continue
        try:
            plan = validate_plan(response["plan"], response.get("play_time"))
            candidates.append((response, plan))
        except ValueError:
            continue
    games = sorted(
        {g for _, p in candidates for g in p["games"] if g != "Any game"}
    ) or ["Any game"]
    best = None
    for start in range(18 * 60, 23 * 60 + 1, 30):
        for game in games:
            people = [
                r
                for r, p in candidates
                if (game in p["games"] or "Any game" in p["games"])
                and (18 * 60 if p["from"] == "Flexible" else minutes(p["from"]))
                <= start
                and minutes(p["until"]) >= start + 30
            ]
            if len(people) >= 2 and (best is None or len(people) > best["count"]):
                best = {
                    "game": game,
                    "time": f"{start // 60:02}:{start % 60:02}",
                    "count": len(people),
                    "user_ids": [str(p["user_id"]) for p in people],
                }
    return best


def report_period(query, now=None):
    now = (now or datetime.now(BANGKOK)).astimezone(BANGKOK)
    today = now.date()
    preset = query.get("period", "30days")
    if preset == "today":
        first, last = today, today
    elif preset == "week":
        first, last = today - timedelta(days=today.weekday()), today
    elif preset == "month":
        first, last = today.replace(day=1), today
    elif preset == "year":
        first, last = today.replace(month=1, day=1), today
    elif preset == "30days":
        first, last = today - timedelta(days=29), today
    elif preset == "custom":
        try:
            first, last = (
                date.fromisoformat(query.get("from", "")),
                date.fromisoformat(query.get("to", "")),
            )
        except ValueError:
            raise ValueError("Choose a valid start and end date.") from None
    else:
        raise ValueError("Unknown date range.")
    if first > last or last > today or (last - first).days > 366:
        raise ValueError("Choose up to 367 days, ending no later than today.")
    return datetime.combine(first, time.min, BANGKOK), datetime.combine(
        last + timedelta(days=1), time.min, BANGKOK
    )
