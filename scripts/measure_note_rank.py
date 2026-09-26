"""Measure `ts_rank_cd` separation for note retrieval (milestone 8d, phase 1).

Phase-8 plan section 9 says to measure `NOTES_MIN_RANK` before fixing
it, "exactly as 2b did for trigram retrieval" (see app/core/memory.py's
module docstring for that precedent). The 8e plan's section 9 amends
this to two thresholds, `PERSONAL_MIN_RANK` and `KNOWLEDGE_MIN_RANK`,
measured separately because personal and knowledge notes now live in
separate tables.

**What this script does, and does not, do.**

- It builds a throwaway Postgres database (never the session one, never
  production -- see `_create_database`/`_drop_database`, which mirror
  tests/conftest.py's fixture), runs `alembic upgrade head` against it,
  inserts ~30 synthetic notes in Russian, French and English across
  varied topics, chunks them with `app.vault.notes_text.prepare`, and
  writes the chunks into *both* `note_chunk_personal` and
  `note_chunk_knowledge` through their access modules, with
  `notes_consent` on.
- It runs ~40 synthetic user messages against
  `app.vault._chunks.search_ranked` (the rank-returning variant added
  for this measurement) for both tables, and prints a table plus
  summary statistics: the distribution of top ranks for true positives
  vs. the best noise rank, per language, and the best separating
  threshold with its precision/recall.
- **It does not pick `NOTES_MIN_RANK`/`PERSONAL_MIN_RANK`/
  `KNOWLEDGE_MIN_RANK` in code**, and it does not touch `app/core/turn.py`.
  Reading its output and deciding a number is a separate, human step.

Run it with:

    uv run python scripts/measure_note_rank.py

It needs the same reachable Postgres cluster the test suite uses
(`ANCHOR_ADMIN_DATABASE_URL`, default `postgresql://anchor:anchor@127.0.0.1:5432/postgres`).
"""

from __future__ import annotations

import asyncio
import os
import random
import string
from dataclasses import dataclass
from pathlib import Path

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession

REPO_ROOT = Path(__file__).resolve().parent.parent


def _admin_dsn() -> str:
    return os.environ.get(
        "ANCHOR_ADMIN_DATABASE_URL", "postgresql://anchor:anchor@127.0.0.1:5432/postgres"
    )


def _run_alembic_upgrade(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config

    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


async def _create_database() -> tuple[str, str]:
    """A throwaway `anchor_measure_<rand>` database. Returns (name, asyncpg url)."""
    db_name = "anchor_measure_" + "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    admin_dsn = _admin_dsn()
    base = admin_dsn.rsplit("/", 1)[0]
    raw_url = f"{base}/{db_name}"
    asyncpg_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    conn = await asyncpg.connect(admin_dsn)
    try:
        await conn.execute(
            f'CREATE DATABASE "{db_name}" TEMPLATE template0 LOCALE \'C.UTF-8\' ENCODING \'UTF8\''
        )
    finally:
        await conn.close()
    return db_name, asyncpg_url


async def _drop_database(db_name: str) -> None:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# The synthetic corpus.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Note:
    title: str
    lang: str  # "ru" | "fr" | "en"
    text: str  # markdown body, as if already stripped of frontmatter


NOTES: list[Note] = [
    Note("Бег", "ru", "## Утренние пробежки\nБегаю по утрам в парке, обычно пять километров.\n\n## Обувь\nКупил новые кроссовки для бега на длинные дистанции."),
    Note("Растяжка", "ru", "После пробежки всегда делаю растяжку минут десять, чтобы не болели мышцы."),
    Note("Сон", "ru", "## Режим\nСтараюсь ложиться до полуночи и спать не меньше семи часов.\n\n## Будильник\nПросыпаюсь без будильника, если легла вовремя."),
    Note("Готовка", "ru", "## Борщ\nВарю борщ по бабушкиному рецепту: свёкла, капуста, немного сахара в конце.\n\n## Плов\nПлов получается лучше в казане, а не в кастрюле."),
    Note("День рождения партнёра", "ru", "У партнёра день рождения в октябре. Хочу заказать столик в том ресторане у канала и подарить книгу."),
    Note("CCRU", "ru", "## История\nCCRU — Cybernetic Culture Research Unit, группа при Уорикском университете в девяностых.\n\n## Гиперстишн\nГиперстишн — вымысел, который делает себя реальным через собственное распространение."),
    Note("GCP IAM", "ru", "## Роли\nВ GCP IAM роль привязывается к участнику через политику на уровне проекта, папки или организации.\n\n## Сервисные аккаунты\nСервисный аккаунт — это тоже участник, и ему можно выдать отдельную роль."),
    Note("Kubernetes на русском", "ru", "## Поды\nПод — минимальная единица развёртывания в Kubernetes, обычно один или несколько контейнеров.\n\n## Деплойменты\nДеплоймент управляет репликами подов и обновляет их постепенно."),
    Note("Погода", "ru", "Осенью в этом городе часто идёт дождь, а зимой почти не бывает снега."),
    Note("Кофе", "ru", "Люблю заваривать кофе через пуровер, но иногда просто беру эспрессо-машину."),

    Note("La Course", "fr", "## Le matin\nJe cours dans le parc tous les matins, environ cinq kilomètres.\n\n## Les chaussures\nJ'ai acheté de nouvelles chaussures pour les longues distances."),
    Note("Le sommeil", "fr", "J'essaie de me coucher avant minuit et de dormir au moins sept heures."),
    Note("La cuisine", "fr", "## Le pot-au-feu\nJe fais un pot-au-feu le dimanche, avec des poireaux et des carottes.\n\n## Le pain\nLe pain est meilleur le lendemain, légèrement grillé."),
    Note("Deleuze", "fr", "## Différence et répétition\nDeleuze développe une ontologie de la différence, opposée à la logique de l'identité.\n\n## Le rhizome\nAvec Guattari, il propose le rhizome comme figure d'une pensée non hiérarchique."),
    Note("L'anniversaire du partenaire", "fr", "L'anniversaire de mon partenaire est en octobre, je pense réserver le restaurant près du canal."),
    Note("Le café", "fr", "Je prépare mon café avec une cafetière italienne, le matin avant de partir."),
    Note("La météo", "fr", "En automne il pleut souvent dans cette ville, et il neige rarement en hiver."),

    Note("Running", "en", "## Morning runs\nI run in the park every morning, usually five kilometres.\n\n## Shoes\nI bought new running shoes for long distances."),
    Note("Sleep", "en", "I try to go to bed before midnight and sleep at least seven hours."),
    Note("Cooking", "en", "## Soup\nI make a big pot of soup on Sundays, mostly vegetables and lentils.\n\n## Bread\nHomemade bread is better the next day, lightly toasted."),
    Note("Kubernetes", "en", "## Pods\nA pod is the smallest deployable unit in Kubernetes, usually one or a few containers.\n\n## Deployments\nA deployment manages pod replicas and rolls out updates gradually."),
    Note("Hyperstition", "en", "## Origins\nThe term comes from the CCRU, the Cybernetic Culture Research Unit at Warwick in the 1990s.\n\n## Definition\nHyperstition is fiction that makes itself real through its own transmission."),
    Note("Partner's birthday", "en", "My partner's birthday is in October. I want to book the restaurant by the canal and get them a book."),
    Note("Coffee", "en", "I brew coffee with a pour-over most mornings, sometimes an espresso machine on weekends."),
    Note("Weather", "en", "It rains a lot here in autumn, and it rarely snows in winter."),
    Note("GCP IAM notes", "en", "## Roles\nIn GCP IAM a role is bound to a principal through a policy at the project, folder, or organization level.\n\n## Service accounts\nA service account is itself a principal and can be granted its own role."),
    Note("Stretching", "en", "I stretch for about ten minutes after every run so my legs don't get sore."),
    Note("Interior plants", "en", "I keep a few succulents on the windowsill; they need almost no watering."),
    Note("Cycling", "en", "On weekends I sometimes cycle along the river instead of running."),
    Note("Reading list", "en", "Currently reading a history of the printing press, slowly, a few pages a night."),
]


@dataclass(frozen=True)
class Message:
    text: str
    lang: str
    expected: str | None  # a Note.title, or None for noise


MESSAGES: list[Message] = [
    # --- Russian: true positives -----------------------------------------
    Message("сегодня утром бегала в парке, ноги немного устали", "ru", "Бег"),
    Message("не могу заснуть, ложусь слишком поздно", "ru", "Сон"),
    Message("хочу сварить борщ на выходных", "ru", "Готовка"),
    Message("надо придумать подарок партнёру на день рождения", "ru", "День рождения партнёра"),
    Message("расскажи про гиперстишн и CCRU ещё раз", "ru", "CCRU"),
    Message("как назначить роль сервисному аккаунту в IAM", "ru", "GCP IAM"),
    Message("что такое деплоймент и под в кубернетес", "ru", "Kubernetes на русском"),
    Message("после бега надо не забыть растянуться", "ru", "Растяжка"),
    # --- Russian: noise ----------------------------------------------------
    Message("привет как дела", "ru", None),  # small talk
    Message("сколько будет два плюс два", "ru", None),  # unrelated
    Message("и в на с у", "ru", None),  # all stopwords
    Message("а", "ru", None),  # all stopwords, tiny
    Message("парк", "ru", None),  # single common word (shared with "Бег")
    Message("кофе хороший сегодня", "ru", "Кофе"),  # true positive, distinct topic

    # --- French: true positives --------------------------------------------
    Message("j'ai couru dans le parc ce matin", "fr", "La Course"),
    Message("je n'arrive pas à dormir, je me couche trop tard", "fr", "Le sommeil"),
    Message("je vais préparer un pot-au-feu ce dimanche", "fr", "La cuisine"),
    Message("il faut trouver un cadeau pour l'anniversaire de mon partenaire", "fr", "L'anniversaire du partenaire"),
    Message("parle-moi encore du rhizome chez Deleuze", "fr", "Deleuze"),
    Message("je prends un café avant de partir", "fr", "Le café"),
    # --- French: noise -------------------------------------------------------
    Message("bonjour, comment ça va", "fr", None),
    Message("quelle heure est-il", "fr", None),
    Message("de la le les", "fr", None),  # stopwords
    Message("parc", "fr", None),  # single common word

    # --- English: true positives ---------------------------------------------
    Message("went for a run in the park this morning", "en", "Running"),
    Message("can't sleep, going to bed too late again", "en", "Sleep"),
    Message("thinking about making soup this weekend", "en", "Cooking"),
    Message("need to figure out a gift for my partner's birthday", "en", "Partner's birthday"),
    Message("what's a kubernetes deployment again", "en", "Kubernetes"),
    Message("explain hyperstition and the CCRU one more time", "en", "Hyperstition"),
    Message("how do IAM roles work for service accounts on GCP", "en", "GCP IAM notes"),
    Message("my legs are sore, forgot to stretch after the run", "en", "Stretching"),
    Message("thinking about getting a bike for weekend rides", "en", "Cycling"),
    Message("started a new book about the history of printing", "en", "Reading list"),
    Message("watering the succulents on the windowsill again", "en", "Interior plants"),
    Message("making coffee with the pour-over this morning", "en", "Coffee"),
    # --- English: noise --------------------------------------------------------
    Message("hey what's up", "en", None),
    Message("what time is it right now", "en", None),
    Message("the a of to", "en", None),  # all stopwords
    Message("park", "en", None),  # single common word, shared with "Running"
    Message("it rained a bit today, nothing unusual", "en", "Weather"),  # true positive
]


# --------------------------------------------------------------------------
# The measurement itself.
# --------------------------------------------------------------------------


async def _index_all(
    session: AsyncSession, note_class: str, path_prefix: str, replace_chunks
) -> dict[int, str]:
    """Insert every NOTES entry as a `vault_file` row plus its chunks.

    `note_class` and `path_prefix` are passed in by the caller rather
    than derived from the model here, so this function never has to
    name a chunk table itself (tests/test_vault_notes_isolation.py
    scans scripts/ too, and only the access module each `replace_chunks`
    belongs to may name its table).

    Returns file_id -> title, so a chunk's heading (which starts with
    the title, per notes_text._heading_path) can be mapped back for
    reporting without keeping a second table of ids.
    """
    from app.db.models import VaultFile
    from app.vault.notes_text import prepare

    file_by_title: dict[int, str] = {}
    for note in NOTES:
        row = VaultFile(path=f"{path_prefix}/{note.title}.md", role="note", note_class=note_class)
        session.add(row)
        await session.flush()
        chunks = prepare(note.text, note.title)
        await replace_chunks(session, row.id, chunks)
        file_by_title[row.id] = note.title
    await session.commit()
    return file_by_title


def _note_for_heading(heading: str | None) -> str | None:
    """The note title a chunk's heading was built from (notes_text._heading_path)."""
    if heading is None:
        return None
    return heading.split(" › ", 1)[0]


@dataclass
class Row:
    message: str
    lang: str
    expected: str | None
    top_rank: float | None
    top_note: str | None
    hit: bool | None  # None when expected is None (no "hit" concept for noise)


async def _measure(session: AsyncSession, model: type, search_ranked) -> list[Row]:
    rows: list[Row] = []
    for msg in MESSAGES:
        results = await search_ranked(session, model, msg.text, 50)
        if results:
            heading, _text, rank = results[0]
        else:
            heading, rank = None, None
        top_note = _note_for_heading(heading)
        hit = (top_note == msg.expected) if msg.expected is not None else None
        rows.append(Row(msg.text, msg.lang, msg.expected, rank, top_note, hit))
    return rows


def _print_table(class_name: str, rows: list[Row]) -> None:
    print(f"\n=== {class_name} ===")
    print(f"{'lang':4} {'expected':28} {'top_note':28} {'rank':>8}  hit")
    for r in rows:
        rank_str = f"{r.top_rank:.4f}" if r.top_rank is not None else "   -   "
        hit_str = "-" if r.hit is None else ("YES" if r.hit else "no")
        expected = r.expected or "(none)"
        top_note = r.top_note or "(none)"
        print(f"{r.lang:4} {expected:28} {top_note:28} {rank_str:>8}  {hit_str}")


def _summary(class_name: str, rows: list[Row]) -> None:
    tp_ranks = [r.top_rank or 0.0 for r in rows if r.expected is not None]
    tp_hit_ranks = [r.top_rank or 0.0 for r in rows if r.expected is not None and r.hit]
    noise_ranks = [r.top_rank or 0.0 for r in rows if r.expected is None]

    print(f"\n--- {class_name}: summary ---")
    print(f"true positives (expected != None): {len(tp_ranks)}")
    if tp_ranks:
        print(f"  rank range: {min(tp_ranks):.4f} .. {max(tp_ranks):.4f}")
    print(f"  of which top hit matched expected: {sum(1 for r in rows if r.hit)} / {len(tp_ranks)}")
    if tp_hit_ranks:
        print(f"  rank range when the top hit *was* correct: {min(tp_hit_ranks):.4f} .. {max(tp_hit_ranks):.4f}")
    print(f"noise (expected == None): {len(noise_ranks)}")
    if noise_ranks:
        print(f"  best (highest) noise rank: {max(noise_ranks):.4f}")
        print(f"  noise rank range: {min(noise_ranks):.4f} .. {max(noise_ranks):.4f}")

    print("  per-language true-positive-hit rank range:")
    for lang in ("ru", "fr", "en"):
        lang_hits = [r.top_rank or 0.0 for r in rows if r.lang == lang and r.expected is not None and r.hit]
        lang_tp = [r for r in rows if r.lang == lang and r.expected is not None]
        n_hit = sum(1 for r in lang_tp if r.hit)
        if lang_hits:
            print(f"    {lang}: {n_hit}/{len(lang_tp)} correct, rank {min(lang_hits):.4f} .. {max(lang_hits):.4f}")
        else:
            print(f"    {lang}: {n_hit}/{len(lang_tp)} correct, no correct-hit ranks to show")

    if not (tp_hit_ranks and noise_ranks):
        print("  -> not enough data on one side to compute a threshold.")
        return

    # Best separating threshold: the value of a candidate that maximises
    # precision+recall (equivalently minimises misclassifications) over
    # this fixed set, treating "hit" (correct top note) as the positive
    # class and every noise row as negative. This is exhaustive over the
    # observed ranks, not a formula -- there are only ~40 of them.
    candidates = sorted(set(tp_hit_ranks) | set(noise_ranks))
    best = None
    for threshold in candidates:
        tp = sum(1 for r in tp_hit_ranks if r >= threshold)
        fn = len(tp_hit_ranks) - tp
        fp = sum(1 for r in noise_ranks if r >= threshold)
        tn = len(noise_ranks) - fp
        precision = tp / (tp + fp) if (tp + fp) else 1.0
        recall = tp / (tp + fn) if (tp + fn) else 1.0
        score = precision + recall
        if best is None or score > best[0]:
            best = (score, threshold, precision, recall, tp, fp, fn, tn)
    _, threshold, precision, recall, tp, fp, fn, tn = best
    print(
        f"  best separating threshold: {threshold:.4f} "
        f"(precision={precision:.2f}, recall={recall:.2f}, "
        f"tp={tp} fp={fp} fn={fn} tn={tn})"
    )


async def _index_and_measure(asyncpg_url: str) -> None:
    from app.db.models import NoteChunkKnowledge, NoteChunkPersonal, UserState
    from app.db.session import create_engine_and_sessionmaker
    from app.vault import notes_knowledge, notes_personal
    from app.vault._chunks import search_ranked

    engine, sessionmaker = create_engine_and_sessionmaker(asyncpg_url)
    try:
        async with sessionmaker() as session:
            session.add(UserState(id=1, chat_id=1, notes_consent=True))
            await session.commit()

        async with sessionmaker() as session:
            print(f"indexing {len(NOTES)} synthetic notes as personal notes ...")
            await _index_all(session, "personal", "Personal", notes_personal.replace_chunks)
        async with sessionmaker() as session:
            print(f"indexing {len(NOTES)} synthetic notes as knowledge notes ...")
            await _index_all(session, "knowledge", "Library", notes_knowledge.replace_chunks)

        for model, class_name in (
            (NoteChunkPersonal, "personal (PERSONAL_MIN_RANK)"),
            (NoteChunkKnowledge, "knowledge (KNOWLEDGE_MIN_RANK)"),
        ):
            async with sessionmaker() as session:
                rows = await _measure(session, model, search_ranked)
            _print_table(class_name, rows)
            _summary(class_name, rows)
    finally:
        await engine.dispose()


def main() -> None:
    db_name, asyncpg_url = asyncio.run(_create_database())
    print(f"scratch database: {db_name}")
    try:
        _run_alembic_upgrade(asyncpg_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        asyncio.run(_index_and_measure(asyncpg_url))
    finally:
        asyncio.run(_drop_database(db_name))
        print(f"\ndropped scratch database {db_name}")


if __name__ == "__main__":
    main()
