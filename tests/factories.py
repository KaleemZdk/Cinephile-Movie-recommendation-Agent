"""Test data builders."""


def make_item(id_, title, kind="movie", year="2010", rating=8.0, pop=10.0):
    """A normalized TMDB list item, as tmdb.normalize() returns it."""
    return {
        "tmdb_id": id_,
        "media_type": kind,
        "title": title,
        "year": year,
        "rating": rating,
        "genres": ["Drama"],
        "overview": "o",
        "poster_path": f"/{id_}.jpg",
        "popularity": pop,
    }
