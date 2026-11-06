"""Fake catalog so the project runs with zero external APIs.
Swap these for real API calls (e.g. Amadeus) in tools.py later."""

def _city(flights, hotels, acts):
    return {"flights": flights, "hotels": hotels, "activities": acts}

CATALOG = {
    "Tokyo": _city(
        [("Budget Air", 650, 1), ("MidAir", 900, 1), ("Premium Air", 1300, 0)],
        [("Asakusa Hostel Inn", "Asakusa", 60, 3.8), ("Shinjuku Central", "Shinjuku", 120, 4.2), ("Park Tower Luxe", "Shinjuku", 250, 4.7)],
        [("Senso-ji Temple", 0, "culture"), ("Tsukiji food tour", 60, "food"), ("TeamLab Planets", 35, "art"),
         ("Meiji Shrine walk", 0, "culture"), ("Sushi omakase", 120, "food"), ("Akihabara arcades", 20, "tech"),
         ("Mt. Fuji day trip", 90, "nature"), ("Shibuya nightlife", 40, "nightlife")]),
    "Paris": _city(
        [("EuroSaver", 700, 1), ("AirFrance Eco", 950, 0), ("AirFrance Premium", 1500, 0)],
        [("Montmartre Hostel", "Montmartre", 70, 3.9), ("Hotel Latin", "Latin Quarter", 160, 4.3), ("Le Meurice-ish", "1st arr.", 400, 4.8)],
        [("Eiffel Tower climb", 30, "culture"), ("Louvre", 22, "art"), ("Seine picnic", 15, "food"),
         ("Versailles day trip", 35, "culture"), ("Wine tasting", 70, "food"), ("Orsay Museum", 16, "art"),
         ("Montmartre stroll", 0, "nature"), ("Cabaret show", 110, "nightlife")]),
    "Lisbon": _city(
        [("TAP Basic", 600, 1), ("TAP Classic", 820, 0), ("TAP Plus", 1200, 0)],
        [("Alfama Guesthouse", "Alfama", 50, 4.0), ("Baixa Boutique", "Baixa", 110, 4.4), ("Avenida Palace", "Avenida", 220, 4.7)],
        [("Tram 28 ride", 3, "culture"), ("Belem pastries", 8, "food"), ("Sintra day trip", 25, "nature"),
         ("Fado dinner", 55, "nightlife"), ("Oceanarium", 22, "nature"), ("LX Factory", 0, "art"),
         ("Tile museum", 10, "art"), ("Seafood dinner", 45, "food")]),
}
