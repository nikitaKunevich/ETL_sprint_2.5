"""
Модуль мигрирует данные о фильмах из SQLite в PostgreSQL в новую схему.
"""

import json
import sqlite3
from dataclasses import astuple
from typing import List, Sequence, Callable, Any, Dict, TypedDict, NamedTuple, Literal
from uuid import uuid4, UUID
import logging

import psycopg2
import psycopg2.extras

from models import (
    OriginalMovie, OriginalMovieActors, OriginalActors, OriginalWriters,
    OriginalData, TransformedFilmWork, TransformedPerson, TransformedFilmWorkPerson,
    TransformedGenre, TransformedFilmWorkGenre,
    TransformedData)

logger = logging.getLogger(__name__)


def sqlite_dict_factory(cursor, row):
    """Фабрика для создания словарей из срок таблицы, где ключи — названия столбцов."""
    row_dict = {}
    for idx, column in enumerate(cursor.description):
        row_dict[column[0]] = row[idx]
    return row_dict


def sqlite_dict_connection_factory(*args, **kwargs):
    con = sqlite3.Connection(*args, **kwargs)
    con.row_factory = sqlite_dict_factory
    return con


EMPTY_VALUES = ["N/A", ""]
INVALID_WRITERS_IDS = []


def to_none_if_empty(value):
    if value in EMPTY_VALUES:
        return None
    else:
        return value


def clean_original_movie_fields(movie):
    """Если какое-то поле заполнено пустым значением, то заменить его на None."""
    return OriginalMovie(
        id=movie.id,
        genre=to_none_if_empty(movie.genre),
        director=to_none_if_empty(movie.director),
        title=movie.title,
        plot=to_none_if_empty(movie.plot),
        imdb_rating=to_none_if_empty(movie.imdb_rating),
        writers=to_none_if_empty(movie.writers),
    )


def fetch_sqlite_data(connection) -> OriginalData:
    """Считываем все данные из старой таблицы, убирая невалидные данные (N/A, '')."""

    # noinspection PyTypeChecker
    cursor = connection.cursor()

    cursor.execute("select DISTINCT * from actors")
    invalid_actors_ids = []
    actor_names: OriginalActors = {}

    for id_name in cursor.fetchall():
        if id_name["name"] in EMPTY_VALUES:
            invalid_actors_ids.append(id_name["id"])
        else:
            actor_names[id_name["id"]] = id_name["name"]

    cursor.execute("select DISTINCT * from writers")
    writer_names: OriginalWriters = {}

    for id_name in cursor.fetchall():
        if id_name["name"] in EMPTY_VALUES:
            INVALID_WRITERS_IDS.append(id_name["id"])
        else:
            writer_names[id_name["id"]] = id_name["name"]

    cursor.execute("select DISTINCT * from movie_actors")
    movie_actors: OriginalMovieActors = {}
    for movie_actor in cursor.fetchall():
        actors = movie_actors.setdefault(movie_actor["movie_id"], [])
        actor_id = int(movie_actor["actor_id"])
        if actor_id not in invalid_actors_ids:
            actors.append(actor_id)

    movies: List[OriginalMovie] = []
    cursor.execute("select DISTINCT * from movies")
    for movie in cursor.fetchall():
        if movie["writers"]:
            writers = [item["id"] for item in json.loads(movie["writers"])]
        else:
            writers = [movie["writer"]]
        writers = [writer for writer in writers if writer not in INVALID_WRITERS_IDS]
        unique_writers = list(set(writers))
        processed_movie = OriginalMovie(id=movie["id"], genre=movie["genre"],
                                        director=movie["director"], title=movie["title"],
                                        plot=movie["plot"], imdb_rating=movie["imdb_rating"],
                                        writers=unique_writers)
        movies.append(processed_movie)

    cursor.close()

    return OriginalData(
        movies=movies,
        movie_actors=movie_actors,
        actor_names=actor_names,
        writer_names=writer_names
    )


def update_transformed_genres(original_movie, transformed_genres, genres_name_to_new_id):
    for genre in original_movie.get_genres():
        if genre not in genres_name_to_new_id:
            transformed_genre = TransformedGenre(id=uuid4(), name=genre)
            genres_name_to_new_id[genre] = transformed_genre.id
            transformed_genres.append(transformed_genre)


MovieId = UUID
Role = Literal['actor', 'director', 'writer']
FullName = str
MoviesRoles = Dict[MovieId, List[Role]]


def migrate_data_to_new_schema(original_data: OriginalData) -> TransformedData:
    """Трансформируем данные из старой схемы в новую схему."""

    cleaned_movies = [clean_original_movie_fields(movie) for movie in original_data.movies]

    person_movies_roles: Dict[FullName, MoviesRoles] = {}

    # Кэш old_id -> new_id уже созданных объектов.
    genres_name_to_new_id = dict()  # name -> id

    transformed_movie_persons: List[TransformedFilmWorkPerson] = []
    transformed_movie_genres: List[TransformedFilmWorkGenre] = []
    transformed_movies: List[TransformedFilmWork] = []
    transformed_persons: List[TransformedPerson] = []
    transformed_genres: List[TransformedGenre] = []

    for original_movie in cleaned_movies:
        # Преобразуем объект фильма из старой схемы в новую.
        transformed_movie = original_movie.to_transformed_movie()
        transformed_movies.append(transformed_movie)

        # Создание объектов таблиц genre и movie_genre.
        update_transformed_genres(original_movie,
                                  transformed_genres,
                                  genres_name_to_new_id)

        for genre in original_movie.get_genres():
            movie_genre = TransformedFilmWorkGenre(uuid4(),
                                                   transformed_movie.id,
                                                   genres_name_to_new_id[genre])
            transformed_movie_genres.append(movie_genre)

        # ------------ Создание объектов таблиц person и movie_person. --------------------

        # заполнить список Людей и инфы о них person_movies_roles
        for director_name in original_movie.get_directors():
            movies_roles = person_movies_roles.setdefault(director_name, {transformed_movie.id: []})
            # noinspection PyTypeChecker
            movies_roles.setdefault(transformed_movie.id, []).append('director')

        movie_actors = original_data.movie_actors.get(original_movie.id, [])
        for actor_id in movie_actors:
            actor_name = original_data.actor_names[actor_id]
            movies_roles = person_movies_roles.setdefault(actor_name, {transformed_movie.id: []})
            # noinspection PyTypeChecker
            movies_roles.setdefault(transformed_movie.id, []).append('actor')

        for writer_id in original_movie.writers:
            writer_name = original_data.writer_names[writer_id]
            movies_roles = person_movies_roles.setdefault(writer_name, {transformed_movie.id: []})
            # noinspection PyTypeChecker
            movies_roles.setdefault(transformed_movie.id, []).append('writer')

    # create persons list
    for full_name, movies_roles in person_movies_roles.items():
        person_id = uuid4()
        transformed_persons.append(TransformedPerson(id=person_id, full_name=full_name))
        for movie_id, roles in movies_roles.items():
            for role in roles:
                tfwp = TransformedFilmWorkPerson(id=uuid4(), film_work_id=movie_id, person_id=person_id, role=role)
                transformed_movie_persons.append(tfwp)

    return TransformedData(
        film_works=transformed_movies,
        film_work_persons=transformed_movie_persons,
        persons=transformed_persons,
        film_work_genres=transformed_movie_genres,
        genres=transformed_genres,
    )


def insert_rows_into_table(cursor, table_name: str, rows: Sequence[Sequence]):
    """
    Генерирует одну длинную строку с значениями для insert с правильным кол-вом параметров в зависимости от количества
    параметров в строке, и исполняет INSERT с этими значениями для заданной таблицы.
    """
    column_count = len(rows[0])
    values_template = "(" + ",".join(("%s",) * column_count) + ")"
    prepared_values = ",".join(cursor.mogrify(values_template, row).decode() for row in rows)
    cursor.execute("insert into %s values %s" % (table_name, prepared_values))


def write_data_to_postgres(transformed_data: TransformedData, connection):
    """Записываем трансформированные данные в PostgreSQL."""

    psycopg2.extras.register_uuid()  # Для конвертации python UUID в psql ::uuid.

    with connection.cursor() as curs:
        insert_rows_into_table(curs, "film_work", [astuple(movie) for movie in transformed_data.film_works])
        insert_rows_into_table(curs, "genre", [astuple(genre) for genre in transformed_data.genres])
        insert_rows_into_table(
            curs, "genre_film_work", [astuple(movie_genre) for movie_genre in transformed_data.film_work_genres]
        )
        insert_rows_into_table(curs, "person", [astuple(person) for person in transformed_data.persons])
        insert_rows_into_table(
            curs,
            "person_film_work",
            [astuple(movie_person) for movie_person in transformed_data.film_work_persons],
        )
