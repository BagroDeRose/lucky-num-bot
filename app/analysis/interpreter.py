"""Deterministic, non-AI text rendering of an AnalysisResult.

Used for the free teaser (always) and as a fallback full report if the AI
report generator is temporarily unavailable — a paying user must never be
left with nothing. Everything here is built from facts already present on
AnalysisResult (digits, frequency, repeated_digits/pairs, detected_patterns,
scores) — nothing is invented, and the scoring engine itself is never
touched or recomputed, only narrated. All text is our own fixed Russian
copy (the only interpolated values are digits/scores), so it's safe to
embed Telegram HTML tags directly without running it through
app.formatting.to_telegram_html.
"""

from __future__ import annotations

from app.analysis.models import AnalysisResult
from app.analysis.rules import BASE_DIGIT_PROFILE, DIGIT_MEANINGS, SCORE_CATEGORIES

# --- shared helpers ---------------------------------------------------

_COUNT_WORDS = {2: "две", 3: "три", 4: "четыре", 5: "пять", 6: "шесть", 7: "семь", 8: "восемь", 9: "девять"}


def _count_phrase(n: int) -> str:
    word = _COUNT_WORDS.get(n, str(n))
    noun = "цифры" if n in (2, 3, 4) else "цифр"
    return f"{word} {noun}"


def _capitalize(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def _digit_runs(digits: list[int]) -> list[tuple[int, int]]:
    """Collapse consecutive identical digits into (digit, run_length) pairs,
    e.g. [5, 3, 1, 1, 8] -> [(5, 1), (3, 1), (1, 2), (8, 1)].
    """
    runs: list[tuple[int, int]] = []
    for d in digits:
        if runs and runs[-1][0] == d:
            prev_digit, prev_count = runs[-1]
            runs[-1] = (prev_digit, prev_count + 1)
        else:
            runs.append((d, 1))
    return runs


def _dominant_categories(digit: int) -> set[str]:
    """Category name(s) where `digit` scores highest in the rule book's base
    profile — the same static table the real scoring engine reads from.
    A read-only lookup, not a re-implementation of the scoring formula.
    """
    profile = BASE_DIGIT_PROFILE[digit]
    top = max(profile)
    return {cat for cat, value in zip(SCORE_CATEGORIES, profile, strict=True) if value == top}


# --- free teaser --------------------------------------------------------


def _teaser_finding(result: AnalysisResult) -> str:
    """One concrete, specific observation — never a generic filler line."""
    pattern_names = {p.name for p in result.detected_patterns}

    if "single_digit_number" in pattern_names:
        digit = result.digits[0]
        return f"Бросается в глаза сразу: весь номер держится на одной цифре — {digit}."
    if result.repeated_digits:
        digit = result.repeated_digits[0]
        count = result.digit_frequency.get(digit, 0)
        return f"Один момент выделяется сразу: цифра {digit} встречается в номере {count} раза."
    if "palindrome" in pattern_names:
        return "Один момент выделяется сразу: номер читается одинаково в обе стороны."
    if result.repeated_pairs:
        return f"Один момент выделяется сразу: в номере повторяется пара «{result.repeated_pairs[0]}»."
    return "В комбинации уже заметен характер, но чтобы понять его до конца, нужно распутать все цифры вместе."


def _personal_line(result: AnalysisResult) -> str | None:
    """One line about how the number meets this person's birth number.

    Returns None for a serial-only analysis, which keeps the teaser for
    non-personalized and legacy analyses exactly as it has always been.
    """
    if result.birth_number is None:
        return None
    if result.birth_resonance == "same_number":
        return (
            f"И сразу совпадение: ваше число рождения — {result.birth_number}, "
            "и купюра сводится к нему же."
        )
    if result.birth_resonance == "present":
        count = result.birth_digit_in_serial_count
        return (
            f"Ваше число рождения — {result.birth_number}, и эта цифра есть "
            f"в самом номере ({count} раз(а))."
        )
    return (
        f"Ваше число рождения — {result.birth_number}; в цифрах этой купюры "
        "оно не встречается, и это тоже часть картины."
    )


def render_teaser(result: AnalysisResult) -> str:
    """Free teaser shown after analysis, before payment.

    Answers "what's the main number and what's the first interesting
    thing about it?" — deliberately stops there and leaves the digit-by-
    digit story, patterns, and the four profiles for the paid report.
    """
    lines = [
        "🔮 Купюра проверена.",
        f"Главное число — <b>{result.reduced_number}</b>.",
        "",
        _teaser_finding(result),
    ]

    personal = _personal_line(result)
    if personal:
        lines += ["", personal]

    lines += [
        "",
        f"Предварительный балл: {result.overall_score}/100.",
        "",
        "Это только верхний слой — полный разбор покажет, как цифры "
        "складываются в единую картину, и что в номере про деньги, удачу, "
        "рост и стабильность.",
    ]
    return "\n".join(line for line in lines if line != "")


# --- fallback full report ------------------------------------------------

_MID_CONNECTORS_SINGULAR = ("Дальше идёт", "Следом", "Затем появляется", "После этого")
_MID_CONNECTORS_PLURAL = ("Дальше идут", "Следом", "Затем появляются", "После этого")

# Rotated (not fixed) so a number with several separate multi-digit runs
# (e.g. "1122334455") doesn't chant the exact same closing clause — and
# "усиливает" is deliberately avoided here since it's one of the words the
# AI-generated report is told to stop overusing; the deterministic fallback
# should hold to the same product voice.
_REPEAT_EMPHASIS_TAILS = (
    "и это не случайная деталь",
    "и повтор здесь явно неслучаен",
    "заметно задавая тон всей комбинации",
    "и это сразу бросается в глаза",
)


def _render_opening(result: AnalysisResult) -> str:
    pattern_names = {p.name for p in result.detected_patterns}

    if "single_digit_number" in pattern_names:
        digit = result.digits[0]
        return (
            f"Здесь не приходится гадать, что в номере главное: он весь состоит "
            f"из одной цифры — {digit}."
        )
    if result.repeated_digits:
        digit = result.repeated_digits[0]
        count = result.digit_frequency.get(digit, 0)
        return f"Цифра {digit} явно доминирует в этой комбинации — она встречается {count} раза."
    if "palindrome" in pattern_names:
        return "Этот номер — палиндром: он читается одинаково в обе стороны, и такая симметрия встречается нечасто."
    if result.repeated_pairs:
        pair = result.repeated_pairs[0]
        return f"В номере есть небольшая, но заметная деталь — повторяющаяся пара «{pair}»."
    return (
        f"На первый взгляд номер выглядит просто набором цифр, но у него есть "
        f"чёткий центр тяжести — главное число {result.reduced_number}."
    )


def render_digit_sum_calculation(result: AnalysisResult) -> str:
    """The digit-sum arithmetic, one line per reduction step, from canonical
    values only. Shared by the deterministic report and the AI report so the
    arithmetic shown to users has exactly one source.

    Every intermediate step is printed: a sum of 29 reads "2 + 9 = 11" then
    "1 + 1 = 2". Printing a single step straight to the reduced number would
    show users false arithmetic ("2 + 9 = 2").
    """
    lines = [" + ".join(str(d) for d in result.digits) + f" = {result.digit_sum}"]
    value = result.digit_sum
    while value >= 10:
        step = sum(int(ch) for ch in str(value))
        lines.append(" + ".join(ch for ch in str(value)) + f" = {step}")
        value = step
    return "\n".join(lines)


def _render_main_number(result: AnalysisResult) -> str:
    reduced = result.reduced_number
    meaning = DIGIT_MEANINGS[reduced]

    interpretation = (
        f"Так получается главное число — {reduced}. "
        f"{_capitalize(meaning)} — вот что оно вносит в характер всей комбинации."
    )
    return render_digit_sum_calculation(result) + "\n\n" + interpretation


def _render_digit_story(result: AnalysisResult) -> str:
    digits = result.digits

    if len(set(digits)) == 1:
        digit = digits[0]
        meaning = DIGIT_MEANINGS[digit]
        reduced = result.reduced_number
        if reduced == digit:
            bridge = (
                f"Даже после свёртки суммы цифр главным остаётся то же число — "
                f"{reduced}: тема держится на всех уровнях номера, не только на поверхности."
            )
        else:
            bridge = (
                f"При этом сумма всех цифр сводится к другому числу — {reduced} "
                f"({DIGIT_MEANINGS[reduced]}). Получается наложение: {digit} задаёт фон "
                f"по всей длине номера, а {reduced} выходит на первый план как итог."
            )
        return (
            f"{_capitalize(_count_phrase(len(digits)))} номера — {digit}. "
            f"{_capitalize(meaning)} — не эпизод, а единственная тема всей "
            f"комбинации, без пауз и отвлечений. {bridge}"
        )

    runs = _digit_runs(digits)
    seen: set[int] = set()
    sentences: list[str] = []
    # Cap how many times the "this theme is back" note fires — on a long,
    # heavily-repeating number it would otherwise chant the same phrase
    # many times over, which is exactly the repetition this report design
    # is meant to avoid.
    returning_notes_used = 0
    max_returning_notes = 2

    for i, (digit, count) in enumerate(runs):
        meaning = DIGIT_MEANINGS[digit]
        returning = digit in seen
        seen.add(digit)
        is_first = i == 0
        is_last = i == len(runs) - 1

        if count >= 2:
            if is_first:
                lead = f"Номер сразу заявляет о себе: {_count_phrase(count)} {digit} подряд"
            elif is_last:
                lead = f"Завершают комбинацию {_count_phrase(count)} {digit} подряд"
            else:
                connector = _MID_CONNECTORS_PLURAL[(i - 1) % len(_MID_CONNECTORS_PLURAL)]
                lead = f"{connector} {_count_phrase(count)} {digit} подряд"
            tail = _REPEAT_EMPHASIS_TAILS[i % len(_REPEAT_EMPHASIS_TAILS)]
            sentence = f"{lead} — {meaning}, {tail}."
        else:
            if is_first:
                sentence = f"Номер начинается с цифры {digit} — {meaning}."
            elif is_last:
                sentence = f"Замыкает комбинацию цифра {digit} — {meaning}."
            else:
                connector = _MID_CONNECTORS_SINGULAR[(i - 1) % len(_MID_CONNECTORS_SINGULAR)]
                sentence = f"{connector} цифра {digit} — {meaning}."
            if returning and returning_notes_used < max_returning_notes:
                sentence += " Эта тема здесь уже не впервые."
                returning_notes_used += 1

        sentences.append(sentence)

    return " ".join(sentences)


def render_special_section(result: AnalysisResult) -> str | None:
    """Only meaningful, real structural findings — never a generic "adds a
    visual accent" filler. Returns None if there's genuinely nothing to say.
    """
    pattern_names = {p.name for p in result.detected_patterns}
    lines: list[str] = []

    if "single_digit_number" in pattern_names:
        digit = result.digits[0]
        lines.append(
            f"Весь номер держится на одной цифре — {digit}. Это не рядовое "
            "повторение, а единственный, доминирующий мотив всей комбинации."
        )
    else:
        for digit in result.repeated_digits:
            count = result.digit_frequency.get(digit, 0)
            lines.append(
                f"Цифра {digit} встречается в номере {count} раза — заметная "
                f"концентрация, которая выводит тему «{DIGIT_MEANINGS[digit]}» "
                "в число ведущих в этой комбинации."
            )
        if result.repeated_pairs:
            pairs_str = ", ".join(f"«{p}»" for p in result.repeated_pairs)
            lines.append(f"В номере повторяются соседние цифры: {pairs_str}.")

    if "palindrome" in pattern_names:
        lines.append(
            "Номер читается одинаково в обе стороны — редкая структурная "
            "симметрия, которая обычно читается как знак равновесия."
        )
    if "ascending_sequence" in pattern_names:
        lines.append("В номере есть возрастающая последовательность цифр — явный мотив движения вперёд.")
    if "descending_sequence" in pattern_names:
        lines.append("В номере есть убывающая последовательность цифр — мотив завершения цикла.")

    if not lines:
        return None
    return "\n".join(f"• {line}" for line in lines)


def render_personal_section(result: AnalysisResult) -> str | None:
    """The birth-date layer of the deterministic report.

    Returns None for serial-only and legacy analyses, so those reports stay
    byte-for-byte what they were before this feature existed. States only
    what the analysis actually computed — the life-path digit, its meaning
    and how it does or doesn't meet the serial — and never the birth date.
    """
    if result.birth_number is None or result.birth_number_meaning is None:
        return None

    lines = [
        f"Ваше число рождения — {result.birth_number}: "
        f"{result.birth_number_meaning}."
    ]

    if result.birth_resonance == "same_number":
        lines.append(
            "Купюра сводится ровно к тому же числу — редкий случай, когда "
            "номер и его владелец говорят на одном языке."
        )
    elif result.birth_resonance == "present":
        lines.append(
            f"Эта цифра встречается и в самом номере "
            f"({result.birth_digit_in_serial_count} раз(а)) — тема звучит "
            "с обеих сторон сразу."
        )
    else:
        lines.append(
            "В цифрах этой купюры оно не появляется: номер ведёт свою линию, "
            "а не повторяет вашу."
        )

    return " ".join(lines)


_STRENGTH_PHRASES: dict[str, tuple[tuple[int, str], ...]] = {
    "money": (
        (8, "Денежная тема здесь явно ведущая."),
        (6, "Денежная тема выражена заметно."),
        (4, "Денежная тема присутствует, но не доминирует."),
        (0, "Денежная тема здесь скорее фоновая."),
    ),
    "luck": (
        (8, "Такой номер обычно связывают с везением, которое приходит внезапно."),
        (6, "Элемент везения здесь заметен."),
        (4, "Везение здесь скорее эпизодическое, чем постоянное."),
        (0, "Тема удачи выражена мягко."),
    ),
    "growth": (
        (8, "Это номер про движение: рост, перемены, новые направления."),
        (6, "Тема роста здесь заметна."),
        (4, "Рост присутствует в умеренной степени."),
        (0, "Динамики в этой комбинации немного."),
    ),
    "stability": (
        (8, "Номер явно тяготеет к порядку и предсказуемости."),
        (6, "Стабильность — заметная черта этой комбинации."),
        (4, "Баланс между переменами и постоянством."),
        (0, "Этот номер скорее про движение, чем про стабильность."),
    ),
}


def _strength_phrase(category: str, score: int) -> str:
    for threshold, phrase in _STRENGTH_PHRASES[category]:
        if score >= threshold:
            return phrase
    return _STRENGTH_PHRASES[category][-1][1]


def _has_directional_theme(digit: int) -> bool:
    """False only for digit 0 — its base profile is tied across all four
    categories (see rules.py), so it has no specific theme to attribute a
    profile to. Skipping it here mirrors the same exclusion the scoring
    engine itself applies (an undirected digit shouldn't be credited with
    driving a specific theme just by being the main number or repeating).
    """
    return len(_dominant_categories(digit)) < len(SCORE_CATEGORIES)


def _profile_lead(result: AnalysisResult, category: str) -> str | None:
    """A fact-grounded opening sentence for a profile, when a specific digit
    (the main number or a repeated one) clearly drove that theme.
    """
    reduced = result.reduced_number
    if _has_directional_theme(reduced) and category in _dominant_categories(reduced):
        return (
            f"Здесь многое определяет главное число {reduced}: "
            f"{DIGIT_MEANINGS[reduced]}."
        )

    for digit in sorted(set(result.digits)):
        if result.digit_frequency.get(digit, 0) < 2:
            continue
        if not _has_directional_theme(digit):
            continue
        if category in _dominant_categories(digit):
            return (
                f"Заметный вклад вносит цифра {digit} — она встречается в номере "
                f"{result.digit_frequency[digit]} раза, а её тема "
                f"(«{DIGIT_MEANINGS[digit]}») естественно проявляется здесь."
            )
    return None


def _render_profile(result: AnalysisResult, category: str, score: int) -> str:
    lead = _profile_lead(result, category)
    strength = _strength_phrase(category, score)
    if lead:
        return f"{lead} {strength}"
    return strength


_STRONG_SUFFIX = {
    "money": "с выраженной денежной темой",
    "luck": "с явным акцентом на удачу",
    "growth": "с сильной тягой к движению и росту",
    "stability": "с выраженной тягой к порядку и стабильности",
}
_WEAK_SUFFIX = {
    "money": "денежная тема выражена значительно спокойнее",
    "luck": "тема удачи здесь в тени",
    "growth": "рост и движение выражены мягче",
    "stability": "стабильность выражена заметно слабее",
}


def _render_summary(result: AnalysisResult) -> str:
    scores = {
        "money": result.money_score,
        "luck": result.luck_score,
        "growth": result.growth_score,
        "stability": result.stability_score,
    }
    best = max(scores, key=lambda k: scores[k])
    worst = min(scores, key=lambda k: scores[k])

    if scores[best] == scores[worst]:
        return f"{result.overall_score}/100 — ровный, сбалансированный номер: ни одна тема здесь не перевешивает остальные."

    return (
        f"{result.overall_score}/100 — это номер {_STRONG_SUFFIX[best]}, "
        f"тогда как {_WEAK_SUFFIX[worst]}."
    )


_VERDICTS: dict[str, tuple[str, ...]] = {
    "money": (
        "Деньги любят цифры, которые не сидят на месте — а здесь их явно в достатке.",
        "Денежная тема здесь не случайный пассажир — она занимает место в первом ряду.",
    ),
    "luck": (
        "Такие номера обычно ловят момент, а не ждут его.",
        "Здесь удача скорее про внимательность, чем про везение вслепую.",
    ),
    "growth": (
        "Этот номер не про паузы — он про то, что дальше.",
        "Здесь чувствуется разгон: номер явно не про то, чтобы стоять на месте.",
    ),
    "stability": (
        "Этот номер держит форму даже под давлением.",
        "Здесь порядок не скучный, а надёжный.",
    ),
}


def _render_verdict(result: AnalysisResult) -> str:
    scores = {
        "money": result.money_score,
        "luck": result.luck_score,
        "growth": result.growth_score,
        "stability": result.stability_score,
    }
    best = max(scores, key=lambda k: scores[k])
    options = _VERDICTS[best]
    return options[result.overall_score % len(options)]


def render_fallback_full_report(result: AnalysisResult) -> str:
    """Plain deterministic full report, used if the AI is unavailable.

    Follows the same section structure and content philosophy as the AI
    report — a personalized reading, not a generic dictionary — just
    without the model's richer prose. Scores are always shown as clean
    "X/10" figures with a grounded interpretation, never as a raw
    contribution breakdown.
    """
    parts: list[str] = [
        "🔮 <b>Денежный разбор</b>",
        f"Серийный номер: <b>{result.normalized_number}</b>",
        "",
        _render_opening(result),
        "",
        f"🔢 <b>Главное число — {result.reduced_number}</b>",
        _render_main_number(result),
        "",
        "🔎 <b>История цифр</b>",
        _render_digit_story(result),
    ]

    special = render_special_section(result)
    if special:
        parts += ["", "✨ <b>Особые сочетания</b>", special]

    personal = render_personal_section(result)
    if personal:
        parts += ["", "🎂 <b>Ваше число рождения</b>", personal]

    parts += [
        "",
        f"💰 <b>Денежный профиль — {result.money_score}/10</b>",
        _render_profile(result, "money", result.money_score),
        "",
        f"🍀 <b>Профиль удачи — {result.luck_score}/10</b>",
        _render_profile(result, "luck", result.luck_score),
        "",
        f"🌱 <b>Профиль роста — {result.growth_score}/10</b>",
        _render_profile(result, "growth", result.growth_score),
        "",
        f"🛡 <b>Профиль стабильности — {result.stability_score}/10</b>",
        _render_profile(result, "stability", result.stability_score),
        "",
        f"⭐ <b>Итог — {result.overall_score}/100</b>",
        _render_summary(result),
        "",
        "💥 <b>Вердикт Жмыха</b>",
        _render_verdict(result),
    ]

    return "\n".join(parts)
