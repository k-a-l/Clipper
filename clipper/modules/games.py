"""Which game is being played: from the platform's category (Twitch) or the stream/video title,
description and tags. Drives hashtags like #GTA6 #GTAVI and the game name in titles and tags.

Twitch's category is authoritative ("Grand Theft Auto VI"); titles are matched against aliases
("gta 6", "gta vi", "gta6") so YouTube streams and videos are covered too.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    name: str  # display name used in titles/descriptions, e.g. "GTA 6"
    hashtags: tuple[str, ...]  # most-searched first
    aliases: tuple[str, ...] = ()  # regexes (lower-case text)
    category: tuple[str, ...] = ()  # exact platform category names (lower-case)


GAMES: tuple[Game, ...] = (
    Game("GTA 6", ("#GTA6", "#GTAVI", "#GTA"), (r"\bgta\s*(?:6|vi)\b", r"grand theft auto\s*(?:6|vi)\b"),
         ("grand theft auto vi",)),
    Game("GTA 5", ("#GTA5", "#GTAV", "#GTARP", "#GTA"), (r"\bgta\s*(?:5|v)\b", r"grand theft auto\s*(?:5|v)\b",
                                                         r"\bgta\s*rp\b", r"\bnopixel\b", r"\bgta online\b"),
         ("grand theft auto v",)),
    Game("GTA 4", ("#GTA4", "#GTAIV", "#GTA"), (r"\bgta\s*(?:4|iv)\b", r"grand theft auto\s*(?:4|iv)\b"),
         ("grand theft auto iv",)),
    Game("GTA San Andreas", ("#GTASanAndreas", "#GTA"), (r"\bsan andreas\b",), ("grand theft auto: san andreas",)),
    Game("Minecraft", ("#Minecraft",), (r"\bminecraft\b",), ("minecraft",)),
    Game("Fortnite", ("#Fortnite",), (r"\bfortnite\b",), ("fortnite",)),
    Game("Valorant", ("#Valorant",), (r"\bvalorant\b",), ("valorant",)),
    Game("Counter-Strike 2", ("#CS2", "#CounterStrike"), (r"\bcs\s*2\b", r"\bcs:?go\b", r"counter[- ]strike"),
         ("counter-strike", "counter-strike 2", "counter-strike: global offensive")),
    Game("League of Legends", ("#LeagueOfLegends", "#LoL"), (r"league of legends",), ("league of legends",)),
    Game("Apex Legends", ("#ApexLegends", "#Apex"), (r"\bapex legends\b", r"\bapex\b"), ("apex legends",)),
    Game("Call of Duty", ("#Warzone", "#CallOfDuty", "#COD"), (r"\bwarzone\b", r"call of duty", r"\bcod\b",
                                                               r"black ops", r"modern warfare"),
         ("call of duty: warzone", "call of duty: black ops 6", "call of duty: black ops 7")),
    Game("EA FC", ("#EAFC", "#FC26", "#FIFA"), (r"\bea\s*(?:sports\s*)?fc\s*\d*\b", r"\bfc\s*2[4-9]\b", r"\bfifa\s*\d*\b"),
         ("ea sports fc 25", "ea sports fc 26", "ea sports fc 24")),
    Game("Roblox", ("#Roblox",), (r"\broblox\b",), ("roblox",)),
    Game("Elden Ring", ("#EldenRing",), (r"\belden ring\b", r"\bnightreign\b"), ("elden ring", "elden ring nightreign")),
    Game("Red Dead Redemption 2", ("#RDR2", "#RedDeadRedemption2"), (r"\brdr\s*2\b", r"red dead"),
         ("red dead redemption 2",)),
    Game("Rocket League", ("#RocketLeague",), (r"\brocket league\b",), ("rocket league",)),
    Game("Overwatch 2", ("#Overwatch2", "#Overwatch"), (r"\boverwatch\b",), ("overwatch 2",)),
    Game("Dota 2", ("#Dota2",), (r"\bdota\s*2?\b",), ("dota 2",)),
    Game("Rust", ("#RustGame", "#Rust"), (r"\brust\b(?! (?:programming|lang))",), ("rust",)),
    Game("Marvel Rivals", ("#MarvelRivals",), (r"\bmarvel rivals\b",), ("marvel rivals",)),
    Game("Among Us", ("#AmongUs",), (r"\bamong us\b",), ("among us",)),
    Game("Pokémon", ("#Pokemon",), (r"\bpok[eé]mon\b",), ()),
    Game("Clash Royale", ("#ClashRoyale",), (r"\bclash royale\b",), ("clash royale",)),
    Game("PUBG", ("#PUBG", "#BGMI"), (r"\bpubg\b", r"\bbgmi\b"), ("pubg: battlegrounds", "pubg mobile")),
    Game("Free Fire", ("#FreeFire",), (r"\bfree ?fire\b",), ("garena free fire",)),
    Game("Schedule I", ("#ScheduleI",), (r"\bschedule (?:i|1)\b",), ("schedule i",)),
    Game("Peak", ("#PeakGame",), (r"\bpeak\b(?= game| gameplay)",), ("peak",)),
)

# Twitch categories that are not games
NON_GAME = {"just chatting", "irl", "music", "sports", "talk shows & podcasts", "art", "asmr", "special events",
            "travel & outdoors", "food & drink", "science & technology", "pools, hot tubs, and beaches",
            "software and game development", "chess", "slots", "makers & crafting", "fitness & health",
            "politics", "animals, aquariums, and zoos", "virtual casino"}


def _hashtag(text: str) -> str:
    return "#" + "".join(w[:1].upper() + w[1:] for w in re.findall(r"[A-Za-z0-9]+", text))


def detect_game(category: str | None = None, *texts: str | None) -> Game | None:
    """Platform category first (exact), then aliases in titles/descriptions/tags."""
    cat = (category or "").strip().lower()
    if cat:
        for g in GAMES:
            if cat in g.category:
                return g
    blob = " ".join(t for t in texts if t).lower()
    if blob:
        for g in GAMES:
            if any(re.search(a, blob) for a in g.aliases):
                return g
    if cat and cat not in NON_GAME:  # a game we don't list: still use the platform's name
        name = (category or "").strip()
        return Game(name, (_hashtag(name),))
    return None


# --------------------------------------------------------------------------- reach
# The most-used companion tags per game (hashtag analytics: #gta5 is posted with #gta 38 %, #gtav 27 %,
# #gtaonline 26 %, #gaming 18 %, #gamer 17 %; #gta6 with #gtaonline, #grandtheftauto, #gtavi, #gta6online),
# plus each game's Shorts tag. Ordered most-used first.
POPULAR: dict[str, tuple[str, ...]] = {
    "GTA 6": ("#GTA6", "#GTAVI", "#GTA", "#GTAOnline", "#GTA6Online", "#GrandTheftAuto", "#GrandTheftAutoVI",
              "#RockstarGames", "#ViceCity", "#GTA6Gameplay", "#GTA6Clips", "#GTA6Shorts"),
    "GTA 5": ("#GTA5", "#GTAV", "#GTA", "#GTAOnline", "#GTARP", "#GrandTheftAuto", "#RockstarGames", "#FiveM",
              "#GTA5Online", "#GTA5Clips", "#GTA5Shorts", "#GTA6"),
    "GTA 4": ("#GTA4", "#GTAIV", "#GTA", "#GrandTheftAuto", "#RockstarGames", "#LibertyCity", "#GTA4Clips"),
    "GTA San Andreas": ("#GTASanAndreas", "#GTASA", "#GTA", "#GrandTheftAuto", "#RockstarGames", "#CJ"),
    "Minecraft": ("#Minecraft", "#MinecraftShorts", "#MinecraftMemes", "#MinecraftBuilds", "#Minecrafter"),
    "Fortnite": ("#Fortnite", "#FortniteClips", "#FortniteBR", "#FortniteShorts", "#FortniteMoments", "#Epic"),
    "Valorant": ("#Valorant", "#ValorantClips", "#ValorantMoments", "#ValorantShorts", "#Riot"),
    "Counter-Strike 2": ("#CS2", "#CounterStrike", "#CSGO", "#CS2Clips", "#CS2Highlights"),
    "League of Legends": ("#LeagueOfLegends", "#LoL", "#LeagueClips", "#LoLClips"),
    "Apex Legends": ("#ApexLegends", "#Apex", "#ApexClips", "#ApexLegendsClips"),
    "Call of Duty": ("#Warzone", "#CallOfDuty", "#COD", "#BlackOps", "#WarzoneClips", "#CODClips"),
    "EA FC": ("#EAFC", "#FC26", "#FIFA", "#Ultimateteam", "#EAFCClips"),
    "Roblox": ("#Roblox", "#RobloxShorts", "#RobloxFunny", "#RobloxEdit"),
    "Red Dead Redemption 2": ("#RDR2", "#RedDeadRedemption2", "#RedDead", "#RockstarGames", "#ArthurMorgan"),
}
GENERIC = ("#Gaming", "#Gamer", "#GamingClips", "#Streamer", "#Viral")


def popular_tags(name: str | None, action_tags: tuple[str, ...] = (), trending: tuple[str, ...] = ()) -> list[str]:
    """Most-used hashtags for a game, then action tags (#GTA6PoliceChase), live trends, generic gaming tags."""
    g = detect_game(None, name) or detect_game(name) if name else None
    base = list(POPULAR.get(g.name if g else "", ())) or (list(g.hashtags) if g else [])
    if g and not base:
        base = list(g.hashtags)
    main = (base[0] if base else "#Gaming").lstrip("#")
    acts = [t.replace("{G}", main) for t in action_tags]
    out: list[str] = []
    for t in [*base[:4], *acts[:2], *base[4:], *trending, *acts[2:], *GENERIC]:
        if t and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out
