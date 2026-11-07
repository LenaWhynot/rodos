#!/usr/bin/env python3
"""Render a Rodos family.json as a standalone offline atlas."""

import argparse
from collections import defaultdict, deque
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

STATUS = {
    "family": "Со слов семьи",
    "documented": "В документе",
    "hypothesis": "Версия",
    "disputed": "Есть расхождение",
}
TREE_STATUSES = {"family", "documented"}
ID_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
PORTRAIT_PATTERN = re.compile(r"data:image/(?:png|jpeg|webp);base64,[a-zA-Z0-9+/=]+")
TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "atlas.html"


def objects(value, field):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field}: ожидается список объектов")
    return value


def field_text(obj, field, *, required=False):
    value = obj.get(field, "")
    if not isinstance(value, str) or (required and not value.strip()):
        raise ValueError(f"{field}: ожидается текст")
    return value


def index_records(records, field):
    indexed = {}
    for record in objects(records, field):
        record_id = field_text(record, "id", required=True)
        if not ID_PATTERN.fullmatch(record_id) or record_id in indexed:
            raise ValueError(f"{field}: недопустимый или повторный id {record_id}")
        indexed[record_id] = record
    return indexed


def check_evidence(record, sources, label):
    status = record.get("status")
    if not isinstance(status, str) or status not in STATUS:
        raise ValueError(f"{label}: неизвестный status")
    refs = record.get("source_ids", [])
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in sources for ref in refs):
        raise ValueError(f"{label}: неизвестная ссылка в source_ids")
    if status == "documented" and not refs:
        raise ValueError(f"{label}: для documented нужен source_ids")


def validate(data):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Ожидается schema_version 1")
    field_text(data, "title", required=True)
    if type(data.get("is_demo", False)) is not bool:
        raise ValueError("is_demo: ожидается true или false")
    people = index_records(data.get("people"), "people")
    sources = index_records(data.get("sources"), "sources")
    relations = index_records(data.get("relationships"), "relationships")
    root_id = field_text(data, "root_id", required=True)
    if root_id not in people:
        raise ValueError(f"root_id: неизвестный человек {root_id}")

    for source in sources.values():
        field_text(source, "title", required=True)
        for field in ("kind", "locator", "excerpt"):
            field_text(source, field)
        url = field_text(source, "url")
        if url:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{source['id']}: ссылка на источник должна начинаться с https://")

    for person in people.values():
        field_text(person, "name", required=True)
        field_text(person, "life_years")
        field_text(person, "summary")
        portrait = field_text(person, "portrait")
        if portrait and not PORTRAIT_PATTERN.fullmatch(portrait):
            raise ValueError(f"{person['id']}: портрет должен быть встроенным PNG, JPEG или WebP")
        questions = person.get("questions", [])
        if not isinstance(questions, list) or any(not isinstance(question, str) for question in questions):
            raise ValueError(f"{person['id']}: questions должен содержать текстовые вопросы")
        for fact in objects(person.get("facts", []), f"{person['id']}.facts"):
            field_text(fact, "title", required=True)
            field_text(fact, "text", required=True)
            check_evidence(fact, sources, person["id"])

    admitted = []
    used_pairs = set()
    for relation in relations.values():
        if relation.get("kind") != "parent_of":
            raise ValueError(f"{relation['id']}: ожидается kind parent_of")
        parent = field_text(relation, "parent_id", required=True)
        child = field_text(relation, "child_id", required=True)
        if parent not in people or child not in people:
            raise ValueError(f"{relation['id']}: неизвестный id в связи {parent} → {child}")
        if parent == child:
            raise ValueError(f"{relation['id']}: человек указан своим родителем")
        if field_text(relation, "role") not in ("", "father", "mother"):
            raise ValueError(f"{relation['id']}: роль должна быть father или mother")
        check_evidence(relation, sources, relation["id"])
        if relation["status"] in TREE_STATUSES:
            if (parent, child) in used_pairs:
                raise ValueError(f"{relation['id']}: эта родительская связь уже внесена")
            used_pairs.add((parent, child))
            admitted.append(relation)
    return people, sources, relations, admitted


def tree_layout(people, root, relations):
    parents = defaultdict(list)
    children = defaultdict(list)
    for relation in relations:
        parent, child = relation["parent_id"], relation["child_id"]
        parents[child].append(parent)
        children[parent].append(child)

    # The longest path from any descendant puts every parent above their children.
    depth = dict.fromkeys(people, 0)
    pending = {person: len(children[person]) for person in people}
    ready = deque(person for person in people if pending[person] == 0)
    while ready:
        child = ready.popleft()
        for parent in parents[child]:
            depth[parent] = max(depth[parent], depth[child] + 1)
            pending[parent] -= 1
            if pending[parent] == 0:
                ready.append(parent)
    if any(pending.values()):
        raise ValueError("Родительские связи образуют цикл")

    generation = {root: 0}
    same_step = True
    queue = deque([root])
    while queue:
        person = queue.popleft()
        for relatives, offset in ((parents[person], 1), (children[person], -1)):
            level = generation[person] + offset
            for relative in relatives:
                if relative not in generation:
                    generation[relative] = level
                    queue.append(relative)
                elif generation[relative] != level:
                    same_step = False
    # Some recorded families span generations along one edge. Keep parents above
    # their children using the topological depth when one-step rows disagree.
    levels = generation if same_step else depth

    side = defaultdict(set)
    for relation in relations:
        role = relation.get("role")
        if relation["child_id"] != root or role not in ("father", "mother"):
            continue
        queue = deque([relation["parent_id"]])
        while queue:
            ancestor = queue.popleft()
            if role not in side[ancestor]:
                side[ancestor].add(role)
                queue.extend(parents[ancestor])

    grouped = defaultdict(lambda: defaultdict(list))
    for person in people:
        if person in generation:
            branch = next(iter(side[person])) if len(side[person]) == 1 else "center"
            grouped[levels[person]][branch].append(person)
    order = ("father", "center", "mother")
    # Let the focus card sit between the branches, without reserving a full column for it.
    centered = {level for level, row in grouped.items() if tuple(row) == ("center",)}
    widths = {
        branch: max((len(row[branch]) for level, row in grouped.items() if level not in centered), default=0)
        for branch in order
    }
    units = max(sum(widths.values()), max((len(grouped[level]["center"]) for level in centered), default=1))
    positions = {}
    top_level = max(grouped)
    for level, row in grouped.items():
        offset = (units - sum(widths.values())) / 2
        row_number = top_level - level
        for branch in order:
            group = row[branch]
            center = units / 2 if level in centered else offset + widths[branch] / 2
            first = center - (len(group) - 1) / 2
            for index, person in enumerate(group):
                positions[person] = (first + index, row_number)
            offset += widths[branch]
    return positions, units, top_level - min(grouped) + 1


def status_badge(status):
    return f'<span class="status {status}">{STATUS[status]}</span>'


def avatar(person):
    name = person["name"]
    if person.get("portrait"):
        return f'<img class="avatar" alt="" src="{escape(person["portrait"])}">'
    initials = "".join(word[0].upper() for word in name.split()[:2])
    return f'<span class="avatar" aria-hidden="true">{escape(initials)}</span>'


def person_card(person, positions, root):
    person_id = person["id"]
    name = escape(person["name"])
    position = positions.get(person_id)
    placement = ""
    if position is not None:
        column, row = position
        placement = f' style="--column:{column:g};--row:{row}"'
    label = escape(person.get("life_years") or "Годы уточняются")
    center = '<span class="focus-label">Точка отсчёта</span>' if person_id == root else ""
    return (
        f'<button type="button" class="person-card" data-person="{person_id}"{placement}>'
        f'{avatar(person)}<span class="person-name">{name}</span>'
        f'<span class="person-years">{label}</span>{center}</button>'
    )


def cite_sources(source_ids, sources):
    items = []
    for source_id in dict.fromkeys(source_ids):
        source = sources[source_id]
        title = escape(source["title"])
        locator = escape(source.get("locator", ""))
        kind = escape(source.get("kind", ""))
        excerpt = escape(source.get("excerpt", ""))
        label = " · ".join(part for part in (kind, locator) if part)
        link = ""
        if source.get("url"):
            url = escape(source["url"])
            link = f'<a href="{url}" target="_blank" rel="noreferrer noopener">Открыть источник ↗</a>'
        quote = f"<blockquote>{excerpt}</blockquote>" if excerpt else ""
        items.append(f"<details class=\"source\"><summary>{title}</summary><p>{label}</p>{quote}{link}</details>")
    return "".join(items)


def biographies(people, sources, relations):
    links_by_person = defaultdict(list)
    for relation in relations.values():
        links_by_person[relation["parent_id"]].append((relation["child_id"], "Ребёнок", relation))
        links_by_person[relation["child_id"]].append((relation["parent_id"], "Родитель", relation))
    articles = []
    for person_id, person in people.items():
        facts = []
        source_ids = []
        for fact in person.get("facts", []):
            title = escape(fact["title"])
            text = escape(fact["text"])
            facts.append(f'<li><strong>{title}</strong>{status_badge(fact["status"])}<p>{text}</p></li>')
            source_ids.extend(fact.get("source_ids", []))
        connections = []
        for relative, label, relation in links_by_person[person_id]:
            source_ids.extend(relation.get("source_ids", []))
            name = escape(people[relative]["name"])
            connections.append(f'<li><strong>{label} · {name}</strong>{status_badge(relation["status"])}</li>')
        questions = [f"<li>{escape(question)}</li>" for question in person.get("questions", [])]
        blocks = []
        for title, items in (("Что известно", facts), ("Родственные связи", connections), ("Открытые вопросы", questions)):
            if items:
                blocks.append(f'<section><h3>{title}</h3><ul class="facts">{"".join(items)}</ul></section>')
        if source_ids:
            blocks.append(f'<section><h3>Источники</h3>{cite_sources(source_ids, sources)}</section>')
        note = escape(person.get("summary", ""))
        summary = f'<p class="summary">{note}</p>' if note else ""
        years = escape(person.get("life_years") or "Годы уточняются")
        content = "".join(blocks) or '<p class="summary">У этой карточки пока есть только основные сведения.</p>'
        articles.append(
            f'<template id="bio-{person_id}"><article><header class="bio-header">{avatar(person)}'
            f'<div><h2 id="bio-title">{escape(person["name"])}</h2><p>{years}</p></div></header>'
            f'{summary}{content}</article></template>'
        )
    return "".join(articles)


def lines(relations, positions):
    # The browser measures the cards after responsive layout and draws the junctions in pixels.
    return "".join(
        f'<path data-relationship="{relation["id"]}" data-parent="{relation["parent_id"]}" '
        f'data-child="{relation["child_id"]}" class="{relation["status"]}"/>'
        for relation in relations
        if relation["parent_id"] in positions and relation["child_id"] in positions
    )


def render(data):
    people, sources, relations, admitted = validate(data)
    root = data["root_id"]
    positions, columns, rows = tree_layout(people, root, admitted)
    cards = "".join(person_card(people[person], positions, root) for person in positions)
    outside = "".join(person_card(person, positions, root) for key, person in people.items() if key not in positions)
    separate = ""
    if outside:
        separate = (
            '<section class="outside"><h2>Отдельные карточки</h2>'
            '<p>Их связь с основной веткой ещё предстоит установить.</p>'
            f'<div class="outside-grid">{outside}</div></section>'
        )
    people_count = len(people)
    line_count = sum(rel["parent_id"] in positions and rel["child_id"] in positions for rel in admitted)
    demo = '<span class="demo-label">Вымышленная семья</span>' if data.get("is_demo", False) else ""
    branches = {rel.get("role") for rel in admitted if rel["child_id"] == root}
    branch_legend = "".join(
        f'<span class="branch-label">{label}</span>'
        for role, label in (("father", "Папина сторона"), ("mother", "Мамина сторона")) if role in branches
    )
    inserts = {
        "TITLE": escape(data["title"]),
        "DEMO": demo,
        "COUNTS": f"Людей в атласе · {people_count}　 Связей на карте · {line_count}",
        "BRANCHES": branch_legend,
        "COLUMNS": str(columns),
        "ROWS": str(rows),
        "CARDS": cards,
        "LINES": lines(admitted, positions),
        "OUTSIDE": separate,
        "BIOGRAPHIES": biographies(people, sources, relations),
    }
    return re.sub(r"@@([A-Z]+)@@", lambda match: inserts[match[1]], TEMPLATE.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    temp_path = None
    try:
        if args.input.resolve() == args.output.resolve():
            raise ValueError("Укажите для HTML путь, отличный от входного JSON")
        data = json.loads(args.input.read_text(encoding="utf-8"))
        document = render(data)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=args.output.parent, encoding="utf-8", newline="\n", delete=False
        ) as out:
            temp_path = Path(out.name)
            out.write(document)
        os.replace(temp_path, args.output)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Родос: {error}\n")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
