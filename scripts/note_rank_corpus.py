"""The frozen corpus for scripts/measure_note_rank.py (milestone 8d, phase 2).

**Frozen before any measurement was run.** This module was written and
reviewed in full, then run once against `search_ranked` (which returns
`matched`, the count of distinct query lexemes a chunk actually
contains, alongside `rank`). After that first run, exactly the label
corrections listed at the bottom of this docstring were made, because
they were plainly wrong (a genuine content-word match mislabelled
`None`, or the reverse) -- never a change to note or message *text*,
and never a change made because a threshold didn't separate cleanly.

~60 notes (25 Russian, 15 French, 20 English) across running, sleep,
cooking, a partner's birthday, CCRU/hyperstition, GCP IAM, Kubernetes,
Deleuze, coffee, weather, and about a dozen more topics per language
(meditation, budgeting, laundry, a dentist visit, a garden, taxes, a
house move, a book club, a cat, a museum visit, git/docker, and so on)
-- one true-positive message per note (60), plus 40 noise messages:
messages sharing exactly one content word with some note («парк»,
«parc», "park"), messages sharing two *incidental* words with an
unrelated note, small talk, all-stopword messages, and questions on
topics with no note at all.

**True positives sometimes share zero content words with their note**,
on purpose (a real paraphrase, "how do I get better at falling asleep
before midnight" for a note about sleep, sharing no root with it once
stemmed) -- lexical search is expected to miss those, and that is a
real, reportable ceiling on this approach, not a bug to route around.

**Label corrections made after the first run** (none of these are text
changes -- see the module docstring's rule above):
- none yet. If a run finds one, it is listed here with the reason.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Note:
    title: str
    lang: str  # "ru" | "fr" | "en"
    text: str


@dataclass(frozen=True)
class Message:
    text: str
    lang: str
    expected: str | None  # a Note.title, or None for noise


NOTES: list[Note] = [
    # ---------------------------------------------------------------- ru (25)
    Note("Бег", "ru", "## Утренние пробежки\nБегаю по утрам в парке, обычно пять километров.\n\n## Обувь\nКупил новые кроссовки для бега на длинные дистанции."),
    Note("Растяжка", "ru", "После пробежки всегда делаю растяжку минут десять, чтобы не болели мышцы ног."),
    Note("Сон", "ru", "## Режим\nСтараюсь ложиться до полуночи и спать не меньше семи часов.\n\n## Будильник\nПросыпаюсь без будильника, если легла вовремя."),
    Note("Готовка", "ru", "## Борщ\nВарю борщ по бабушкиному рецепту: свёкла, капуста, немного сахара в конце.\n\n## Плов\nПлов получается лучше в казане, а не в кастрюле."),
    Note("День рождения партнёра", "ru", "У партнёра день рождения в октябре. Хочу заказать столик в том ресторане у канала и подарить книгу."),
    Note("CCRU", "ru", "## История\nCCRU — Cybernetic Culture Research Unit, группа при Уорикском университете в девяностых.\n\n## Гиперстишн\nГиперстишн — вымысел, который делает себя реальным через собственное распространение."),
    Note("GCP IAM", "ru", "## Роли\nВ GCP IAM роль привязывается к участнику через политику на уровне проекта, папки или организации.\n\n## Сервисные аккаунты\nСервисный аккаунт — это тоже участник, и ему можно выдать отдельную роль."),
    Note("Kubernetes на русском", "ru", "## Поды\nПод — минимальная единица развёртывания в Kubernetes, обычно один или несколько контейнеров.\n\n## Деплойменты\nДеплоймент управляет репликами подов и обновляет их постепенно."),
    Note("Погода", "ru", "Осенью в этом городе часто идёт дождь, а зимой почти не бывает снега."),
    Note("Кофе", "ru", "Люблю заваривать кофе через пуровер, но иногда просто беру эспрессо-машину по утрам."),
    Note("Медитация", "ru", "Сижу пятнадцать минут утром с закрытыми глазами, слежу за дыханием, ничего особенного не жду."),
    Note("Планёрки на работе", "ru", "По понедельникам у нас общая планёрка, обсуждаем задачи на неделю и кто чем занят."),
    Note("Бюджет", "ru", "Веду таблицу расходов по категориям: еда, транспорт, подписки, откладываю процент на подушку."),
    Note("Стирка", "ru", "Тёмные вещи стираю на тридцати градусах отдельно от белых, иначе краска садится."),
    Note("Продукты", "ru", "Список покупок на неделю: молоко, яйца, гречка, овощи для супа, хлеб."),
    Note("Стоматолог", "ru", "Записался к стоматологу на осмотр в следующий вторник, давно не был на чистке."),
    Note("Гитара", "ru", "Разучиваю новую песню на гитаре, пока плохо получается баррэ-аккорд."),
    Note("Английский язык", "ru", "Каждый вечер читаю по главе книги на английском, слова из непонятных мест выписываю."),
    Note("Огород на балконе", "ru", "На балконе выращиваю базилик и помидоры черри, поливаю через день."),
    Note("Налоги", "ru", "В апреле надо подать декларацию, в этом году добавились доходы от подработки."),
    Note("Ремонт квартиры", "ru", "Обои в спальне решил менять сам, в субботу поеду выбирать цвет в магазин."),
    Note("Кот", "ru", "Кот стал меньше есть последние дни, надо будет показать его ветеринару, если не наладится."),
    Note("Музей", "ru", "В субботу сходили в музей современного искусства, понравился зал с инсталляциями."),
    Note("Поход в горы", "ru", "Планируем поход на выходных, нужно проверить палатку и купить газ для горелки."),
    Note("Фотография", "ru", "Снимаю город на плёночную камеру, проявляю сам, результат вижу только через неделю."),

    # ---------------------------------------------------------------- fr (15)
    Note("La Course", "fr", "## Le matin\nJe cours dans le parc tous les matins, environ cinq kilomètres.\n\n## Les chaussures\nJ'ai acheté de nouvelles chaussures pour les longues distances."),
    Note("Le sommeil", "fr", "J'essaie de me coucher avant minuit et de dormir au moins sept heures chaque nuit."),
    Note("La cuisine", "fr", "## Le pot-au-feu\nJe fais un pot-au-feu le dimanche, avec des poireaux et des carottes.\n\n## Le pain\nLe pain est meilleur le lendemain, légèrement grillé."),
    Note("L'anniversaire du partenaire", "fr", "L'anniversaire de mon partenaire est en octobre, je pense réserver le restaurant près du canal."),
    Note("Deleuze", "fr", "## Différence et répétition\nDeleuze développe une ontologie de la différence, opposée à la logique de l'identité.\n\n## Le rhizome\nAvec Guattari, il propose le rhizome comme figure d'une pensée non hiérarchique."),
    Note("Le café", "fr", "Je prépare mon café avec une cafetière italienne, le matin avant de partir travailler."),
    Note("La météo", "fr", "En automne il pleut souvent dans cette ville, et il neige rarement en hiver."),
    Note("Le yoga", "fr", "Je fais vingt minutes de yoga le soir, surtout des postures pour le dos."),
    Note("Le jardin", "fr", "Je plante des tomates et du basilic sur le balcon, j'arrose un jour sur deux."),
    Note("Les impôts", "fr", "La déclaration est à faire en avril, cette année j'ai des revenus en plus à déclarer."),
    Note("Le déménagement", "fr", "On déménage le mois prochain, il faut trouver des cartons et réserver un camion."),
    Note("Le club de lecture", "fr", "Le club de lecture se réunit une fois par mois, ce mois-ci on discute d'un roman russe."),
    Note("La guitare", "fr", "J'apprends un nouveau morceau à la guitare, l'accord barré me pose encore des soucis."),
    Note("Le vélo", "fr", "Le week-end je fais parfois du vélo le long de la rivière plutôt que de courir."),
    Note("Les randonnées", "fr", "On prépare une randonnée en montagne pour le week-end, il faut vérifier la tente."),

    # ---------------------------------------------------------------- en (20)
    Note("Running", "en", "## Morning runs\nI run in the park every morning, usually five kilometres.\n\n## Shoes\nI bought new running shoes for long distances."),
    Note("Sleep", "en", "I try to go to bed before midnight and sleep at least seven hours every night."),
    Note("Cooking", "en", "## Soup\nI make a big pot of soup on Sundays, mostly vegetables and lentils.\n\n## Bread\nHomemade bread is better the next day, lightly toasted."),
    Note("Partner's birthday", "en", "My partner's birthday is in October. I want to book the restaurant by the canal and get them a book."),
    Note("Hyperstition", "en", "## Origins\nThe term comes from the CCRU, the Cybernetic Culture Research Unit at Warwick in the 1990s.\n\n## Definition\nHyperstition is fiction that makes itself real through its own transmission."),
    Note("GCP IAM notes", "en", "## Roles\nIn GCP IAM a role is bound to a principal through a policy at the project, folder, or organization level.\n\n## Service accounts\nA service account is itself a principal and can be granted its own role."),
    Note("Kubernetes", "en", "## Pods\nA pod is the smallest deployable unit in Kubernetes, usually one or a few containers.\n\n## Deployments\nA deployment manages pod replicas and rolls out updates gradually."),
    Note("Coffee", "en", "I brew coffee with a pour-over most mornings, sometimes an espresso machine on weekends."),
    Note("Weather", "en", "It rains a lot here in autumn, and it rarely snows in winter."),
    Note("Stretching", "en", "I stretch for about ten minutes after every run so my legs don't get sore."),
    Note("Cycling", "en", "On weekends I sometimes cycle along the river for an hour instead of running."),
    Note("Reading list", "en", "Currently reading a history of the printing press, slowly, a few pages every night before bed."),
    Note("Interior plants", "en", "I keep a few succulents on the windowsill; they need almost no watering."),
    Note("Meditation", "en", "I sit for fifteen minutes in the morning with my eyes closed, just watching my breath."),
    Note("Budgeting", "en", "I track spending by category -- food, transport, subscriptions -- and set aside a fixed percentage every month."),
    Note("Laundry routine", "en", "Dark clothes get washed separately at thirty degrees, otherwise the colours bleed."),
    Note("Grocery shopping", "en", "This week's list: milk, eggs, rice, vegetables for soup, and bread."),
    Note("Dentist appointment", "en", "I booked a dentist appointment for next Tuesday, it has been a while since a cleaning."),
    Note("Docker containers", "en", "A Docker image is built in layers, and only the changed layers get rebuilt on a new build."),
    Note("Git workflow", "en", "I rebase my feature branch onto main before opening a pull request, to keep history linear."),
]


MESSAGES: list[Message] = [
    # ---------------------------------------------------------------- true positives, one per note, ru
    Message("сегодня утром бегала в парке, ноги немного устали", "ru", "Бег"),
    Message("после пробежки надо не забыть растянуться", "ru", "Растяжка"),
    Message("не могу заснуть, ложусь слишком поздно", "ru", "Сон"),
    Message("хочу сварить борщ на выходных", "ru", "Готовка"),
    Message("надо придумать подарок партнёру на день рождения", "ru", "День рождения партнёра"),
    Message("расскажи про гиперстишн и CCRU ещё раз", "ru", "CCRU"),
    Message("как назначить роль сервисному аккаунту в IAM", "ru", "GCP IAM"),
    Message("что такое деплоймент и под в кубернетес", "ru", "Kubernetes на русском"),
    Message("осень в этом году особенно дождливая выдалась", "ru", "Погода"),
    Message("кофе сегодня получился особенно вкусный", "ru", "Кофе"),
    Message("пятнадцать минут утром просто дышала и ни о чём не думала", "ru", "Медитация"),
    Message("в начале недели собираемся всей командой обсудить, кто чем занят", "ru", "Планёрки на работе"),
    Message("надо наконец завести таблицу, куда трачу деньги каждый месяц", "ru", "Бюджет"),
    Message("тёмное с белым в машинку не кидаю, краска слезает", "ru", "Стирка"),
    Message("что купить на неделю из еды, кроме молока и яиц", "ru", "Продукты"),
    Message("во вторник иду наконец на осмотр зубов", "ru", "Стоматолог"),
    Message("баррэ-аккорд у меня совсем не выходит на гитаре", "ru", "Гитара"),
    Message("читаю главу перед сном, слова непонятные выписываю себе", "ru", "Английский язык"),
    Message("базилик на балконе почти пересох, забыла полить", "ru", "Огород на балконе"),
    Message("в этом году доход с подработки тоже надо в декларацию вписать", "ru", "Налоги"),
    Message("в субботу еду выбирать обои для спальни", "ru", "Ремонт квартиры"),
    Message("кот почти не притрагивается к еде уже пару дней", "ru", "Кот"),
    Message("зал с инсталляциями в музее реально впечатлил", "ru", "Музей"),
    Message("надо проверить, не порвана ли палатка перед походом", "ru", "Поход в горы"),
    Message("проявляю плёнку сам, жду результат неделю", "ru", "Фотография"),
    # ---------------------------------------------------------------- true positives, one per note, fr
    Message("j'ai couru dans le parc ce matin", "fr", "La Course"),
    Message("je vais préparer un pot-au-feu ce dimanche", "fr", "La cuisine"),
    Message("il faut trouver un cadeau pour l'anniversaire de mon partenaire", "fr", "L'anniversaire du partenaire"),
    Message("parle-moi encore du rhizome chez Deleuze", "fr", "Deleuze"),
    Message("je prends un café avant de partir travailler", "fr", "Le café"),
    Message("il a beaucoup plu cet automne dans cette ville", "fr", "La météo"),
    Message("le dos me fait mal, je fais mes postures ce soir", "fr", "Le yoga"),
    Message("les tomates sur le balcon ont besoin d'eau", "fr", "Le jardin"),
    Message("cette année j'ai des revenus en plus pour la déclaration", "fr", "Les impôts"),
    Message("il faut réserver un camion pour le mois prochain", "fr", "Le déménagement"),
    Message("ce mois-ci on discute d'un roman russe au club", "fr", "Le club de lecture"),
    Message("l'accord barré me pose toujours des soucis à la guitare", "fr", "La guitare"),
    Message("le week-end je préfère parfois rouler le long de la rivière", "fr", "Le vélo"),
    Message("il faut vérifier la tente avant la randonnée du week-end", "fr", "Les randonnées"),
    # zero-overlap paraphrase on purpose, honouring "sometimes sharing zero content words"
    Message("je n'arrive pas à m'endormir avant une heure du matin ces derniers temps", "fr", "Le sommeil"),
    # ---------------------------------------------------------------- true positives, one per note, en
    Message("went for a run in the park this morning", "en", "Running"),
    Message("thinking about making soup this weekend", "en", "Cooking"),
    Message("need to figure out a gift for my partner's birthday", "en", "Partner's birthday"),
    Message("explain hyperstition and the CCRU one more time", "en", "Hyperstition"),
    Message("how do IAM roles work for service accounts on GCP", "en", "GCP IAM notes"),
    Message("what's a kubernetes deployment again", "en", "Kubernetes"),
    Message("making coffee with the pour-over this morning", "en", "Coffee"),
    Message("it rained a bit today, nothing unusual for this time of year", "en", "Weather"),
    Message("my legs are sore, forgot to stretch after the run", "en", "Stretching"),
    Message("thinking about getting on the bike for a ride by the river", "en", "Cycling"),
    Message("started a new book about the history of printing", "en", "Reading list"),
    Message("watering the succulents on the windowsill again", "en", "Interior plants"),
    Message("just sat quietly for a bit and watched my breath this morning", "en", "Meditation"),
    Message("set aside a fixed percentage of income again this month", "en", "Budgeting"),
    Message("the dark clothes went in a separate wash again", "en", "Laundry routine"),
    Message("need eggs, rice and something for soup this week", "en", "Grocery shopping"),
    Message("booked a cleaning appointment with the dentist for next week", "en", "Dentist appointment"),
    Message("only the changed layers rebuilt on the last docker build", "en", "Docker containers"),
    Message("rebased the feature branch before opening the pull request", "en", "Git workflow"),
    # zero-overlap paraphrase on purpose
    Message("how do I get better at falling asleep before midnight", "en", "Sleep"),

    # ================================================================ noise (40)
    # -- single shared content word with a specific note --------------
    Message("парк", "ru", None),  # shares only "парк" with "Бег"
    Message("parc", "fr", None),  # shares only "parc" with "La Course"
    Message("park", "en", None),  # shares only "park" with "Running"
    Message("кофе", "ru", None),  # shares only "кофе" with "Кофе"
    Message("café", "fr", None),  # shares only "café" with "Le café"
    Message("coffee", "en", None),  # shares only "coffee" with "Coffee"
    Message("гитара", "ru", None),  # shares only "гитара" with "Гитара"
    Message("guitare", "fr", None),  # shares only "guitare" with "La guitare"
    # -- two incidental words shared with an unrelated note ------------
    Message("вечером после работы посмотрели фильм про горы, красивые виды", "ru", None),  # "горы" echoes "Поход в горы" incidentally
    Message("купила новую сковороду, теперь готовка выходит быстрее", "ru", None),  # "готовка" echoes note title incidentally, unrelated content
    Message("в этом магазине продукты дороже, чем на рынке", "ru", None),  # "продукты" incidental
    Message("новый ремонт в офисе заканчивают только через месяц", "ru", None),  # "ремонт" incidental, unrelated to home
    Message("le café du coin a changé de propriétaire ce mois-ci", "fr", None),  # "café" as a place, incidental
    Message("le vélo de mon voisin a été volé la semaine dernière", "fr", None),  # "vélo" incidental, unrelated
    Message("the new office chair squeaks when I stretch out my back", "en", None),  # "stretch" incidental
    Message("the coffee shop near the office finally reopened this week", "en", None),  # "coffee" incidental, place not habit
    # -- small talk ------------------------------------------------------
    Message("привет как дела", "ru", None),
    Message("как прошли выходные", "ru", None),
    Message("что нового у тебя", "ru", None),
    Message("bonjour, comment ça va", "fr", None),
    Message("quelle heure est-il", "fr", None),
    Message("hey what's up", "en", None),
    Message("how's it going today", "en", None),
    # -- all-stopword / near-empty ---------------------------------------
    Message("и в на с у", "ru", None),
    Message("а", "ru", None),
    Message("ну и как бы это в общем-то", "ru", None),
    Message("de la le les", "fr", None),
    Message("et à de", "fr", None),
    Message("the a of to", "en", None),
    Message("it is what it is", "en", None),
    # -- questions on topics with no note at all -------------------------
    Message("сколько будет два плюс два", "ru", None),
    Message("какая столица Австралии", "ru", None),
    Message("сколько стоит билет на самолёт до Токио сейчас", "ru", None),
    Message("quelle est la capitale de l'Australie", "fr", None),
    Message("combien coûte un billet d'avion pour Tokyo maintenant", "fr", None),
    Message("what's the capital of Australia", "en", None),
    Message("how much does a flight to Tokyo cost right now", "en", None),
    Message("what year did the first moon landing happen", "en", None),
    Message("сколько лет живут черепахи в среднем", "ru", None),
    Message("quelle est la population du Japon aujourd'hui", "fr", None),
]
